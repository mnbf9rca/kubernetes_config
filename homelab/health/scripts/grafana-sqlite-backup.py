#!/usr/bin/env python3
"""Take a consistent point-in-time copy of Grafana's SQLite database.

WHY THIS EXISTS
---------------
`grafana.db` was captured only by the nightly restic sweep of the local-path
tree, taken while Grafana is running. That copy is a live file read page by
page with no lock, so it can be torn, and the gate that guards it asserts size
only. A Grafana major upgrade migrates the schema in place on first start, so
reverting the image tag alone does not revert the database: without a logical
copy there is no clean rollback from a failed major.

This produces one, using SQLite's online backup API - the same mechanism as the
CLI's `.backup` - which takes a read lock, copies whole pages, and restarts
itself if a writer commits mid-copy. The result is a database as of one instant,
not a smear across the run.

NO sqlite3 BINARY, DELIBERATELY
-------------------------------
The nightly `influx-backup` CronJob runs `alpine/k8s:1.36.0`, which has no
`sqlite3` executable: upstream's Dockerfile installs only curl, ca-certificates,
bash, git, py3-pip, jq, yq and gettext. The two alternatives were a second image
in the pod, or `apk add sqlite` at run time as the VPS quiesce sidecars do. Both
were rejected: the health namespace pins every image and forbids keel, and a
nightly `apk add` makes the backup depend on a package CDN at 02:30. `py3-pip`
pulls in `python3`, Alpine builds `python3` against `sqlite-dev` with no split
subpackage, so `import sqlite3` is already present in the image the job runs -
and `Connection.backup()` is the same C API the shell's `.backup` calls.

NOTHING HERE WRITES TO THE SOURCE
---------------------------------
The source is opened `mode=ro` through a read-only volume mount. Grafana's
default is `wal = false` (`conf/defaults.ini`), so a rollback-journal database
opens read-only without needing to create a `-shm`. If Grafana is ever switched
to WAL (`GF_DATABASE_WAL=true`), this breaks loudly at open time and the mount
in `homelab/health/backups.yaml` has to become read-write - change both together
or the backup stops.

VERIFY BEFORE PUBLISHING
------------------------
A `.backup` that "succeeds" against a truncated or empty source produces a
structurally valid, current-mtime, EMPTY database, which sails straight through
a freshness gate. So the copy is opened, `PRAGMA integrity_check` must return
exactly `ok`, it must contain schema objects, and it must clear a byte floor -
and only then is it renamed into place. Publication is atomic: the copy is
written to a dotfile and `os.replace`d, so a failed run leaves last night's
artifact intact rather than truncating it.

Usage:
  grafana-sqlite-backup.py <source-db> <dest-dir> <date>

Exit status:
  0  a verified artifact was published at <dest-dir>/<date>-grafana.db
  1  nothing was published; the reason is on stderr, named
"""

import os
import sqlite3
import sys

# How long to wait for a writer to release the database before giving up.
# Matches the `.timeout 30000` the VPS quiesce sidecars use.
BUSY_TIMEOUT_SECONDS = 30

# PLACEHOLDER FLOOR - RAISE AFTER THE FIRST REAL RUN.
# 64 KiB is deliberately far below anything a real Grafana database can be (the
# live-file gate row for `grafana.db` already sits at 256 KiB). It exists to
# reject a zero-length or truncated copy, not to track growth. Once the first
# nightly dump has run, read its size from the job log and raise this to roughly
# an order of magnitude below it, the same way the gate's floors were set.
MIN_BYTES = 65536

# A database with no schema objects is not a Grafana database, whatever its
# size. `count(*) from sqlite_master` is a schema-only read.
MIN_SCHEMA_OBJECTS = 1


def fail(message):
    """Report a named reason on stderr and exit non-zero.

    Every failure path goes through here so the pod log always says which
    assertion refused to publish, rather than showing a bare traceback.
    """
    sys.stderr.write("FATAL: %s\n" % message)
    sys.exit(1)


def take_backup(source, tmp_path):
    """Copy `source` to `tmp_path` with SQLite's online backup API."""
    src = None
    dst = None
    try:
        src = sqlite3.connect(
            "file:%s?mode=ro" % source, uri=True, timeout=BUSY_TIMEOUT_SECONDS
        )
        dst = sqlite3.connect(tmp_path, timeout=BUSY_TIMEOUT_SECONDS)
        # pages=0 copies the whole database in one step, holding the read lock
        # for its duration. That is what SQLite recommends for a database this
        # size; a paged copy would release and retake the lock repeatedly and
        # restart from the top on every intervening write.
        src.backup(dst, pages=0)
    finally:
        for conn in (dst, src):
            if conn is not None:
                conn.close()


def verify(path):
    """Assert the copy opens, is intact, and carries a schema.

    Returns the number of schema objects it found.
    """
    conn = sqlite3.connect(
        "file:%s?mode=ro" % path, uri=True, timeout=BUSY_TIMEOUT_SECONDS
    )
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        # A clean database answers with exactly one row, `ok`. Anything else is
        # a list of the problems found, and is a refusal to publish.
        if rows != [("ok",)]:
            raise ValueError("integrity_check reported %d problem row(s)" % len(rows))
        objects = conn.execute("select count(*) from sqlite_master").fetchone()[0]
    finally:
        conn.close()
    return int(objects)


def run(source, dest_dir, date):
    """Produce one verified artifact. Returns its path.

    Raises on any failure, having removed the staging file - the caller turns
    that into a named FATAL. Nothing is published unless every assertion passed.
    """
    final_path = os.path.join(dest_dir, "%s-grafana.db" % date)
    tmp_path = os.path.join(dest_dir, ".tmp-%s-grafana.db" % date)

    # A staging file left by an interrupted previous run must not be appended to
    # or mistaken for a copy taken now.
    for stale in (tmp_path, tmp_path + "-journal", tmp_path + "-wal"):
        if os.path.exists(stale):
            os.remove(stale)

    try:
        take_backup(source, tmp_path)
        objects = verify(tmp_path)
        if objects < MIN_SCHEMA_OBJECTS:
            raise ValueError(
                "copy has %d schema objects (floor %d) - the source is empty or "
                "was truncated" % (objects, MIN_SCHEMA_OBJECTS)
            )
        size = os.path.getsize(tmp_path)
        if size < MIN_BYTES:
            raise ValueError(
                "copy is %d bytes (floor %d) - implausibly small for a Grafana "
                "database" % (size, MIN_BYTES)
            )
        # Atomic within the filesystem: either last night's artifact or this
        # night's is at `final_path`, never a half-written file.
        os.replace(tmp_path, final_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    sys.stdout.write(
        "note: published %s - %d bytes, %d schema objects\n"
        % (final_path, size, objects)
    )
    return final_path


def main(argv):
    if len(argv) != 4:
        fail("usage: grafana-sqlite-backup.py <source-db> <dest-dir> <date>")
    source, dest_dir, date = argv[1], argv[2], argv[3]

    # Checked explicitly rather than left to sqlite3: connecting to a missing
    # file in `mode=ro` fails with "unable to open database file", which reads
    # identically to a permissions problem and to a wrong mount path.
    if not os.path.isfile(source):
        fail("no database at %s - check the grafana-data mount" % source)
    if not os.path.isdir(dest_dir):
        fail("no dump directory at %s - check the mkdirs initContainer" % dest_dir)

    try:
        run(source, dest_dir, date)
    except sqlite3.Error as exc:
        # The class name, not the message: a sqlite3 error message can quote the
        # path it was given, and this text is read back by a human in the pod
        # log where the path is fine - but keeping the two shapes identical
        # means no future caller can start feeding it to a ping body by accident.
        fail("sqlite refused the backup (%s) - source %s" % (type(exc).__name__, source))
    except (OSError, ValueError) as exc:
        fail("%s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
