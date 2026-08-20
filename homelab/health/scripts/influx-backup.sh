#!/bin/sh
# Nightly InfluxDB backup driver. Runs in the `influx-backup` CronJob pod
# (alpine/k8s), NOT in the influxdb pod.
#
# It takes two kinds of dump by exec-ing into the influxdb pod, prunes the
# short tail, and pings healthchecks.io. restic sweeps the same PVC an hour
# later and holds the long history in B2.
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

DATE=$(date +%F)
POD=$(kubectl -n health get pod -l app=influxdb -o jsonpath='{.items[0].metadata.name}')

# Native backup (online, via the HTTP API, operator token read from the influxdb
# pod's own env). `sh -c CMD name arg` sets $0=name and $1=arg inside the inner
# shell, so the date crosses the boundary as a positional parameter rather than
# as spliced-in quoting.
kubectl -n health exec "$POD" -- sh -c "$(cat /scripts/influx-native-backup.sh)" \
  influx-native-backup "$DATE"

# Portable line-protocol export, per bucket, last 8 days.
kubectl -n health exec "$POD" -- sh -c "$(cat /scripts/influx-export-lp.sh)" \
  influx-export-lp "$DATE"

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
    echo "FATAL: prune $_label: nothing matches $1 — retention has nothing to" \
         "work on, which means the dump above did not land" >&2
    return 1
  fi
  # shellcheck disable=SC2012 # every name here is written by the two scripts
  # above as `<YYYY-MM-DD>` or `<YYYY-MM-DD>-<bucket>.lp.gz`, so the whitespace
  # and newline hazards behind SC2012 cannot occur; `ls -t` also sorts by
  # mtime, which `find` alone does not.
  ls -1dt "$@" | tail -n "+$((_keep + 1))" | xargs -r rm -rf
}

prune_to native 14 /dumps/native/*
prune_to lp     60 /dumps/lp/*.lp.gz

# Dead-man's switch. Last line on purpose: this script is `set -eu`, so the
# ping is only reached when everything above succeeded, and healthchecks.io
# reads silence as failure.
wget -q -O- "https://hc-ping.com/$HC_UUID" >/dev/null
