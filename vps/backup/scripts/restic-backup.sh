#!/bin/sh
# Nightly restic backup of the VPS local-path PVC tree, plus the backup
# verification gate that checks the quiesce sidecars' snapshots.
#
# Runs from the `restic-backup` CronJob. Credentials and the repository URL
# arrive in the environment from the `restic-b2` Secret (envFrom), and nothing
# here names them: VPS_RESTIC_REPOSITORY and friends are in the VPS envsubst
# allowlist, and this file passes through envsubst on its way into a ConfigMap.
# See `make check-script-substitution`. $HC_UUID below is safe: the CronJob's
# `env:` is where the allowlisted UUID placeholder lives, and only the renamed
# HC_UUID reaches this file. Do not write the allowlisted name here even in a
# comment — a comment is substituted exactly like code, which is how the guard
# first earned its keep.
#
# DELIBERATELY NOT SHARED with the homelab restic job for the same reason the
# two restic-init.sh files are not shared: the two clusters have different
# allowlists, so one file cannot be safe in both trees.
#
# shellcheck disable=SC3040 # `set -o pipefail` is not POSIX, but the
# restic/restic:0.17.3 image's /bin/sh is busybox ash, which implements it. If
# a future image did not, this line would fail and the job would stop loudly
# rather than silently swallowing a broken pipeline.
set -uo pipefail

# Dead-man's-switch. /start detects started-but-never-finished
# and records the run duration; the exit-code ping distinguishes
# success from failure. Pings never fail the job.
#
# EACH PING CARRIES A BODY - a short key=value summary of what this
# run observed, so the healthchecks.io Events log answers "what did
# it see?" without a pod log that may already have aged out. Four
# properties here are load-bearing:
#
#   1. A PING MUST NEVER FAIL THE JOB, AND A BODY MUST NEVER COST A
#      PING. A failed body POST falls back to a bodiless ping on the
#      SAME file - no intermediate copy, because `head -c src > cpy`
#      truncates cpy BEFORE head runs, so any head failure leaves an
#      empty file and --post-file=<empty> is a SUCCESSFUL post of a
#      blank body that never falls back.
#   2. NEVER EMIT A COMMAND'S OUTPUT. A script cannot classify its own
#      command output at runtime, and this body goes to a third party
#      who keeps it until the ping log rotates. The concrete leaks: the
#      scripts influx-backup.sh execs pass the InfluxDB operator token
#      on argv, and a failing wget quotes the ping URL, which IS the
#      check's write credential. (A restic repository URI is NOT a
#      reason - it grants nothing; see the tiers in AGENTS.md.)
#      `make check-ping-bodies` enforces it; read spec section 9.2
#      before adding a field.
#   3. A BARE TRAILING SLASH IS AN HTTP 400 (verified live against
#      hc-ping.com), so the URL is built conditionally. Unconditional
#      "$HC/$1" would break the plain success ping, and the bodiless
#      fallback would rebuild the same broken URL and not rescue it.
#   4. `true >`, NOT `: >`. `:` is a POSIX special built-in and a
#      redirection error on one aborts a non-interactive shell even
#      behind `|| true` (verified in dash: exits 2 without reaching
#      the next line). This matters the day this job gains
#      readOnlyRootFilesystem.
#
# hc_ping resets the body after every ping, so the exit body can
# never open with "summary=starting". That is a construction, not a
# discipline at call sites.
HC_BODY=/tmp/hc-body
# The stderr redirection PRECEDES the body redirection in both. Redirections
# are applied left to right, so `>> "$HC_BODY" 2>/dev/null` cannot suppress the
# shell's own "cannot create" diagnostic - only this order can (verified in dash
# and busybox 1.36.1). Property 4 above is what keeps the job alive on that day;
# this is what keeps its log readable.
hc_reset() { true 2>/dev/null > "$HC_BODY" || true; }
emit() { { printf '%s' "$*" | LC_ALL=C tr -cd '\040-\176'; printf '\n'; } 2>/dev/null >> "$HC_BODY" || true; }

HC="https://hc-ping.com/${HC_UUID}"
# ping_hc [SUFFIX] - "" | start | <exit-status>.
ping_hc() {
  _sf=${1:-}
  _u=$HC
  [ -z "$_sf" ] || _u="$HC/$_sf"
  if [ -s "$HC_BODY" ]; then
    if wget -q -T 10 -O- --post-file="$HC_BODY" "$_u" >/dev/null 2>&1; then
      hc_reset; return 0
    fi
    echo "hc: body POST failed, retrying without a body" >&2
  fi
  # A failed ping prints FIXED text. No URL, no tool output: for a
  # ping the URL IS the write credential, and a pod log is not a
  # place to put one either.
  wget -q -T 10 -O- "$_u" >/dev/null 2>&1 || echo "hc: ping not delivered" >&2
  hc_reset
  return 0
}

# ONE SINK PER DIAGNOSTIC. The gate's ERROR lines go to the pod log
# AND into the ping body from a single call, so the first reword
# cannot desynchronise the log from the third-party body - on the
# one channel that exists to be trusted once the log has aged out.
# Do not write `echo "ERROR: x"; emit "error=x"` at ten sites.
#
# Lines are held in a variable rather than a second file because
# spec section 5 makes `summary=` line 1 of the body, and the errors
# are known before the summary is; they are flushed after it.
HC_ERR_LINES=""
say_err() {
  echo "ERROR: $*"
  # COLLAPSE NEWLINES AT CAPTURE. flush_errors splits this accumulator on
  # newlines to make one body record per diagnostic, and it does that BEFORE
  # `emit` sanitises - so a newline inside a value (a PVC directory name can
  # carry one) splits into extra records and can synthesise a key nobody
  # emitted: a second `prune=` after, and contradicting, the real one. Killing
  # it here is what makes spec section 5's one-record-per-emit invariant true
  # for this path too, and not only for direct `emit` calls.
  _sm=$(printf '%s' "$*" | tr '\n\r' '  ')
  HC_ERR_LINES="$HC_ERR_LINES
error=$_sm"
}
flush_errors() {
  [ -n "$HC_ERR_LINES" ] || return 0
  printf '%s\n' "$HC_ERR_LINES" | while read -r _fl; do
    # check-ping-bodies: untaint _fl - every line here was produced by say_err, whose argument the same check validated at its call site
    [ -z "$_fl" ] || emit "$_fl"
  done
}

# STEP names the phase for failed_step= in the ping body. `step NAME
# [TEXT]` echoes TEXT (or NAME) exactly as the plain echoes it
# replaces did, so the pod log is byte-identical. The last step is
# named `finished` rather than `done` because `done` is a shell
# keyword and shellcheck rejects it unquoted in a test.
STEP=start
step() { STEP=$1; echo "==> ${2:-$1}"; }

# ---- Backup verification gate -----------------------------
# Verifies the quiesce snapshots that the sqlite sidecars (n8n,
# karakeep, uptime-kuma, freshrss) and umami's pg_dumpall sidecar
# refresh every 12h. Two checks with deliberately different
# authority:
#
#   1. EXPECTED SET - AUTHORITATIVE, sets the exit code. Every
#      snapshot we know must exist is resolved, exists and is
#      fresh. A bare mtime sweep cannot distinguish "no stale
#      files because all is well" from "no stale files because
#      /data is empty, unmounted, or an app's PVC is blank" -
#      identical output, and the latter is catastrophic: restic
#      succeeds on an empty tree and pings 0.
#   2. BROAD SWEEP - ADVISORY, warns only. Its unique value is
#      catching stale files nobody listed; its false-positive
#      mode is an orphaned PV directory (recreate a PVC and the
#      Retain-reclaimed old directory stays behind with a frozen
#      *.restic in it), which would pin this red forever - alert
#      fatigue on the one channel that must mean "restore is
#      broken". So its FINDINGS cannot fail the job. Its
#      INABILITY TO LOOK still can: a find that exits non-zero
#      fails the gate, because "I could not look" must never be
#      reported as "everything is fine".
#
# MAINTENANCE CONTRACT: a new sqlite-backed service must be added
# to EXPECTED_SNAPSHOTS below. Forget, and that service's backups
# are unverified - silently. That trade is deliberate: an explicit
# list somebody has to maintain beats a wildcard that silently
# accepts nothing at all.
#
# Entries are "label:glob". They must be globs because
# local-path-provisioner names each PVC directory
# <pvName>_<namespace>_<pvcName> and pvName is a random UUID.
# Each glob resolves to its MOST RECENTLY MODIFIED match, so an
# orphaned directory can neither mask a missing snapshot nor
# poison the verdict.
#
# Freshness is stat(1) with a checked exit status, never
# `[ -n "$(find ...)" ]`: that form captures stdout only and
# ignores find's status, so a permission error, an I/O error or a
# file vanishing mid-walk yields empty output and a verdict of
# "fresh". Every inability to read is a failure here, not a pass.
STALE_MINUTES=900
STALE_SECONDS=$(( STALE_MINUTES * 60 ))

EXPECTED_SNAPSHOTS="
n8n:/data/*_vps_n8n-data/database.sqlite.restic
karakeep:/data/*_vps_karakeep-data/db.db.restic
uptime-kuma:/data/*_vps_uptime-kuma-data/kuma.db.restic
umami:/data/*_vps_umami-pg-data/dump.sql.restic
pinepods:/data/*_vps_pinepods-pg-data/dump.sql.restic
"
# shellcheck disable=SC2125 # storing the glob UNEXPANDED is the whole
# point: newest_match expands it later, under a controlled `set +f`,
# after the caller has decided how to interpret "nothing matched".
FRESHRSS_DB_GLOB=/data/*_vps_freshrss-data/users/*/db.sqlite

# Resolve a glob to its most recently modified existing match.
# stdout: the path. 0 = found, 1 = nothing matched, 2 = something
# matched but could not be read (never reported as fresh).
newest_match() {
  # DANGEROUS SHAPE, PRESERVED DELIBERATELY: this ends with an unconditional
  # `set -f` and never restores the caller's setting. It is safe ONLY because
  # every call site is a command substitution, so the change dies with the
  # subshell. If a refactor ever removes that subshell, globbing stays off for
  # the rest of the run and check_freshrss's per-user loop silently iterates a
  # literal, unexpanded pattern and verifies nothing at all. Do not restructure
  # the call sites; do not "tidy" the set -f pair.
  set +f
  # shellcheck disable=SC2086 # unquoted ON PURPOSE — the split-and-glob is how
  # the pattern in $1 becomes the list of matches. Quoting it would make every
  # lookup miss and the gate would report every snapshot MISSING.
  set -- $1
  set -f
  _best=""; _best_m=-1
  for _c in "$@"; do
    [ -e "$_c" ] || continue
    _m=$(stat -c %Y "$_c" 2>/dev/null) || return 2
    if [ "$_m" -gt "$_best_m" ]; then _best_m=$_m; _best=$_c; fi
  done
  [ -n "$_best" ] || return 1
  printf '%s\n' "$_best"
}

# Age assertion on one resolved path. $1 = path, $2 = label.
assert_fresh() {
  _am=$(stat -c %Y "$1" 2>&1) || {
    # check-ping-bodies: untaint _am - stat's own message, not emitted: say_err below names the label and path only
    say_err "$2: cannot stat $1"
    # stat's own message belongs in the POD LOG and nowhere else: it is the
    # difference between ENOENT, EACCES and a wedged mount. Not a sink.
    echo "       stat said: $_am"
    return 1
  }
  _aage=$(( NOW - _am ))
  [ "$_aage" -lt "$STALE_SECONDS" ] && return 0
  say_err "$2 STALE: $1 is $(( _aage / 3600 ))h old (limit $(( STALE_SECONDS / 3600 ))h)"
  return 1
}

# FreshRSS keeps one sqlite DB per user, so its expected set is
# dynamic. Iterate the SOURCE glob and assert a sibling snapshot
# for EACH user DB. Taking the newest of the snapshot glob would
# let one healthy user's fresh snapshot mask another user whose DB
# has never been snapshotted at all - that user's data would then
# sit in the backup as a raw, unquiesced file with the gate green.
# NB: POSIX sh has no locals - every variable here is global.
# This function is called from check_expected, so its working
# variables are _f-prefixed to avoid clobbering the caller's
# accumulator. Reusing `_rc` in both silently discarded the
# caller's verdict, which pinged green over printed errors.
check_freshrss() {
  _frc=0
  FRS_OK=0
  FRS_TOT=0
  _flive=$(newest_match "$FRESHRSS_DB_GLOB"); _fs=$?
  if [ "$_fs" -eq 1 ]; then
    echo "note: no freshrss user DBs yet - nothing to verify"
    return 0
  fi
  if [ "$_fs" -ne 0 ]; then
    say_err "freshrss user DBs UNREADABLE under $FRESHRSS_DB_GLOB"
    return 1
  fi
  # Only the PVC directory holding the most recently modified user
  # DB is verified; an orphaned directory is ignored.
  _fdir=${_flive%/users/*}
  for _fdb in "$_fdir"/users/*/db.sqlite; do
    [ -f "$_fdb" ] || continue
    _fsnap="$_fdb.restic"
    FRS_TOT=$(( FRS_TOT + 1 ))
    if [ ! -f "$_fsnap" ]; then
      say_err "freshrss user DB has NO snapshot: $_fsnap"
      _frc=1
    elif assert_fresh "$_fsnap" "freshrss snapshot"; then
      FRS_OK=$(( FRS_OK + 1 ))
    else
      _frc=1
    fi
  done
  return $_frc
}

check_expected() {
  _rc=0
  # EXP_OK/EXP_TOT feed `expected=` in the ping body. freshrss counts as one
  # entry of the expected set, and its per-user detail is FRS_OK/FRS_TOT.
  EXP_OK=0
  EXP_TOT=0
  set -f                      # split the list without glob-expanding it
  for _e in $EXPECTED_SNAPSHOTS; do
    EXP_TOT=$(( EXP_TOT + 1 ))
    _label=${_e%%:*}
    _glob=${_e#*:}
    _path=$(newest_match "$_glob"); _s=$?
    case "$_s" in
      0) if assert_fresh "$_path" "$_label snapshot"; then
           EXP_OK=$(( EXP_OK + 1 ))
         else
           _rc=1
         fi ;;
      1) say_err "$_label snapshot MISSING - nothing matches $_glob"; _rc=1 ;;
      *) say_err "$_label snapshot UNREADABLE - could not stat a match of $_glob"; _rc=1 ;;
    esac
  done
  set +f
  EXP_TOT=$(( EXP_TOT + 1 ))
  if check_freshrss; then
    EXP_OK=$(( EXP_OK + 1 ))
  else
    _rc=1
  fi
  return $_rc
}

sweep_advisory() {
  # find's STDOUT is the file list and its STDERR is diagnostics; the two
  # must not be merged. With `2>&1` a warning line - an unreadable
  # subdirectory, a directory that vanished mid-walk - was captured into
  # $_sout and then printed as though it were the path of a stale
  # snapshot. Same shape as the `du` stderr bug fixed in the homelab gate
  # (#33): the exit status is what says whether the walk completed, the
  # stdout is what says what it found. Diagnostics go to the pod log,
  # where they are read, rather than into a variable that is parsed.
  _sout=$(find /data -name '*.restic' -mmin +"$STALE_MINUTES" -print); _ss=$?
  if [ "$_ss" -ne 0 ]; then
    ADV_STALE=unknown
    say_err "advisory sweep could not complete - find exited $_ss (diagnostics on stderr, above)"
    return 1
  fi
  [ -n "$_sout" ] || { ADV_STALE=0; return 0; }
  # A COUNT, never the list. The paths go to the pod log below; only how many
  # there were reaches the ping body, and only after a digits-only gate.
  ADV_STALE=$(printf '%s\n' "$_sout" | grep -c .)
  case "$ADV_STALE" in ''|*[!0-9]*) ADV_STALE=unknown ;; esac
  # check-ping-bodies: untaint ADV_STALE - gated to digits by the case above; it is a count, never find's output
  echo "WARNING (advisory, does not fail the job) stale *.restic files:"
  echo "$_sout"
  return 0
}

# Initialised before the run so `set -u` cannot bite in the body block below
# if the chain aborts before a gate function ever ran.
EXP_OK=0; EXP_TOT=0; FRS_OK=0; FRS_TOT=0; ADV_STALE=unknown; RESTIC_CHECK=not-reached

hc_reset
emit "summary=starting"
ping_hc start
rc=0
# Explicit && chaining rather than `set -e` inside a group:
# errexit is ignored for any command in an AND-OR list, so a
# `{ set -e; ... } || rc=$?` block would keep running after a
# failure and report the last command's status.
{
  step snapshots &&
  { restic snapshots || true; } &&
  step unlock &&
  # Removes STALE locks only (no --remove-all). This is what
  # actually recovers a lock left by a SIGKILLed previous run.
  restic unlock &&
  step backup "backup /data" &&
  restic backup /data \
    --tag nightly \
    --exclude='*.tmp' \
    --exclude='cache/*' &&
  step forget "forget + prune" &&
  # --group-by paths is LOAD-BEARING. restic forget defaults to
  # grouping by host+paths, and every CronJob pod has a unique
  # hostname, so each nightly snapshot landed in a group of its
  # own and the policy below kept ALL of them: verified
  # 2026-08-20 with 137 snapshots in 137 groups across 131
  # hostnames, zero ever pruned. Grouping by paths alone makes
  # the retention policy actually apply.
  restic forget --prune \
    --group-by paths \
    --keep-daily 7 \
    --keep-weekly 4 \
    --keep-monthly 6 &&
  step check &&
  restic check &&
  step finished "done"
} || rc=$?

# The gate runs LAST, deliberately. Making it a precondition
# is the intuitive move and it is wrong: one stale or missing
# sqlite snapshot would then skip the entire night's backup of
# everything else, and the backup is worth more than the gate.
# Protect the data first, then fail the job so the fault still
# turns the healthchecks.io ping red. Do not "fix" this by
# moving it back above the chain.
echo "==> backup verification gate"
# NOW is sampled here, not at script start: the backup above
# can run for hours, and an age measured against a stale clock
# reading would silently loosen the freshness threshold.
NOW=$(date +%s)
gate_rc=0
check_expected || gate_rc=1
sweep_advisory || gate_rc=1
# Only promote to failure if the backup itself succeeded - a
# real restic failure keeps its own, more specific exit code.
[ "$rc" -ne 0 ] || rc=$gate_rc

# ---- ping body ----------------------------------------------------
# Built AFTER the exit status is final, and never inside an && chain
# or an rc-determining pipeline. Every value is a count this gate had
# already computed; nothing is captured from a command. Spec 9.2.
# Three states. `not-reached` is the INITIAL value and must keep meaning "the chain
# died before restic check ran"; a check that ran and failed is `failed`. Reporting
# the second as the first contradicts failed_step=check in the same body.
if   [ "$STEP" = finished ]; then RESTIC_CHECK=ok
elif [ "$STEP" = check ];    then RESTIC_CHECK=failed
fi
if [ "$rc" -eq 0 ]; then
  emit "summary=ok - $EXP_OK/$EXP_TOT expected snapshots fresh, $FRS_OK/$FRS_TOT freshrss users, $ADV_STALE advisory"
elif [ "$STEP" = finished ]; then
  emit "summary=FAILED rc=$rc - backup verification gate failed"
  emit "failed_step=gate"
else
  # restic's own failure: a B2 outage, a credential rotation, a lock
  # conflict, a corrupt pack. NOTHING CAPTURED, EVER - not restic's
  # stdout, not its stderr, not a slice of either. Not because a
  # repository URI is sensitive (it is not), but because output this
  # script did not construct cannot be classified at runtime.
  emit "summary=FAILED rc=$rc - restic exited non-zero"
  emit "failed_step=$STEP"
  emit "error=restic exited non-zero; see pod log"
fi
emit "expected=$EXP_OK/$EXP_TOT"
emit "freshrss_users=$FRS_OK/$FRS_TOT"
emit "advisory_stale=$ADV_STALE"
emit "restic_check=$RESTIC_CHECK"
flush_errors

ping_hc "$rc"
exit $rc
