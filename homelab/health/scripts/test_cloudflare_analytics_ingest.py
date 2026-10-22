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
import io
import json
import os
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

# The script is named with hyphens (it is a kubectl-mounted file, not a module),
# so it cannot be imported by name.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "cloudflare-analytics-ingest.py")
_spec = importlib.util.spec_from_file_location("cf_ingest", _PATH)
cf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf)

# The module's own default, captured at import before any setUp has run. An
# assertion that reads SUMMARY[0] back after setUp wrote it is asserting on the
# setUp, not on the source, and would stay green if the source's default were
# changed to a success verdict -- the exact regression it claims to guard.
DEFAULT_SUMMARY = cf.SUMMARY[0]

UTC = timezone.utc


def hour(h):
    return datetime(2026, 8, 20, h, 0, 0, tzinfo=UTC)


def row(host="a.example", path="/x", status=200, country="GB",
        dt="2026-08-20T10:00:00Z", count=1, interval=1.0, method="GET",
        bot="", security="unknown", agent="Mozilla/5.0", content_type="html"):
    return {
        "count": count,
        "avg": {"sampleInterval": interval},
        "dimensions": {
            "datetimeHour": dt,
            "clientRequestHTTPHost": host,
            "clientRequestPath": path,
            "clientRequestHTTPMethodName": method,
            "edgeResponseStatus": status,
            "clientCountryName": country,
            "verifiedBotCategory": bot,
            "securitySource": security,
            "userAgent": agent,
            "edgeResponseContentTypeName": content_type,
        },
    }


def beacon_row(host="blog.example", path="/post", referer="", country="GB",
               device="desktop", dt="2026-08-20T10:00:00Z", count=1, visits=1,
               interval=1.0):
    return {
        "count": count,
        "sum": {"visits": visits},
        "avg": {"sampleInterval": interval},
        "dimensions": {
            "datetimeHour": dt,
            "requestHost": host,
            "requestPath": path,
            "refererHost": referer,
            "countryName": country,
            "deviceType": device,
        },
    }


class CfZones(unittest.TestCase):
    """Zone enumeration, with http_get stubbed so no network is touched."""

    def setUp(self):
        self.real_get = cf.http_get
        self.urls = []

    def tearDown(self):
        cf.http_get = self.real_get

    def stub(self, pages, status=200):
        def _get(url, headers, timeout=None):
            self.urls.append(url)
            page = int(urllib.parse.parse_qs(url.split("?", 1)[1])["page"][0])
            return status, json.dumps(pages[page - 1])
        cf.http_get = _get

    @staticmethod
    def page(zones, total_pages=1, account="acct"):
        return {"success": True, "errors": [],
                "result": [{"name": n, "id": t, "account": {"id": account}}
                           for n, t in zones],
                "result_info": {"total_pages": total_pages}}

    def test_lists_every_page_sorted_by_name_with_the_account(self):
        self.stub([self.page([("b.example", "2"), ("c.example", "3")], 2),
                   self.page([("a.example", "1")], 2)])
        self.assertEqual(cf.cf_zones("t"), ([("a.example", "1"),
                                             ("b.example", "2"),
                                             ("c.example", "3")], "acct"))
        self.assertEqual(len(self.urls), 2)

    def test_two_accounts_raise(self):
        # The beacon stream is account-scoped and this job handles one account.
        self.stub([self.page([("a.example", "1")], 2, account="one"),
                   self.page([("b.example", "2")], 2, account="two")])
        with self.assertRaises(cf.QueryFailed):
            cf.cf_zones("t")

    def test_entry_without_account_raises(self):
        self.stub([{"success": True, "result": [{"name": "x.example", "id": "1"}],
                    "result_info": {"total_pages": 1}}])
        with self.assertRaises(cf.QueryFailed):
            cf.cf_zones("t")

    def test_api_failure_raises_not_empties(self):
        # A failed listing must never read as "no zones": that would ingest
        # nothing and move no watermark, which is at least loud - but a
        # partial listing would silently skip a zone, so success is checked.
        self.stub([{"success": False, "errors": [{"message": "denied"}],
                    "result": None}])
        with self.assertRaises(cf.QueryFailed):
            cf.cf_zones("t")

    def test_empty_account_raises(self):
        self.stub([self.page([])])
        with self.assertRaises(cf.QueryFailed):
            cf.cf_zones("t")

    def test_entry_without_id_raises(self):
        self.stub([{"success": True, "result": [{"name": "x.example"}],
                    "result_info": {"total_pages": 1}}])
        with self.assertRaises(cf.QueryFailed):
            cf.cf_zones("t")


class Watermarks(unittest.TestCase):
    """influx_watermarks() parses annotated CSV where every table has its own
    header and a table lacking a tag has no column for it at all."""

    CSV = ("#group,false,false,false,true\r\n"
           "#datatype,string,long,dateTime:RFC3339,string\r\n"
           "#default,_result,,,\r\n"
           ",result,table,_time,source\r\n"
           ",_result,0,2026-10-20T21:33:18Z,cloudflare\r\n"
           ",_result,1,2026-10-21T18:36:59Z,beacon\r\n"
           "\r\n"
           "#group,false,false,false,true\r\n"
           "#datatype,string,long,dateTime:RFC3339,string\r\n"
           "#default,_result,,,\r\n"
           ",result,table,_time,zone\r\n"
           ",_result,2,2026-10-20T21:00:00Z,cynexia.com\r\n"
           ",_result,3,2026-10-20T17:00:00Z,making-tracks.app\r\n"
           "\r\n"
           "#group,false,false,false,true,true\r\n"
           "#datatype,string,long,dateTime:RFC3339,string,string\r\n"
           "#default,_result,,,,\r\n"
           ",result,table,_time,source,zone\r\n"
           ",_result,4,2026-10-20T21:36:59Z,cloudflare,cynexia.com\r\n")

    def setUp(self):
        self.real_post = cf.http_post

    def tearDown(self):
        cf.http_post = self.real_post

    def query(self, status, text):
        cf.http_post = lambda url, body, headers, timeout=None: (status, text)
        return cf.influx_watermarks(
            {"url": "u", "org": "o", "bucket": "b", "token": "t"})

    def test_one_watermark_per_zone_and_source_pair(self):
        got = self.query(200, self.CSV)
        self.assertEqual(got, {
            ("", "cloudflare"): datetime(2026, 10, 20, 21, 33, 18, tzinfo=UTC),
            ("", "beacon"): datetime(2026, 10, 21, 18, 36, 59, tzinfo=UTC),
            ("cynexia.com", ""): datetime(2026, 10, 20, 21, 0, tzinfo=UTC),
            ("making-tracks.app", ""): datetime(2026, 10, 20, 17, 0, tzinfo=UTC),
            ("cynexia.com", "cloudflare"):
                datetime(2026, 10, 20, 21, 36, 59, tzinfo=UTC),
        })

    def test_empty_bucket_is_an_empty_dict(self):
        self.assertEqual(self.query(200, ""), {})

    def test_influx_error_body_raises_not_empties(self):
        # The 25-days-of-STALE regression: an error that parses as "no data"
        # would seed every zone from the retention floor.
        with self.assertRaises(cf.QueryFailed):
            self.query(200, '{"code":"invalid","message":"schema collision"}')
        with self.assertRaises(cf.QueryFailed):
            self.query(500, "boom")


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


class Rewind(unittest.TestCase):
    def test_floors_to_the_hour(self):
        # Cloudflare honours a sub-hour datetime_geq, so a start of 18:37 would
        # overwrite hour 18 with its 18:37..19:00 slice. The watermark is the
        # previous run's wall-clock minute; the rewind must land on :00.
        wm = datetime(2026, 8, 20, 20, 37, 52, tzinfo=UTC)
        self.assertEqual(cf.rewind(wm), hour(18))

    def test_exact_hour_is_unchanged(self):
        self.assertEqual(cf.rewind(hour(20)), hour(18))


class RetentionFloor(unittest.TestCase):
    def test_rounds_down_then_adds_the_margin(self):
        # 31 days before 18:18:48 is 18:18:48 on the 20th; floored to 18:00,
        # plus two hours.
        now = datetime(2026, 10, 21, 18, 18, 48, tzinfo=UTC)
        self.assertEqual(cf.retention_floor(now, 31 * 86400),
                         datetime(2026, 9, 20, 20, 0, tzinfo=UTC))

    def test_floor_is_between_one_and_two_hours_inside_the_window(self):
        # THE REAL GUARANTEE. The rounding gives back up to 59 minutes of the
        # margin, so the floor must still sit more than an hour inside
        # Cloudflare's boundary at every minute of the clock hour: a run that
        # takes most of an hour (activeDeadlineSeconds is 20 minutes) then
        # never straddles it, whatever minute the Job was created at.
        for minute, second in ((0, 0), (0, 1), (18, 48), (37, 0), (59, 59)):
            now = datetime(2026, 10, 21, 18, minute, second, tzinfo=UTC)
            margin = cf.retention_floor(now, 31 * 86400) - (
                now - timedelta(days=31))
            self.assertGreater(margin, timedelta(hours=1), (minute, second))
            self.assertLessEqual(margin, timedelta(hours=2), (minute, second))

    def test_constant_across_a_clock_hour(self):
        a = datetime(2026, 10, 21, 18, 0, 0, tzinfo=UTC)
        b = datetime(2026, 10, 21, 18, 59, 59, tzinfo=UTC)
        self.assertEqual(cf.retention_floor(a, 3600), cf.retention_floor(b, 3600))


class Resume(unittest.TestCase):
    """resume() decides where a stream starts and whether hours were lost."""

    def test_new_stream_seeds_from_the_floor(self):
        self.assertEqual(cf.resume(None, hour(5)), (hour(5), 0))

    def test_current_stream_rewinds_the_overlap(self):
        wm = datetime(2026, 8, 20, 20, 37, 52, tzinfo=UTC)
        self.assertEqual(cf.resume(wm, hour(5)), (hour(18), 0))

    def test_overlap_below_the_floor_is_clamped_not_a_gap(self):
        # The watermark is hour 18 and the floor is hour 19: hours 16 to 18
        # are stored and merely cannot be rewritten. Nothing after the
        # watermark is older than the floor, so there is no gap. This is the
        # refetch-after-delete case, and it used to raise a false alarm.
        wm = datetime(2026, 8, 20, 18, 36, 59, tzinfo=UTC)
        self.assertEqual(cf.resume(wm, hour(19)), (hour(19), 0))

    def test_hours_after_the_watermark_below_the_floor_are_the_gap(self):
        # Watermark in hour 10, floor at hour 14: hours 11, 12 and 13 were
        # never stored and can no longer be fetched.
        wm = datetime(2026, 8, 20, 10, 36, 59, tzinfo=UTC)
        self.assertEqual(cf.resume(wm, hour(14)), (hour(14), 3))


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
        self.assertEqual(key[5], "unknown")   # method
        self.assertEqual(key[9], "none")      # content type

    def test_content_type_is_part_of_the_key_and_empty_is_none(self):
        acc = {}
        cf.aggregate([row(content_type="html"), row(content_type="json"),
                      row(content_type="")], "z", set(), acc)
        self.assertEqual(sorted(key[9] for key in acc), ["html", "json", "none"])

    def test_method_is_part_of_the_key(self):
        acc = {}
        cf.aggregate([row(method="GET"), row(method="POST")], "z", set(), acc)
        self.assertEqual(sorted(key[5] for key in acc), ["GET", "POST"])

    def test_private_paths_collapse_and_sum(self):
        # family-foqos.app's /profile/<person> and /navigate/<person> must never
        # reach InfluxDB: every raw path under a prefix is one masked series
        # and the counts add up, exactly as truncation does.
        acc = {}
        cf.aggregate([row(path="/profile/alice", count=2),
                      row(path="/profile/bob/settings", count=3),
                      row(path="/navigate/carol", count=5),
                      row(path="/profile", count=7)],
                     "family-foqos.app", set(), acc)
        by_path = {key[2]: count for key, (count, _) in acc.items()}
        self.assertEqual(by_path,
                         {"/profile/*": 5, "/navigate/*": 5, "/profile": 7})

    def test_private_prefixes_are_per_zone(self):
        acc = {}
        cf.aggregate([row(path="/profile/alice")], "cynexia.com", set(), acc)
        (key,) = acc
        self.assertEqual(key[2], "/profile/alice")

    def test_bot_category_and_security_source_are_tags(self):
        acc = {}
        cf.aggregate([row(bot="Search Engine Crawler", security="waf")],
                     "z", set(), acc)
        (key,) = acc
        self.assertEqual(key[6], "Search Engine Crawler")
        self.assertEqual(key[7], "waf")

    def test_empty_bot_category_is_none_and_missing_source_is_unknown(self):
        acc = {}
        cf.aggregate([{"count": 1, "avg": {},
                       "dimensions": {"datetimeHour": "2026-08-20T10:00:00Z",
                                      "verifiedBotCategory": ""}}],
                     "z", set(), acc)
        (key,) = acc
        self.assertEqual(key[6], "none")
        self.assertEqual(key[7], "unknown")

    def test_user_agent_collapses_to_client_and_sums(self):
        # The raw user agent must never reach InfluxDB: three agents, two
        # classes, and the counts within a class add up.
        acc = {}
        cf.aggregate([row(agent="Uptime-Kuma/2.0.0", count=2),
                      row(agent="Uptime-Kuma/1.23.16", count=3),
                      row(agent="curl/8.0", count=5),
                      row(agent="", count=7)],
                     "z", set(), acc)
        by_client = {key[8]: count for key, (count, _) in acc.items()}
        self.assertEqual(by_client, {"uptime-kuma": 5, "other": 12})
        for line in cf.points(acc):
            self.assertNotIn("Uptime-Kuma/", line)
            self.assertNotIn("curl", line)


class AggregateBeacon(unittest.TestCase):
    def test_excluded_zone_and_its_subdomains_are_never_stored(self):
        # Operator ruling: nothing under family-foqos.app reaches InfluxDB.
        acc = {}
        cf.aggregate_beacon(
            [beacon_row(host="family-foqos.app"),
             beacon_row(host="www.family-foqos.app"),
             beacon_row(host="WWW.Family-Foqos.App"),
             beacon_row(host="blog.example"),
             beacon_row(host="notfamily-foqos.app")],
            set(), acc)
        self.assertEqual(sorted(key[0] for key in acc),
                         ["blog.example", "notfamily-foqos.app"])

    def test_counts_and_visits_sum_and_interval_is_count_weighted(self):
        acc = {}
        cf.aggregate_beacon([beacon_row(count=2, visits=1, interval=1.0),
                             beacon_row(count=6, visits=5, interval=3.0)],
                            set(), acc)
        (count, visits, weighted), = acc.values()
        self.assertEqual((count, visits), (8, 6))
        self.assertAlmostEqual(weighted / count, (2 * 1.0 + 6 * 3.0) / 8)

    def test_paths_truncate_like_the_edge_and_full_path_hosts_apply(self):
        acc = {}
        cf.aggregate_beacon([beacon_row(path="/a/b/c/d"),
                             beacon_row(host="full.example", path="/a/b/c/d")],
                            {"full.example"}, acc)
        self.assertEqual(sorted(key[1] for key in acc), ["/a/b/*", "/a/b/c/d"])

    def test_empty_referer_is_none_and_missing_dimensions_fall_back(self):
        acc = {}
        cf.aggregate_beacon([beacon_row(referer=""),
                             {"count": 1, "sum": {}, "avg": {},
                              "dimensions": {"datetimeHour": "2026-08-20T10:00:00Z"}}],
                            set(), acc)
        by_host = {key[0]: key for key in acc}
        self.assertEqual(by_host["blog.example"][2], "none")
        self.assertEqual(by_host["unknown"][2:5], ("none", "unknown", "unknown"))

    def test_line_protocol_shape_and_no_referer_path(self):
        acc = {}
        cf.aggregate_beacon([beacon_row(referer="news.example", count=3, visits=2)],
                            set(), acc)
        (line,) = list(cf.beacon_points(acc))
        self.assertTrue(line.startswith("page_loads,source=beacon,"))
        self.assertIn(",host=blog.example,", line)
        self.assertIn(",referer_host=news.example,", line)
        self.assertIn(",device=desktop ", line)
        self.assertIn(" count=3i,visits=2i,sample_interval=", line)
        self.assertNotIn("referer_path", line)
        self.assertTrue(line.endswith(" %d" % int(hour(10).timestamp())))

    def test_lines_are_ordered_oldest_first(self):
        acc = {}
        cf.aggregate_beacon([beacon_row(dt="2026-08-20T12:00:00Z", host="a.example"),
                             beacon_row(dt="2026-08-20T10:00:00Z", host="z.example")],
                            set(), acc)
        stamps = [int(line.rsplit(" ", 1)[1]) for line in cf.beacon_points(acc)]
        self.assertEqual(stamps, sorted(stamps))


class Points(unittest.TestCase):
    def test_line_protocol_shape(self):
        acc = {}
        cf.aggregate([row(count=2, interval=1.0)], "myzone", set(), acc)
        (line,) = list(cf.points(acc))
        self.assertTrue(line.startswith("http_requests,"))
        self.assertIn("zone=myzone", line)
        self.assertIn("host=a.example", line)
        self.assertIn(",method=GET,", line)
        self.assertIn(",bot_category=none,", line)
        self.assertIn(",security_source=unknown,", line)
        self.assertIn(",client=other,", line)
        self.assertIn(",content_type=html ", line)
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

    def ok_beacon(self, rows):
        return {"data": {"viewer": {"accounts": [
            {"rumPageloadEventsAdaptiveGroups": rows}]}}}


class CfRetention(StubbedQuery):
    def settings(self, edge, beacon):
        return {"data": {"viewer": {
            "zones": [{"settings": {"httpRequestsAdaptiveGroups":
                                    {"notOlderThan": edge}}}],
            "accounts": [{"settings": {"rumPageloadEventsAdaptiveGroups":
                                       {"notOlderThan": beacon}}}]}}}

    def test_reads_both_datasets(self):
        self.stub(200, self.settings(2678400, 15897600))
        self.assertEqual(cf.cf_retention("t", "z", "a", self.budget),
                         (2678400, 15897600))
        self.assertEqual(self.calls[0]["zoneTag"], "z")
        self.assertEqual(self.calls[0]["accountTag"], "a")
        self.assertEqual(self.budget["used"], 1)

    def test_missing_or_non_positive_retention_raises(self):
        # A floor of zero would seed every stream from "now" and silently
        # skip the whole window; an absent one must not default.
        self.stub(200, self.settings(0, 15897600))
        with self.assertRaises(cf.QueryFailed):
            cf.cf_retention("t", "z", "a", self.budget)
        self.stub(200, {"data": {"viewer": {"zones": [{"settings": {}}],
                                            "accounts": [{}]}}})
        with self.assertRaises(cf.QueryFailed):
            cf.cf_retention("t", "z", "a", self.budget)


class CfQuery(StubbedQuery):
    def test_beacon_dataset_reads_the_account_scope(self):
        self.stub(200, self.ok_beacon([beacon_row()]))
        got = cf.cf_query("t", "a", hour(0), hour(1), 10, self.budget, cf.BEACON)
        self.assertEqual(len(got), 1)
        self.assertEqual(self.calls[0]["tag"], "a")
        self.assertIn("rumPageloadEventsAdaptiveGroups", cf.BEACON[0])
        self.assertNotIn("refererPath", cf.BEACON[0])

    def test_beacon_dataset_rejects_the_zone_shape(self):
        self.stub(200, self.ok([beacon_row()]))
        with self.assertRaises(cf.QueryFailed):
            cf.cf_query("t", "a", hour(0), hour(1), 10, self.budget, cf.BEACON)

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
        self.assertEqual(self.calls[0]["tag"], "zt")


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

    def test_zones_at_the_chunk_cap_fit_the_call_budget(self):
        # 21 zones at MAX_CHUNKS each is the shape of a full catch-up; the
        # budget reserve must leave room for one more zone's first chunk to
        # subdivide rather than fail.
        self.assertLess(cf.BUDGET_RESERVE, cf.MAX_GRAPHQL_CALLS)
        self.assertGreaterEqual(cf.BUDGET_RESERVE, 2)

    def test_retention_margin_covers_the_rounding_and_a_run(self):
        # The floor rounds down by up to 59 minutes and a run may take 20; one
        # hour of margin would leave as little as a minute. The guarantee
        # itself is asserted in RetentionFloor.
        self.assertGreaterEqual(cf.RETENTION_MARGIN_HOURS, 2)


class Pusher(unittest.TestCase):
    """A push must never fail the job, and a message must never cost a push.

    These two are the whole reason the pusher is not three lines. The heartbeat
    is the difference between "the job failed" and "the job never ran", so
    anything that can stop it reaching uptime.cynexia.com is a monitoring
    outage. The healthchecks.io pinger this replaces was built around the same
    pair; what changed is that there is no body to fall back from, so a broken
    message must not take the heartbeat with it.
    """

    def setUp(self):
        self.calls = []
        self._real = cf.urllib.request.urlopen
        cf.urllib.request.urlopen = self._fake
        self.logged = []
        self._real_log = cf.log
        cf.log = self.logged.append

    def tearDown(self):
        cf.urllib.request.urlopen = self._real
        cf.log = self._real_log

    def _fake(self, request, data=None, timeout=None):
        self.calls.append((request.full_url, request))

        class _R:
            def close(self_inner):
                pass
        return _R()

    def test_status_and_message_ride_the_query_string(self):
        # kuma reads both from the query string; a POST body would be ignored.
        cf.make_pusher("https://uptime.example/api/push/tok")(
            "up", "verdict=ok chunks=1/8")
        self.assertEqual(len(self.calls), 1)
        url, request = self.calls[0]
        self.assertIsNone(request.data, "the push is a GET, not a POST")
        self.assertTrue(url.startswith("https://uptime.example/api/push/tok?"))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["status"], ["up"])
        self.assertEqual(query["msg"], ["verdict=ok chunks=1/8"])

    def test_the_user_agent_is_never_urllibs_default(self):
        # Cloudflare fronts uptime-kuma and answers `Python-urllib/3.x` with
        # 403 `error code: 1010` before kuma ever sees the request - and a
        # failed push is swallowed by design, so the loss would be silent.
        cf.make_pusher("https://uptime.example/api/push/tok")("up", "verdict=ok")
        agent = self.calls[0][1].get_header("User-agent")
        self.assertTrue(agent)
        self.assertNotIn("urllib", agent.lower())
        self.assertEqual(agent, cf.PUSH_USER_AGENT)

    def test_message_is_cut_to_what_kuma_stores(self):
        cf.make_pusher("https://uptime.example/api/push/tok")("up", "x" * 500)
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.calls[0][0]).query)
        self.assertEqual(len(query["msg"][0]), 200)

    def test_push_failure_is_swallowed_entirely(self):
        def boom(request, data=None, timeout=None):
            raise OSError("no route to host")
        cf.urllib.request.urlopen = boom
        cf.make_pusher("https://uptime.example/api/push/tok")(
            "down", "verdict=failed")            # must not raise

    def test_unencodable_message_does_not_cost_the_push_or_the_run(self):
        # The encode is inside the try. Evaluated outside it, this would
        # propagate past sys.exit(rc) and the run would die in its own reporter.
        class Unencodable:
            def __str__(self):
                raise UnicodeEncodeError("ascii", "x", 0, 1, "boom")
        cf.make_pusher("https://uptime.example/api/push/tok")(
            "up", Unencodable())                 # must not raise
        self.assertEqual(self.calls, [])

    def test_push_failure_log_names_the_class_not_the_repr(self):
        # repr(exc) is banned: the exception in hand may be a QueryFailed whose
        # message carries a zone tag from the zone listing or a raw response body.
        def boom(request, data=None, timeout=None):
            raise cf.QueryFailed("zone tag abc123 said: <secret payload>")
        cf.urllib.request.urlopen = boom
        cf.make_pusher("https://uptime.example/api/push/tok")("down")
        self.assertTrue(self.logged, "the failure should be logged")
        for line in self.logged:
            self.assertNotIn("abc123", line)
            self.assertNotIn("secret payload", line)
            self.assertIn("QueryFailed", line)

    def test_push_failure_log_never_quotes_the_url(self):
        # The push URL carries the monitor's token as its last path segment.
        def boom(request, data=None, timeout=None):
            raise OSError("connection refused")
        cf.urllib.request.urlopen = boom
        cf.make_pusher("https://uptime.example/api/push/s3cr3ttoken")("up")
        self.assertTrue(self.logged)
        for line in self.logged:
            self.assertNotIn("s3cr3ttoken", line)
            self.assertNotIn("uptime.example", line)

    def test_empty_push_url_pushes_nothing(self):
        cf.make_pusher("")("up", "verdict=ok")
        self.assertEqual(self.calls, [])


class HeartbeatMessage(unittest.TestCase):
    """Format rules from spec section 5, enforced rather than trusted."""

    def setUp(self):
        cf.SUMMARY[0] = "verdict=failed"
        del cf.BODY_LINES[:]

    tearDown = setUp

    def test_verdict_is_always_first(self):
        cf.hc_emit("chunks=3/8")
        cf.hc_summary("ok")
        self.assertEqual(cf.hc_body().splitlines()[0], "verdict=ok")
        self.assertTrue(cf.kuma_msg().startswith("verdict=ok "))
        self.assertIn("chunks=3/8", cf.hc_body().splitlines())

    def test_every_line_is_a_key_value_pair_in_printable_ascii(self):
        cf.hc_summary("ok")
        cf.hc_emit("lag=37m \u2014 em dash and caf\u00e9")
        cf.hc_emit("note=a\nb")
        body = cf.hc_body()
        self.assertTrue(body.endswith("\n"))
        for line in body.splitlines():
            self.assertRegex(line, r"^[a-z0-9_]+=")
            self.assertTrue(all(" " <= ch <= "~" for ch in line), line)
        # The newline inside a value was stripped, not allowed to forge a
        # keyless record: three emits in, three lines out.
        self.assertEqual(len(body.splitlines()), 3)

    def test_default_verdict_is_a_failure_not_a_success(self):
        # If nothing ever calls hc_summary, the message must not claim success.
        # Asserted against DEFAULT_SUMMARY, captured at import from the source,
        # NOT against SUMMARY[0], which setUp has just written.
        self.assertTrue(DEFAULT_SUMMARY.startswith("verdict="))
        default = DEFAULT_SUMMARY.split("=", 1)[1]
        self.assertIn(default, cf.VERDICTS)
        self.assertEqual(default, "failed")

    def test_a_verdict_off_the_enum_is_coerced_to_failed_and_logged(self):
        # The enum is enforced in hc_summary, so it is live in the module rather
        # than read only by this file. Coerced, never raised: a message may
        # never cost a push, and `failed` is the safe direction.
        logged = []
        real_log, cf.log = cf.log, logged.append
        try:
            cf.hc_summary("ok - 3 chunks, 250 series")   # a formatted sentence
        finally:
            cf.log = real_log
        self.assertEqual(cf.SUMMARY[0], "verdict=failed")
        self.assertTrue(any("VERDICTS" in line for line in logged), logged)

    def test_the_cut_never_lands_mid_token(self):
        cf.hc_summary("ok")
        for _ in range(40):
            cf.hc_emit("committed_through=2026-08-26T18:00:00Z")
        msg = cf.kuma_msg()
        self.assertLessEqual(len(msg), cf.MSG_LIMIT)
        for token in msg.split(" "):
            self.assertRegex(token, r"^[a-z0-9_]+=\S*$", token)

    def test_the_summary_only_ever_carries_a_member_of_the_enum(self):
        # A kuma msg is one line and the first token is what an alert shows, so
        # the verdict must be one of a fixed few - not a formatted sentence.
        for verdict in cf.VERDICTS:
            cf.hc_summary(verdict)
            self.assertEqual(cf.hc_body().splitlines()[0],
                             "verdict=" + verdict)

    def test_the_message_is_one_line_and_never_longer_than_kuma_stores(self):
        cf.hc_summary("ok")
        for _ in range(60):
            cf.hc_emit("committed_through=2026-08-26T18:00:00Z")
        msg = cf.kuma_msg()
        # At most the limit, and close to it: the trim to a token boundary gives
        # back up to one token, so exact equality would fail for the right
        # reason. A far-short result would mean the join broke.
        self.assertLessEqual(len(msg), cf.MSG_LIMIT)
        self.assertGreater(len(msg), cf.MSG_LIMIT - 45)
        self.assertNotIn("\n", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
