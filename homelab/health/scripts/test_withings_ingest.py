#!/usr/bin/env python3
"""Unit tests for withings-ingest.py.

Stdlib `unittest` only, because this repo has no Python toolchain: no
requirements file, no virtualenv, no pytest. The script is stdlib-only so it
runs on a bare `python:3.14-alpine3.22` image with no pip, and a test suite that
needed installing would not get run.

    python3 homelab/health/scripts/test_withings_ingest.py

Seven groups, one per way this run can be wrong in a way that costs data or a
browser trip. The functions copied verbatim from cloudflare-analytics-ingest.py
are NOT re-tested here: `esc_tag`, `env` and `http_post` carry their own suites
in test_cloudflare_analytics_ingest.py, and a second copy of those assertions
proves nothing about this script. Review them by diffing the two files.

No network, no cluster, no InfluxDB: every call is stubbed by replacing the
module's `http_post`.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

# The script is named with hyphens (it is a kubectl-mounted file, not a module),
# so it cannot be imported by name.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "withings-ingest.py")
_spec = importlib.util.spec_from_file_location("withings_ingest", _PATH)
wi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wi)

# The module's own default, captured at import before any setUp has run.
DEFAULT_SUMMARY = wi.SUMMARY[0]

REAL_TOKEN = "refresh-token-that-must-never-be-printed"


class StateDir(unittest.TestCase):
    """Base class: TOKEN_FILE points at a temporary directory."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.real_token_file = wi.TOKEN_FILE
        wi.TOKEN_FILE = os.path.join(self.dir, "withings_tokens.json")
        self.real_post = wi.http_post
        self.real_log = wi.log
        self.logged = []
        wi.log = self.logged.append

    def tearDown(self):
        wi.TOKEN_FILE = self.real_token_file
        wi.http_post = self.real_post
        wi.log = self.real_log

    def seed(self, token=REAL_TOKEN):
        with open(wi.TOKEN_FILE, "w") as handle:
            json.dump({"refresh_token": token, "userid": "42"}, handle)
        with open(wi.TOKEN_FILE) as handle:
            return handle.read()

    def stub(self, status, doc):
        text = json.dumps(doc)

        def _post(url, body, headers, timeout=None):
            return status, text
        wi.http_post = _post


class TokenFile(StateDir):
    """The design, in assertions. Losing this file costs a browser trip."""

    def test_write_state_replaces_the_file_with_the_new_token(self):
        self.seed()
        wi.write_state({"refresh_token": "new-token", "userid": "42"})
        with open(wi.TOKEN_FILE) as handle:
            self.assertEqual(json.load(handle),
                             {"refresh_token": "new-token", "userid": "42"})

    def test_a_failed_replace_leaves_the_original_byte_identical(self):
        original = self.seed()
        real_replace = wi.os.replace

        def boom(src, dst):
            raise OSError("read-only file system")
        wi.os.replace = boom
        try:
            with self.assertRaises(OSError):
                wi.write_state({"refresh_token": "new-token", "userid": "42"})
        finally:
            wi.os.replace = real_replace
        with open(wi.TOKEN_FILE) as handle:
            self.assertEqual(handle.read(), original)
        # No temporary file survives beside it.
        self.assertEqual(os.listdir(self.dir), ["withings_tokens.json"])

    def test_a_withings_status_error_under_http_200_raises(self):
        # THE withings-sync BUG, written as a test: a failed call arrives as
        # HTTP 200 with a non-zero `status`, and must never reach write_state.
        original = self.seed()
        self.stub(200, {"status": 401, "error": "invalid_grant"})
        with self.assertRaises(wi.IngestFailed):
            wi.token_request("id", "secret", grant_type="refresh_token",
                             refresh_token=REAL_TOKEN)
        with open(wi.TOKEN_FILE) as handle:
            self.assertEqual(handle.read(), original)

    def test_a_body_missing_refresh_token_raises_and_writes_nothing(self):
        original = self.seed()
        self.stub(200, {"status": 0, "body": {"access_token": "a",
                                              "expires_in": 10800}})
        with self.assertRaises(wi.IngestFailed):
            wi.token_request("id", "secret", grant_type="refresh_token",
                             refresh_token=REAL_TOKEN)
        with open(wi.TOKEN_FILE) as handle:
            self.assertEqual(handle.read(), original)

    def test_a_5xx_raises_and_writes_nothing(self):
        original = self.seed()
        self.stub(503, {"status": 0, "body": {}})
        with self.assertRaises(wi.IngestFailed):
            wi.token_request("id", "secret", grant_type="refresh_token",
                             refresh_token=REAL_TOKEN)
        with open(wi.TOKEN_FILE) as handle:
            self.assertEqual(handle.read(), original)

    def test_no_error_message_quotes_the_refresh_token(self):
        self.seed()
        self.stub(200, {"status": 401})
        with self.assertRaises(wi.IngestFailed) as caught:
            wi.token_request("id", "secret", grant_type="refresh_token",
                             refresh_token=REAL_TOKEN)
        self.assertNotIn(REAL_TOKEN, str(caught.exception))
        for line in self.logged:
            self.assertNotIn(REAL_TOKEN, line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
