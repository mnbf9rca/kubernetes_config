#!/usr/bin/env python3
"""Report Renovate dependency lookup failures to one uptime-kuma push monitor.

The daily read identifies the bot's Dependency Dashboard by title and checks
its body for lookup failures. A missing dashboard or a reported lookup failure
pushes DOWN; a readable dashboard without those markers pushes UP. Pull requests
and dashboard age do not affect this signal.

An API error, rate limit, paginated response or unreadable dashboard body is
indeterminate and pushes NOTHING. It must never become a successful read or a
synthetic failure; persistent silence is detected by the monitor's own interval.
There is no start signal.

Only fixed verdicts, fixed next actions and integer facts reach the one-line
heartbeat. Remote package names go to the pod log, never the push message.
The verdict is first, and the message is bounded to 200 characters.

Exit status is always 0: the verdict decides the heartbeat, while bounded
retries handle transient read failures without restarting the Job.

The manifest passes the push URL as PUSH_URL deliberately. Generated scripts
ride the envsubst stream, so they must never name a substituted secret variable.
The placeholder stays in homelab/ops/update-watch.yaml.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- tunables ---------------------------------------------------------------

GITHUB_API = "https://api.github.com"

HTTP_TIMEOUT = 20
RETRIES = 2
RETRY_BACKOFF_SECONDS = 5

USER_AGENT = "kubernetes-config-update-watch"

RENOVATE_LOGIN = "renovate[bot]"

# The `config:recommended` default. This repo deliberately does not set
# `dependencyDashboardTitle`, so this literal identifies the dashboard.
# If it ever changes, the monitor is pushed DOWN as `dashboard-missing` --
# loud, not silent, which is the safe failure direction.
DASHBOARD_TITLE = "Dependency Dashboard"

# Every verdict this watcher can emit. A heartbeat message may carry a member of
# this set and nothing else that is not an int.
V_OK = "ok"
V_DASHBOARD_MISSING = "dashboard-missing"
V_LOOKUP_FAILED = "renovate-lookup-failed"
V_RATE_LIMITED = "rate-limited"
V_SECONDARY_LIMIT = "secondary-limit"
V_REPO_UNREACHABLE = "repo-unreachable"
V_API_ERROR = "api-error"

VERDICTS = frozenset({
    V_OK, V_DASHBOARD_MISSING, V_LOOKUP_FAILED, V_RATE_LIMITED,
    V_SECONDARY_LIMIT, V_REPO_UNREACHABLE, V_API_ERROR,
})

DETERMINATE = frozenset({V_OK, V_DASHBOARD_MISSING, V_LOOKUP_FAILED})
GREEN = frozenset({V_OK})

# What to DO about each verdict, emitted as the message's `next=` field.
#
# EVERY STRING BELOW IS A FIXED LITERAL, chosen at edit time and keyed by a
# member of VERDICTS. That is the allowed shape for text in a message: a
# verdict from a fixed enum selects one of a fixed set of sentences, so nothing
# GitHub sent can steer what is written. Do not build one of these by formatting
# in a count or anything else derived at run time -- the numbers already have
# their own `key=int` fields.
#
# Keep them one line, printable ASCII, and short: the message travels verbatim
# into every notification transport the monitor has configured, and an alert
# that needs scrolling is an alert nobody reads. Shortness matters more since
# the move to kuma, because the whole message is now cut at 200 characters. The
# substring `confirm` is avoided here as house style -- it drives a
# healthchecks.io UI nag, which now applies only to the two restic checks that
# stayed there, and one spelling across the estate is worth keeping.
NEXT_ACTIONS = {
    V_OK: "none",
    V_DASHBOARD_MISSING:
        "check the Mend Renovate app is still installed on the repo:"
        " github.com/settings/installations",
    # 103 characters. The line names two places to read and NO remedy, and the
    # missing remedy is deliberate. One dashboard warning covers failures with
    # nothing in common, so the remedy is whatever that run's log shows. The
    # one case diagnosed (the keel images, August 28, 2026) ruled out repo
    # config, registry authentication and runner memory in turn, and ended at
    # a stale negative entry in Mend's own shared package cache: the failing
    # lookup issues no HTTP request at all. Evicting that is a support ticket
    # to Mend, and nothing in this repo reaches it -- the cache key is registry
    # plus package name -- so it is not an action to put on a phone alert. The
    # count says how many packages, the dashboard says which, the log says why.
    V_LOOKUP_FAILED:
        "read the Dependency Dashboard repository problems, then the Mend"
        " run log for the failing lookup's cause",
    V_RATE_LIMITED:
        "no action for one run - the unauthenticated quota is per IP;"
        " look at the Events log if it repeats",
    V_SECONDARY_LIMIT:
        "no action for one run - GitHub secondary rate limit;"
        " look at the Events log if it repeats",
    V_REPO_UNREACHABLE:
        "check GH_REPO in homelab/ops/update-watch.yaml and that the repo is"
        " still public under that name",
    V_API_ERROR:
        "kubectl -n ops logs job/update-watch --tail 50 - the http= line above"
        " names the status, if there was one",
}

# Only reachable if a verdict is added to VERDICTS and not to NEXT_ACTIONS. It
# is a literal too, so the invariant "`next=` is always fixed text" holds even
# then; the unit tests assert the map is complete so it stays unreachable.
NEXT_FALLBACK = "kubectl -n ops logs job/update-watch --tail 50"


def next_action_for(verdict):
    """The fixed `next=` literal for a verdict. Never remote text."""
    return NEXT_ACTIONS.get(verdict, NEXT_FALLBACK)


def log(msg):
    print(msg, flush=True)


# --- heartbeat message ------------------------------------------------------
# Same accumulator shape as the health namespace's ingest job: a module-level
# summary slot plus a list of key=value lines, so the FIRST token is always
# `verdict=` whatever order things were emitted in.
#
# ONE LINE, NOT A BODY, SINCE 2026-08-26. healthchecks.io stored an arbitrary
# body; kuma stores a single `msg` string. So the same lines are printed to the
# pod log in full and joined with spaces, cut at 200 characters, for the push -
# which is why `next=` is emitted EARLY now rather than last. Under a body it
# was last so the eye landed on it; under a one-line message the tail is what
# the cut takes, so last would be the first thing lost.
#
# NEVER EMIT A REMOTE TITLE, A RESPONSE BODY OR repr(exc) HERE.

_UNPRINTABLE = re.compile(r"[^\040-\176]")
SUMMARY = ["verdict=api-error"]
BODY_LINES = []


def _clean(text):
    """One line, printable ASCII. Mirrors the shell emitters' `tr -cd`."""
    return _UNPRINTABLE.sub("", str(text))


def hc_summary(text):
    SUMMARY[0] = "verdict=" + _clean(text)


def hc_emit(key_value):
    BODY_LINES.append(_clean(key_value))


def hc_body():
    """Every line, for the pod log."""
    return "\n".join(SUMMARY + BODY_LINES) + "\n"


# What kuma stores in a heartbeat's `msg` column. The cut is applied here rather
# than at the push, so the same bound is visible to the tests.
MSG_LIMIT = 200


def kuma_msg():
    """The same lines as ONE line, cut to what kuma stores.

    THE CUT LANDS ON A TOKEN BOUNDARY, NEVER MID-TOKEN. A plain `[:200]` left
    fragments like `oldes` and `ht` at the end of the message -- a key with no
    value, or half a key, which reads as data rather than as truncation. Trimming
    back to the last whole token drops the partial pair instead, so every
    `key=value` an operator sees is one this run actually emitted.
    """
    joined = " ".join(SUMMARY + BODY_LINES)
    if len(joined) <= MSG_LIMIT:
        return joined
    return joined[:MSG_LIMIT].rsplit(" ", 1)[0]


# --- the single request -----------------------------------------------------

def issues_url(repo):
    return "%s/repos/%s/issues?state=open&per_page=100" % (GITHUB_API, repo)


def header(headers, name):
    """Case-insensitive header lookup over a plain mapping."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def fetch(repo, opener=None, sleep=time.sleep):
    """One GET, with bounded retries. Returns (status, headers, body_text).

    A status of 0 means the request never produced an HTTP response at all
    (DNS failure, timeout, connection reset) -- classified as `api-error`, never
    as a successful read.
    """
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        issues_url(repo),
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": USER_AGENT})
    attempt = 0
    while True:
        try:
            with opener(request, timeout=HTTP_TIMEOUT) as response:
                status = int(response.status)
                headers = dict(response.headers.items())
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            headers = dict(exc.headers.items()) if exc.headers else {}
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:                      # noqa: BLE001 - best effort
                body = ""
        except Exception as exc:                   # noqa: BLE001 - classify it
            # THE CLASS NAME ONLY, never repr(exc): a urllib exception's message
            # can quote the URL, and the URL is not what belongs in a pod log
            # line that a future editor might copy into a ping body.
            log("request failed (%s)" % type(exc).__name__)
            status, headers, body = 0, {}, ""
        # A 5xx or a transport failure is worth one bounded retry; a 403, a 404
        # or a 200 is an answer, not a blip.
        if (status == 0 or status >= 500) and attempt < RETRIES:
            attempt += 1
            sleep(RETRY_BACKOFF_SECONDS)
            continue
        return status, headers, body


# --- classification ------------------------------------------------

def classify(status, headers, body):
    """Return (verdict, items).

    `items` is the parsed JSON array when the verdict is determinate, else None.
    A determinate read returns the sentinel verdict None so the caller's own
    dashboard check decides what it means; every other return is a final verdict.
    """
    if status in (403, 429):
        remaining = (header(headers, "x-ratelimit-remaining") or "").strip()
        if remaining == "0":
            return V_RATE_LIMITED, None
        return V_SECONDARY_LIMIT, None
    if status == 404:
        # A renamed, deleted or privated repo would otherwise be a permanent,
        # confident green.
        return V_REPO_UNREACHABLE, None
    if status != 200:
        return V_API_ERROR, None
    link = header(headers, "link") or ""
    if 'rel="next"' in link:
        # A truncated page may omit the dashboard. Refuse to guess.
        return V_API_ERROR, None
    try:
        payload = json.loads(body)
    except ValueError:
        return V_API_ERROR, None
    if not isinstance(payload, list):
        # HTTP 200 carrying a JSON object is an error page or a proxy
        # interception; `len()` on it would return a key count.
        return V_API_ERROR, None
    return None, payload


def find_dashboard(items):
    """Find the bot's dashboard by title, ignoring PRs and other issues."""
    for item in items:
        if not isinstance(item, dict):
            continue
        user = item.get("user") or {}
        if (user.get("login") == RENOVATE_LOGIN
                and "pull_request" not in item
                and item.get("title") == DASHBOARD_TITLE):
            return item
    return None


# --- the dashboard's repository problems ------------------------------------
#
# Observed 2026-08-28 on issue 59, which had carried it unnoticed for weeks:
#
#   > Renovate failed to look up the following dependencies:
#   > `Failed to look up docker package ghcr.io/keel-hq/keel: no-result`.
#   > Files affected: `homelab/bootstrap/keel/keel.yaml`, ...
#
# The ITEM pattern requires a datasource word before `package`, so the section's
# own heading -- "failed to look up the following dependencies" -- does not
# match it and cannot inflate the count by one.
#
# THE SECTION PATTERN IS A UNION OF TWO MARKERS, because neither is
# unconditional. The blockquote above comes from Renovate's
# getDepWarningsDashboard, which returns '' when renovate.json sets
# suppressNotifications: ["dependencyLookupWarnings"]. The one-line
# `Package lookup failures` bullet in the issue's "## Repository Problems"
# section comes from logger.warn('Package lookup failures') via
# extractRepoProblems, a path that suppression does not gate. Under suppression,
# though, that bullet reaches the body only when another caller (a pull request
# body, onboarding, reconfigure) ran getDepWarnings first in the same run -- so
# the two are kept as a union and neither may be dropped for the other. The
# alternation is on the literal bullet TEXT, not on the "## Repository Problems"
# heading, so a deprecation or config problem written into that same section
# does not fire this verdict.
LOOKUP_FAILED_SECTION = re.compile(
    r"failed to look up the following dependencies"
    r"|Package lookup failures", re.IGNORECASE)
LOOKUP_FAILED_ITEM = re.compile(
    r"Failed to look up\s+\S+\s+package\s", re.IGNORECASE)


def count_lookup_failures(dashboard):
    """How many package lookups the dashboard body reports as failed, or None.

    None means the body carries no lookup-failure section -- NOT zero, which
    would be a count taken from a section that is not there. A body that is
    missing or is not a string is None as well: an unread body is never evidence
    that every lookup succeeded.

    Zero is returned when the section is present but no item line parsed, which
    is what a Renovate reword looks like. The verdict still fires on it: the
    section says a lookup failed, and the count is only ever an aid to triage.
    """
    body = dashboard.get("body") if isinstance(dashboard, dict) else None
    if not isinstance(body, str):
        return None
    items = LOOKUP_FAILED_ITEM.findall(body)
    if items:
        return len(items)
    if LOOKUP_FAILED_SECTION.search(body):
        return 0
    return None


def log_lookup_failures(dashboard):
    """The failed-lookup lines, TO THE POD LOG AND NOWHERE ELSE.

    This is the one place remote dashboard text is printed, and it is the reason
    the heartbeat can get away with a bare count: the message says how many, the
    pod log says which packages, and the fix needs the names. Nothing here feeds
    a sink -- `log` is not one, and putting one of these lines in a body would
    violate the heartbeat contract.
    """
    body = dashboard.get("body") if isinstance(dashboard, dict) else None
    if not isinstance(body, str):
        return
    for line in body.splitlines():
        if LOOKUP_FAILED_ITEM.search(line) or LOOKUP_FAILED_SECTION.search(line):
            log("dashboard repository problem: " + _clean(line)[:300])


def decide(dashboard):
    """Return a verdict and integer facts from the dashboard lookup signal."""
    if dashboard is None:
        return V_DASHBOARD_MISSING, {}
    if not isinstance(dashboard.get("body"), str):
        return V_API_ERROR, {}
    failures = count_lookup_failures(dashboard)
    if failures is not None:
        return V_LOOKUP_FAILED, {"lookup_failures": failures}
    return V_OK, {}


def ping_suffix(verdict):
    """`0` on a green read, `fail` on a determinate red, `log` otherwise.

    THE THREE-WAY CONTRACT, KEPT AFTER THE MOVE TO kuma. This function no longer
    builds a URL; it is the canonical spelling of the decision, and
    `push_status` below is the same decision in kuma's two-state vocabulary. The
    unit tests assert the two agree, because if they ever disagree the check's
    meaning has quietly forked.

    `log` meant "record an event and change nothing": it could not postpone,
    suppress or trigger an alert, and with no start ping in play it could not arm
    a failure timer either.
    """
    if verdict in GREEN:
        return "0"
    if verdict in DETERMINATE:
        return "fail"
    return "log"


def push_status(verdict):
    """`up`, `down`, or None meaning SEND NOTHING.

    None is the whole migration risk, so it is spelled out. healthchecks.io had
    a third ping kind that recorded an event and changed no state. The kuma push
    API has two states and no third kind, so an indeterminate run must push
    NOTHING: pushing `up` would report a successful read that did not happen, and
    pushing `down` would turn every transient GitHub 503 into an alert. Sending
    nothing records no state change and, if the condition persists, lets the
    monitor go DOWN at its own interval - which is what silence did before.
    """
    if verdict in GREEN:
        return "up"
    if verdict in DETERMINATE:
        return "down"
    return None


# --- uptime-kuma push -------------------------------------------------------

def make_pusher(push_url):
    """Dead-man's-switch pusher. A push must never be able to fail the job, and
    a message must never cost a push."""
    def push(status, msg=""):
        if not push_url or status is None:
            return
        try:
            # THE ENCODE IS INSIDE THE TRY. Evaluated on the line before
            # urlopen, an encoding error would propagate out of push() and the
            # heartbeat would be lost - a message costing a push.
            query = urllib.parse.urlencode(
                {"status": status, "msg": str(msg)[:200]})
            # THE User-Agent IS LOAD-BEARING AND IS NOT COSMETIC. uptime-kuma
            # sits behind Cloudflare, which answers urllib's DEFAULT
            # `Python-urllib/3.x` agent with HTTP 403 and `error code: 1010`
            # before the request ever reaches kuma. Measured in-cluster on
            # 2026-08-26: the default agent got 403/1010 and this one got
            # kuma's own 404 for a bogus token, from the same URL in the same
            # process. Every shell runner in the estate pushes with curl or
            # wget and is unaffected, so this trap is Python-only - and it is
            # SILENT, because a push failure is swallowed by design. Do not
            # drop this header.
            request = urllib.request.Request(
                push_url + "?" + query, headers={"User-Agent": USER_AGENT})
            urllib.request.urlopen(request, timeout=10).close()
        except Exception as exc:                   # noqa: BLE001 - best effort
            # FIXED TEXT PLUS A CLASS NAME. Never the URL: a push URL carries
            # the monitor's token as its last path segment.
            log("uptime-kuma push failed (ignored): %s"
                % type(exc).__name__)
    return push


# --- main -------------------------------------------------------------------

def main():
    repo = os.environ.get("GH_REPO", "").strip()
    if not repo:
        log("FATAL: GH_REPO is unset")
        return V_API_ERROR, {}, 0

    status, headers, body = fetch(repo)
    verdict, items = classify(status, headers, body)
    if verdict is not None:
        log("indeterminate: %s (http %d)" % (verdict, status))
        return verdict, {"http": int(status)}, len(items or ())

    dashboard = find_dashboard(items)
    verdict, facts = decide(dashboard)
    if dashboard is not None:
        log_lookup_failures(dashboard)
    log("read %d open issue(s): verdict %s" % (len(items), verdict))
    return verdict, facts, len(items)


def build_message(verdict, facts, run_epoch):
    """Build one bounded line: verdict, run time, fixed advice, then counters."""
    lookup_failures = int(facts.get("lookup_failures", -1))
    http = int(facts.get("http", -1))

    hc_summary(verdict)
    hc_emit("run_epoch=%d" % run_epoch)
    next_action = next_action_for(verdict)
    hc_emit("next=" + next_action)
    if lookup_failures >= 0:
        hc_emit("lookup_failures=%d" % lookup_failures)
    if http >= 0:
        hc_emit("http=%d" % http)
    return kuma_msg()


if __name__ == "__main__":
    run_epoch = int(time.time())
    facts = {}
    try:
        verdict, facts, _count = main()
    except Exception as exc:                       # noqa: BLE001 - report, then log
        import traceback
        traceback.print_exc()
        log("FATAL: unhandled %s" % type(exc).__name__)
        verdict = V_API_ERROR

    if verdict not in VERDICTS:
        verdict = V_API_ERROR

    msg = build_message(verdict, facts, run_epoch)

    # EVERY LINE TO THE POD LOG, then the cut-down one-liner to kuma. An
    # indeterminate verdict pushes NOTHING, and push() returns without
    # a request when push_status gives None.
    log("heartbeat message (full):\n" + hc_body())
    status = push_status(verdict)
    if status is None:
        log("indeterminate verdict %s: pushing nothing" % verdict)
    make_pusher(os.environ.get("PUSH_URL", ""))(status, msg)
    sys.exit(0)
