#!/bin/sh
# Runs INSIDE the influxdb pod, shipped there as text by influx-backup.sh:
#
#   kubectl -n health exec "$POD" -- sh -c "$(cat /scripts/influx-export-lp.sh)" \
#     influx-export-lp "$DATE"
#
# `sh -c CMD name arg1` sets $0=name and $1=arg1, so the date arrives as $1.
#
# $DOCKER_INFLUXDB_INIT_ADMIN_TOKEN is the influxdb container's OWN env var and
# must reach this script unexpanded; see influx-native-backup.sh.
#
# /backups is the `health-dumps` PVC as mounted in THIS pod. The CronJob pod
# mounts the same PVC at /dumps and prunes there.
#
# Portable line-protocol export, per bucket, over the last 8 days. Windowed to
# avoid compaction races and keep runs small; the weekly overlap gives
# continuity between consecutive exports.
#
# The bucket list is EXPLICIT, and a missing bucket is fatal. A new bucket must
# be added here or it is silently never exported — the same class of bug as the
# VPS gate's expected-set assertion. `cloudflare` therefore requires
# `make health-influx-cloudflare-bootstrap` to have been run BEFORE the apply
# that adds it here, and `withings` the same for
# `make health-influx-withings-bootstrap`.
set -eu

DATE=$1

START=$(date -d "8 days ago" +%FT%TZ 2>/dev/null || date -v-8d +%FT%TZ)

for B in apple_metrics apple_workouts garmin cloudflare withings; do
  # A pipeline exits with its LAST command status, so a failed
  # `influx bucket list` leaves BID empty and sails past set -e. The explicit
  # test is what turns "bucket does not exist" into a named failure instead of
  # an opaque export-lp error.
  BID=$(influx bucket list -o cynexia -n "$B" --hide-headers \
          -t "$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN" | awk '{print $1}')
  if [ -z "$BID" ]; then
    echo "FATAL: bucket $B not found — it is listed for backup but does not exist" >&2
    exit 1
  fi
  influxd inspect export-lp --bucket-id "$BID" \
    --engine-path /var/lib/influxdb2/engine \
    --output-path "/backups/lp/$DATE-$B.lp.gz" --compress \
    --start "$START"
done
