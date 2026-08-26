#!/bin/sh
# Nightly logical dump of the hindsight database. Runs in the `hindsight-pg-dump`
# CronJob pod on postgres:17.6-alpine - NOT on the server's pgvector image, which
# ships neither curl nor wget and so could never deliver a heartbeat. What matters
# for pg_dump is that the MAJOR matches the server's (17); the manifest says so
# beside the image and the two are bumped together.
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
# NO `set -o pipefail`, because nothing here needs it: pg_dump writes a plain
# file with --file rather than piping into gzip, and the content assertion reads
# that file directly, so no verdict is ever taken from the last stage of a
# pipeline. The one pipeline in the file - the message transliteration in
# push_kuma - cannot fail the job by design, and yields an empty message if it
# somehow does.
set -eu

# ---- uptime-kuma push with a short message --------------------------------
# EXIT CODE FROM AN EXIT TRAP, so a failure can never be silence - the repo rule
# for scheduled work, and the same shape influx-backup.sh uses. Since 2026-08-26
# the destination is the `hindsight-pg-dump` uptime-kuma PUSH monitor rather than
# a healthchecks.io check, which changes two things:
#
#   NO /start PING. The push API has no such concept: a push is a heartbeat
#   carrying a status. `activeDeadlineSeconds` on the CronJob is the WHOLE of the
#   hang bound, and the monitor's heartbeat interval plus retry is the silence
#   bound. A run that starts and wedges is killed by the deadline and then shows
#   up as a missing heartbeat - the same alarm, one step later.
#
#   THE MESSAGE IS SHORT AND FIXED-SHAPE. kuma stores one `msg` string per
#   heartbeat, so what travels with the alert is a verdict, a table count and a
#   size. The rest is echoed to this pod's log by the trap.
#
# A PUSH MUST NEVER FAIL THE JOB, AND A MESSAGE MUST NEVER COST A PUSH.
# NEVER EMIT A COMMAND'S OUTPUT: a failing wget quotes what it was given, a push
# URL carries the monitor's token as its last path segment, and pg_dump's
# diagnostics quote the connection string. `make check-ping-bodies` enforces it.
# Everything below is a count, a byte size or a verdict from a fixed enum.
# `emit` keeps its name: that guard recognises a body sink by FUNCTION NAME and
# never by the ping host.
#
# THE TOKEN REACHES THIS SCRIPT AS `PUSH_URL`, NOT AS ITS REAL NAME, for the
# same envsubst reason as PGPASSWORD above. `make check-script-substitution`
# enforces the rename; the real name is in homelab/hindsight/hindsight.yaml.
#
# `true >`, not `: >`: a redirection error on a POSIX special built-in aborts the
# shell even behind `|| true`. Tokens are space-separated on ONE line, because a
# kuma msg is one line.
MSG_FILE=/tmp/kuma-msg
# The stderr redirection PRECEDES the message redirection in both. Redirections are
# applied left to right, so `>> "$MSG_FILE" 2>/dev/null` cannot suppress the shell's
# own "cannot create" diagnostic - only this order can.
msg_reset() { true 2>/dev/null > "$MSG_FILE" || true; }
# shellcheck disable=SC2329 # called only from on_exit, which runs from the EXIT trap.
emit() { { printf '%s ' "$*" | LC_ALL=C tr -cd '\040-\176'; } 2>/dev/null >> "$MSG_FILE" || true; }

# GET https://uptime.cynexia.com/api/push/<token>?status=up|down&msg=<short>
#
# WGET, NOT curl, AND THE CHOICE IS PER-IMAGE. postgres:17.6-alpine ships busybox
# wget and NO curl - probed in-cluster on 2026-08-26, which is also why this job
# does not run on the server's Debian pgvector image, which has neither. Do not
# copy the curl form from the health scripts into this file without re-probing;
# do not "fix" a missing client by installing one at runtime either, because an
# apk add in a backup job puts a network dependency in the recovery path. If a
# future image drops wget the dump still runs and only the heartbeat is lost, so
# the monitor goes DOWN by silence - the correct outcome, and visible on the
# first manual run.
#
# THE MESSAGE IS MADE URL-SAFE HERE, BY TRANSLITERATION. wget has no
# `--data-urlencode`, so every character outside the unreserved set plus `=:/.-`
# becomes `+`, which a query parser decodes back to a space. Every message this
# script builds is key=value pairs of digits and lower-case words, so nothing
# legible is lost - and no value is ever interpolated into the URL as syntax.
# Capped at 200 characters because kuma stores the msg in one column.
#
# stderr is discarded and a FIXED line printed instead: wget's own diagnostics
# quote what they were handed, and a pod log is not a place for a token.
# shellcheck disable=SC2329 # called only from on_exit, which runs from the EXIT trap.
push_kuma() {
  _st=$1
  _m=$(cut -c1-200 "$MSG_FILE" 2>/dev/null | tr -d '\n' \
       | LC_ALL=C tr -c 'A-Za-z0-9=._:/-' '+') || _m=""
  wget -q -T 15 -O /dev/null "$PUSH_URL?status=$_st&msg=$_m" >/dev/null 2>&1 \
    || echo "kuma: push not delivered" >&2
  msg_reset
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

# ---- message values -------------------------------------------------------
# `unknown` sentinels so `set -u` cannot bite inside the trap if the run dies
# before a measurement ran, and so a missing measurement reads as missing rather
# than as zero. VERDICT starts at the failure that is true before anything has
# run, and is narrowed as the script gets further.
TABLES=unknown
DUMP_BYTES=unknown
# KiB, not bytes, in the heartbeat: the message is one line and a size in KiB is
# what the restic gate's floor is expressed in too. Bytes stay in the pod log.
DUMP_KIB=unknown
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
  # THE FULL DETAIL GOES TO THE POD LOG, and a verdict plus two numbers travel
  # with the alert. This line is what the multi-line healthchecks.io body used to
  # be; read it before ttlSecondsAfterFinished collects the pod.
  echo "detail: rc=$_xrc verdict=$VERDICT tables=$TABLES dump_bytes=$DUMP_BYTES kept=$KEPT"
  msg_reset
  emit "verdict=$VERDICT"
  emit "dump_kib=$DUMP_KIB"
  emit "tables=$TABLES"
  emit "kept=$KEPT"
  if [ "$_xrc" -eq 0 ]; then
    push_kuma up
  else
    push_kuma down
  fi
  exit "$_xrc"
}
trap on_exit EXIT

msg_reset

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
# Computed BEFORE the floor test, so a dump that fails the floor still reports
# how big it was - which is the first thing anyone reading that alert wants.
DUMP_KIB=$(( DUMP_BYTES / 1024 ))
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
