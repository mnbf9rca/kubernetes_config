#!/usr/bin/env python3
"""Unit tests for cloudflare-analytics-ingest.py.

Stdlib `unittest` only, because the repo has no Python toolchain: there is no
requirements file, no virtualenv and no pytest, and the script itself is
deliberately stdlib-only so it can run on a bare `python:3.13-alpine3.22` image
with no pip install. A test suite that needed installing would not get run.

    python3 homelab/health/scripts/test_cloudflare_analytics_ingest.py

These lock down the behaviours the script's docstring calls its four rules,
plus the two escaping paths where getting it wrong corrupts data silently
rather than failing loudly:

  * A FAILED QUERY IS NOT AN EMPTY ONE - Cloudflare answers a broken query with
    HTTP 200 and an `errors` array. `test_200_with_errors_*` is the regression
    guard for the trap that would otherwise advance the watermark over hours
    that were never fetched.
  * Tag escaping - a newline in a tag value ends the point and injects a forged
    one. `clientRequestHTTPHost` comes from the client's Host header, so this is
    attacker-influenced input.
  * Aggregation - path truncation and row-cap subdivision both merge several
    source rows into one series, so summation is required for correctness, not
    speed.

No network, no cluster, no InfluxDB: every Cloudflare call is stubbed by
replacing the module's `http_post`.
"""
import importlib.util
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

# The script is named with hyphens (it is a kubectl-mounted file, not a module),
# so it cannot be imported by name.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "cloudflare-analytics-ingest.py")
_spec = importlib.util.spec_from_file_location("cf_ingest", _PATH)
cf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf)

UTC = timezone.utc


def hour(h):
    return datetime(2026, 8, 20, h, 0, 0, tzinfo=UTC)


def row(host="a.example", path="/x", status=200, country="GB",
        dt="2026-08-20T10:00:00Z", count=1, interval=1.0):
    return {
        "count": count,
        "avg": {"sampleInterval": interval},
        "dimensions": {
            "datetimeHour": dt,
            "clientRequestHTTPHost": host,
            "clientRequestPath": path,
            "edgeResponseStatus": status,
            "clientCountryName": country,
        },
    }


class ParseZoneTags(unittest.TestCase):
    def test_pairs_in_order(self):
        self.assertEqual(cf.parse_zone_tags("a=1,b=2"), [("a", "1"), ("b", "2")])

    def test_tolerates_whitespace_and_blank_entries(self):
        self.assertEqual(cf.parse_zone_tags(" a = 1 , , b=2 "),
                         [("a", "1"), ("b", "2")])

    def test_zone_tag_may_contain_equals(self):
        self.assertEqual(cf.parse_zone_tags("a=x=y"), [("a", "x=y")])

    def test_rejects_entry_without_equals(self):
        with self.assertRaises(SystemExit):
            cf.parse_zone_tags("a=1,broken")

    def test_rejects_empty_half(self):
        with self.assertRaises(SystemExit):
            cf.parse_zone_tags("=1")
        with self.assertRaises(SystemExit):
            cf.parse_zone_tags("a=")

    def test_rejects_empty_input(self):
        # An empty CF_ZONE_TAGS must fail loudly, not silently ingest nothing.
        with self.assertRaises(SystemExit):
            cf.parse_zone_tags("  ,  ")


class TruncatePath(unittest.TestCase):
    def test_keeps_two_segments(self):
        self.assertEqual(cf.truncate_path("/api/v1", False), "/api/v1")

    def test_marks_dropped_segments(self):
        # The /* is what keeps a request for exactly /api/v1 distinguishable
        # from one for something beneath it.
        self.assertEqual(cf.truncate_path("/api/v1/thing/42", False),
                         "/api/v1/*")

    def test_strips_query_and_fragment(self):
        self.assertEqual(cf.truncate_path("/a/b?q=1#frag", False), "/a/b")

    def test_empty_becomes_root(self):
        self.assertEqual(cf.truncate_path("", False), "/")
        self.assertEqual(cf.truncate_path(None, False), "/")
        self.assertEqual(cf.truncate_path("///", False), "/")

    def test_adds_leading_slash(self):
        self.assertEqual(cf.truncate_path("a/b", False), "/a/b")

    def test_allowlisted_host_keeps_full_path(self):
        self.assertEqual(cf.truncate_path("/api/v1/thing/42", True),
                         "/api/v1/thing/42")

    def test_strips_control_characters(self):
        self.assertEqual(cf.truncate_path("/a\n/b", False), "/a/b")

    def test_caps_length_both_modes(self):
        long_path = "/" + "z" * 500
        self.assertEqual(len(cf.truncate_path(long_path, True)), 256)
        self.assertEqual(len(cf.truncate_path(long_path, False)), 256)


class ParseHour(unittest.TestCase):
    def test_parses_zulu(self):
        self.assertEqual(cf.parse_hour("2026-08-20T10:00:00Z"), hour(10))

    def test_missing_raises_query_failed(self):
        # QueryFailed, not ValueError: it must not advance the watermark.
        with self.assertRaises(cf.QueryFailed):
            cf.parse_hour(None)

    def test_garbage_raises_query_failed(self):
        with self.assertRaises(cf.QueryFailed):
            cf.parse_hour("not-a-time")


class EscTag(unittest.TestCase):
    def test_escapes_line_protocol_specials(self):
        self.assertEqual(cf.esc_tag("a,b=c d"), "a\\,b\\=c\\ d")

    def test_escapes_backslash_first(self):
        # Backslash must be doubled before the others are introduced, or the
        # escapes themselves get re-escaped.
        self.assertEqual(cf.esc_tag("a\\b"), "a\\\\b")

    def test_strips_newline_rather_than_escaping_it(self):
        # Line protocol has no escape for a newline in a tag value: a newline
        # ends the point. Escaping is not an option, so it is stripped.
        self.assertNotIn("\n", cf.esc_tag("evil\ninjected,x=1"))
        self.assertEqual(cf.esc_tag("a\nb"), "ab")

    def test_strips_carriage_return_and_nulls(self):
        self.assertEqual(cf.esc_tag("a\r\x00b"), "ab")

    def test_empty_becomes_unknown(self):
        # An empty tag value is rejected by InfluxDB, so it must never be sent.
        self.assertEqual(cf.esc_tag(""), "unknown")
        self.assertEqual(cf.esc_tag("\n\n"), "unknown")

    def test_coerces_non_strings(self):
        self.assertEqual(cf.esc_tag(200), "200")


class Aggregate(unittest.TestCase):
    def test_sums_rows_that_collapse_to_one_series(self):
        # The whole reason aggregate() exists: truncation merges these two
        # distinct source paths into a single output series.
        acc = {}
        cf.aggregate([row(path="/api/v1/a"), row(path="/api/v1/b")],
                     "z", set(), acc)
        self.assertEqual(len(acc), 1)
        (count, _weighted), = acc.values()
        self.assertEqual(count, 2)

    def test_accumulates_across_calls(self):
        # Row-cap subdivision calls aggregate() once per sub-window; the totals
        # must be the same as one undivided call.
        acc = {}
        cf.aggregate([row(count=3)], "z", set(), acc)
        cf.aggregate([row(count=4)], "z", set(), acc)
        self.assertEqual(len(acc), 1)
        (count, _), = acc.values()
        self.assertEqual(count, 7)

    def test_distinct_dimensions_stay_distinct(self):
        acc = {}
        cf.aggregate([row(status=200), row(status=404)], "z", set(), acc)
        self.assertEqual(len(acc), 2)

    def test_zone_name_is_part_of_the_key(self):
        acc = {}
        cf.aggregate([row()], "zone-a", set(), acc)
        cf.aggregate([row()], "zone-b", set(), acc)
        self.assertEqual(len(acc), 2)

    def test_sample_interval_is_count_weighted(self):
        acc = {}
        cf.aggregate([row(count=1, interval=1.0), row(count=3, interval=5.0)],
                     "z", set(), acc)
        (count, weighted), = acc.values()
        self.assertEqual(count, 4)
        self.assertAlmostEqual(weighted / count, (1 * 1.0 + 3 * 5.0) / 4)

    def test_allowlisted_host_is_not_truncated(self):
        acc = {}
        cf.aggregate([row(host="full.example", path="/a/b/c/d")],
                     "z", {"full.example"}, acc)
        (key,) = acc
        self.assertEqual(key[2], "/a/b/c/d")

    def test_missing_dimensions_fall_back_not_crash(self):
        acc = {}
        cf.aggregate([{"count": 1, "avg": {},
                       "dimensions": {"datetimeHour": "2026-08-20T10:00:00Z"}}],
                     "z", set(), acc)
        (key,) = acc
        self.assertEqual(key[1], "unknown")   # host
        self.assertEqual(key[4], "unknown")   # country


class Points(unittest.TestCase):
    def test_line_protocol_shape(self):
        acc = {}
        cf.aggregate([row(count=2, interval=1.0)], "myzone", set(), acc)
        (line,) = list(cf.points(acc))
        self.assertTrue(line.startswith("http_requests,"))
        self.assertIn("zone=myzone", line)
        self.assertIn("host=a.example", line)
        self.assertIn("count=2i", line)
        self.assertTrue(line.endswith(" %d" % int(hour(10).timestamp())))

    def test_output_is_deterministic(self):
        # points() sorts, so a re-run produces an identical body. Without that,
        # an idempotent rewrite would churn.
        acc = {}
        cf.aggregate([row(status=500), row(status=200), row(status=404)],
                     "z", set(), acc)
        self.assertEqual(list(cf.points(acc)), list(cf.points(acc)))

    def test_lines_are_ordered_oldest_first(self):
        # THE PARTIAL-COMMIT RULE. influx_write() batches, so a later batch can
        # fail with earlier ones already stored. The watermark is max(_time)
        # over what is stored and the next run rewinds only OVERLAP_HOURS
        # behind it, so a surviving prefix must never contain a point from
        # later in the chunk than a point that was NOT written. Ordering by
        # timestamp is what guarantees that; ordering by the tag tuple (zone
        # first, as this once did) does not, and loses those hours silently.
        acc = {}
        cf.aggregate(
            [row(dt="2026-08-20T12:00:00Z", host="zzz.example"),
             row(dt="2026-08-20T10:00:00Z", host="mmm.example"),
             row(dt="2026-08-20T11:00:00Z", host="aaa.example")],
            "z", set(), acc)
        stamps = [int(line.rsplit(" ", 1)[1]) for line in cf.points(acc)]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(
            stamps,
            [int(hour(h).timestamp()) for h in (10, 11, 12)])

    def test_ordering_beats_tag_ordering_across_zones(self):
        # The specific regression: two zones over the same window. Sorted on
        # the tag tuple, every point of zone `a` (including the newest hour)
        # sorts ahead of every point of zone `b`, so a first batch that
        # committed only zone `a` would carry the chunk's END timestamp while
        # zone `b`'s earliest hours were never written.
        acc = {}
        cf.aggregate([row(dt="2026-08-20T10:00:00Z"),
                      row(dt="2026-08-20T12:00:00Z")], "a", set(), acc)
        cf.aggregate([row(dt="2026-08-20T11:00:00Z")], "b", set(), acc)
        stamps = [int(line.rsplit(" ", 1)[1]) for line in cf.points(acc)]
        self.assertEqual(stamps, sorted(stamps))

    def test_tags_are_escaped_in_output(self):
        acc = {}
        cf.aggregate([row(host="a b,c")], "z", set(), acc)
        (line,) = list(cf.points(acc))
        self.assertIn("host=a\\ b\\,c", line)
        self.assertEqual(line.count("\n"), 0)


class StubbedQuery(unittest.TestCase):
    """Tests that replace http_post, so no network is touched."""

    def setUp(self):
        self.real_post = cf.http_post
        self.saved = {name: getattr(cf, name)
                      for name in ("GRAPHQL_ROW_LIMIT", "MAX_GRAPHQL_CALLS")}
        self.calls = []
        self.budget = {"used": 0}

    def tearDown(self):
        cf.http_post = self.real_post
        for name, value in self.saved.items():
            setattr(cf, name, value)

    def shrink(self, row_limit=3, max_calls=9):
        """Scale the caps down so a fan-out test stays fast and readable.

        cf_fetch reads both at call time, so patching the module attributes is
        enough. The real values only change how big the numbers are, not which
        branch runs, and the genuine values are asserted in Tunables.
        """
        cf.GRAPHQL_ROW_LIMIT = row_limit
        cf.MAX_GRAPHQL_CALLS = max_calls

    def stub(self, status, doc):
        # Serialise once. The subdivision tests replay a 10k-row response up to
        # MAX_GRAPHQL_CALLS times, and re-encoding it per call dominates the
        # suite's runtime for no benefit.
        text = json.dumps(doc)

        def _post(url, body, headers, timeout=None):
            self.calls.append(json.loads(body.decode())["variables"])
            return status, text
        cf.http_post = _post

    def ok(self, rows):
        return {"data": {"viewer": {"zones": [
            {"httpRequestsAdaptiveGroups": rows}]}}}


class CfQuery(StubbedQuery):
    def test_clean_response_returns_rows(self):
        self.stub(200, self.ok([row()]))
        got = cf.cf_query("t", "z", hour(0), hour(1), 10, self.budget)
        self.assertEqual(len(got), 1)

    def test_clean_empty_response_is_a_real_answer(self):
        # Zero rows from a clean response is legitimate: no traffic that hour.
        self.stub(200, self.ok([]))
        self.assertEqual(cf.cf_query("t", "z", hour(0), hour(1), 10,
                                     self.budget), [])

    def test_200_with_errors_raises(self):
        # THE TRAP. HTTP 200 plus an errors array is a FAILED query. Treating
        # it as "no traffic" advances the watermark over unfetched hours and
        # loses them once Cloudflare's 8 days roll past.
        self.stub(200, {"errors": [{"message": "rate limited"}],
                        "data": {"viewer": {"zones": []}}})
        with self.assertRaises(cf.QueryFailed):
            cf.cf_query("t", "z", hour(0), hour(1), 10, self.budget)

    def test_200_with_errors_and_plausible_data_still_raises(self):
        # The nastiest shape: a well-formed rows array alongside an errors
        # array. The errors check must win.
        self.stub(200, dict(self.ok([row()]),
                            errors=[{"message": "partial failure"}]))
        with self.assertRaises(cf.QueryFailed):
            cf.cf_query("t", "z", hour(0), hour(1), 10, self.budget)

    def test_non_json_body_raises(self):
        cf.http_post = lambda *a, **k: (200, "<html>502</html>")
        with self.assertRaises(cf.QueryFailed):
            cf.cf_query("t", "z", hour(0), hour(1), 10, self.budget)

    def test_http_error_status_raises(self):
        self.stub(503, {"data": None})
        with self.assertRaises(cf.QueryFailed):
            cf.cf_query("t", "z", hour(0), hour(1), 10, self.budget)

    def test_unexpected_shape_raises(self):
        self.stub(200, {"data": {"viewer": {}}})
        with self.assertRaises(cf.QueryFailed):
            cf.cf_query("t", "z", hour(0), hour(1), 10, self.budget)

    def test_empty_zone_list_raises(self):
        # A token that cannot see the zone is a config fault, not silence.
        self.stub(200, {"data": {"viewer": {"zones": []}}})
        with self.assertRaises(cf.QueryFailed):
            cf.cf_query("t", "z", hour(0), hour(1), 10, self.budget)

    def test_null_groups_raises(self):
        self.stub(200, self.ok(None))
        with self.assertRaises(cf.QueryFailed):
            cf.cf_query("t", "z", hour(0), hour(1), 10, self.budget)

    def test_budget_is_consumed_and_enforced(self):
        self.stub(200, self.ok([]))
        budget = {"used": cf.MAX_GRAPHQL_CALLS - 1}
        cf.cf_query("t", "z", hour(0), hour(1), 10, budget)
        self.assertEqual(budget["used"], cf.MAX_GRAPHQL_CALLS)
        with self.assertRaises(cf.QueryFailed):
            cf.cf_query("t", "z", hour(0), hour(1), 10, budget)

    def test_window_is_sent_as_iso_zulu(self):
        self.stub(200, self.ok([]))
        cf.cf_query("t", "zt", hour(0), hour(1), 10, self.budget)
        self.assertEqual(self.calls[0]["start"], "2026-08-20T00:00:00Z")
        self.assertEqual(self.calls[0]["end"], "2026-08-20T01:00:00Z")
        self.assertEqual(self.calls[0]["zoneTag"], "zt")


class CfFetch(StubbedQuery):
    def test_short_response_is_returned_whole(self):
        self.stub(200, self.ok([row()]))
        warnings = []
        got = cf.cf_fetch("t", "z", hour(0), hour(2), warnings, self.budget)
        self.assertEqual(len(got), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(warnings, [])

    def test_row_cap_triggers_subdivision(self):
        # First call returns the cap and must be split; the halves come back
        # short, so the result is the concatenation of both halves.
        self.shrink()
        responses = [[row()] * cf.GRAPHQL_ROW_LIMIT, [row()], [row()]]

        def _post(url, body, headers, timeout=None):
            self.calls.append(json.loads(body.decode())["variables"])
            return 200, json.dumps(self.ok(responses[len(self.calls) - 1]))
        cf.http_post = _post

        warnings = []
        got = cf.cf_fetch("t", "z", hour(0), hour(2), warnings, self.budget)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(got), 2)
        self.assertEqual(warnings, [])

    def test_subdivision_floor_warns_instead_of_looping(self):
        # Below the floor, halving cannot help. The run must report an
        # incomplete window rather than recurse forever.
        self.shrink()
        self.stub(200, self.ok([row()] * cf.GRAPHQL_ROW_LIMIT))
        warnings = []
        start = hour(0)
        end = start + timedelta(seconds=cf.MIN_SUBDIVIDE_SECONDS)
        cf.cf_fetch("t", "z", start, end, warnings, self.budget)
        self.assertEqual(len(warnings), 1)
        self.assertIn("TRUNCATED", warnings[0])

    def test_budget_bounds_a_pathological_subdivision(self):
        # Every response is capped, so subdivision fans out. The budget must
        # stop it before Cloudflare's rate limit is burned.
        self.shrink()
        self.stub(200, self.ok([row()] * cf.GRAPHQL_ROW_LIMIT))
        with self.assertRaises(cf.QueryFailed):
            cf.cf_fetch("t", "z", hour(0), hour(23), [], self.budget)
        self.assertLessEqual(self.budget["used"], cf.MAX_GRAPHQL_CALLS)


class Env(unittest.TestCase):
    def test_missing_is_fatal(self):
        os.environ.pop("CF_TEST_ABSENT", None)
        with self.assertRaises(SystemExit):
            cf.env("CF_TEST_ABSENT")

    def test_empty_is_fatal(self):
        # An empty secret must not be treated as a usable value.
        os.environ["CF_TEST_EMPTY"] = ""
        try:
            with self.assertRaises(SystemExit):
                cf.env("CF_TEST_EMPTY")
        finally:
            os.environ.pop("CF_TEST_EMPTY", None)

    def test_default_is_used(self):
        os.environ.pop("CF_TEST_ABSENT", None)
        self.assertEqual(cf.env("CF_TEST_ABSENT", "fallback"), "fallback")


class Tunables(unittest.TestCase):
    def test_chunk_is_under_cloudflares_one_day_ceiling(self):
        self.assertLess(cf.CHUNK_HOURS, 24)

    def test_backfill_cannot_outrun_retention(self):
        # MAX_CHUNKS * CHUNK_HOURS is what one run can reach back. It is
        # deliberately a little short of RETENTION_HOURS; if it ever exceeded
        # it, a run would query windows Cloudflare has already dropped.
        self.assertLessEqual(cf.MAX_CHUNKS * cf.CHUNK_HOURS,
                             cf.RETENTION_HOURS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
