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

# Publish a dump only if it actually contains a schema — the same assertion the
# sqlite sidecars make with `select count(*) from sqlite_master`, and for the
# same reason.
#
# pg_dumpall EXITS 0 against a freshly-initialised postgres that has no umami
# schema. Recreate the PVC, or let PGDATA re-initialise for any reason, and the
# image's entrypoint creates an empty `umami` database; pg_dumpall then writes a
# valid, roughly 100-line, roles-and-databases-only file, `mv` puts it at
# dump.sql.restic with a current mtime, and it sails through the restic gate,
# whose freshness test is mtime and nothing else. The backup would look healthy
# and restore to an empty analytics database.
#
# `CREATE TABLE` is the discriminator: a roles-only dump has none, and umami's
# Prisma-managed schema has many. Checking for the `umami` database instead
# would not work — the entrypoint creates that database whether or not anything
# is in it.
MIN_TABLES=1

dump_has_schema() {
  # $1 = path to the candidate dump. 0 = safe to publish, 1 = do not publish.
  _tables=$(grep -c '^CREATE TABLE ' "$1")
  _grc=$?
  # grep -c exits 1 for "no match" (having printed 0) and greater than 1 for a
  # real error. Only those two verdicts are expected; anything else means the
  # file could not be read, which must never be reported as a good dump.
  if [ "$_grc" -gt 1 ]; then
    echo "ERROR: cannot read $1 to verify it (grep exited $_grc)" >&2
    return 1
  fi
  if [ "$_tables" -lt "$MIN_TABLES" ]; then
    echo "ERROR: dump has $_tables CREATE TABLE statements (need at least" \
         "$MIN_TABLES) - this looks like a roles-only dump of an empty" \
         "postgres, refusing to publish" >&2
    return 1
  fi
  return 0
}

while true; do
  if pg_dumpall -U umami -h 127.0.0.1 > "$DUMP.restic.tmp"; then
    if dump_has_schema "$DUMP.restic.tmp"; then
      mv "$DUMP.restic.tmp" "$DUMP.restic"
      sleep 43200
    else
      # The previous dump.sql.restic is left untouched, so it keeps ageing and
      # the restic gate turns the healthchecks.io check red once it passes 15h.
      # That is the intended path: a human must look at an empty database, and
      # nothing this container can do would repair it.
      echo "ERROR: dump rejected by the content check; retrying in 5m" >&2
      rm -f "$DUMP.restic.tmp"
      sleep 300
    fi
  else
    echo "ERROR: pg_dumpall failed; retrying in 5m" >&2
    rm -f "$DUMP.restic.tmp"
    sleep 300
  fi
done
