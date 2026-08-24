#!/bin/sh
# Nightly health-namespace logical backup driver. Runs in the `influx-backup`
# CronJob pod (alpine/k8s), NOT in the influxdb pod.
#
# It takes two kinds of InfluxDB dump by exec-ing into the influxdb pod, takes a
# consistent point-in-time copy of Grafana's SQLite database in this pod, prunes
# the short tail of all three, and pings healthchecks.io. restic sweeps the same
# PVC an hour later and holds the long history in B2.
#
# THE GRAFANA STEP RUNS HERE, NOT OVER `kubectl exec`, and not in a job of its
# own. The grafana pod has no sqlite3 and no python3, so the copy has to be
# taken from outside it; local-path is node-local and ReadWriteOnce permits a
# second pod on the SAME node, so this pod mounts the `grafana-data` PVC
# read-only at /grafana and reads the database directly. A sibling CronJob would
# have bought a second healthchecks.io check, a second image pin and a second
# set of deadlines for one `.backup` call. See grafana-sqlite-backup.py for why
# it is Python rather than the sqlite3 CLI.
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

# ---- healthchecks.io ping with a body ------------------------------------
# CONVERTED FROM SUCCESS-ONLY TO /start + EXIT CODE. This script is `set -eu`
# with the ping last, so until now a failing prune, a missing script or a dead
# influxdb pod produced EXACTLY NOTHING until the 6h grace expired ~30 hours
# later. It is the only check here whose failures were invisible.
#
# THIS CHANGES WHEN THE CHECK ALERTS, and that is the point:
#
#   script exits non-zero   was: silence, red ~30h later   now: red in a minute
#   pod killed/unscheduled  was: red at last_ping+1d+6h     now: red at last_start+6h
#   success                 was: green                      now: green, with a duration
#
# The accepted cost is a transient failure - an influxdb pod mid-restart when
# `kubectl exec` lands, an API-server blip - that used to self-heal into
# silence and now pages immediately. 30 hours of silence on a hard failure is
# worse than an occasional false red on a nightly job.
#
# A PING MUST NEVER FAIL THE JOB, AND A BODY MUST NEVER COST A PING.
# NEVER EMIT A COMMAND'S OUTPUT: this script `kubectl exec`s two scripts that
# pass the InfluxDB OPERATOR token on argv, so anything echoing argv - a
# syntax error, a CLI usage dump, a future `set -x` - would put the token that
# reads and writes every health bucket into a third-party-held body, repeated
# nightly. `make check-ping-bodies` enforces it; spec section 9.2 says why.
# A BARE TRAILING SLASH IS AN HTTP 400, so the URL is built conditionally.
# `true >`, not `: >`: a redirection error on a POSIX special built-in aborts
# the shell even behind `|| true`.
HC_BODY=/tmp/hc-body
# The stderr redirection PRECEDES the body redirection in both. Redirections
# are applied left to right, so `>> "$HC_BODY" 2>/dev/null` cannot suppress the
# shell's own "cannot create" diagnostic - only this order can (verified in dash
# and busybox 1.36.1). Property 4 above is what keeps the job alive on that day;
# this is what keeps its log readable.
hc_reset() { true 2>/dev/null > "$HC_BODY" || true; }
emit() { { printf '%s' "$*" | LC_ALL=C tr -cd '\040-\176'; printf '\n'; } 2>/dev/null >> "$HC_BODY" || true; }

# ping_hc [SUFFIX] - "" | start | <exit-status>. Always returns 0.
ping_hc() {
  _sf=${1:-}
  _u="https://hc-ping.com/$HC_UUID"
  [ -z "$_sf" ] || _u="$_u/$_sf"
  if [ -s "$HC_BODY" ]; then
    if curl -fsS -m 15 -o /dev/null --data-binary @"$HC_BODY" "$_u"; then
      hc_reset; return 0
    fi
    echo "hc: body POST failed, retrying without a body" >&2
  fi
  # Fixed text. No URL, no tool output: for a ping the URL IS the write
  # credential, and a pod log is not a place to put one either.
  curl -fsS -m 15 -o /dev/null "$_u" || echo "hc: ping not delivered" >&2
  hc_reset
  return 0
}

# STEP names the phase for failed_step=. `step` also clears FATAL_MSG, so a
# FATAL from an earlier phase can never be reported against a later one.
STEP=startup
FATAL_MSG=""
step() { STEP=$1; FATAL_MSG=""; }

# ONE SINK PER DIAGNOSTIC. `fatal` writes the message to stderr exactly as the
# FATAL: echoes it replaces did, and holds it for the body. Both existing
# FATAL branches were written to be read; today nothing reads them.
fatal() {
  FATAL_MSG=$*
  echo "FATAL: $*" >&2
  exit 1
}

# Body values. `unknown` sentinels so `set -u` cannot bite in the trap if the
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
  hc_reset
  if [ "$_xrc" -eq 0 ]; then
    emit "summary=ok - native dump $NATIVE_MIB MiB, $LP_FILES lp exports, grafana dump $GRAFANA_KIB KiB, pruned $PRUNED_NATIVE native / $PRUNED_LP lp / $PRUNED_GRAFANA grafana"
    emit "native_kib=$NATIVE_KIB"
    emit "lp_files=$LP_FILES"
    emit "lp_kib=$LP_KIB"
    emit "grafana_kib=$GRAFANA_KIB"
    emit "pruned_native=$PRUNED_NATIVE"
    emit "pruned_lp=$PRUNED_LP"
    emit "pruned_grafana=$PRUNED_GRAFANA"
  else
    emit "summary=FAILED rc=$_xrc - $STEP"
    emit "failed_step=$STEP"
    # NOTHING CAPTURED, EVER. failed_step=native and failed_step=lp are bare
    # `kubectl exec` calls with no FATAL of their own, and the only diagnostic
    # available for them is the exec'd script's output - which is produced by
    # scripts that pass the operator token on argv. Those two branches emit
    # failed_step and nothing else, by construction: FATAL_MSG is empty unless
    # `fatal` set it. If a diagnostic is wanted there, add a FATAL: line to
    # THIS script naming the step, in the same commit, and emit that literal.
    [ -z "$FATAL_MSG" ] || emit "error=$FATAL_MSG"
  fi
  ping_hc "$_xrc"
  exit "$_xrc"
}
trap on_exit EXIT

hc_reset
emit "summary=starting"
ping_hc start

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
# the PREVIOUS nights' dumps and succeed, and the healthchecks.io ping at the
# end would report SUCCESS for a backup that captured nothing. Verified:
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
# therefore produced exit 0, `set -e` never fired, the script ran on to the
# healthchecks.io ping below, and the check went GREEN. Retention would have
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

# ---- measurements for the ping body --------------------------------------
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
# a matching one; after the first real run, read grafana_kib= off the ping and
# raise both to roughly an order of magnitude below it.

# The dead-man's switch fires from the EXIT trap above, on every path. There is
# deliberately no ping on this line any more: a ping here would only be reached
# on success, which is the behaviour this conversion exists to remove.
step finished
exit 0
