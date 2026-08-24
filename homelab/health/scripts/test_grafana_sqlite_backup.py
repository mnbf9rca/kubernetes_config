#!/usr/bin/env python3
"""Unit tests for grafana-sqlite-backup.py.

Stdlib `unittest` only, for the same reason as
`test_cloudflare_analytics_ingest.py`: this repo has no Python toolchain, no
virtualenv and no pytest, so a suite that needed installing would not get run.
`make check-script-lint` executes every `test_*.py` beside a script.

    python3 homelab/health/scripts/test_grafana_sqlite_backup.py

What these lock down is the failure the script exists to prevent: publishing an
artifact that LOOKS fine to the nightly gate. The gate checks existence,
freshness and size, and a `.backup` of an empty or truncated source produces a
structurally valid database with a current mtime that clears all three. So the
tests assert that the verification refuses those, that a refusal leaves the
PREVIOUS night's artifact intact rather than truncating it, and that no staging
file is left behind to be picked up as a copy taken later.
"""
import importlib.util
import os
import sqlite3
import tempfile
import unittest

# The script is a kubectl-mounted file named with hyphens, so it cannot be
# imported by name.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "grafana-sqlite-backup.py")
_spec = importlib.util.spec_from_file_location("grafana_sqlite_backup", _PATH)
gb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gb)


def make_db(path, rows=0):
    """A database with the shape of Grafana's: a table, optionally populated."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("create table dashboard (id integer primary key, data blob)")
        for i in range(rows):
            conn.execute("insert into dashboard (id, data) values (?, ?)", (i, b"x" * 512))
        conn.commit()
    finally:
        conn.close()


class GrafanaBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.src = os.path.join(self.dir, "grafana.db")
        self.dest = os.path.join(self.dir, "dumps")
        os.mkdir(self.dest)
        # Floors are asserted explicitly in their own tests; the rest of the
        # suite should not have to build a 64 KiB fixture to exercise anything
        # else.
        self._min_bytes = gb.MIN_BYTES
        gb.MIN_BYTES = 1024

    def tearDown(self):
        gb.MIN_BYTES = self._min_bytes
        self.tmp.cleanup()

    def artifacts(self):
        return sorted(os.listdir(self.dest))

    def test_publishes_a_queryable_copy(self):
        make_db(self.src, rows=100)
        path = gb.run(self.src, self.dest, "2026-08-24")
        self.assertEqual(self.artifacts(), ["2026-08-24-grafana.db"])
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(
                conn.execute("select count(*) from dashboard").fetchone()[0], 100
            )
        finally:
            conn.close()

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def test_source_is_not_modified(self):
        make_db(self.src, rows=100)
        before = self.read(self.src)
        gb.run(self.src, self.dest, "2026-08-24")
        self.assertEqual(self.read(self.src), before)

    def test_empty_source_is_refused(self):
        # A zero-byte file is a valid, empty SQLite database: `.backup` succeeds
        # on it and yields an artifact with a current mtime. This is the exact
        # shape the freshness gate cannot tell from a good one.
        open(self.src, "wb").close()
        with self.assertRaises(ValueError) as ctx:
            gb.run(self.src, self.dest, "2026-08-24")
        self.assertIn("schema objects", str(ctx.exception))
        self.assertEqual(self.artifacts(), [])

    def test_size_floor_is_enforced(self):
        make_db(self.src, rows=1)
        gb.MIN_BYTES = 10 * 1024 * 1024
        with self.assertRaises(ValueError) as ctx:
            gb.run(self.src, self.dest, "2026-08-24")
        self.assertIn("floor", str(ctx.exception))
        self.assertEqual(self.artifacts(), [])

    def test_failed_run_leaves_the_previous_artifact_intact(self):
        make_db(self.src, rows=100)
        gb.run(self.src, self.dest, "2026-08-23")
        good = os.path.join(self.dest, "2026-08-23-grafana.db")
        good_bytes = self.read(good)

        gb.MIN_BYTES = 10 * 1024 * 1024
        with self.assertRaises(ValueError):
            gb.run(self.src, self.dest, "2026-08-24")

        # Yesterday's artifact untouched, and no staging file left behind for a
        # later run - or a later reader - to mistake for a real copy.
        self.assertEqual(self.artifacts(), ["2026-08-23-grafana.db"])
        self.assertEqual(self.read(good), good_bytes)

    def test_stale_staging_file_is_replaced_not_appended(self):
        make_db(self.src, rows=100)
        tmp_path = os.path.join(self.dest, ".tmp-2026-08-24-grafana.db")
        with open(tmp_path, "wb") as fh:
            fh.write(b"not a database")
        gb.run(self.src, self.dest, "2026-08-24")
        self.assertEqual(self.artifacts(), ["2026-08-24-grafana.db"])

    def test_missing_source_exits_one(self):
        with self.assertRaises(SystemExit) as ctx:
            gb.main(["grafana-sqlite-backup.py", self.src, self.dest, "2026-08-24"])
        self.assertEqual(ctx.exception.code, 1)

    def test_missing_dest_dir_exits_one(self):
        make_db(self.src, rows=1)
        with self.assertRaises(SystemExit) as ctx:
            gb.main(
                [
                    "grafana-sqlite-backup.py",
                    self.src,
                    os.path.join(self.dir, "nope"),
                    "2026-08-24",
                ]
            )
        self.assertEqual(ctx.exception.code, 1)

    def test_wrong_argument_count_exits_one(self):
        with self.assertRaises(SystemExit) as ctx:
            gb.main(["grafana-sqlite-backup.py", self.src])
        self.assertEqual(ctx.exception.code, 1)

    def test_corrupt_source_is_refused(self):
        # Header intact, pages garbage: opens, then fails integrity_check.
        make_db(self.src, rows=200)
        with open(self.src, "r+b") as fh:
            fh.seek(4096)
            fh.write(b"\xff" * 4096)
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            gb.run(self.src, self.dest, "2026-08-24")
        self.assertEqual(self.artifacts(), [])


if __name__ == "__main__":
    unittest.main()
