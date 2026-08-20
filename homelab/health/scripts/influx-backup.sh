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
# shellcheck disable=SC2012 # every name here is written by the two scripts
# above as `<YYYY-MM-DD>` or `<YYYY-MM-DD>-<bucket>.lp.gz`, so the whitespace
# and newline hazards behind SC2012 cannot occur; `ls -t` also sorts by mtime,
# which `find` alone does not.
ls -1dt /dumps/native/* | tail -n +15 | xargs -r rm -rf
# shellcheck disable=SC2012 # as above
ls -1t  /dumps/lp/*.lp.gz | tail -n +61 | xargs -r rm -f

# Dead-man's switch. Last line on purpose: this script is `set -eu`, so the
# ping is only reached when everything above succeeded, and healthchecks.io
# reads silence as failure.
wget -q -O- "https://hc-ping.com/$HC_UUID" >/dev/null
