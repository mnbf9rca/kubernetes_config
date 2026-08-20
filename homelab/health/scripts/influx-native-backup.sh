#!/bin/sh
# Runs INSIDE the influxdb pod, shipped there as text by influx-backup.sh:
#
#   kubectl -n health exec "$POD" -- sh -c "$(cat /scripts/influx-native-backup.sh)" \
#     influx-native-backup "$DATE"
#
# `sh -c CMD name arg1` sets $0=name and $1=arg1, so the date arrives as $1.
#
# $DOCKER_INFLUXDB_INIT_ADMIN_TOKEN is the influxdb container's OWN env var. It
# must reach this script unexpanded, which it does: command substitution output
# is not re-expanded by the shell that produced it, so the outer pod never sees
# the token — only the inner shell resolves it.
#
# /backups is the `health-dumps` PVC as mounted in THIS pod. The CronJob pod
# mounts the same PVC at /dumps and prunes there.
set -eu

DATE=$1

influx backup "/backups/native/$DATE" \
  -t "$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN" \
  --host http://localhost:8086
