#!/usr/bin/env python3
"""Unit tests for update-watch.py.

Stdlib `unittest` only, for the same reason as the health namespace's suite:
this repo has no Python toolchain, and the script is deliberately stdlib-only so
it runs on a bare `python:3.13-alpine3.22` image with no pip step. A test suite
that needed installing would not get run.

    python3 homelab/ops/scripts/test_update_watch.py

What these lock down is the script's one piece of genuine logic -- the
three-way outcome classification and the issue partition -- because a bug in
either is INVISIBLE AT RUNTIME. A classifier that always reports "zero pull
requests" holds the monitor permanently UP over an unread repo, which looks
exactly like a healthy estate.

  * `classify` must never turn a non-answer into an answer: 403, 429, 404, 5xx,
    a transport failure, a paginated response and an HTTP 200 carrying a JSON
    object are all indeterminate.
  * `partition` must identify the Dependency Dashboard POSITIVELY by title. The
    regression this guards is a "Fix Renovate Configuration" issue being read as
    the dashboard -- confident green while Renovate is halted.
  * `decide` must return red for an update that has waited past
    `PR_AGE_RED_DAYS`, a Dependency Dashboard that has not moved in
    `RENOVATE_ALIVE_MAX_DAYS`, a missing dashboard and a configuration error --
    and must ignore human issues and human pull requests. A young pull request
    is GREEN, which is the point of the relaxed threshold.

No network and no push: these exercise return values only and never call the
`hc_emit`/`hc_summary` sinks, so no test-local name can teach the ping-body
guard that a name is safe to emit.
"""
import importlib.util
import os
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

# The script is a kubectl-mounted file with a hyphen in its name, so it cannot
# be imported by name.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "update-watch.py")
_spec = importlib.util.spec_from_file_location("update_watch", _PATH)
uw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uw)

NOW = datetime(2026, 8, 24, 6, 45, 0, tzinfo=timezone.utc)


def stamp(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def bot_pr(number, days_ago=1):
    return {"number": number, "title": "Update grafana/grafana to v13.2.0",
            "user": {"login": "renovate[bot]"},
            "pull_request": {"url": "https://example.invalid/%d" % number},
            "created_at": stamp(days_ago)}


def dashboard(days_ago=6):
    return {"number": 2, "title": "Dependency Dashboard",
            "user": {"login": "renovate[bot]"}, "updated_at": stamp(days_ago)}


def config_error():
    return {"number": 99, "title": "Action Required: Fix Renovate Configuration",
            "user": {"login": "renovate[bot]"}, "updated_at": stamp(0)}


def human_issue():
    return {"number": 7, "title": "something is broken",
            "user": {"login": "mnbf9rca"}, "updated_at": stamp(3)}


def human_pr():
    return {"number": 8, "title": "feat: a change",
            "user": {"login": "mnbf9rca"},
            "pull_request": {"url": "https://example.invalid/8"},
            "created_at": stamp(2)}


class TestClassify(unittest.TestCase):
    """Rule 1: "I could not look" is never "everything is fine"."""

    def test_200_with_a_list_is_determinate(self):
        verdict, items = uw.classify(200, {}, '[{"number": 1}]')
        self.assertIsNone(verdict)
        self.assertEqual(items, [{"number": 1}])

    def test_200_with_an_object_is_not_zero_pull_requests(self):
        # A proxy error page. `len()` on it would return a key count.
        verdict, items = uw.classify(200, {}, '{"message": "Bad gateway"}')
        self.assertEqual(verdict, uw.V_API_ERROR)
        self.assertIsNone(items)

    def test_200_with_unparseable_body(self):
        verdict, _ = uw.classify(200, {}, "<html>nope</html>")
        self.assertEqual(verdict, uw.V_API_ERROR)

    def test_paginated_response_refuses_to_guess(self):
        headers = {"Link": '<https://api.github.com/x?page=2>; rel="next"'}
        verdict, _ = uw.classify(200, headers, "[]")
        self.assertEqual(verdict, uw.V_API_ERROR)

    def test_403_with_quota_exhausted_is_rate_limited(self):
        verdict, _ = uw.classify(403, {"X-RateLimit-Remaining": "0"}, "")
        self.assertEqual(verdict, uw.V_RATE_LIMITED)

    def test_403_with_quota_remaining_is_a_secondary_limit(self):
        verdict, _ = uw.classify(403, {"x-ratelimit-remaining": "57"}, "")
        self.assertEqual(verdict, uw.V_SECONDARY_LIMIT)

    def test_429_is_classified_like_403(self):
        verdict, _ = uw.classify(429, {"X-RateLimit-Remaining": "0"}, "")
        self.assertEqual(verdict, uw.V_RATE_LIMITED)

    def test_404_is_repo_unreachable(self):
        verdict, _ = uw.classify(404, {}, "")
        self.assertEqual(verdict, uw.V_REPO_UNREACHABLE)

    def test_server_error_is_api_error(self):
        verdict, _ = uw.classify(503, {}, "")
        self.assertEqual(verdict, uw.V_API_ERROR)

    def test_transport_failure_is_api_error(self):
        # fetch() reports a DNS or timeout failure as status 0.
        verdict, _ = uw.classify(0, {}, "")
        self.assertEqual(verdict, uw.V_API_ERROR)

    def test_every_indeterminate_verdict_is_log(self):
        for verdict in (uw.V_RATE_LIMITED, uw.V_SECONDARY_LIMIT,
                        uw.V_REPO_UNREACHABLE, uw.V_API_ERROR):
            self.assertEqual(uw.ping_suffix(verdict), "log", verdict)

    def test_determinate_verdicts_are_zero_or_fail(self):
        for verdict in (uw.V_OK, uw.V_UPDATES_WAITING):
            self.assertEqual(uw.ping_suffix(verdict), "0", verdict)
        for verdict in (uw.V_UPDATES_PENDING, uw.V_DASHBOARD_MISSING,
                        uw.V_CONFIG_ERROR):
            self.assertEqual(uw.ping_suffix(verdict), "fail", verdict)


class TestPartition(unittest.TestCase):
    """Rule 3: the dashboard is identified positively, by title."""

    def test_pull_requests_dashboard_and_config_issues_are_separated(self):
        items = [bot_pr(57), dashboard(), config_error(), human_issue(),
                 human_pr()]
        prs, dash, config = uw.partition(items)
        self.assertEqual([pr["number"] for pr in prs], [57])
        self.assertEqual(dash["title"], "Dependency Dashboard")
        self.assertEqual([issue["number"] for issue in config], [99])

    def test_a_config_error_issue_is_not_mistaken_for_the_dashboard(self):
        # THE REGRESSION THIS SUITE EXISTS FOR. "The renovate[bot] issue that is
        # not a pull request" would read this as a healthy dashboard and report
        # green while Renovate has stopped proposing anything at all.
        prs, dash, config = uw.partition([config_error()])
        self.assertEqual(prs, [])
        self.assertIsNone(dash)
        self.assertEqual(len(config), 1)

    def test_human_activity_is_ignored_entirely(self):
        prs, dash, config = uw.partition([human_issue(), human_pr()])
        self.assertEqual((prs, dash, config), ([], None, []))

    def test_malformed_items_do_not_raise(self):
        prs, dash, config = uw.partition(["not a dict", {}, {"user": None}])
        self.assertEqual((prs, dash, config), ([], None, []))


class TestDecide(unittest.TestCase):

    def decide(self, items):
        return uw.decide(*uw.partition(items), now=NOW)

    def test_clean_repo_is_green(self):
        verdict, facts = self.decide([dashboard(), human_issue()])
        self.assertEqual(verdict, uw.V_OK)
        self.assertEqual(facts["prs_open"], 0)
        self.assertEqual(facts["dash_age_days"], 6)

    def test_open_pull_requests_are_green_with_the_oldest_named(self):
        # Renamed: an open pull request is the NORMAL state under session
        # cadence. The facts it carries are unchanged and still asserted.
        items = [dashboard(), bot_pr(57, days_ago=11), bot_pr(61, days_ago=2)]
        verdict, facts = self.decide(items)
        self.assertEqual(verdict, uw.V_UPDATES_WAITING)
        self.assertEqual(facts["prs_open"], 2)
        self.assertEqual(facts["oldest_pr"], 57)
        self.assertEqual(facts["oldest_pr_days"], 11)

    def test_missing_dashboard_is_red(self):
        verdict, facts = self.decide([])
        self.assertEqual(verdict, uw.V_DASHBOARD_MISSING)
        self.assertNotIn("dash_age_days", facts)

    def test_config_error_outranks_a_pending_update(self):
        # A halted Renovate makes the pull-request count untrustworthy, so the
        # verdict names the cause that explains the rest.
        verdict, facts = self.decide([dashboard(), bot_pr(57), config_error()])
        self.assertEqual(verdict, uw.V_CONFIG_ERROR)
        self.assertEqual(facts["config_issues"], 1)

    def test_every_verdict_decide_can_return_is_in_the_enum(self):
        for items in ([], [dashboard()], [dashboard(), bot_pr(1)],
                      [config_error()]):
            verdict, _ = self.decide(items)
            self.assertIn(verdict, uw.VERDICTS)


class TestNextActions(unittest.TestCase):
    """The body's `next=` line: one FIXED LITERAL per verdict (rule 4).

    The value of this field is that an alert says what to do without anyone
    opening a runbook, so the failure to guard against is a verdict with no
    entry -- which would silently fall back to the generic pod-log line.
    """

    def test_every_verdict_has_its_own_action(self):
        self.assertEqual(set(uw.NEXT_ACTIONS), set(uw.VERDICTS))
        for verdict in uw.VERDICTS:
            self.assertEqual(uw.next_action_for(verdict),
                             uw.NEXT_ACTIONS[verdict], verdict)

    def test_actions_are_distinct_so_the_line_is_worth_reading(self):
        actions = list(uw.NEXT_ACTIONS.values())
        self.assertEqual(len(actions), len(set(actions)))

    def test_every_action_is_one_line_of_short_printable_ascii(self):
        for verdict, action in list(uw.NEXT_ACTIONS.items()) + [
                ("fallback", uw.NEXT_FALLBACK)]:
            with self.subTest(verdict=verdict):
                self.assertEqual(uw._clean(action), action)
                self.assertNotIn("\n", action)
                # The message travels verbatim into every notification
                # transport, and kuma cuts it at 200 characters, so an alert
                # nobody scrolls is an alert nobody reads.
                self.assertLessEqual(len(action), 120)
                # `confirm` drives a healthchecks.io UI nag. This job no longer
                # reaches healthchecks.io, but the two restic checks do and one
                # spelling across the estate is worth keeping.
                self.assertNotIn("confirm", action.lower())

    def test_the_four_red_verdicts_name_a_command_or_a_place_to_look(self):
        # The intended signal and the three liveness failures are the ones an
        # operator acts on, so each must point somewhere specific. Renamed from
        # "three" when `renovate-stale` joined them: an unasserted literal is
        # one a reword can silently gut.
        self.assertIn("gh pr list", uw.NEXT_ACTIONS[uw.V_UPDATES_PENDING])
        self.assertIn("apply-homelab", uw.NEXT_ACTIONS[uw.V_UPDATES_PENDING])
        self.assertIn("Mend job log", uw.NEXT_ACTIONS[uw.V_RENOVATE_STALE])
        self.assertIn("managerFilePatterns",
                      uw.NEXT_ACTIONS[uw.V_RENOVATE_STALE])
        self.assertIn("installations", uw.NEXT_ACTIONS[uw.V_DASHBOARD_MISSING])
        self.assertIn("gh issue list", uw.NEXT_ACTIONS[uw.V_CONFIG_ERROR])

    def test_an_unknown_verdict_still_gets_a_literal(self):
        # Unreachable while the map is complete, but the invariant that `next=`
        # is always fixed text must not depend on that.
        self.assertEqual(uw.next_action_for("not-a-verdict"), uw.NEXT_FALLBACK)


class TestFetch(unittest.TestCase):

    def test_server_errors_are_retried_then_reported(self):
        calls = []

        class Response:
            status = 503
            headers = {}

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            calls.append(request.full_url)
            return Response()

        status, _, _ = uw.fetch("owner/repo", opener=opener, sleep=lambda s: None)
        self.assertEqual(status, 503)
        self.assertEqual(len(calls), uw.RETRIES + 1)

    def test_a_403_is_an_answer_and_is_not_retried(self):
        calls = []

        class Response:
            status = 403
            headers = {"x-ratelimit-remaining": "0"}

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            calls.append(request.full_url)
            return Response()

        status, headers, _ = uw.fetch("owner/repo", opener=opener,
                                      sleep=lambda s: None)
        self.assertEqual(status, 403)
        self.assertEqual(len(calls), 1)
        self.assertEqual(uw.classify(status, headers, "")[0], uw.V_RATE_LIMITED)

    def test_the_url_is_the_open_issues_listing(self):
        self.assertEqual(
            uw.issues_url("mnbf9rca/kubernetes_config"),
            "https://api.github.com/repos/mnbf9rca/kubernetes_config"
            "/issues?state=open&per_page=100")


class TestRelaxedPullRequestThreshold(unittest.TestCase):
    """An open pull request is the NORMAL state under session cadence.

    The old rule went red on any open pull request, which under a 4-to-6-week
    session makes red the steady state -- and an alarm that is normally red is
    not an alarm. Red now means "this has been waiting long enough that a
    session was skipped".
    """

    def setUp(self):
        self.now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        self.dash = {"user": {"login": uw.RENOVATE_LOGIN},
                     "title": uw.DASHBOARD_TITLE,
                     "updated_at": "2026-08-25T12:00:00Z"}

    def _pr(self, days_old):
        created = self.now - timedelta(days=days_old)
        return {"user": {"login": uw.RENOVATE_LOGIN},
                "pull_request": {"url": "x"},
                "number": 101,
                "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")}

    def test_a_young_pull_request_is_green(self):
        verdict, facts = uw.decide([self._pr(3)], self.dash, [], self.now)
        self.assertEqual(verdict, uw.V_UPDATES_WAITING)
        self.assertEqual(facts["prs_open"], 1)
        self.assertEqual(uw.ping_suffix(verdict), "0")

    def test_a_pull_request_just_under_the_threshold_is_still_green(self):
        verdict, _ = uw.decide([self._pr(uw.PR_AGE_RED_DAYS - 1)],
                               self.dash, [], self.now)
        self.assertEqual(verdict, uw.V_UPDATES_WAITING)

    def test_a_pull_request_at_exactly_the_threshold_is_still_green(self):
        # The boundary itself. `decide` compares with `>`, so the threshold day
        # is the last green one -- the same shape as the dashboard clause.
        verdict, facts = uw.decide([self._pr(uw.PR_AGE_RED_DAYS)],
                                   self.dash, [], self.now)
        self.assertEqual(verdict, uw.V_UPDATES_WAITING)
        self.assertEqual(facts["oldest_pr_days"], uw.PR_AGE_RED_DAYS)

    def test_a_pull_request_past_the_threshold_is_red(self):
        verdict, facts = uw.decide([self._pr(uw.PR_AGE_RED_DAYS + 1)],
                                   self.dash, [], self.now)
        self.assertEqual(verdict, uw.V_UPDATES_PENDING)
        self.assertEqual(facts["oldest_pr_days"], uw.PR_AGE_RED_DAYS + 1)
        self.assertEqual(uw.ping_suffix(verdict), "fail")

    def test_the_threshold_is_about_a_session_and_a_half(self):
        self.assertGreaterEqual(uw.PR_AGE_RED_DAYS, 42)
        self.assertLessEqual(uw.PR_AGE_RED_DAYS, 60)

    def test_a_config_error_still_outranks_any_pull_request_age(self):
        config = [{"user": {"login": uw.RENOVATE_LOGIN}, "title": "Action Required"}]
        verdict, _ = uw.decide([self._pr(1)], self.dash, config, self.now)
        self.assertEqual(verdict, uw.V_CONFIG_ERROR)


class TestRenovateLiveness(unittest.TestCase):
    """Renovate's own liveness, as a RED VERDICT ON THE SAME SIGNAL.

    An earlier design gave this a destination of its own, on the argument that
    an alerting backend notifies on status FLIPS and the first one was
    permanently red. This task removes the permanent red, so the one signal
    flips on a Renovate death exactly as a second one would have. One
    destination, one enum -- and that held when the destination changed from a
    healthchecks.io check to a kuma push monitor.

    What these lock down is that liveness OUTRANKS the pull-request rules.
    A dead Renovate with a young pull request still open must not read as
    `updates-waiting`, which is green: that is the failure the split was
    invented to prevent, and precedence is what actually prevents it.
    """

    def setUp(self):
        self.now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

    def _dash(self, days_old):
        moved = self.now - timedelta(days=days_old)
        return {"user": {"login": uw.RENOVATE_LOGIN},
                "title": uw.DASHBOARD_TITLE,
                "updated_at": moved.strftime("%Y-%m-%dT%H:%M:%SZ")}

    def _pr(self, days_old):
        created = self.now - timedelta(days=days_old)
        return {"user": {"login": uw.RENOVATE_LOGIN},
                "pull_request": {"url": "x"},
                "number": 101,
                "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")}

    def test_a_fresh_dashboard_and_no_pull_requests_is_ok(self):
        verdict, _ = uw.decide([], self._dash(1), [], self.now)
        self.assertEqual(verdict, uw.V_OK)

    def test_a_dashboard_at_the_threshold_is_still_ok(self):
        verdict, _ = uw.decide(
            [], self._dash(uw.RENOVATE_ALIVE_MAX_DAYS), [], self.now)
        self.assertEqual(verdict, uw.V_OK)

    def test_a_dashboard_past_the_threshold_is_red(self):
        verdict, _ = uw.decide(
            [], self._dash(uw.RENOVATE_ALIVE_MAX_DAYS + 1), [], self.now)
        self.assertEqual(verdict, uw.V_RENOVATE_STALE)
        self.assertEqual(uw.ping_suffix(uw.V_RENOVATE_STALE), "fail")

    def test_staleness_outranks_a_young_pull_request(self):
        # THE WHOLE POINT. A young pull request alone is green. Renovate can
        # die with one still open, and the check must go red anyway.
        verdict, _ = uw.decide([self._pr(2)],
                               self._dash(uw.RENOVATE_ALIVE_MAX_DAYS + 1),
                               [], self.now)
        self.assertEqual(verdict, uw.V_RENOVATE_STALE)

    def test_a_config_error_still_outranks_staleness(self):
        config = [{"user": {"login": uw.RENOVATE_LOGIN}, "title": "Action Required"}]
        verdict, _ = uw.decide(
            [], self._dash(uw.RENOVATE_ALIVE_MAX_DAYS + 1), config, self.now)
        self.assertEqual(verdict, uw.V_CONFIG_ERROR)

    def test_a_missing_dashboard_keeps_its_own_more_specific_verdict(self):
        verdict, _ = uw.decide([], None, [], self.now)
        self.assertEqual(verdict, uw.V_DASHBOARD_MISSING)

    def test_an_unparseable_dashboard_timestamp_is_not_evidence_of_life(self):
        # A dashboard whose timestamp did not parse yields no dash_age_days.
        # That is a read failure about one field, never proof Renovate is
        # alive, so it must NOT silently pass the liveness rule as `ok`.
        dash = {"user": {"login": uw.RENOVATE_LOGIN},
                "title": uw.DASHBOARD_TITLE,
                "updated_at": "not-a-timestamp"}
        verdict, facts = uw.decide([], dash, [], self.now)
        self.assertNotIn("dash_age_days", facts)
        self.assertEqual(verdict, uw.V_API_ERROR)
        self.assertEqual(uw.ping_suffix(verdict), "log")

    def test_unparseable_pull_request_timestamps_stay_green_unlike_the_dashboard(self):
        # THE ASYMMETRY, ASSERTED SO IT IS A DECISION AND NOT AN OVERSIGHT. An
        # unparseable DASHBOARD timestamp is `api-error`, because that field is
        # the only evidence Renovate is alive. An unparseable pull-request
        # `created_at` is not: the pull requests were still counted, so
        # "updates are waiting" is known true and only their age is unreadable,
        # and the green `updates-waiting` states that truth without escalating.
        pr = {"user": {"login": uw.RENOVATE_LOGIN},
              "pull_request": {"url": "x"},
              "number": 101,
              "created_at": "not-a-timestamp"}
        verdict, facts = uw.decide([pr], self._dash(1), [], self.now)
        self.assertNotIn("oldest_pr_days", facts)
        self.assertEqual(facts["prs_open"], 1)
        self.assertEqual(verdict, uw.V_UPDATES_WAITING)

    def test_renovate_stale_is_determinate_and_red(self):
        self.assertIn(uw.V_RENOVATE_STALE, uw.VERDICTS)
        self.assertIn(uw.V_RENOVATE_STALE, uw.DETERMINATE)
        self.assertNotIn(uw.V_RENOVATE_STALE, uw.GREEN)

    def test_the_threshold_is_a_whole_number_of_days_at_or_above_the_floor(self):
        # RENAMED, AND THE NAME MATTERS. This was
        # `test_the_threshold_was_armed_not_left_at_a_sentinel`, which claimed
        # something no assertion here can check: `>= 14` cannot tell a threshold
        # armed from observation apart from the unarmed floor, and as of
        # 2026-08-26 the value IS the unarmed floor -- six heartbeats had been
        # logged, fewer than the fourteen the arming rule needs. Those six were
        # healthchecks.io pings whose bodies the vault's read-only API key
        # cannot fetch; the history now accumulates in the kuma monitor, where
        # it is readable. The constant's own comment and monitoring.md carry
        # that status; a test name must not contradict them. What this actually
        # checks is the type and the floor.
        self.assertIsInstance(uw.RENOVATE_ALIVE_MAX_DAYS, int)
        self.assertGreaterEqual(uw.RENOVATE_ALIVE_MAX_DAYS, 14)

    def test_there_is_no_second_check_left_behind(self):
        # A removal test. The second UUID, its enum and its action map are
        # gone; a half-removal that leaves `alive_decide` importable but
        # unpinged is the shape this asserts against.
        for name in ("alive_decide", "alive_ping_suffix", "ALIVE_VERDICTS",
                     "ALIVE_NEXT_ACTIONS", "alive_next_action_for",
                     "A_OK", "A_STALE", "A_UNKNOWN"):
            self.assertFalse(hasattr(uw, name), name)


class TestKumaPush(unittest.TestCase):
    """`log` means SEND NOTHING, and that is the whole migration risk.

    healthchecks.io had a third ping kind that recorded an event and changed
    no state. The kuma push API has two states and no third kind, so an
    indeterminate run must push NOTHING - pushing `up` would report a
    successful read that did not happen, and pushing `down` would turn every
    transient GitHub 503 into an alert.
    """

    def test_green_verdicts_push_up(self):
        for verdict in sorted(uw.GREEN):
            self.assertEqual(uw.push_status(verdict), "up", verdict)

    def test_determinate_reds_push_down(self):
        for verdict in sorted(uw.DETERMINATE - uw.GREEN):
            self.assertEqual(uw.push_status(verdict), "down", verdict)

    def test_indeterminate_verdicts_push_nothing(self):
        for verdict in sorted(uw.VERDICTS - uw.DETERMINATE):
            self.assertIsNone(uw.push_status(verdict), verdict)

    def test_push_status_agrees_with_ping_suffix(self):
        # One decision, two spellings. If they ever disagree, the check's
        # meaning has quietly forked.
        mapping = {"0": "up", "fail": "down", "log": None}
        for verdict in sorted(uw.VERDICTS):
            self.assertEqual(uw.push_status(verdict),
                             mapping[uw.ping_suffix(verdict)], verdict)


class TestPusher(unittest.TestCase):
    """A push must never fail the job, and a message must never cost a push."""

    def setUp(self):
        self.calls = []
        self._real = uw.urllib.request.urlopen
        uw.urllib.request.urlopen = self._fake
        self.logged = []
        self._real_log = uw.log
        uw.log = self.logged.append

    def tearDown(self):
        uw.urllib.request.urlopen = self._real
        uw.log = self._real_log

    def _fake(self, request, data=None, timeout=None):
        self.calls.append((request.full_url, request))

        class _R:
            def close(self_inner):
                pass
        return _R()

    def test_status_and_message_ride_the_query_string(self):
        uw.make_pusher("https://uptime.example/api/push/tok")(
            "up", "verdict=ok next=none")
        self.assertEqual(len(self.calls), 1)
        url, request = self.calls[0]
        self.assertIsNone(request.data, "the push is a GET, not a POST")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["status"], ["up"])
        self.assertEqual(query["msg"], ["verdict=ok next=none"])

    def test_the_user_agent_is_never_urllibs_default(self):
        # Cloudflare fronts uptime-kuma and answers `Python-urllib/3.x` with
        # 403 `error code: 1010` before kuma ever sees the request - and a
        # failed push is swallowed by design, so the loss would be silent.
        uw.make_pusher("https://uptime.example/api/push/tok")("up", "verdict=ok")
        agent = self.calls[0][1].get_header("User-agent")
        self.assertTrue(agent)
        self.assertNotIn("urllib", agent.lower())
        self.assertEqual(agent, uw.USER_AGENT)

    def test_a_none_status_sends_nothing_at_all(self):
        # The indeterminate path. push_status returns None and the pusher must
        # make no request whatever, not a request with a missing status.
        uw.make_pusher("https://uptime.example/api/push/tok")(None, "verdict=x")
        self.assertEqual(self.calls, [])

    def test_message_is_cut_to_what_kuma_stores(self):
        uw.make_pusher("https://uptime.example/api/push/tok")("down", "x" * 500)
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.calls[0][0]).query)
        self.assertEqual(len(query["msg"][0]), 200)

    def test_push_failure_is_swallowed_and_never_quotes_the_url(self):
        def boom(request, data=None, timeout=None):
            raise OSError("connection refused")
        uw.urllib.request.urlopen = boom
        uw.make_pusher("https://uptime.example/api/push/s3cr3ttoken")(
            "up", "verdict=ok")                  # must not raise
        self.assertTrue(self.logged)
        for line in self.logged:
            self.assertNotIn("s3cr3ttoken", line)
            self.assertNotIn("uptime.example", line)
            self.assertIn("OSError", line)

    def test_empty_push_url_pushes_nothing(self):
        uw.make_pusher("")("up", "verdict=ok")
        self.assertEqual(self.calls, [])


class TestHeartbeatMessage(unittest.TestCase):
    """One line, verdict first, cut to what kuma stores."""

    def setUp(self):
        uw.SUMMARY[0] = "verdict=api-error"
        del uw.BODY_LINES[:]

    tearDown = setUp

    def test_verdict_is_the_first_token(self):
        uw.hc_emit("prs_open=3")
        uw.hc_summary(uw.V_UPDATES_PENDING)
        self.assertTrue(uw.kuma_msg().startswith(
            "verdict=" + uw.V_UPDATES_PENDING + " "))

    def test_default_verdict_is_indeterminate_not_a_success(self):
        # If nothing ever calls hc_summary, the message must not claim a green
        # read of the repo.
        self.assertTrue(uw.hc_body().startswith("verdict="))
        self.assertNotIn(uw.SUMMARY[0].split("=", 1)[1], uw.GREEN)

    def test_the_message_is_one_line_and_bounded(self):
        uw.hc_summary(uw.V_OK)
        for _ in range(80):
            uw.hc_emit("oldest_pr_days=12")
        msg = uw.kuma_msg()
        self.assertEqual(len(msg), 200)
        self.assertNotIn("\n", msg)

    def test_the_summary_only_ever_carries_a_member_of_the_enum(self):
        for verdict in sorted(uw.VERDICTS):
            uw.hc_summary(verdict)
            self.assertEqual(uw.hc_body().splitlines()[0],
                             "verdict=" + verdict)


if __name__ == "__main__":
    unittest.main()
