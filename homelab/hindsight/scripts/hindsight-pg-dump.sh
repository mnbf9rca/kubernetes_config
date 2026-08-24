#!/bin/sh
# Nightly logical dump of the hindsight database. Runs in the `hindsight-pg-dump`
# CronJob pod, from the SAME pinned pgvector image the server runs, so pg_dump's
# client version matches the server's for free.
#
# WHY THIS EXISTS. Both hindsight PVCs are local-path, so the 03:00 restic sweep
# of /var/mnt/ssd/local-path-provisioner captures them with no backup-side
# plumbing — but it captures the LIVE postgres data directory, torn, because the
# homelab deliberately runs no quiesce sidecars. This dump is the recovery
# artifact; the restic sweep is what carries it off the node. Same division of
# labour as health/influx-backup.
#
# IT CONNECTS OVER THE SERVICE (-h hindsight-postgres), not by exec-ing into the
# postgres pod. That keeps the job free of pods/exec RBAC and of a ServiceAccount,
# and keeps the password off argv: pg_dump reads $PGPASSWORD from the environment,
# where a secretKeyRef put it.
#
# THE ENV VAR IS `PGPASSWORD`, NOT `HINDSIGHT_PG_PASSWORD`. Generated scripts ride
# the same envsubst stream as every manifest, and envsubst substitutes the bare
# $NAME form as well as ${NAME}. Every name in ENVSUBST_VAR_NAMES — which includes
# all six hindsight vars — would therefore be replaced by its real value inside a
# ConfigMap. The container maps the secret to a differently named variable and this
# script only ever sees that name. `make check-script-substitution` enforces it.
#
# NO `set -o pipefail`. This image's /bin/sh is dash (the pgvector image is
# Debian-based, not Alpine), and dash does not implement pipefail. So nothing here
# hides a failure inside a pipeline: pg_dump writes a plain file with --file, and
# the content assertion reads that file directly.
set -eu

# ---- healthchecks.io ping with a body ------------------------------------
# START PLUS EXIT CODE, from an EXIT trap, so a failure can never be silence —
# the repo rule for scheduled work, and the same shape influx-backup.sh uses.
#
# A PING MUST NEVER FAIL THE JOB, AND A BODY MUST NEVER COST A PING.
# NEVER EMIT A COMMAND'S OUTPUT: a failing curl or wget quotes the ping URL, which
# IS the check's write credential, and pg_dump's diagnostics quote the connection
# string. `make check-ping-bodies` enforces it. Everything below is a count, a byte
# size or a verdict from a fixed enum.
# A BARE TRAILING SLASH IS AN HTTP 400, so the URL is built conditionally.
# `true >`, not `: >`: a redirection error on a POSIX special built-in aborts the
# shell even behind `|| true`.
HC_BODY=/tmp/hc-body
# The stderr redirection PRECEDES the body redirection in both. Redirections are
# applied left to right, so `>> "$HC_BODY" 2>/dev/null` cannot suppress the shell's
# own "cannot create" diagnostic — only this order can.
hc_reset() { true 2>/dev/null > "$HC_BODY" || true; }
emit() { { printf '%s' "$*" | LC_ALL=C tr -cd '\040-\176'; printf '\n'; } 2>/dev/null >> "$HC_BODY" || true; }

# WHICH HTTP CLIENT. The pgvector image is chosen for pg_dump version parity, not
# for its tooling, and it may ship neither curl nor wget. Both are probed rather
# than assumed. If neither exists this job still runs and still dumps; what is lost
# is the ping, and the healthchecks.io check then goes red by silence — the correct
# outcome, and visible on the first manual run at rollout step 5. Do not "fix" a
# missing client by installing one at runtime: an apt-get in a backup job puts a
# network dependency in the recovery path.
HC_CURL=$(command -v curl 2>/dev/null || true)
HC_WGET=$(command -v wget 2>/dev/null || true)

# _hc_send URL BODYFILE — BODYFILE may be empty. Non-zero if not delivered.
_hc_send() {
  if [ -n "$HC_CURL" ]; then
    if [ -n "$2" ]; then
      curl -fsS -m 15 -o /dev/null --data-binary @"$2" "$1"
    else
      curl -fsS -m 15 -o /dev/null "$1"
    fi
  elif [ -n "$HC_WGET" ]; then
    if [ -n "$2" ]; then
      wget -q -T 15 -O /dev/null --post-file="$2" "$1"
    else
      wget -q -T 15 -O /dev/null "$1"
    fi
  else
    echo "hc: neither curl nor wget in this image; the check will go red by silence" >&2
    return 1
  fi
}

# ping_hc [SUFFIX] — "" | start | <exit-status>. Always returns 0.
ping_hc() {
  _sf=${1:-}
  _u="https://hc-ping.com/$HC_UUID"
  [ -z "$_sf" ] || _u="$_u/$_sf"
  if [ -s "$HC_BODY" ]; then
    if _hc_send "$_u" "$HC_BODY"; then
      hc_reset; return 0
    fi
    echo "hc: body POST failed, retrying without a body" >&2
  fi
  # Fixed text. No URL and no tool output: for a ping the URL IS the write
  # credential, and a pod log is not a place to put one either.
  _hc_send "$_u" "" || echo "hc: ping not delivered" >&2
  hc_reset
  return 0
}

# ---- naming ---------------------------------------------------------------
# THE PUBLISHED NAME CARRIES A TIMESTAMP TO SECONDS, NOT A DATE. Under date-only
# naming, an operator who runs `make hindsight-upgrade`, merges, applies, watches
# the migration go wrong and re-runs the target would atomically publish a
# POST-migration dump over the pre-upgrade one — destroying the exact rollback the
# target exists to create, in the exact situation it exists for. Retention keeps
# the newest 7 ARTIFACTS, not the newest 7 dates: a day with three runs consumes
# three slots, and each is a distinct restore point.
#
# THE STAGING NAMES CARRY $$ AS WELL. `concurrencyPolicy: Forbid` governs only
# CronJob-owned Jobs, so the 02:15 nightly can start midway through a manual
# pre-upgrade run and the two would otherwise share one staging path.
#
# The leading dot and the .raw/.tmp suffixes keep both staging files out of the
# `hindsight-*.sql.gz` glob that the prune below and the restic verification gate
# in homelab/backup/restic-cronjob.yaml both walk.
TS=$(date -u +%Y%m%d%H%M%S)
RAW="/dumps/.hindsight-$TS.$$.sql.raw"
TMP="/dumps/.hindsight-$TS.$$.sql.gz.tmp"
OUT="/dumps/hindsight-$TS.sql.gz"

# A FLOOR, NOT A TARGET. pg_dump exits 0 against an empty database, so the exit
# code alone is a lie; this and the CREATE TABLE count are what make a published
# artifact mean something. Measured at rollout step 5 (2026-08-24): the first
# real dump was 48,829 B / 23 tables; 4096 sits an order of magnitude below it,
# the same derivation the restic gate's EXPECTED_ARTIFACTS entries document.
# The gate's hindsight-dump row carries the same floor - raise the two together.
MIN_BYTES=4096
KEEP=7

# ---- body values ----------------------------------------------------------
# `unknown` sentinels so `set -u` cannot bite inside the trap if the run dies
# before a measurement ran, and so a missing measurement reads as missing rather
# than as zero. VERDICT starts at the failure that is true before anything has
# run, and is narrowed as the script gets further.
TABLES=unknown
DUMP_BYTES=unknown
KEPT=unknown
VERDICT=dump-failed

# THE TRAP'S FIRST ACTION IS CAPTURING $?. Anything before that — a `trap -`, an
# echo, a reset — overwrites the status being reported. It is armed BEFORE the
# first thing that can fail.
# shellcheck disable=SC2329 # invoked by `trap ... EXIT` below, not by name.
on_exit() {
  _xrc=$?
  trap - EXIT
  rm -f "$RAW" "$TMP" 2>/dev/null || true
  hc_reset
  if [ "$_xrc" -eq 0 ]; then
    emit "summary=ok - hindsight dump published, $TABLES tables, $DUMP_BYTES B, $KEPT kept"
  else
    emit "summary=FAILED rc=$_xrc - hindsight-pg-dump"
  fi
  emit "rc=$_xrc"
  emit "tables=$TABLES"
  emit "dump_bytes=$DUMP_BYTES"
  emit "kept=$KEPT"
  emit "verdict=$VERDICT"
  ping_hc "$_xrc"
  exit "$_xrc"
}
trap on_exit EXIT

hc_reset
emit "summary=starting"
ping_hc start

# ---- dump -----------------------------------------------------------------
# --clean --if-exists so the restore runbook needs no separate drop step, and
# PLAIN format so the content assertion below can read the SQL rather than trusting
# an exit code. `--file` rather than a pipe into gzip: dash has no pipefail, and a
# pg_dump that died mid-stream would otherwise be reported by gzip's status.
echo "==> pg_dump hindsight"
pg_dump -h hindsight-postgres -U hindsight -d hindsight --clean --if-exists --file="$RAW"

# ---- content assertion ----------------------------------------------------
# grep -c exits 1 on zero matches, which `set -e` would treat as fatal before the
# verdict could be narrowed, so the count is defaulted explicitly.
TABLES=$(grep -c '^CREATE TABLE ' "$RAW") || TABLES=0
case "$TABLES" in ''|*[!0-9]*) TABLES=0 ;; esac
# check-ping-bodies: untaint TABLES - a grep -c line count, gated to digits by the case above; no line of the dump itself is ever read out
if [ "$TABLES" -lt 1 ]; then
  VERDICT=empty-dump
  echo "ERROR: the dump contains no CREATE TABLE statements - refusing to publish it" >&2
  exit 1
fi

echo "==> gzip"
gzip -c "$RAW" > "$TMP"
rm -f "$RAW"

DUMP_BYTES=$(stat -c %s "$TMP") || DUMP_BYTES=0
case "$DUMP_BYTES" in ''|*[!0-9]*) DUMP_BYTES=0 ;; esac
# check-ping-bodies: untaint DUMP_BYTES - stat's byte count, gated to digits by the case above
if [ "$DUMP_BYTES" -lt "$MIN_BYTES" ]; then
  VERDICT=empty-dump
  echo "ERROR: the compressed dump is below the ${MIN_BYTES} B floor - refusing to publish it" >&2
  exit 1
fi

# ---- publish --------------------------------------------------------------
# `mv` within one filesystem is atomic, so neither the restic sweep nor the
# verification gate can ever see a half-written artifact under the published name.
mv "$TMP" "$OUT"
echo "==> published a dump of $DUMP_BYTES B"

# ---- prune ----------------------------------------------------------------
# Newest KEEP artifacts. The published names sort lexically in timestamp order, so
# a sorted glob is enough and no mtime is consulted.
prune() {
  # shellcheck disable=SC2046 # deliberate: the sorted list must be word-split into
  # positional parameters, and every name here was generated by this script as
  # hindsight-<digits>.sql.gz, so it carries no whitespace.
  set -- $(printf '%s\n' /dumps/hindsight-*.sql.gz | sort -r)
  _n=0
  for _p in "$@"; do
    [ -f "$_p" ] || continue
    _n=$(( _n + 1 ))
    [ "$_n" -le "$KEEP" ] || rm -f "$_p"
  done
}
prune

# A glob loop, not `ls | wc -l`: pure arithmetic, so nothing here is captured
# output and the count needs no untaint marker.
KEPT=0
for _f in /dumps/hindsight-*.sql.gz; do
  [ -f "$_f" ] || continue
  KEPT=$(( KEPT + 1 ))
done

VERDICT=ok
echo "==> done - $TABLES tables, $DUMP_BYTES B, $KEPT kept"
exit 0
