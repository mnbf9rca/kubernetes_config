#!/bin/sh
# Quiesce sidecar for a single-database sqlite app. Shared by n8n, karakeep and
# uptime-kuma; the only difference between those three was the path, which now
# arrives as $SNAPSHOT_DB from the container's env.
#
# No output suppression, an explicit busy timeout and an atomic publish. The
# original form swallowed every failure mode (`apk add ... >/dev/null` with no
# set -e, `|| true` on the sqlite3 call), so it could produce no snapshot
# forever while the pod stayed Ready. See spec §4 P1.
#
# `set -e` is DELIBERATELY ABSENT — do not "harden" it back in. If this sidecar
# exits, kubelet restarts it, the retry is immediate, and a persistent fault
# (SQLITE_BUSY past the 30s timeout, disk full, a corrupt page, an apk/CDN
# blip) becomes CrashLoopBackOff. A container that is not Running is not Ready,
# which makes the whole POD not Ready, which drops the application from its
# EndpointSlice and 502s it at cloudflared — a backup fault taking the
# application offline. That is exactly the outage the spec rejected sidecar
# readiness probes to avoid (§4 P1, §6), and exiting is strictly worse because
# the snapshotter stops running too. Instead: log loudly to stderr, back off
# 5 min, keep going. Detecting "no fresh snapshot" is not this container's job
# at all — it belongs to the restic verification gate, which reports it to
# healthchecks.io without ever touching Pod readiness. See the comment on the
# (deliberately absent) probes in each Deployment.
#
# "No source DB present" is likewise a tolerated state, not an error: on a
# fresh volume the app has not created its database yet, so there is genuinely
# nothing to snapshot. The loop logs and re-polls every 5 min. That tolerance
# is not a hole — an empty or wrongly-mounted volume is caught by the restic
# gate.
set -u

# shellcheck source=vps/workloads/scripts/sqlite-snapshot-lib.sh
# shellcheck disable=SC1091 # the library is a sibling key in the same
# ConfigMap and is only resolvable at /scripts inside the container.
. /scripts/sqlite-snapshot-lib.sh

# A missing SNAPSHOT_DB is a manifest bug, but it is still handled by logging
# and sleeping rather than exiting, for the reason above: an exit here would
# CrashLoopBackOff the sidecar and take the application out of its
# EndpointSlice. A misconfigured backup must not be able to cause an outage.
while [ -z "${SNAPSHOT_DB:-}" ]; do
  echo "FATAL: SNAPSHOT_DB is unset - this sidecar has no database to snapshot;" \
       "set it in the container's env in the Deployment" >&2
  sleep 300
done
DB=$SNAPSHOT_DB

while true; do
  if ! ensure_sqlite3; then
    sleep 300
    continue
  fi
  if [ ! -f "$DB" ]; then
    echo "no source DB at $DB yet - nothing to snapshot; retrying in 5m"
    sleep 300
    continue
  fi
  if snapshot "$DB"; then
    sleep 43200
  else
    echo "ERROR: snapshot of $DB failed; retrying in 5m" >&2
    sleep 300
  fi
done
