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
requests" produces a permanently green check over an unread repo, which looks
exactly like a healthy estate.

  * `classify` must never turn a non-answer into an answer: 403, 429, 404, 5xx,
    a transport failure, a paginated response and an HTTP 200 carrying a JSON
    object are all indeterminate.
  * `partition` must identify the Dependency Dashboard POSITIVELY by title. The
    regression this guards is a "Fix Renovate Configuration" issue being read as
    the dashboard -- confident green while Renovate is halted.
  * `decide` must return red for a pending update, a missing dashboard and a
    configuration error, and must ignore human issues and human pull requests.

No network and no ping: these exercise return values only and never call the
`hc_emit`/`hc_summary` sinks, so no test-local name can teach the ping-body
guard that a name is safe to emit.
"""
import importlib.util
import os
import unittest
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

    def test_every_indeterminate_verdict_pings_log(self):
        for verdict in (uw.V_RATE_LIMITED, uw.V_SECONDARY_LIMIT,
                        uw.V_REPO_UNREACHABLE, uw.V_API_ERROR):
            self.assertEqual(uw.ping_suffix(verdict), "log", verdict)

    def test_determinate_verdicts_ping_zero_or_fail(self):
        self.assertEqual(uw.ping_suffix(uw.V_OK), "0")
        for verdict in (uw.V_UPDATES_PENDING, uw.V_DASHBOARD_MISSING,
                        uw.V_CONFIG_ERROR, uw.V_STALE):
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

    def test_open_pull_requests_are_red_with_the_oldest_named(self):
        items = [dashboard(), bot_pr(57, days_ago=11), bot_pr(61, days_ago=2)]
        verdict, facts = self.decide(items)
        self.assertEqual(verdict, uw.V_UPDATES_PENDING)
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

    def test_stale_branch_is_not_armed(self):
        # Informational until tuned against observed values: `dash_age_days` is
        # emitted from day one, but a stale dashboard must not turn the check
        # red on a guessed threshold.
        self.assertIsNone(uw.RENOVATE_STALE_DAYS)
        verdict, facts = self.decide([dashboard(days_ago=900)])
        self.assertEqual(verdict, uw.V_OK)
        self.assertEqual(facts["dash_age_days"], 900)

    def test_every_verdict_decide_can_return_is_in_the_enum(self):
        for items in ([], [dashboard()], [dashboard(), bot_pr(1)],
                      [config_error()]):
            verdict, _ = self.decide(items)
            self.assertIn(verdict, uw.VERDICTS)


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


if __name__ == "__main__":
    unittest.main()
