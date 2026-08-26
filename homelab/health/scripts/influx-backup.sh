#!/bin/sh
# Nightly health-namespace logical backup driver. Runs in the `influx-backup`
# CronJob pod (alpine/k8s), NOT in the influxdb pod.
#
# It takes two kinds of InfluxDB dump by exec-ing into the influxdb pod, takes a
# consistent point-in-time copy of Grafana's SQLite database in this pod, prunes
# the short tail of all three, and pushes a heartbeat to uptime-kuma. restic
# sweeps the same PVC THIRTY MINUTES later - this CronJob runs at 02:30 and the
# restic job with its freshness gate at 03:00 - and holds the long history in B2.
#
# THE GRAFANA STEP RUNS HERE, NOT OVER `kubectl exec`, and not in a job of its
# own. The grafana pod has no sqlite3 and no python3, so the copy has to be
# taken from outside it; local-path is node-local and ReadWriteOnce permits a
# second pod on the SAME node, so this pod mounts the `grafana-data` PVC
# read-only at /grafana and reads the database directly. A sibling CronJob would
# have bought a second monitor, a second image pin and a second set of deadlines
# for one `.backup` call. See grafana-sqlite-backup.py for why it is Python
# rather than the sqlite3 CLI.
#
# TWO MOUNT PATHS, ONE VOLUME — read this before "fixing" a path.
# The `health-dumps` PVC is mounted at /backups inside the influxdb pod and at
# /dumps inside THIS pod. So the two exec'd scripts write to /backups/native
# and /backups/lp, and the prune below deletes from /dumps/native and
# /dumps/lp. Those are the same directories seen through two mounts. Read
# standalone this looks like a bug; it is not.
#
# The two inner scripts are shipped to the influxdb pod as text, via
# `sh -c "$(cat ...)"`, because the influxdb pod does not mount this ConfigMap
# and must not start doing so: mounting it there would couple influxdb.yaml to
# the backup ConfigMap and force an influxdb rollout on every script edit.
set -eu
# shellcheck disable=SC3040 # `set -o pipefail` is not POSIX, but the
# alpine/k8s:1.36.0 image's /bin/sh is busybox ash, which implements it. It is
# required by the prune step below, whose failure was previously masked by the
# last command in its pipeline. If a future image lacked it, this line would
# fail under `set -e` and the job would stop loudly rather than silently
# swallowing a broken pipeline.
set -o pipefail

# ---- uptime-kuma push with a short message --------------------------------
# EXIT CODE FROM AN EXIT TRAP, NOT SUCCESS-ONLY. This script is `set -eu` with
# the heartbeat last, so under the original success-only shape a failing prune,
# a missing script or a dead influxdb pod produced EXACTLY NOTHING until the
# grace expired ~30 hours later. It was the only check here whose failures were
# invisible. The trap pushes `down` instead, so a hard failure alerts in a
# minute. The accepted cost is a transient failure - an influxdb pod mid-restart
# when `kubectl exec` lands, an API-server blip - that used to self-heal into
# silence and now alerts immediately. 30 hours of silence on a hard failure is
# worse than an occasional false red on a nightly job.
#
# THE SWITCH FROM healthchecks.io TO AN uptime-kuma PUSH MONITOR (2026-08-26)
# changed two things and nothing else:
#
#   NO /start PING. The push API has no such concept: a push is a heartbeat
#   carrying a status. So `activeDeadlineSeconds` on the CronJob is the WHOLE of
#   the hang bound, and the monitor's heartbeat interval plus retries is the
#   silence bound. A run that starts and wedges is killed by the deadline and
#   then shows up as a missing heartbeat - the same alarm, one step later.
#
#   THE MESSAGE IS SHORT AND FIXED-SHAPE. kuma stores one `msg` string per
#   heartbeat, so what travels with the alert is a verdict plus two numbers. The
#   full nightly detail - every size and every prune count - is echoed to this
#   pod's log by the trap instead. Read the pod log first; the heartbeat history
#   is the fallback, not the record.
#
# A PUSH MUST NEVER FAIL THE JOB, AND A MESSAGE MUST NEVER COST A PUSH.
# NEVER EMIT A COMMAND'S OUTPUT: this script `kubectl exec`s two scripts that
# pass the InfluxDB OPERATOR token on argv, so anything echoing argv - a
# syntax error, a CLI usage dump, a future `set -x` - would put the token that
# reads and writes every health bucket into the alert, repeated nightly. And a
# failing curl quotes the URL, which for a push carries the monitor's token as
# its last path segment. `make check-ping-bodies` enforces both; spec section
# 9.2 says why. `emit` deliberately keeps its name: that guard recognises a body
# sink by FUNCTION NAME, never by the ping host, so renaming it would drop this
# file out of coverage.
#
# THE TOKEN REACHES THIS SCRIPT AS `PUSH_URL`, NOT AS ITS REAL NAME. Generated
# scripts ride the same envsubst stream as every manifest and envsubst
# substitutes the bare $NAME form as well as ${NAME}, so naming the allowlisted
# variable here - even in a comment - would publish the token inside a
# ConfigMap. `make check-script-substitution` enforces the rename; the real name
# is in homelab/health/backups.yaml, where substitution is what is meant to
# happen. PUSH_URL arrives already carrying the token.
#
# `true >`, not `: >`: a redirection error on a POSIX special built-in aborts
# the shell even behind `|| true`. Tokens are space-separated on ONE line,
# because a kuma msg is one line.
MSG_FILE=/tmp/kuma-msg
# The stderr redirection PRECEDES the message redirection in both. Redirections
# are applied left to right, so `>> "$MSG_FILE" 2>/dev/null` cannot suppress the
# shell's own "cannot create" diagnostic - only this order can (verified in dash
# and busybox 1.36.1). Keeping the push alive on that day is what the `|| true`
# does; this is what keeps the log readable.
msg_reset() { true 2>/dev/null > "$MSG_FILE" || true; }
# shellcheck disable=SC2329 # called only from on_exit, which runs from the EXIT trap.
emit() { { printf '%s ' "$*" | LC_ALL=C tr -cd '\040-\176'; } 2>/dev/null >> "$MSG_FILE" || true; }

# GET https://uptime.cynexia.com/api/push/<token>?status=up|down&msg=<short>
# `-G --data-urlencode` builds the query safely: the message never has to be
# escaped by hand, and no value is interpolated into the URL string. The msg is
# capped at 200 characters because kuma stores it in one column and an alert
# nobody can read is worse than a shorter one. curl, not wget: alpine/k8s:1.36.0
# has both (re-verified in-cluster 2026-08-26) and curl is what does the
# encoding.
# shellcheck disable=SC2329 # called only from on_exit, which runs from the EXIT trap.
push_kuma() {
  _st=$1
  _m=$(cut -c1-200 "$MSG_FILE" 2>/dev/null) || _m=""
  curl -fsS -m 15 -o /dev/null -G \
    --data-urlencode "status=$_st" \
    --data-urlencode "msg=$_m" \
    "$PUSH_URL" || echo "kuma: push not delivered" >&2
  msg_reset
  return 0
}

# STEP names the phase for failed_step=. `step` also clears FATAL_MSG, so a
# FATAL from an earlier phase can never be reported against a later one.
STEP=startup
FATAL_MSG=""
step() { STEP=$1; FATAL_MSG=""; }

# ONE SINK PER DIAGNOSTIC. `fatal` writes the message to stderr exactly as the
# FATAL: echoes it replaces did, and holds it for the heartbeat message, where
# the trap emits it last so a long one is what the 200-character cut takes.
fatal() {
  FATAL_MSG=$*
  echo "FATAL: $*" >&2
  exit 1
}

# How many line-protocol exports a complete run produces: one per bucket in
# influx-export-lp.sh's explicit `for B in ...` list. It is a literal here
# because that list lives in the OTHER pod's script and cannot be read from this
# one. Nothing depends on the two agreeing - influx-export-lp.sh already fails by
# name on a bucket it cannot find, so on the success path n is always m - but a
# `buckets=5/4` in the heartbeat is the visible tell that a bucket was added
# there and not here. Adding a bucket is three edits now: create it, list it in
# influx-export-lp.sh, and raise this.
LP_EXPECTED=4

# Message values. `unknown` sentinels so `set -u` cannot bite in the trap if the
# run dies before a measurement ran, and so a missing measurement reads as
# missing rather than as zero.
NATIVE_KIB=unknown
LP_FILES=unknown
LP_KIB=unknown
NATIVE_MIB=unknown
GRAFANA_KIB=unknown
PRUNED=unknown
PRUNED_NATIVE=unknown
PRUNED_LP=unknown
PRUNED_GRAFANA=unknown

# THE TRAP'S FIRST ACTION IS CAPTURING $?. Anything before that - a `trap -`,
# an echo, a reset - overwrites the status being reported. It is armed BEFORE
# the first thing that can fail, which is why the `script-missing` branch
# below can no longer exit silently.
# shellcheck disable=SC2329 # invoked by `trap ... EXIT` below, not by name.
on_exit() {
  _xrc=$?
  trap - EXIT
  # THE FULL NIGHTLY DETAIL GOES HERE, TO THE POD LOG, and only a verdict plus
  # two numbers travel with the alert. This line is what the multi-line
  # healthchecks.io body used to be; the migration to a one-line kuma msg is why
  # it exists. Read it before ttlSecondsAfterFinished collects the pod.
  echo "detail: rc=$_xrc step=$STEP native_kib=$NATIVE_KIB native_mib=$NATIVE_MIB" \
       "lp_files=$LP_FILES lp_kib=$LP_KIB grafana_kib=$GRAFANA_KIB" \
       "pruned_native=$PRUNED_NATIVE pruned_lp=$PRUNED_LP pruned_grafana=$PRUNED_GRAFANA"
  msg_reset
  if [ "$_xrc" -eq 0 ]; then
    emit "verdict=ok"
  else
    emit "verdict=failed"
    emit "failed_step=$STEP"
  fi
  emit "buckets=$LP_FILES/$LP_EXPECTED"
  emit "grafana_kib=$GRAFANA_KIB"
  # LAST, because it is the only variable-length token and the msg is cut at 200
  # characters: everything above it is guaranteed to survive.
  #
  # NOTHING CAPTURED, EVER. failed_step=native and failed_step=lp are bare
  # `kubectl exec` calls with no FATAL of their own, and the only diagnostic
  # available for them is the exec'd script's output - which is produced by
  # scripts that pass the operator token on argv. Those two branches emit
  # failed_step and nothing else, by construction: FATAL_MSG is empty unless
  # `fatal` set it. If a diagnostic is wanted there, add a FATAL: line to
  # THIS script naming the step, in the same commit, and emit that literal.
  if [ "$_xrc" -ne 0 ] && [ -n "$FATAL_MSG" ]; then
    emit "error=$FATAL_MSG"
  fi
  if [ "$_xrc" -eq 0 ]; then
    push_kuma up
  else
    push_kuma down
  fi
  exit "$_xrc"
}
trap on_exit EXIT

msg_reset

step influxdb-pod-lookup
DATE=$(date +%F)
POD=$(kubectl -n health get pod -l app=influxdb -o jsonpath='{.items[0].metadata.name}')

# ASSERT THE TWO SHIPPED-AS-TEXT SCRIPTS ARE ACTUALLY THERE, BEFORE USING THEM.
# Both run inside the influxdb pod, which does not mount this ConfigMap, so
# they cross the boundary as `sh -c "$(cat /scripts/x.sh)"`. That construct
# fails open in a way the inline block scalar it replaced could not:
#
#   - a command substitution in ARGUMENT position does not trip `set -e`, so a
#     `cat` of a missing file is not fatal, it just yields the empty string;
#   - `sh -c ''` exits 0.
#
# Both exec steps would therefore become silent no-ops, `prune_to` would find
# the PREVIOUS nights' dumps and succeed, and the trap would push an `up`
# heartbeat for a backup that captured nothing. Verified:
#
#   $ sh -c 'set -eu; printf "[%s]" "$(cat /nonexistent 2>/dev/null)"; \
#            sh -c "" n a; echo rc=$?'
#   [] rc=0
#
# A missing key here is not exotic: the ConfigMap mounts fine with a key
# dropped from the generator, so the pod starts and only the file is absent.
step script-missing
for _s in /scripts/influx-native-backup.sh /scripts/influx-export-lp.sh; do
  if [ ! -s "$_s" ]; then
    fatal "$_s is missing or empty - refusing to run a backup that would" \
          "silently capture nothing and then report success"
  fi
done

# Native backup (online, via the HTTP API, operator token read from the influxdb
# pod's own env). `sh -c CMD name arg` sets $0=name and $1=arg inside the inner
# shell, so the date crosses the boundary as a positional parameter rather than
# as spliced-in quoting.
step native
kubectl -n health exec "$POD" -- sh -c "$(cat /scripts/influx-native-backup.sh)" \
  influx-native-backup "$DATE"

# Portable line-protocol export, per bucket, last 8 days.
step lp
kubectl -n health exec "$POD" -- sh -c "$(cat /scripts/influx-export-lp.sh)" \
  influx-export-lp "$DATE"

# Consistent point-in-time copy of Grafana's SQLite database, taken with
# SQLite's online backup API against the read-only /grafana mount. The script
# verifies the copy (integrity_check, schema objects, byte floor) and publishes
# it atomically, so anything that lands in /dumps/grafana has already been
# opened and read back.
#
# No `[ -s ]` guard like the two scripts above need: this one is EXECUTED, not
# spliced into a `sh -c` argument, so a missing or empty file is a non-zero exit
# from python3 rather than a silent no-op, and `set -e` stops the run here.
#
# python3, not sqlite3: alpine/k8s:1.36.0 has no sqlite3 binary. The reasoning
# and the alternatives considered are in grafana-sqlite-backup.py's header.
step grafana
python3 /scripts/grafana-sqlite-backup.py /grafana/grafana.db /dumps/grafana "$DATE"

# Prune: keep 14 native dumps and 60 line-protocol exports. restic retention
# holds the long tail in B2.
#
# THE PIPELINE STATUS HERE IS LOAD-BEARING. This used to be a bare
#
#   ls -1dt /dumps/native/* | tail -n +15 | xargs -r rm -rf
#
# under `set -eu`, and a pipeline exits with its LAST command's status — so the
# verdict came from `xargs`, which succeeds at doing nothing. Every way `ls`
# can fail (an unmatched glob, the PVC not mounted, a permissions problem)
# therefore produced exit 0, `set -e` never fired, the run reached its exit
# trap with rc 0, and the monitor went UP. Retention would have
# stopped silently, health-dumps would have filled, and the failure would first
# have surfaced as the next influx backup dying on ENOSPC.
#
# Two things close that: `set -o pipefail` above, so `ls` failing fails the
# pipeline, and the explicit `-e` test in prune_to, which turns the commonest
# case — a glob that matched nothing — into a named error instead of a bare
# `ls: No such file or directory`.
#
# An unmatched glob really is a fault here, not an empty first run: both
# directories are created by the mkdirs initContainer, and the two exec'd
# scripts above have already written today's dump into each of them by the time
# this runs. Nothing matching means something upstream did not happen.
prune_to() {
  # $1 = label for the error message, $2 = how many newest entries to keep,
  # $3.. = the already-expanded paths.
  _label=$1
  _keep=$2
  shift 2
  if [ ! -e "$1" ]; then
    fatal "prune $_label: nothing matches $1 - retention has nothing to" \
          "work on, which means the dump above did not land"
  fi
  # THE PIPELINE STATUS IS STILL LOAD-BEARING. Capturing the victim list into
  # a variable does not weaken it: under `set -e` the status of an assignment
  # IS the status of the command substitution, and `set -o pipefail` above
  # makes that the pipeline's worst status. So an `ls` that fails still aborts
  # the script, exactly as it did when the pipeline ran inline. What the
  # capture buys is a COUNT, which is the only new thing here.
  #
  # shellcheck disable=SC2012 # every name here is written by the two scripts
  # above as `<YYYY-MM-DD>` or `<YYYY-MM-DD>-<bucket>.lp.gz`, so the whitespace
  # and newline hazards behind SC2012 cannot occur; `ls -t` also sorts by
  # mtime, which `find` alone does not.
  _victims=$(ls -1dt "$@" | tail -n "+$((_keep + 1))")
  PRUNED=0
  [ -n "$_victims" ] || return 0
  PRUNED=$(printf '%s\n' "$_victims" | grep -c .)
  case "$PRUNED" in ''|*[!0-9]*) PRUNED=unknown ;; esac
  # check-ping-bodies: untaint PRUNED - a count of lines, gated to digits by the case above; the paths themselves never leave this function
  printf '%s\n' "$_victims" | xargs -r rm -rf
}

step prune-native
prune_to native 14 /dumps/native/*
PRUNED_NATIVE=$PRUNED

step prune-lp
prune_to lp     60 /dumps/lp/*.lp.gz
PRUNED_LP=$PRUNED

# 14 generations, matching the native influx dumps: a fortnight is long enough
# to notice a bad Grafana upgrade and roll back, and restic holds the longer
# history in B2. The glob deliberately excludes the `.tmp-` staging file, which
# a failed dump removes anyway.
step prune-grafana
prune_to grafana 14 /dumps/grafana/*-grafana.db
PRUNED_GRAFANA=$PRUNED

# ---- measurements for the heartbeat message and the detail line -----------
# EVERY COMMAND HERE IS SUFFIXED `|| true` WITH A SENTINEL. A measurement must
# never be able to fail a backup that succeeded, and `set -e` is in force.
# `du -sk`, not `du -sb`: busybox du has no -b, and -sk is what the restic gate
# already uses. Units live in the key (spec section 5).
step measure
NATIVE_KIB=$(du -sk "/dumps/native/$DATE" 2>/dev/null | cut -f1) || NATIVE_KIB=unknown
case "$NATIVE_KIB" in ''|*[!0-9]*) NATIVE_KIB=unknown ;; esac
# check-ping-bodies: untaint NATIVE_KIB - du's KiB total, gated to digits by the case above
if [ "$NATIVE_KIB" = unknown ]; then
  NATIVE_MIB=unknown
else
  NATIVE_MIB=$(( NATIVE_KIB / 1024 ))
fi

# A glob loop, not `ls | grep -c`: pure arithmetic, so nothing here is
# captured output and nothing needs an untaint marker.
LP_FILES=0
for _lp in /dumps/lp/"$DATE"-*.lp.gz; do
  [ -f "$_lp" ] || continue
  LP_FILES=$(( LP_FILES + 1 ))
done
LP_KIB=$(du -ck /dumps/lp/"$DATE"-*.lp.gz 2>/dev/null | tail -n1 | cut -f1) || LP_KIB=unknown
case "$LP_KIB" in ''|*[!0-9]*) LP_KIB=unknown ;; esac
# check-ping-bodies: untaint LP_KIB - du's KiB total, gated to digits by the case above

GRAFANA_KIB=$(du -sk "/dumps/grafana/$DATE-grafana.db" 2>/dev/null | cut -f1) || GRAFANA_KIB=unknown
case "$GRAFANA_KIB" in ''|*[!0-9]*) GRAFANA_KIB=unknown ;; esac
# check-ping-bodies: untaint GRAFANA_KIB - du's KiB total, gated to digits by the case above
# THIS NUMBER IS HOW THE GATE FLOOR GETS SET. grafana-sqlite-backup.py ships a
# deliberately conservative 64 KiB placeholder and homelab/backup/restic-cronjob.yaml
# a matching one; after the first real run, read grafana_kib= off the heartbeat
# message and raise both to roughly an order of magnitude below it.

# The dead-man's switch fires from the EXIT trap above, on every path. There is
# deliberately no push on this line: a push here would only be reached on
# success, which is the behaviour the exit-trap conversion exists to remove.
step finished
exit 0
