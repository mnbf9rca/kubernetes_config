#!/usr/bin/env python3
"""Unit tests for withings-ingest.py.

Stdlib `unittest` only, because this repo has no Python toolchain: no
requirements file, no virtualenv, no pytest. The script is stdlib-only so it
runs on a bare `python:3.14-alpine3.22` image with no pip, and a test suite that
needed installing would not get run.

    python3 homelab/health/scripts/test_withings_ingest.py

Each group covers a way this run can be wrong in a way that costs data or a
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
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
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


class FlagQueries(unittest.TestCase):
    def setUp(self):
        self.real_post = wi.http_post
        self.calls = []

    def tearDown(self):
        wi.http_post = self.real_post

    def stub(self, status, body):
        def post(url, data, headers, timeout=None):
            self.calls.append((url, data.decode(), headers))
            return status, body
        wi.http_post = post

    def test_canonical_grpid_accepts_only_positive_uint64(self):
        self.assertEqual(wi.canonical_grpid("1"), "1")
        self.assertEqual(wi.canonical_grpid(18446744073709551615),
                         "18446744073709551615")
        for bad in (0, "0", "01", "-1", " 1", "1 ",
                    "18446744073709551616", True, None):
            with self.subTest(bad=bad), self.assertRaises(wi.IngestFailed):
                wi.canonical_grpid(bad)
        self.assertEqual(self.calls, [])

    def test_marker_time_preserves_all_nine_fractional_digits(self):
        self.assertEqual(wi.parse_influx_ns("2026-10-06T12:00:00.123456789Z"),
                         1791288000123456789)
        with self.assertRaises(wi.IngestFailed):
            wi.parse_influx_ns("not-a-time")

    def test_pending_flags_reads_repeated_tables_and_exact_markers(self):
        self.stub(200, (
            "#group,false,false,false,false,false\r\n"
            "#datatype,string,long,dateTime:RFC3339,string,string\r\n"
            ",result,table,_time,grpid,_value\r\n"
            ",_result,0,2026-10-06T12:00:00.123456789Z,12,pending\r\n"
            "#group,false,false,false,false,false\r\n"
            "#datatype,string,long,dateTime:RFC3339,string,string\r\n"
            ",result,table,_time,grpid,_value\r\n"
            ",_result,1,2026-10-06T12:00:01.000000001Z,12,pending\r\n"
        ))
        self.assertEqual(wi.pending_flags(CFG),
                         {"12": [1791288000123456789, 1791288001000000001]})
        self.assertIn('r._field == "status"', self.calls[0][1])
        self.assertIn('r._value == "pending"', self.calls[0][1])
        self.assertIn('from(bucket:"withings")', self.calls[0][1])

    def test_empty_annotated_csv_is_an_empty_queue(self):
        self.stub(200, ",result,table,_time,grpid,_value\r\n")
        self.assertEqual(wi.pending_flags(CFG), {})
        # InfluxDB may return no table at all for a valid empty query.
        self.stub(200, "\r\n")
        self.assertEqual(wi.pending_flags(CFG), {})

    def test_flag_query_errors_are_not_empty(self):
        for status, body in ((200, '{"code":"invalid"}'),
                             (200, 'broken csv'),
                             (200, ",result,table,_time,grpid\r\n"),
                             (200, ",result,table,_time,grpid,_value\r\n"
                                   ",_result,0,2026-10-06T12:00:00Z,12\r\n"),
                             (503, "unavailable")):
            with self.subTest(status=status, body=body):
                self.stub(status, body)
                with self.assertRaises(wi.IngestFailed):
                    wi.pending_flags(CFG)

    def test_local_group_times_include_future_points_without_body_values(self):
        self.stub(200, (
            ",result,table,_time,grpid\r\n"
            ",_result,0,2026-10-06T12:00:00Z,12\r\n"
            ",_result,0,2030-10-06T12:00:00Z,12\r\n"
            ",_result,0,2030-10-06T12:00:00Z,12\r\n"
        ))
        self.assertEqual(wi.local_group_times(CFG, "12"),
                         [1791288000, 1917518400])
        flux = self.calls[0][1]
        self.assertIn('stop: 2262-04-11T23:47:16Z', flux)
        self.assertIn('r.grpid == "12"', flux)
        self.assertIn('keep(columns: ["grpid", "_time"])', flux)
        self.assertNotIn("_value", flux)
        with self.assertRaises(wi.IngestFailed):
            wi.local_group_times(CFG, "01")
        self.assertEqual(len(self.calls), 1)


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

    def test_the_query_is_scoped_to_the_new_measurement(self):
        # The old measurement stays queryable in this bucket until it is
        # deleted, and the resume point must not see it: scoping the filter is
        # what makes the first run after the rename find an empty result and
        # page the account from FIRST_RUN_START.
        seen = {}

        def _post(url, body, headers, timeout=None):
            seen["flux"] = body.decode()
            return 200, CSV_EMPTY

        wi.http_post = _post
        self.assertIsNone(wi.resume_point(CFG))
        self.assertIn('r._measurement == "withings_measure_group"',
                      seen["flux"])

    def test_the_query_no_longer_filters_on_a_field(self):
        # There is no string field left, so nothing has to be filtered out
        # before the group() - and a `_field == "value"` filter would now match
        # nothing at all and read as an empty bucket on every run.
        seen = {}

        def _post(url, body, headers, timeout=None):
            seen["flux"] = body.decode()
            return 200, CSV_EMPTY

        wi.http_post = _post
        wi.resume_point(CFG)
        self.assertNotIn("_field", seen["flux"])

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

    def test_rate_limit_and_malformed_offset_are_fatal(self):
        wi.http_post = lambda url, body, headers, timeout=None: (429, "rate limited")
        with self.assertRaises(wi.IngestFailed):
            wi.fetch_measures("access", 1000)
        self.stub_pages([{"status": 0, "body": {"measuregrps": [],
                                               "more": 1, "offset": "bad"}}])
        with self.assertRaises(wi.IngestFailed):
            wi.fetch_measures("access", 1000)


class FieldName(unittest.TestCase):
    """The field key is built from the group's own shape, never from a
    hard-coded set of codes.

    The rule, from the design:

        name(code)      = TYPES[code][0]        if known, else "type_<code>"
        suffix(pos)     = POSITIONS[pos]        if known, else "position_<pos>"
                          "position_none"       if the measure carries none
        segmental(code) = name(code) ends in "_segments"
        repeated(code)  = this group holds more than one measure with the code
        field           = name(code)                                if neither
                        = name minus "_segments" + "_" + suffix(pos) if either
    """

    def test_a_whole_body_code_is_its_bare_name(self):
        self.assertEqual(wi.field_name(1, None, False), "weight")

    def test_a_whole_body_codes_position_is_discarded(self):
        # The water types arrive at position 7 (whole_body). That is an
        # electrode path, not an anatomy, and it must not reach the key.
        self.assertEqual(wi.field_name(168, 7, False), "extracellular_water")

    def test_a_segmental_code_loses_the_suffix_and_gains_the_position(self):
        self.assertEqual(wi.field_name(175, 10, True), "muscle_mass_left_leg")
        self.assertEqual(wi.field_name(174, 2, True), "fat_mass_right_arm")
        self.assertEqual(wi.field_name(173, 12, True), "fat_free_mass_torso")

    def test_a_lone_segmental_measure_still_takes_a_position(self):
        # A partial reading - one limb, a failed contact - must not write a
        # bare `fat_free_mass_segments`: a key in neither vocabulary, with its
        # position discarded and no duplicate to stop the run, so successive
        # partial readings from different limbs would pile into one column.
        self.assertEqual(wi.field_name(173, 3, False), "fat_free_mass_left_arm")

    def test_an_unknown_code_is_written_as_type_n(self):
        self.assertEqual(wi.field_name(9999, None, False), "type_9999")

    def test_an_unknown_repeated_code_takes_a_position_suffix(self):
        # Five measures of an unknown code become five keys, not one.
        self.assertEqual(wi.field_name(176, 2, True), "type_176_right_arm")

    def test_an_unknown_position_is_written_as_position_n(self):
        # Two unknown positions must not collide.
        self.assertEqual(wi.field_name(175, 99, True),
                         "muscle_mass_position_99")
        self.assertEqual(wi.field_name(175, 98, True),
                         "muscle_mass_position_98")

    def test_a_repeated_code_with_no_position_is_position_none(self):
        self.assertEqual(wi.field_name(1, None, True), "weight_position_none")

    def test_no_types_name_can_collide_with_the_residue_form(self):
        # `type_<n>` is reserved for an unnamed code. If a TYPES name ever
        # matched it, residue and a named field would share a key.
        residue = re.compile(r"^type_[0-9]+$")
        for name, _unit in wi.TYPES.values():
            self.assertIsNone(residue.match(name), name)

    def test_a_field_key_is_escaped(self):
        # A raw space would end the field key and the rest would misparse.
        real = dict(wi.TYPES)
        wi.TYPES[9998] = ("space name", "")
        try:
            self.assertEqual(wi.field_name(9998, None, False), "space\\ name")
        finally:
            wi.TYPES.clear()
            wi.TYPES.update(real)


class GroupId(unittest.TestCase):
    """`grpid` is the per-reading entity and is now a TAG, so a group without
    one has no identity and cannot be written."""

    def test_a_grpid_becomes_an_escaped_tag_value(self):
        self.assertEqual(wi.group_id({"grpid": 12345}), "12345")

    def test_a_missing_grpid_raises(self):
        with self.assertRaises(wi.IngestFailed):
            wi.group_id({"date": 100})

    def test_an_empty_grpid_raises(self):
        with self.assertRaises(wi.IngestFailed):
            wi.group_id({"grpid": ""})

    def test_a_whitespace_grpid_raises(self):
        with self.assertRaises(wi.IngestFailed):
            wi.group_id({"grpid": "   "})

    def test_a_null_grpid_raises(self):
        with self.assertRaises(wi.IngestFailed):
            wi.group_id({"grpid": None})


class Points(unittest.TestCase):
    """ONE GROUP IS ONE LINE. The old shape wrote one line per measure with
    `grpid` as a string field; the string field poisoned every ungrouped
    aggregate and the naive pivot returned a segment as the whole body."""

    def setUp(self):
        self.real_log = wi.log
        wi.log = lambda msg: None

    def tearDown(self):
        wi.log = self.real_log

    def test_line_protocol_shape_and_ordering(self):
        groups = [
            {"grpid": 7, "date": 200, "deviceid": "dev1",
             "measures": [{"type": 1, "value": 74850, "unit": -3}]},
            {"grpid": 6, "date": 100,
             "measures": [{"type": 130, "value": 1234, "unit": -2}]},
        ]
        self.assertEqual(wi.points(groups), [
            "withings_measure_group,person=rob,grpid=6,deviceid=unknown"
            " atrial_fibrillation_result=12.34 100",
            "withings_measure_group,person=rob,grpid=7,deviceid=dev1"
            " weight=74.850 200",
        ])

    def test_one_group_is_one_line_however_many_measures(self):
        lines = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                            "measures": [
                                {"type": 1, "value": 74850, "unit": -3},
                                {"type": 6, "value": 1834, "unit": -2},
                                {"type": 8, "value": 13729, "unit": -3},
                            ]}])
        self.assertEqual(len(lines), 1)
        self.assertIn(" fat_mass_weight=13.729,fat_ratio=18.34,"
                      "weight=74.850 100", lines[0])

    def test_fields_are_sorted_by_key(self):
        # Sorted, so two runs of the same group produce identical bytes.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [
                               {"type": 88, "value": 3200, "unit": -3},
                               {"type": 1, "value": 74850, "unit": -3},
                           ]}])[0]
        body = line.split(" ")[1]
        keys = [pair.split("=")[0] for pair in body.split(",")]
        self.assertEqual(keys, sorted(keys))

    def test_there_is_no_string_field(self):
        # The schema-collision error class is gone from this bucket only if
        # nothing here writes a quoted value.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 1, "value": 74850,
                                         "unit": -3}]}])[0]
        self.assertNotIn('"', line)

    def test_grpid_is_a_tag(self):
        line = wi.points([{"grpid": 4242, "date": 100, "deviceid": "d",
                           "measures": [{"type": 1, "value": 1,
                                         "unit": 0}]}])[0]
        self.assertIn(",grpid=4242,", line)

    def test_a_group_without_a_grpid_raises(self):
        with self.assertRaises(wi.IngestFailed):
            wi.points([{"date": 100, "deviceid": "d",
                        "measures": [{"type": 1, "value": 1, "unit": 0}]}])

    def test_a_group_without_a_deviceid_is_tagged_unknown(self):
        line = wi.points([{"grpid": 1, "date": 100,
                           "measures": [{"type": 4, "value": 1780,
                                         "unit": -3}]}])[0]
        self.assertIn(",deviceid=unknown ", line)

    def test_a_model_string_is_tagged_verbatim(self):
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "model": "Body Cardio",
                           "measures": [{"type": 1, "value": 1,
                                         "unit": 0}]}])[0]
        self.assertIn(",model=Body\\ Cardio ", line)

    def test_a_group_without_a_model_carries_no_model_tag(self):
        # Groups from before 2022 carry none, and nothing invents one.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 1, "value": 1,
                                         "unit": 0}]}])[0]
        self.assertNotIn("model=", line)

    def test_modelid_is_not_written(self):
        # The same fact under a second name, on a point deviceid already keys.
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "modelid": 6, "model_id": 6,
                           "measures": [{"type": 1, "value": 1,
                                         "unit": 0}]}])[0]
        self.assertNotIn("modelid=", line)
        self.assertNotIn("model_id=", line)

    def test_a_segmental_family_writes_five_fields_on_one_line(self):
        measures = [{"type": 175, "value": 3000 + n, "unit": -3,
                     "position": p}
                    for n, p in enumerate([2, 3, 12, 10, 11])]
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": measures}])[0]
        for suffix in ("right_arm", "left_arm", "torso",
                       "left_leg", "right_leg"):
            self.assertIn("muscle_mass_%s=" % suffix, line)

    def test_a_whole_body_code_at_position_seven_keeps_its_bare_name(self):
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 168, "value": 12000,
                                         "unit": -3, "position": 7}]}])[0]
        self.assertIn(" extracellular_water=12.000 ", line)

    def test_an_unknown_code_is_written_as_type_n_and_is_not_dropped(self):
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": [{"type": 9999, "value": 5,
                                         "unit": 0}]}])[0]
        self.assertIn(" type_9999=5 ", line)

    def test_a_duplicate_field_key_stops_the_run(self):
        # The same code at the same position twice in one group. It raises
        # rather than overwriting: a silently overwritten reading is worse
        # than a job that wedges until a person looks.
        with self.assertRaises(wi.IngestFailed):
            wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                        "measures": [
                            {"type": 175, "value": 1, "unit": 0,
                             "position": 2},
                            {"type": 175, "value": 2, "unit": 0,
                             "position": 2},
                        ]}])

    def test_two_positionless_measures_of_one_code_stop_the_run(self):
        # Both resolve to `_position_none`, which is the second and last way
        # the duplicate stop is reachable.
        with self.assertRaises(wi.IngestFailed):
            wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                        "measures": [
                            {"type": 1, "value": 74850, "unit": -3},
                            {"type": 1, "value": 74855, "unit": -3},
                        ]}])

    def test_a_new_segmental_code_cannot_reach_the_duplicate_stop(self):
        # Repetition is read from the group's shape, so an unknown code
        # repeated across five positions needs no edit and raises nothing.
        measures = [{"type": 176, "value": 100 + p, "unit": -2,
                     "position": p} for p in (2, 3, 12, 10, 11)]
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "d",
                           "measures": measures}])[0]
        self.assertIn("type_176_right_arm=", line)
        self.assertIn("type_176_left_leg=", line)

    def test_nothing_is_written_for_any_group_when_one_raises(self):
        # points() raises, so no line is written for ANY group in the run, the
        # resume point does not advance, and every later run fails the same way
        # until a person looks.
        good = {"grpid": 1, "date": 100, "deviceid": "d",
                "measures": [{"type": 1, "value": 1, "unit": 0}]}
        bad = {"grpid": 2, "date": 200, "deviceid": "d",
               "measures": [{"type": 1, "value": 1, "unit": 0},
                            {"type": 1, "value": 2, "unit": 0}]}
        with self.assertRaises(wi.IngestFailed):
            wi.points([good, bad])

    def test_a_typed_height_is_one_point_with_one_field(self):
        line = wi.points([{"grpid": 1, "date": 100,
                           "measures": [{"type": 4, "value": 1780,
                                         "unit": -3}]}])[0]
        self.assertEqual(
            line,
            "withings_measure_group,person=rob,grpid=1,deviceid=unknown"
            " height=1.780 100")

    def test_a_cuff_reading_is_one_point_with_three_fields(self):
        line = wi.points([{"grpid": 1, "date": 100, "deviceid": "cuff",
                           "measures": [
                               {"type": 9, "value": 78, "unit": 0},
                               {"type": 10, "value": 121, "unit": 0},
                               {"type": 11, "value": 62, "unit": 0},
                           ]}])[0]
        self.assertIn(" diastolic_blood_pressure=78,heart_pulse=62,"
                      "systolic_blood_pressure=121 100", line)

    def test_a_group_with_no_measures_writes_no_line(self):
        # A line with no fields is not valid line protocol.
        self.assertEqual(
            wi.points([{"grpid": 1, "date": 100, "measures": []}]), [])

    def test_a_measure_missing_a_field_raises_rather_than_writing_junk(self):
        with self.assertRaises(wi.IngestFailed):
            wi.points([{"grpid": 1, "date": 1, "measures": [{"type": 1}]}])

    def test_a_group_missing_its_date_raises_rather_than_landing_at_epoch(self):
        # A defaulted timestamp writes a 1970 outlier that every panel shows
        # and that no later run corrects.
        with self.assertRaises(wi.IngestFailed):
            wi.points([{"grpid": 1,
                        "measures": [{"type": 1, "value": 1, "unit": 0}]}])


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.flags = {"url": CFG["url"], "org": CFG["org"],
                      "bucket": "withings_flags", "token": "flags-token"}
        self.events = []
        self.pending = {"12": [1791288000123456789]}
        self.groups = [{"grpid": 1, "date": 100, "measures": []},
                       {"grpid": 2, "date": 200, "measures": []}]
        self.times = [150]
        self.after_delete = []
        self.late_marker = None
        self.real_log = wi.log
        wi.log = lambda msg: None

    def tearDown(self):
        wi.log = self.real_log

    def run_gate(self):
        def pending(_cfg):
            self.events.append("flags")
            return {key: value[:] for key, value in self.pending.items()}

        def fetch(_token, since):
            self.events.append("full_fetch")
            self.assertEqual(since, 1230768000)
            if self.late_marker is not None:
                self.pending["12"].append(self.late_marker)
            return self.groups

        def local(_cfg, grpid):
            self.events.append("local")
            return self.after_delete if "delete" in self.events else self.times

        def delete(_cfg, grpid, stop):
            self.events.append("delete")
            self.assertEqual(grpid, "12")
            self.assertEqual(stop, max(self.times) + 1)

        def resolve(_cfg, grpid, timestamp):
            self.events.append(("resolve", grpid, timestamp))

        with patch.object(wi, "pending_flags", pending), \
             patch.object(wi, "fetch_measures", fetch), \
             patch.object(wi, "local_group_times", local), \
             patch.object(wi, "delete_group", delete), \
             patch.object(wi, "resolve_flag", resolve):
            wi.reconcile_flags(CFG, self.flags, "access-token")

    def test_no_flags_makes_no_second_withings_call(self):
        self.pending = {}
        self.run_gate()
        self.assertEqual(self.events, ["flags"])

    def test_every_local_time_must_be_bracketed(self):
        self.times = [150, 250]
        self.run_gate()
        self.assertEqual(self.events, ["flags", "full_fetch", "local"])

    def test_tombstone_like_group_is_present(self):
        self.groups.append({"grpid": 12, "date": 150, "measures": []})
        self.run_gate()
        self.assertEqual(self.events, ["flags", "full_fetch", "local"])

    def test_bracketed_absence_deletes_confirms_then_resolves_captured_markers(self):
        self.pending["12"].append(1791288001000000001)
        self.run_gate()
        self.assertEqual(self.events, ["flags", "full_fetch", "local", "delete",
                                       "local", ("resolve", "12", 1791288000123456789),
                                       ("resolve", "12", 1791288001000000001)])

    def test_empty_or_one_sided_response_cannot_delete(self):
        for groups in ([], self.groups[:1], self.groups[1:]):
            with self.subTest(groups=groups):
                self.groups = groups
                self.events = []
                self.run_gate()
                self.assertNotIn("delete", self.events)
                self.assertFalse(any(isinstance(e, tuple) for e in self.events))

    def test_no_local_points_resolves_only_if_absent_in_nonempty_source(self):
        self.times = []
        self.run_gate()
        self.assertNotIn("delete", self.events)
        self.assertIn(("resolve", "12", 1791288000123456789), self.events)
        self.events = []
        self.groups.append({"grpid": 12, "date": 150, "measures": []})
        self.run_gate()
        self.assertFalse(any(isinstance(e, tuple) for e in self.events))

    def test_source_index_validates_every_group_before_any_absence(self):
        for bad in ({"grpid": "01", "date": 150},
                    {"grpid": 3, "date": "bad"},
                    {"grpid": 3, "date": 1.5}, None):
            with self.subTest(bad=bad):
                self.groups = [{"grpid": 1, "date": 100}, bad,
                               {"grpid": 2, "date": 200}]
                self.events = []
                with self.assertRaises(wi.IngestFailed):
                    self.run_gate()
                self.assertNotIn("local", self.events)
                self.assertNotIn("delete", self.events)

    def test_faults_leave_markers_unresolved_at_verify_stage(self):
        for faulty in ("pending_flags", "fetch_measures", "local_group_times", "delete_group",
                       "resolve_flag"):
            with self.subTest(faulty=faulty):
                wi.STAGE[0] = "verify"
                events = []
                local_reads = iter([self.times, []])
                with patch.object(wi, "pending_flags", return_value=self.pending), \
                     patch.object(wi, "fetch_measures", return_value=self.groups), \
                     patch.object(wi, "local_group_times", side_effect=lambda *_: next(local_reads)), \
                     patch.object(wi, "delete_group", side_effect=lambda *a: events.append("delete")), \
                     patch.object(wi, "resolve_flag", side_effect=lambda *a: events.append("resolve")), \
                     patch.object(wi, faulty, side_effect=wi.IngestFailed("fault")):
                    with self.assertRaises(wi.IngestFailed) as caught:
                        wi.reconcile_flags(CFG, self.flags, "access-token")
                self.assertNotIn("resolve", events)
                self.assertEqual(wi.STAGE[0], "verify")
                if faulty in ("local_group_times", "delete_group", "resolve_flag"):
                    self.assertIn("grpid 12", str(caught.exception))
                    self.assertNotIn("fault", str(caught.exception))

    def test_failed_zero_point_confirmation_never_resolves(self):
        self.after_delete = [150]
        with self.assertRaises(wi.IngestFailed):
            self.run_gate()
        self.assertNotIn(("resolve", "12", 1791288000123456789), self.events)

    def test_new_flag_during_run_stays_pending(self):
        self.pending = {"12": [1791288000123456789]}
        self.late_marker = 1791288001000000001
        self.run_gate()
        self.assertEqual(len(self.pending["12"]), 2)
        self.assertEqual([e for e in self.events if isinstance(e, tuple)],
                         [("resolve", "12", 1791288000123456789)])

    def test_delete_group_uses_measurement_and_validated_tag(self):
        sent = []
        with patch.object(wi, "http_post", side_effect=lambda url, body, headers: (
                sent.append((url, json.loads(body))), (204, ""))[1]):
            wi.delete_group(CFG, "12", 251)
            with self.assertRaises(wi.IngestFailed):
                wi.delete_group(CFG, "01", 251)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["predicate"],
                         '_measurement="withings_measure_group" AND grpid="12"')
        self.assertEqual(sent[0][1]["start"], "1970-01-01T00:00:00Z")
        self.assertEqual(sent[0][1]["stop"], "1970-01-01T00:04:11Z")

    def test_resolve_preserves_nanoseconds(self):
        sent = []
        with patch.object(wi, "http_post", side_effect=lambda url, body, headers: (
                sent.append((url, body.decode(), headers)), (204, ""))[1]):
            wi.resolve_flag(self.flags, "12", 1791288000123456789)
        self.assertIn("precision=ns", sent[0][0])
        self.assertEqual(sent[0][1],
                         'withings_suspect,grpid=12 status="resolved" 1791288000123456789')
        self.assertEqual(sent[0][2]["Authorization"], "Token flags-token")


class RunOrder(StateDir):
    """The sequencing rules from the design's data flow.

    Steps 3 and 4 may not be swapped, and step 2 may not move below step 3.
    """

    def setUp(self):
        super(RunOrder, self).setUp()
        self.order = []
        self.real_env = dict(os.environ)
        os.environ["INFLUX_TOKEN"] = "influx-token"
        os.environ["INFLUX_FLAGS_TOKEN"] = "flags-token"
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
                if 'from(bucket:"withings_flags")' in body.decode():
                    self.order.append("flags")
                    return 200, ""
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
                          "write", "flags"])
        self.assertEqual(wi.STAGE[0], "verify")

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


class GuideDrift(unittest.TestCase):
    """The guide's units table, asserted against TYPES and POSITIONS.

    THIS IS THE ONE COPY THAT CANNOT BE DELETED. Its readers are LLMs holding
    an MCP connection and nothing else: no checkout of this repository, and no
    file system to read TYPES from. So a units correction made in TYPES and not
    in the guide is a wrong answer served to every MCP client - and the units
    were corrected five at a time inside one day (commit e4c6e6d), so the drift
    this guards is observed rather than hypothetical.

    There is no generator: the assertion removes the drift a generator would
    exist to prevent, at a fraction of the code. If a later redesign drops the
    guide's units table, this class goes with it.
    """

    GUIDE = os.path.join(_HERE, "health-data-guide.md")
    ROW = re.compile(
        r"^\|\s*`([a-z0-9_]+(?:_<position>)?)`\s*\|\s*(.+?)\s*\|\s*(\d+)\s*\|$")
    SEGMENT = "_<position>"
    NO_UNIT = "—"        # the em dash the table uses for "no unit stated"

    def block(self, name):
        """The text between `<!-- name -->` and `<!-- /name -->`."""
        with open(self.GUIDE, encoding="utf-8") as handle:
            body = handle.read()
        opener = "<!-- %s -->" % name
        closer = "<!-- /%s -->" % name
        self.assertIn(opener, body, "the guide lost the %s marker" % name)
        self.assertIn(closer, body, "the guide lost the /%s marker" % name)
        return body.split(opener, 1)[1].split(closer, 1)[0]

    def rows(self):
        found = []
        for line in self.block("field-table").splitlines():
            match = self.ROW.match(line.strip())
            if match:
                found.append(match.groups())
        return found

    def test_the_table_holds_every_live_field(self):
        # 18 whole-body names plus the three segmental families as three rows.
        self.assertEqual(len(self.rows()), 21)

    def test_every_row_names_a_code_that_types_holds(self):
        for name, _unit, code in self.rows():
            self.assertIn(int(code), wi.TYPES, name)

    def test_every_row_agrees_with_the_types_name(self):
        for name, _unit, code in self.rows():
            expected = wi.TYPES[int(code)][0]
            if name.endswith(self.SEGMENT):
                expected = expected.replace("_segments", "") + self.SEGMENT
            self.assertEqual(name, expected,
                             "guide row %r disagrees with TYPES[%s]"
                             % (name, code))

    def test_every_row_agrees_with_the_types_unit(self):
        for name, unit, code in self.rows():
            expected = wi.TYPES[int(code)][1]
            written = "" if unit == self.NO_UNIT else unit
            self.assertEqual(written, expected,
                             "guide unit for %r disagrees with TYPES[%s]"
                             % (name, code))

    def test_a_segmental_row_names_a_segments_type(self):
        # The `_segments` strip belongs to the naming rule, not to the table,
        # so a row written with the placeholder must come from such a code.
        for name, _unit, code in self.rows():
            if name.endswith(self.SEGMENT):
                self.assertTrue(wi.TYPES[int(code)][0].endswith("_segments"),
                                name)

    def test_every_named_segment_position_is_in_positions(self):
        named = re.findall(r"`([a-z_]+)`", self.block("segment-positions"))
        self.assertEqual(len(named), 5)
        for position in named:
            self.assertIn(position, wi.POSITIONS.values(), position)


if __name__ == "__main__":
    unittest.main(verbosity=2)
