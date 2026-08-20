#!/bin/sh
# Quiesce sidecar for umami's postgres. Same shape as the sqlite sidecars, with
# pg_dumpall in place of `.backup`.
#
# Retries after 5 min on failure instead of sleeping 12h past it. The old form
# slept 43200 unconditionally, including on failure. That matters now that the
# restic job has a staleness gate: with `strategy: Recreate` this container
# starts concurrently with the postmaster, so the first dump after every apply
# or keel update races startup and usually fails — the existing dump.sql.restic
# would then age past the 15h threshold and turn the backup job red for
# something that is not a backup fault.
#
# `set -e` is DELIBERATELY ABSENT here for the same reason as the sqlite
# sidecars: exiting on a failed dump would CrashLoopBackOff this container,
# making the whole Pod not Ready and taking the postgres Service's only
# endpoint down — i.e. a failed backup would take umami itself offline.
set -u

DUMP=/var/lib/postgresql/data/dump.sql

while true; do
  if pg_dumpall -U umami -h 127.0.0.1 > "$DUMP.restic.tmp"; then
    mv "$DUMP.restic.tmp" "$DUMP.restic"
    sleep 43200
  else
    echo "ERROR: pg_dumpall failed; retrying in 5m" >&2
    rm -f "$DUMP.restic.tmp"
    sleep 300
  fi
done
