#!/usr/bin/env python3
"""Drive one uptime-kuma push monitor from this repo's Renovate state.

WHY THIS EXISTS
---------------
Every image in the `health` namespace is version- or digest-pinned and keel is
forbidden there, so updates arrive as Renovate pull requests and nothing was
pointing at them. This job counts the open `renovate[bot]` pull requests on this
repo once a day and drives one monitor: UP while updates simply wait, DOWN when
one has waited long enough that an update session was plainly skipped, DOWN when
Renovate itself has gone quiet or is visibly broken, DOWN (through silence) when
this job stops running.

SINCE 2026-08-28 IT ALSO READS ONE THING OUT OF THE DASHBOARD'S BODY: the
repository problem Renovate writes when a package lookup fails. An image
Renovate cannot look up gets no pull request, and every image the update engine
is forbidden to touch is pinned, so a failed lookup freezes it silently --
`make check-renovate-scope` still reports it watched, because that guard proves
the file is in scope and never that the lookup succeeded. Only a COUNT of failed
lookups reaches the heartbeat: the warning block names the packages, and a
package name is remote text (rule 4). The lines themselves go to the pod log.

THE FOUR RULES THIS SCRIPT EXISTS TO ENFORCE. Read them before changing anything.

  1. "I COULD NOT LOOK" IS NEVER "EVERYTHING IS FINE". A rate limit, a 404, a
     server error, a timeout, a paginated response or an HTTP 200 carrying a
     JSON *object* are all INDETERMINATE: they push NOTHING AT ALL, which
     records no state change and cannot flip the monitor. Counting zero pull
     requests out of any of them would be a confident green over an unread repo.
     This replaced a healthchecks.io `/log` ping on 2026-08-26 and is
     behaviourally the same thing minus the event line in the history: if the
     condition persists, the monitor goes DOWN at its own interval, exactly as
     the check went red by silence.

  2. NOR IS IT "IT FAILED". There is deliberately no start signal, and the push
     API has no such concept to reintroduce: a push is a heartbeat carrying a
     status. Under healthchecks.io a `/start` plus a single transient GitHub 503
     would have alerted one grace period later, because `/log` did not clear
     `last_start`; having no start signal is what makes rule 1 true. Do not
     invent a synthetic one.

  3. THE DASHBOARD IS IDENTIFIED POSITIVELY, BY TITLE. Renovate opens other
     non-pull-request issues from the same account -- most importantly "Action
     Required: Fix Renovate Configuration", during which it stops proposing pull
     requests entirely. "The renovate[bot] issue that is not a PR" would read
     that as a healthy dashboard and report green while Renovate is halted.

  4. NO REMOTE STRING EVER REACHES THE HEARTBEAT MESSAGE. Every emitted value
     is the result of `int()` on something this script derived, or a member of
     the VERDICTS enum below. A pull-request title is unvalidated remote text and
     the message is read verbatim into every notification transport the monitor
     has configured. `make check-ping-bodies` enforces it against an explicit
     allowlist of the names below, and recognises the two sinks by FUNCTION NAME
     rather than by destination host - which is why `hc_emit` and `hc_summary`
     keep their names after the move to kuma.

Exit status is ALWAYS 0. The exit code would conflate "the job worked" with
"there is nothing to do", and a non-zero exit would trigger a `backoffLimit`
re-run that double-pushes for no benefit. The verdict, not the exit code, is what
decides the heartbeat. Retries live in-script; "the job did not run at all" is
covered by the monitor's own interval-plus-retry silence.

ENVIRONMENT VARIABLES ARE RENAMED ON PURPOSE. The CronJob manifest assembles
the whole push URL from the allowlisted placeholder and passes it as `PUSH_URL`,
and this file names only `PUSH_URL`. Generator files ride the same envsubst
stream as every manifest and envsubst rewrites the bare `$NAME` form too, so
naming an `ENVSUBST_VAR_NAMES` entry here -- even in a comment -- would publish
its value inside a ConfigMap. `make check-script-substitution` enforces the
rename; do not "simplify" it away. The placeholder's real name is in
homelab/ops/update-watch.yaml, where substitution is what is meant to happen.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --- tunables ---------------------------------------------------------------

GITHUB_API = "https://api.github.com"

# One request per day against the unauthenticated `core` quota (60/hour/IP).
# `/issues`, not `/pulls`: GitHub surfaces every pull request as an issue, so one
# call returns the pending signal (bot pull requests) and the liveness signals
# (the Dependency Dashboard, and any configuration-error issue) together.
HTTP_TIMEOUT = 20
RETRIES = 2
RETRY_BACKOFF_SECONDS = 5

USER_AGENT = "kubernetes-config-update-watch"

RENOVATE_LOGIN = "renovate[bot]"

# The `config:recommended` default. This repo deliberately does not set
# `dependencyDashboardTitle`, so this literal is the whole identification (rule
# 3). If it ever changes, the monitor is pushed DOWN as `dashboard-missing` --
# loud, not silent, which is the safe failure direction.
DASHBOARD_TITLE = "Dependency Dashboard"

# RED ONLY WHEN A SESSION HAS PLAINLY BEEN SKIPPED. The estate updates in a
# session every 4 to 6 weeks, so an open Renovate pull request is the NORMAL
# state for weeks at a time. The original rule -- red on any open pull request --
# makes red the steady state under that cadence, and an alarm that is normally
# red is not an alarm: it trains the operator to ignore the one time it means
# something. 45 days is a session and a half.
PR_AGE_RED_DAYS = 45

# THE LIVENESS THRESHOLD, ON THIS SAME MONITOR. The verdict becomes
# `renovate-stale` when the Dependency Dashboard issue has not been touched in
# this many days. The dashboard's `updated_at` is a stable API field; this
# signal reads none of the body -- the one marker read out of the markdown is at
# LOOKUP_FAILED_SECTION below.
#
# THIS USED TO BE A SECOND CHECK WITH ITS OWN UUID. The argument for splitting
# was that an alerting backend notifies on status FLIPS and this signal was
# permanently red under the old any-open-pull-request rule, so a folded-in
# signal could never fire. `updates-waiting` removes the permanent red, so the
# one monitor flips on a Renovate death exactly as a second one would have. One
# destination, one enum. (Ruled 2026-08-26, when the healthchecks.io account was
# capped at 20 checks; this job has since moved to a kuma push monitor, and the
# argument holds unchanged there.)
#
# NOT ARMED FROM OBSERVATION YET -- THIS IS THE FLOOR, AND THAT IS DELIBERATE.
# The arming rule is twice the maximum `dash_age_days` seen across the last 30
# heartbeats, floored at 14 days. Read 2026-08-26: this job shipped 2026-08-24
# and had logged 6 pings in total, fewer than the 14 the rule needs, so there is
# no observed maximum to double and the floor stands. Those six were
# healthchecks.io pings whose bodies the read-only API key in the vault could
# not fetch; the history now lives in the `homelab-update-watch` monitor in
# uptime-kuma, where each heartbeat's message is readable in the UI. Re-read it
# after a month of data and re-arm this: a threshold tighter than the quiet
# periods is DOWN every fortnight, and one looser than a month lets Renovate die
# unnoticed.
RENOVATE_ALIVE_MAX_DAYS = 14

# Every verdict this watcher can emit. A heartbeat message may carry a member of
# this set and nothing else that is not an int.
V_OK = "ok"
V_UPDATES_WAITING = "updates-waiting"
V_UPDATES_PENDING = "updates-pending"
V_RENOVATE_STALE = "renovate-stale"
V_DASHBOARD_MISSING = "dashboard-missing"
V_CONFIG_ERROR = "renovate-config-error"
V_LOOKUP_FAILED = "renovate-lookup-failed"
V_RATE_LIMITED = "rate-limited"
V_SECONDARY_LIMIT = "secondary-limit"
V_REPO_UNREACHABLE = "repo-unreachable"
V_API_ERROR = "api-error"

VERDICTS = frozenset({
    V_OK, V_UPDATES_WAITING, V_UPDATES_PENDING, V_RENOVATE_STALE,
    V_DASHBOARD_MISSING, V_CONFIG_ERROR, V_LOOKUP_FAILED, V_RATE_LIMITED,
    V_SECONDARY_LIMIT, V_REPO_UNREACHABLE, V_API_ERROR,
})

# The verdicts that mean "the repo was read successfully". Everything else is
# indeterminate and pings /log (rule 1). `renovate-lookup-failed` belongs here
# and not with the indeterminate ones: the repo WAS read, and the dashboard says
# in as many words that a lookup failed. That is an answer, not a non-answer.
DETERMINATE = frozenset({
    V_OK, V_UPDATES_WAITING, V_UPDATES_PENDING, V_RENOVATE_STALE,
    V_DASHBOARD_MISSING, V_CONFIG_ERROR, V_LOOKUP_FAILED,
})

# The determinate verdicts that are GREEN. `updates-waiting` is green on
# purpose: an update sitting in a pull request is this estate working as
# designed, not a fault. `renovate-stale` is deliberately NOT here.
GREEN = frozenset({V_OK, V_UPDATES_WAITING})

# What to DO about each verdict, emitted as the body's `next=` line.
#
# EVERY STRING BELOW IS A FIXED LITERAL, chosen at edit time and keyed by a
# member of VERDICTS. That is the one shape rule 4 allows for text in a body: a
# verdict from a fixed enum selects one of a fixed set of sentences, so nothing
# GitHub sent can steer what is written. Do not build one of these by formatting
# in a count, a pull-request number or anything else derived at run time -- the
# numbers already have their own `key=int` lines, and an interpolated `next=`
# would be the first body line that is not literal-or-int.
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
    V_UPDATES_WAITING:
        "none - an open pull request is normal between update sessions;"
        " this line is informational",
    # 105 characters, and it must stay under 120: the existing
    # test_every_action_is_one_line_of_short_printable_ascii caps every entry
    # in this map. It keeps BOTH substrings
    # test_the_four_red_verdicts_name_a_command_or_a_place_to_look asserts on,
    # `gh pr list` and `apply-homelab`.
    V_UPDATES_PENDING:
        "run the update session: gh pr list -R mnbf9rca/kubernetes_config"
        " -A app/renovate, then make apply-homelab",
    # 111 characters. Carried over from the superseded two-check design, in
    # which Renovate's liveness had a UUID of its own; that design never
    # shipped, so searching history for its identifiers finds nothing. The
    # sentence is unchanged and it now lives in the one map, one contract.
    V_RENOVATE_STALE:
        "Renovate has gone quiet - read the Mend job log, then check"
        " renovate.json managerFilePatterns still match files",
    V_DASHBOARD_MISSING:
        "check the Mend Renovate app is still installed on the repo:"
        " github.com/settings/installations",
    V_CONFIG_ERROR:
        "gh issue list -R mnbf9rca/kubernetes_config --author app/renovate"
        " - read it and fix renovate.json",
    # 109 characters. The likeliest fix is a registry `hostRules` entry, so the
    # line names the remedy as well as the place: the count says how many
    # packages, the dashboard says which, and a hostRules entry is what moves
    # them again.
    V_LOOKUP_FAILED:
        "read the Dependency Dashboard repository problems, then add a"
        " renovate.json hostRules entry for that registry",
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
    """The fixed `next=` literal for a verdict. Never remote text (rule 4)."""
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
# NEVER EMIT A PULL-REQUEST TITLE, A RESPONSE BODY OR repr(exc) HERE. See rule 4.

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
    as zero pull requests.
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


# --- classification (rule 1) ------------------------------------------------

def classify(status, headers, body):
    """Return (verdict, items).

    `items` is the parsed JSON array when the verdict is determinate, else None.
    A determinate read returns the sentinel verdict None so the caller's own
    partition decides what it means; every other return is a final verdict.
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
        # This repo cannot legitimately have 100 open issues, and a truncated
        # page would undercount. Refusing to guess is the point.
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


# --- partition (rule 3) -----------------------------------------------------

def partition(items):
    """Split the issue list into the three things this job cares about.

    Returns (pull_requests, dashboard, config_issues) where `dashboard` is the
    Dependency Dashboard issue or None, and `config_issues` is the list of OTHER
    open renovate[bot] non-pull-request issues (a configuration error halts
    Renovate entirely).

    Human issues and human pull requests are ignored: a human pull request must
    never turn this check red.
    """
    pull_requests, dashboard, config_issues = [], None, []
    for item in items:
        if not isinstance(item, dict):
            continue
        user = item.get("user") or {}
        if user.get("login") != RENOVATE_LOGIN:
            continue
        if item.get("pull_request"):
            pull_requests.append(item)
        elif item.get("title") == DASHBOARD_TITLE:
            dashboard = item
        else:
            config_issues.append(item)
    return pull_requests, dashboard, config_issues


# --- the dashboard's repository problems ------------------------------------
#
# THE ONE THING READ OUT OF THE DASHBOARD'S MARKDOWN, and the exception is
# narrow on purpose. Rule 3 refuses to take an INVENTORY from the body -- the
# dashboard-held checkbox list -- because a reworded body would silently
# undercount and the count would still be reported as authoritative. This marker
# is the opposite shape: it can only fail to FIRE. A reword loses one red
# verdict and nothing else; it cannot turn anything green, because no other
# verdict consults the body at all.
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
    be rule 4 exactly.
    """
    body = dashboard.get("body") if isinstance(dashboard, dict) else None
    if not isinstance(body, str):
        return
    for line in body.splitlines():
        if LOOKUP_FAILED_ITEM.search(line) or LOOKUP_FAILED_SECTION.search(line):
            log("dashboard repository problem: " + _clean(line)[:300])


def parse_github_time(text):
    """GitHub's ISO-8601 `...Z` timestamps, or None if unparseable."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def age_days(then, now):
    """Whole days between two datetimes, floored at 0."""
    if then is None or now is None:
        return None
    delta = (now - then).total_seconds()
    if delta < 0:
        return 0
    return int(delta // 86400)


def decide(pull_requests, dashboard, config_issues, now):
    """Return (verdict, facts) for a determinate read.

    `facts` holds only integers, so a caller cannot emit a remote string by
    accident (rule 4).

    PRECEDENCE, and why: a configuration error means Renovate has stopped
    proposing pull requests, so a pull-request count taken during one is not
    trustworthy -- it is reported first. A missing dashboard means the same
    class of doubt. Renovate's own liveness comes next, and here precedence
    changes the COLOUR rather than just the label: a young pull request alone is
    the green `updates-waiting`, so a dead Renovate with one still open must be
    judged stale before the pull-request rules ever run. Only under a dashboard
    that exists and has moved recently does a pull-request count mean what it
    says.

    A FAILED LOOKUP SITS JUST ABOVE THE PULL-REQUEST RULES, for the same reason
    a configuration error sits at the top: a dependency Renovate cannot look up
    proposes nothing, so the pull-request count is an undercount by exactly the
    frozen images. It is below staleness because a Renovate that has stopped
    running altogether is the larger fact, and its dashboard's problem section is
    as stale as the rest of it.
    """
    facts = {"prs_open": len(pull_requests), "config_issues": len(config_issues)}

    if pull_requests:
        numbers = [int(pr.get("number") or 0) for pr in pull_requests]
        oldest = min(n for n in numbers if n > 0) if any(
            n > 0 for n in numbers) else 0
        facts["oldest_pr"] = oldest
        created = [parse_github_time(pr.get("created_at"))
                   for pr in pull_requests]
        ages = [age_days(c, now) for c in created if c is not None]
        if ages:
            facts["oldest_pr_days"] = max(ages)

    failures = None
    if dashboard is not None:
        dash_age = age_days(parse_github_time(dashboard.get("updated_at")), now)
        if dash_age is not None:
            facts["dash_age_days"] = dash_age
        # Recorded whatever the verdict turns out to be: a run that loses the
        # precedence contest to a configuration error still carries the count,
        # and the two causes are related often enough to be worth seeing
        # together.
        failures = count_lookup_failures(dashboard)
        if failures is not None:
            facts["lookup_failures"] = failures

    if config_issues:
        return V_CONFIG_ERROR, facts
    if dashboard is None:
        return V_DASHBOARD_MISSING, facts

    # RENOVATE'S OWN LIVENESS, ABOVE THE PULL-REQUEST RULES. A configuration
    # error and a missing dashboard already outrank it and are more specific,
    # so they are handled first; everything below this point assumes a
    # dashboard that exists.
    dash_age = facts.get("dash_age_days")
    if dash_age is None:
        # The dashboard exists but its timestamp did not parse. That is a read
        # failure about this one field, never evidence that Renovate is alive,
        # so it must not fall through to a GREEN pull-request verdict.
        # Indeterminate: pings /log and changes nothing.
        return V_API_ERROR, facts
    if int(dash_age) > RENOVATE_ALIVE_MAX_DAYS:
        return V_RENOVATE_STALE, facts

    if failures is not None:
        return V_LOOKUP_FAILED, facts

    if pull_requests:
        # An open pull request is normal; an OLD one means a session was
        # skipped. `oldest_pr_days` is absent only when EVERY pull request had
        # an unparseable timestamp.
        #
        # THE ASYMMETRY WITH THE DASHBOARD CLAUSE ABOVE IS DELIBERATE. There, an
        # unparseable timestamp is `api-error`, because the field IS the
        # evidence: with no readable `updated_at` there is nothing left saying
        # Renovate is alive, and defaulting to green would invent that. Here the
        # field is not the evidence -- the pull requests were still counted, so
        # "updates are waiting" is known to be true either way and only their
        # AGE is unreadable. Defaulting to the green `updates-waiting` therefore
        # states something true and merely declines to escalate, where the same
        # default above would state something unknown. Both are tested.
        if facts.get("oldest_pr_days", 0) > PR_AGE_RED_DAYS:
            return V_UPDATES_PENDING, facts
        return V_UPDATES_WAITING, facts
    return V_OK, facts


def ping_suffix(verdict):
    """`0` on a green read, `fail` on a determinate red, `log` otherwise.

    THE THREE-WAY CONTRACT, KEPT AFTER THE MOVE TO kuma. This function no longer
    builds a URL; it is the canonical spelling of the decision, and
    `push_status` below is the same decision in kuma's two-state vocabulary. The
    unit tests assert the two agree, because if they ever disagree the check's
    meaning has quietly forked.

    `log` meant "record an event and change nothing": it could not postpone,
    suppress or trigger an alert, and with no start ping in play it could not arm
    a failure timer either (rules 1 and 2).
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

    now = datetime.now(timezone.utc)
    status, headers, body = fetch(repo)
    verdict, items = classify(status, headers, body)
    if verdict is not None:
        log("indeterminate: %s (http %d)" % (verdict, status))
        return verdict, {"http": int(status)}, len(items or ())

    pull_requests, dashboard, config_issues = partition(items)
    verdict, facts = decide(pull_requests, dashboard, config_issues, now)
    if dashboard is not None:
        # Unconditional, not gated on the verdict: a lookup failure under a
        # configuration error is exactly the run where the names are wanted.
        log_lookup_failures(dashboard)
    log("read %d open issue(s): verdict %s" % (len(items), verdict))
    return verdict, facts, len(items)


def build_message(verdict, facts, run_epoch):
    """Assemble this run's heartbeat message, in order, and return it.

    A FUNCTION RATHER THAN A BLOCK INSIDE `__main__`, so the ORDER below is
    reachable from the test suite. While it lived in `__main__` the budget test
    had to re-implement the order to exercise it, which meant it asserted on its
    own copy and would have stayed green through a reordering of the real thing.

    `-1` is the "not observed on this run" sentinel. An indeterminate run omits
    the count fields entirely rather than emitting a placeholder string like
    `unknown`, which would break the integer-or-enum-literal rule the ping-body
    guard enforces.

    THE ORDER IS A BUDGET, NOT A PREFERENCE, and it is asserted by
    test_run_epoch_and_next_survive_the_cut_for_every_verdict.

    A kuma msg is cut at MSG_LIMIT, and `next=` alone is 89 to 111 characters --
    over half of it. So not everything fits, and what survives has to be decided
    here rather than discovered later. Measured across all ten verdicts on
    2026-08-26, the assembled message ran 130 to 289 characters.
    """
    # Every value below is the result of `int()` on something this script
    # derived, or a member of VERDICTS. Nothing from GitHub reaches here (rule 4).
    prs_open = int(facts.get("prs_open", -1))
    oldest_pr = int(facts.get("oldest_pr", -1))
    oldest_pr_days = int(facts.get("oldest_pr_days", -1))
    dash_age_days = int(facts.get("dash_age_days", -1))
    config_issues = int(facts.get("config_issues", -1))
    lookup_failures = int(facts.get("lookup_failures", -1))
    http = int(facts.get("http", -1))

    hc_summary(verdict)
    # `run_epoch=` goes FIRST, ahead even of `next=`, and that is the fix for a
    # real defect rather than a style choice. It sat last, on the reasoning that
    # kuma timestamps every heartbeat anyway -- but a silence-triggered alert
    # carries the PREVIOUS run's message, and `run_epoch=` is how the reader
    # tells "this message is about this alert" from "the watcher went quiet a
    # day ago". Placed last it was cut from every verdict except `ok`: all four
    # reds and `updates-waiting` ran 259 to 289 characters and lost it, so the
    # field was absent from exactly the cases it exists for, while
    # docs/operations/monitoring.md told the operator to read it. It is 21
    # characters of fixed width, and you must know a message is current before
    # you trust its advice -- so it precedes the advice.
    hc_emit("run_epoch=%d" % run_epoch)
    # SECOND: the command to run next, a fixed literal selected by the verdict
    # (see NEXT_ACTIONS). It sat last under a multi-line body, where the eye
    # landed on it; in a one-line message the tail is what the cut takes.
    #
    # BOUND TO A NAME FIRST, NEVER INLINED. `next_action` is on
    # check-ping-bodies.py's PY_VALUE_ALLOWLIST with a written reason;
    # `next_action_for(...)` inside the sink argument is a CALL, which that
    # guard refuses outright — correctly, since allowing calls there is how a
    # formatted string would get in. Inlining this is exactly the edit the
    # guard caught on 2026-08-26.
    next_action = next_action_for(verdict)
    hc_emit("next=" + next_action)
    # Then the counters, most-acted-on first. On the three longest `next=`
    # strings the cut reaches the tail of this group; every one of them is in
    # the pod log's full body, which is where triage starts.
    #
    # `lookup_failures=` HEADS THE GROUP, ahead even of `prs_open=`, and only a
    # run that found the section emits it at all. Its `next=` is 109 characters,
    # which with `verdict=` and `run_epoch=` leaves 34 characters, so past the
    # second counter it would be cut from the one message it exists for. Every
    # run that saw no repository problem omits it, so heading the group costs
    # the other verdicts nothing.
    if lookup_failures >= 0:
        hc_emit("lookup_failures=%d" % lookup_failures)
    if prs_open >= 0:
        hc_emit("prs_open=%d" % prs_open)
    if oldest_pr_days >= 0:
        hc_emit("oldest_pr_days=%d" % oldest_pr_days)
    if dash_age_days >= 0:
        hc_emit("dash_age_days=%d" % dash_age_days)
    if config_issues >= 0:
        hc_emit("config_issues=%d" % config_issues)
    if oldest_pr >= 0:
        hc_emit("oldest_pr=%d" % oldest_pr)
    if http >= 0:
        hc_emit("http=%d" % http)
    # LAST, and the tokens the cut is meant to take first if anything has to go:
    # the two thresholds this run was judged against, which an alert quotes back
    # so the reader need not open the source -- but which are literals in that
    # source and unchanged between runs, so losing them costs the least.
    hc_emit("pr_age_red_days=%d" % PR_AGE_RED_DAYS)
    hc_emit("renovate_alive_max_days=%d" % RENOVATE_ALIVE_MAX_DAYS)
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
    # indeterminate verdict pushes NOTHING (rule 1), and push() returns without
    # a request when push_status gives None.
    log("heartbeat message (full):\n" + hc_body())
    status = push_status(verdict)
    if status is None:
        log("indeterminate verdict %s: pushing nothing" % verdict)
    make_pusher(os.environ.get("PUSH_URL", ""))(status, msg)
    sys.exit(0)
