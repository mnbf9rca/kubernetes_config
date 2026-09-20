#!/usr/bin/env python3
"""Tests for github-mirror.py. Stdlib unittest, run by make check-script-lint.

Only behaviour that can go wrong at runtime is tested: forced-update and
deletion classification against real git repositories, and the enumeration
fail-safe paths (empty list, API error) that must touch nothing on disk.
"""
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
# The script has a hyphen in its name, so it cannot be imported by name.
_spec = importlib.util.spec_from_file_location("github_mirror", HERE / "github-mirror.py")
gm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gm)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], env=GIT_ENV,
                          check=True, capture_output=True, text=True)


def make_upstream(root):
    up = root / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    (up / "a").write_text("1\n")
    git(up, "add", "a")
    git(up, "commit", "-q", "-m", "one")
    return up


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.up = make_upstream(self.root)
        self.dest = self.root / "mirror"
        self.assertEqual(gm.mirror(str(self.up), self.dest, GIT_ENV).returncode, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fast_forward_is_not_forced(self):
        before = gm.refs(self.dest)
        (self.up / "a").write_text("2\n")
        git(self.up, "commit", "-q", "-am", "two")
        gm.mirror(str(self.up), self.dest, GIT_ENV)
        after = gm.refs(self.dest)
        self.assertEqual(gm.classify(before, after, "refs/heads/main", self.dest), (0, 0))

    def test_force_push_is_forced(self):
        before = gm.refs(self.dest)
        git(self.up, "commit", "-q", "--amend", "-m", "rewritten")
        gm.mirror(str(self.up), self.dest, GIT_ENV)
        after = gm.refs(self.dest)
        self.assertEqual(gm.classify(before, after, "refs/heads/main", self.dest), (1, 0))

    def test_deleted_default_branch_is_deleted(self):
        git(self.up, "checkout", "-q", "-b", "other")
        git(self.up, "branch", "-D", "main")
        before = gm.refs(self.dest)
        gm.mirror(str(self.up), self.dest, GIT_ENV)
        after = gm.refs(self.dest)
        self.assertEqual(gm.classify(before, after, "refs/heads/main", self.dest), (0, 1))

    def test_other_branch_force_push_is_ignored(self):
        git(self.up, "checkout", "-q", "-b", "feature")
        (self.up / "a").write_text("f\n")
        git(self.up, "commit", "-q", "-am", "feature")
        gm.mirror(str(self.up), self.dest, GIT_ENV)
        before = gm.refs(self.dest)
        git(self.up, "commit", "-q", "--amend", "-m", "feature rewritten")
        gm.mirror(str(self.up), self.dest, GIT_ENV)
        after = gm.refs(self.dest)
        self.assertEqual(gm.classify(before, after, "refs/heads/main", self.dest), (0, 0))


class TestEnumerationFailSafe(unittest.TestCase):
    def setUp(self):
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        tok = Path(self.tmp.name) / "token"
        tok.write_text("github_pat_not_real\n")
        self.env = {"MIRROR_DATA": str(self.data), "GITHUB_TOKEN_FILE": str(tok),
                    "GITHUB_LOGIN": "someone", "HOME": self.tmp.name}
        self.saved = dict(os.environ)
        os.environ.update(self.env)
        self.saved_api = gm.api_get_all
        self.saved_export = gm.run_export
        gm.run_export = lambda *a, **k: 0

    def tearDown(self):
        gm.api_get_all = self.saved_api
        gm.run_export = self.saved_export
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def _status(self):
        return (self.data / ".status").read_text().strip()

    def test_empty_repo_list_touches_nothing(self):
        gm.api_get_all = lambda path, token=None: []
        self.assertEqual(gm.main(), 0)
        self.assertTrue(self._status().startswith("verdict=enumerate-failed "))
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), [".status"])

    def test_api_error_touches_nothing(self):
        def boom(path, token=None):
            raise gm.ApiError("HTTP 401")
        gm.api_get_all = boom
        self.assertEqual(gm.main(), 0)
        self.assertTrue(self._status().startswith("verdict=enumerate-failed "))
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), [".status"])

    def test_mirror_exception_writes_failed_status(self):
        gm.api_get_all = lambda path, token=None: (
            [{"name": "repo", "clone_url": "file:///unused", "default_branch": "main"}]
            if path.startswith("/user/repos") else []
        )
        with patch.object(gm, "mirror", side_effect=RuntimeError("mirror failed")):
            self.assertEqual(gm.main(), 0)
        self.assertTrue(self._status().startswith("verdict=mirror-failed "))


if __name__ == "__main__":
    unittest.main()
