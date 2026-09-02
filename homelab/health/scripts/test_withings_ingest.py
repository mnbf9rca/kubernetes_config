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
from urllib.parse import parse_qsl as urllib_parse_qsl

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


CSV_ONE_ROW = (
    "#group,false,false,false\r\n"
    "#datatype,string,long,dateTime:RFC3339\r\n"
    "#default,_result,,\r\n"
    ",result,table,_time\r\n"
    ",_result,0,2026-08-30T06:14:00Z\r\n"
)

CSV_EMPTY = (
    "#group,false,false,false\r\n"
    "#datatype,string,long,dateTime:RFC3339\r\n"
    ",result,table,_time\r\n"
)

CFG = {"url": "http://influx.example", "org": "cynexia",
       "bucket": "withings", "token": "influx-token"}


class ResumePoint(unittest.TestCase):
    """A FAILED QUERY IS NOT AN EMPTY ONE.

    Reading a broken query as an empty bucket would re-backfill the whole
    account on every run.
    """

    def setUp(self):
        self.real_post = wi.http_post

    def tearDown(self):
        wi.http_post = self.real_post

    def stub_text(self, status, text):
        wi.http_post = lambda url, body, headers, timeout=None: (status, text)

    def test_annotated_csv_yields_the_newest_time(self):
        self.stub_text(200, CSV_ONE_ROW)
        self.assertEqual(
            wi.resume_point(CFG),
            wi.datetime(2026, 8, 30, 6, 14, tzinfo=wi.timezone.utc))

    def test_an_empty_result_is_none(self):
        self.stub_text(200, CSV_EMPTY)
        self.assertIsNone(wi.resume_point(CFG))

    def test_none_seeds_from_first_run_start(self):
        now = wi.datetime(2026, 9, 2, 12, 0, tzinfo=wi.timezone.utc)
        self.assertEqual(wi.window_start(None, now).strftime("%Y-%m-%d"),
                         wi.FIRST_RUN_START)

    def test_a_non_2xx_raises_rather_than_reading_as_empty(self):
        self.stub_text(503, "service unavailable")
        with self.assertRaises(wi.IngestFailed):
            wi.resume_point(CFG)

    def test_a_2xx_influxdb_error_object_raises(self):
        self.stub_text(200, '{"code":"invalid","message":"schema collision"}')
        with self.assertRaises(wi.IngestFailed):
            wi.resume_point(CFG)

    def test_an_unparseable_time_raises(self):
        self.stub_text(200, ",result,table,_time\r\n,_result,0,not-a-time\r\n")
        with self.assertRaises(wi.IngestFailed):
            wi.resume_point(CFG)

    def test_a_future_resume_point_is_clamped_to_now(self):
        # A scale with a wrong clock can date a point in the future, which would
        # otherwise push lastupdate past the present and skip everything
        # modified in between.
        now = wi.datetime(2026, 9, 2, 12, 0, tzinfo=wi.timezone.utc)
        future = wi.datetime(2030, 1, 1, tzinfo=wi.timezone.utc)
        self.assertEqual(wi.window_start(future, now),
                         now - wi.timedelta(seconds=wi.OVERLAP_SECONDS))

    def test_a_past_resume_point_is_rewound_by_the_overlap(self):
        now = wi.datetime(2026, 9, 2, 12, 0, tzinfo=wi.timezone.utc)
        newest = wi.datetime(2026, 9, 1, 8, 0, tzinfo=wi.timezone.utc)
        self.assertEqual(wi.window_start(newest, now),
                         newest - wi.timedelta(seconds=wi.OVERLAP_SECONDS))


class Scaling(unittest.TestCase):
    """74850 at unit -3 is 74.850 kg, and rendering it as 74850 is the classic
    Withings mistake."""

    def test_weight_scales_to_kilograms(self):
        self.assertEqual(wi.scaled(74850, -3), "74.850")

    def test_zero_at_unit_zero(self):
        self.assertEqual(wi.scaled(0, 0), "0")

    def test_a_positive_unit_is_fixed_point_not_scientific(self):
        rendered = wi.scaled(12, 6)
        self.assertEqual(rendered, "12000000")
        self.assertNotIn("E", rendered.upper())

    def test_a_negative_value_keeps_its_sign(self):
        self.assertEqual(wi.scaled(-1250, -2), "-12.50")


class Fetch(unittest.TestCase):
    def setUp(self):
        self.real_post = wi.http_post
        self.real_max = wi.MAX_PAGES
        self.real_log = wi.log
        wi.log = lambda msg: None
        self.calls = []

    def tearDown(self):
        wi.http_post = self.real_post
        wi.MAX_PAGES = self.real_max
        wi.log = self.real_log

    def stub_pages(self, pages):
        def _post(url, body, headers, timeout=None):
            self.calls.append(dict(urllib_parse_qsl(body.decode())))
            return 200, json.dumps(pages[len(self.calls) - 1])
        wi.http_post = _post

    def test_more_triggers_a_second_call_and_groups_concatenate_in_order(self):
        self.stub_pages([
            {"status": 0, "body": {"measuregrps": [{"grpid": 1}], "more": 1,
                                   "offset": 1}},
            {"status": 0, "body": {"measuregrps": [{"grpid": 2}], "more": 0}},
        ])
        groups = wi.fetch_measures("access", 1000)
        self.assertEqual([g["grpid"] for g in groups], [1, 2])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]["offset"], "1")

    def test_no_more_key_stops_after_one_call(self):
        self.stub_pages([{"status": 0, "body": {"measuregrps": [{"grpid": 1}]}}])
        self.assertEqual(len(wi.fetch_measures("access", 1000)), 1)
        self.assertEqual(len(self.calls), 1)

    def test_a_more_that_never_clears_raises_at_the_page_cap(self):
        wi.MAX_PAGES = 3
        page = {"status": 0, "body": {"measuregrps": [], "more": 1, "offset": 5}}
        wi.http_post = lambda url, body, headers, timeout=None: (
            200, json.dumps(page))
        with self.assertRaises(wi.IngestFailed):
            wi.fetch_measures("access", 1000)


class Points(unittest.TestCase):
    def test_line_protocol_shape_and_ordering(self):
        groups = [
            {"grpid": 7, "date": 200, "deviceid": "dev1",
             "measures": [{"type": 1, "value": 74850, "unit": -3}]},
            {"grpid": 6, "date": 100, "measures":
                [{"type": 170, "value": 1234, "unit": -2}]},
        ]
        lines = wi.points(groups)
        self.assertEqual(lines, [
            'withings_measure,person=rob,type=170,deviceid=unknown '
            'grpid="6",value=12.34 100',
            'withings_measure,person=rob,type=1,deviceid=dev1 '
            'grpid="7",value=74.850 200',
        ])

    def test_a_measure_missing_a_field_raises_rather_than_writing_junk(self):
        with self.assertRaises(wi.IngestFailed):
            wi.points([{"grpid": 1, "date": 1, "measures": [{"type": 1}]}])

    def test_a_group_missing_its_date_raises_rather_than_landing_at_epoch(self):
        # A group with no `date` is the same class of fault as a measure with no
        # `value`, and gets the same answer. Dropping it at epoch 0 would put a
        # 1970 outlier on every panel that no later run corrects.
        with self.assertRaises(wi.IngestFailed):
            wi.points([{"grpid": 1,
                        "measures": [{"type": 1, "value": 1, "unit": 0}]}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
