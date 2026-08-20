#!/bin/sh
# Quiesce sidecar for FreshRSS. Separate from sqlite-snapshot.sh because
# FreshRSS keeps ONE DATABASE PER USER, so this iterates a glob rather than
# naming one file, and its retry accounting is per-user.
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
# which makes the whole POD not Ready, which drops FreshRSS from its
# EndpointSlice and 502s it at cloudflared — a backup fault taking the
# application offline. That is exactly the outage the spec rejected sidecar
# readiness probes to avoid (§4 P1, §6), and exiting is strictly worse because
# the snapshotter stops running too. Instead: log loudly to stderr, back off
# 5 min, keep going. Detecting "no fresh snapshot" is not this container's job
# at all — it belongs to the restic verification gate, which reports it to
# healthchecks.io without ever touching Pod readiness. See the comment on the
# (deliberately absent) probes in the Deployment.
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

GLOB=/var/www/FreshRSS/data/users

while true; do
  if ! ensure_sqlite3; then
    sleep 300
    continue
  fi
  n=0
  failed=0
  # One user failing must NOT stop the others being snapshotted: this loop logs
  # and continues. The skipped user is caught by the restic gate, which must
  # assert a snapshot PER USER — a newest-of-glob check would be fooled here,
  # since alice's snapshot keeps being refreshed while bob has none at all.
  for db in "$GLOB"/*/db.sqlite; do
    [ -f "$db" ] || continue
    if snapshot "$db"; then
      n=$((n + 1))
    else
      echo "ERROR: snapshot of $db failed - continuing with remaining users" >&2
      failed=$((failed + 1))
    fi
  done
  if [ "$failed" -ne 0 ]; then
    echo "ERROR: $failed of $((n + failed)) FreshRSS user DB snapshot(s) failed; retrying in 5m" >&2
    sleep 300
  elif [ "$n" -eq 0 ]; then
    echo "no FreshRSS user DBs under $GLOB yet - nothing to snapshot; retrying in 5m"
    sleep 300
  else
    sleep 43200
  fi
done
