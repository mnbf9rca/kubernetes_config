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
module's `http_post`. The one exception is the exit-handler test, which runs
the script as a subprocess because that handler lives under `if __name__ ==
"__main__"` and no import can reach it - and that run dies on an unset
variable before it opens a socket.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
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

    def test_the_request_asks_for_every_type_of_real_measurement(self):
        # The four things the query must say, and the one it must not: no
        # `meastypes`, or a type code newer firmware invents never arrives.
        self.stub_pages([{"status": 0, "body": {"measuregrps": []}}])
        wi.fetch_measures("access", 1234)
        self.assertEqual(self.calls[0]["action"], "getmeas")
        self.assertEqual(self.calls[0]["category"], "1")
        self.assertEqual(self.calls[0]["lastupdate"], "1234")
        self.assertNotIn("meastypes", self.calls[0])

    def test_a_more_that_never_clears_raises_at_the_page_cap(self):
        wi.MAX_PAGES = 3
        # The offset advances on every page, so what stops this run is the cap
        # rather than the non-advancing-offset guard the next test covers.
        self.stub_pages([
            {"status": 0, "body": {"measuregrps": [], "more": 1, "offset": n}}
            for n in (1, 2, 3)])
        with self.assertRaises(wi.IngestFailed):
            wi.fetch_measures("access", 1000)
        self.assertEqual(len(self.calls), 3)

    def test_a_more_whose_offset_does_not_advance_raises_at_once(self):
        self.stub_pages([
            {"status": 0, "body": {"measuregrps": [{"grpid": 1}], "more": 1,
                                   "offset": 1}},
            {"status": 0, "body": {"measuregrps": [{"grpid": 1}], "more": 1,
                                   "offset": 1}},
        ])
        with self.assertRaises(wi.IngestFailed):
            wi.fetch_measures("access", 1000)
        self.assertEqual(len(self.calls), 2)


class Points(unittest.TestCase):
    def setUp(self):
        self.real_log = wi.log
        wi.log = lambda msg: None

    def tearDown(self):
        wi.log = self.real_log

    def test_line_protocol_shape_and_ordering(self):
        groups = [
            {"grpid": 7, "date": 200, "deviceid": "dev1",
             "measures": [{"type": 1, "value": 74850, "unit": -3}]},
            {"grpid": 6, "date": 100, "measures":
                [{"type": 130, "value": 1234, "unit": -2}]},
        ]
        lines = wi.points(groups)
        self.assertEqual(lines, [
            'withings_measure,person=rob,type=130,'
            'type_name=atrial_fibrillation_result,'
            'deviceid=unknown grpid="6",value=12.34 100',
            'withings_measure,person=rob,type=1,type_name=weight,unit=kg,'
            'deviceid=dev1 grpid="7",value=74.850 200',
        ])

    def test_a_known_type_carries_its_name_and_unit(self):
        # A caller reading this bucket should not need the code table.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 11, "value": 62,
                                         "unit": 0}]}])[0]
        self.assertIn(",type_name=heart_pulse,", line)
        self.assertIn(",unit=bpm,", line)

    def test_an_app_observed_unit_is_tagged_like_any_other(self):
        # The spec leaves 226 blank; the operator read kcal off the app.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 226, "value": 1700,
                                         "unit": 0}]}])[0]
        self.assertIn(",type_name=basal_metabolic_rate,", line)
        self.assertIn(",unit=kcal,", line)

    def test_a_type_the_spec_gives_no_unit_for_carries_no_unit_tag(self):
        # Twelve codes are still blank; an absent tag beats a guess.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 130, "value": 0,
                                         "unit": 0}]}])[0]
        self.assertIn(",type_name=atrial_fibrillation_result,", line)
        self.assertNotIn("unit=", line)

    def test_an_unknown_code_is_named_unknown_and_is_still_written(self):
        # Newer firmware invents codes. Dropping one would lose the reading.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 9999, "value": 5,
                                         "unit": 0}]}])[0]
        self.assertIn(",type=9999,", line)
        self.assertIn(",type_name=unknown,", line)
        self.assertNotIn("unit=", line)
        self.assertTrue(line.endswith('grpid="1",value=5 100'))

    def test_a_segmental_measure_carries_its_position(self):
        # Types 173, 174 and 175 repeat a type per body position, 0-28.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 174, "value": 1234,
                                         "unit": -3, "position": 5}]}])[0]
        self.assertIn(",position=5,", line)

    def test_a_segmental_measure_carries_its_position_name(self):
        # The numeric code alone makes a panel legend unreadable.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 175, "value": 1234,
                                         "unit": -3, "position": 10}]}])[0]
        self.assertIn(",position=10,", line)
        self.assertIn(",position_name=left_leg ", line)

    def test_position_7_is_whole_body(self):
        # The spec omits 7; the water types arrive at it and must not read as
        # `unknown` on every panel that shows them.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 168, "value": 1234,
                                         "unit": -3, "position": 7}]}])[0]
        self.assertIn(",position=7,", line)
        self.assertIn(",position_name=whole_body ", line)

    def test_a_position_outside_the_table_is_named_unknown(self):
        # Newer hardware invents positions, exactly as it invents type codes.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 175, "value": 1234,
                                         "unit": -3, "position": 99}]}])[0]
        self.assertIn(",position=99,", line)
        self.assertIn(",position_name=unknown ", line)

    def test_a_measure_without_a_position_carries_no_position_tag(self):
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 1, "value": 74850,
                                         "unit": -3, "position": None}]}])[0]
        self.assertNotIn("position=", line)
        self.assertNotIn("position_name", line)

    def test_a_tag_value_holding_a_space_is_escaped(self):
        # A raw space would end the tag set and the rest would parse as fields.
        real = dict(wi.TYPES)
        wi.TYPES[9998] = ("space name", "a b")
        try:
            line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                               "measures": [{"type": 9998, "value": 1,
                                             "unit": 0}]}])[0]
        finally:
            wi.TYPES.clear()
            wi.TYPES.update(real)
        self.assertIn(",type_name=space\\ name,", line)
        self.assertIn(",unit=a\\ b,", line)

    def test_a_group_carrying_a_device_model_tags_both_code_and_name(self):
        # The account has more than one scale; a panel needs to tell them apart
        # without anyone memorising which opaque deviceid is which.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "modelid": 6, "model": "Body Cardio",
                           "measures": [{"type": 1, "value": 74850,
                                         "unit": -3}]}])[0]
        self.assertIn(",modelid=6,", line)
        self.assertIn(",model=Body\\ Cardio ", line)

    def test_a_group_without_a_device_model_carries_neither_tag(self):
        # Older groups predate the field; an absent tag beats a placeholder.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 1, "value": 74850,
                                         "unit": -3}]}])[0]
        self.assertNotIn("modelid=", line)
        self.assertNotIn("model=", line)

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


class RunOrder(StateDir):
    """The sequencing rules from the design's data flow.

    Steps 3 and 4 may not be swapped, and step 2 may not move below step 3.
    """

    def setUp(self):
        super(RunOrder, self).setUp()
        self.order = []
        self.real_env = dict(os.environ)
        os.environ["INFLUX_TOKEN"] = "influx-token"
        os.environ["WITHINGS_CLIENT_ID"] = "client-id"
        os.environ["WITHINGS_CLIENT_SECRET"] = "client-secret"
        wi.SUMMARY[0] = DEFAULT_SUMMARY
        del wi.BODY_LINES[:]
        wi.STAGE[0] = "refresh"
        self.real_write_state = wi.write_state

    def tearDown(self):
        super(RunOrder, self).tearDown()
        wi.write_state = self.real_write_state
        os.environ.clear()
        os.environ.update(self.real_env)

    def route(self, write_state_raises=False):
        """Stub every call, recording the order they arrive in."""
        def _post(url, body, headers, timeout=None):
            if "/api/v2/query" in url:
                self.order.append("resume")
                return 200, CSV_ONE_ROW
            if url == wi.WITHINGS_TOKEN_URL:
                self.order.append("refresh")
                return 200, json.dumps({"status": 0, "body": {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "userid": "42"}})
            if url == wi.WITHINGS_MEASURE_URL:
                self.order.append("getmeas")
                return 200, json.dumps({"status": 0, "body": {
                    "measuregrps": [{"grpid": 1, "date": 100,
                                     "deviceid": "d",
                                     "measures": [{"type": 1, "value": 74850,
                                                   "unit": -3}]}]}})
            self.order.append("write")
            return 204, ""
        wi.http_post = _post

        real_write_state = wi.write_state

        def _write_state(state):
            self.order.append("write_state")
            if write_state_raises:
                raise OSError("read-only file system")
            real_write_state(state)
        wi.write_state = _write_state

    def test_resume_precedes_refresh_and_the_persist_precedes_getmeas(self):
        self.seed()
        self.route()
        self.assertEqual(wi.main(), 0)
        self.assertEqual(self.order,
                         ["resume", "refresh", "write_state", "getmeas",
                          "write"])

    def test_a_failed_persist_stops_the_run_before_any_getmeas(self):
        # The new access token must not be used: the OLD refresh token is still
        # on disk and still valid for 8 hours.
        self.seed()
        self.route(write_state_raises=True)
        self.assertEqual(wi.main(), 1)
        self.assertNotIn("getmeas", self.order)
        self.assertEqual(wi.STAGE[0], "token_persist")
        self.assertIn("failure=token_persist", wi.BODY_LINES)

    def test_a_missing_token_file_fails_before_any_network_call(self):
        self.route()
        self.assertEqual(wi.main(), 1)
        self.assertEqual(self.order, [])
        self.assertEqual(wi.STAGE[0], "refresh")

    def test_an_unparseable_token_file_fails_before_any_network_call(self):
        with open(wi.TOKEN_FILE, "w") as handle:
            handle.write("{not json")
        self.route()
        self.assertEqual(wi.main(), 1)
        self.assertEqual(self.order, [])

    def test_a_successful_run_persists_the_rotated_token(self):
        self.seed()
        self.route()
        wi.main()
        with open(wi.TOKEN_FILE) as handle:
            self.assertEqual(json.load(handle)["refresh_token"], "new-refresh")

    def test_no_output_carries_the_stored_refresh_token(self):
        # A failing refresh and a failing fetch, in turn: neither the log nor
        # the heartbeat may quote the credential on the way out.
        for failing in ("refresh", "getmeas"):
            self.seed()
            del self.logged[:]
            del wi.BODY_LINES[:]

            def _post(url, body, headers, timeout=None, failing=failing):
                if "/api/v2/query" in url:
                    return 200, CSV_ONE_ROW
                if url == wi.WITHINGS_TOKEN_URL and failing == "refresh":
                    return 200, json.dumps({"status": 401})
                if url == wi.WITHINGS_TOKEN_URL:
                    return 200, json.dumps({"status": 0, "body": {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh", "userid": "42"}})
                return 200, json.dumps({"status": 503})
            wi.http_post = _post
            with self.assertRaises(wi.IngestFailed) as caught:
                wi.main()
            self.assertNotIn(REAL_TOKEN, str(caught.exception))
            for line in self.logged + wi.BODY_LINES:
                self.assertNotIn(REAL_TOKEN, line)


class Heartbeat(unittest.TestCase):
    """Format rules from the design's monitoring section, enforced not trusted."""

    def setUp(self):
        self.calls = []
        self._real_urlopen = wi.urllib.request.urlopen
        wi.urllib.request.urlopen = self._fake
        self.logged = []
        self._real_log = wi.log
        wi.log = self.logged.append
        wi.SUMMARY[0] = "verdict=failed"
        del wi.BODY_LINES[:]

    def tearDown(self):
        wi.urllib.request.urlopen = self._real_urlopen
        wi.log = self._real_log
        wi.SUMMARY[0] = "verdict=failed"
        del wi.BODY_LINES[:]

    def _fake(self, request, data=None, timeout=None):
        self.calls.append((request.full_url, request))

        class _R:
            def close(self_inner):
                pass
        return _R()

    def test_verdict_is_always_first(self):
        wi.hc_emit("groups=3")
        wi.hc_summary("ok")
        self.assertEqual(wi.hc_body().splitlines()[0], "verdict=ok")
        self.assertTrue(wi.kuma_msg().startswith("verdict=ok "))

    def test_the_default_verdict_is_a_failure(self):
        self.assertEqual(DEFAULT_SUMMARY, "verdict=failed")
        self.assertIn(DEFAULT_SUMMARY.split("=", 1)[1], wi.VERDICTS)

    def test_a_verdict_off_the_enum_is_coerced_to_failed(self):
        wi.hc_summary("ok - 3 groups")
        self.assertEqual(wi.SUMMARY[0], "verdict=failed")
        self.assertTrue(any("VERDICTS" in line for line in self.logged))

    def test_every_failure_token_is_a_member_of_stages(self):
        for stage in wi.STAGES:
            del wi.BODY_LINES[:]
            wi.hc_emit("failure=%s" % stage)
            token = wi.BODY_LINES[0].split("=", 1)[1]
            self.assertIn(token, wi.STAGES)

    def test_the_message_is_one_line_printable_and_cut_on_a_boundary(self):
        wi.hc_summary("ok")
        for _ in range(60):
            wi.hc_emit("groups=1234567890")
        msg = wi.kuma_msg()
        self.assertLessEqual(len(msg), wi.MSG_LIMIT)
        self.assertNotIn("\n", msg)
        for token in msg.split(" "):
            self.assertRegex(token, r"^[a-z0-9_]+=\S*$", token)

    def test_status_and_message_ride_the_query_string(self):
        wi.make_pusher("https://uptime.example/api/push/tok")(
            "up", "verdict=ok groups=2 points=9")
        url, request = self.calls[0]
        self.assertIsNone(request.data, "the push is a GET, not a POST")
        self.assertTrue(url.startswith("https://uptime.example/api/push/tok?"))

    def test_the_user_agent_is_never_urllibs_default(self):
        wi.make_pusher("https://uptime.example/api/push/tok")("up", "verdict=ok")
        agent = self.calls[0][1].get_header("User-agent")
        self.assertTrue(agent)
        self.assertNotIn("urllib", agent.lower())
        self.assertEqual(agent, wi.PUSH_USER_AGENT)

    def test_a_push_failure_is_swallowed_and_names_only_the_class(self):
        def boom(request, data=None, timeout=None):
            raise wi.IngestFailed("withings said <secret payload>")
        wi.urllib.request.urlopen = boom
        wi.make_pusher("https://uptime.example/api/push/s3cr3ttoken")("down")
        self.assertTrue(self.logged)
        for line in self.logged:
            self.assertNotIn("secret payload", line)
            self.assertNotIn("s3cr3ttoken", line)
            self.assertNotIn("uptime.example", line)
            self.assertIn("IngestFailed", line)

    def test_an_unset_variable_is_named_in_the_log_on_the_way_out(self):
        # The module-level exit handler is only reachable by RUNNING the script:
        # it sits under `if __name__ == "__main__"` and no import executes it.
        # env() raises SystemExit carrying the NAME of the unset variable, and
        # dropping that name left an alert reading `failure=refresh`, whose
        # documented remedy is a browser re-authorization - the wrong repair for
        # a Secret key that was never wired. This run dies inside env() before
        # it opens a socket.
        environ = dict(os.environ)
        for name in ("INFLUX_TOKEN", "WITHINGS_CLIENT_ID",
                     "WITHINGS_CLIENT_SECRET", "PUSH_URL"):
            environ.pop(name, None)
        done = subprocess.run([sys.executable, _PATH], env=environ,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("INFLUX_TOKEN", done.stdout)
        self.assertIn("verdict=failed", done.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
