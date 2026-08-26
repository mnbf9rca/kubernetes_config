#!/usr/bin/env python3
"""Drive one healthchecks.io check from this repo's Renovate state.

WHY THIS EXISTS
---------------
Every image in the `health` namespace is version- or digest-pinned and keel is
forbidden there, so updates arrive as Renovate pull requests and nothing was
pointing at them. This job counts the open `renovate[bot]` pull requests on this
repo once a day and drives one check: green while updates simply wait, red when
one has waited long enough that an update session was plainly skipped, red when
Renovate itself has gone quiet or is visibly broken, red (through silence) when
this job stops running.

THE FOUR RULES THIS SCRIPT EXISTS TO ENFORCE. Read them before changing anything.

  1. "I COULD NOT LOOK" IS NEVER "EVERYTHING IS FINE". A rate limit, a 404, a
     server error, a timeout, a paginated response or an HTTP 200 carrying a
     JSON *object* are all INDETERMINATE: they ping `/log`, which records an
     event and cannot change the check's status. Counting zero pull requests out
     of any of them would be a confident green over an unread repo.

  2. NOR IS IT "IT FAILED". There is deliberately NO `/start` ping. Upstream
     marks a check down when a start signal is not followed by a success within
     the grace time, and a `/log` ping does not clear `last_start` -- so a
     `/start` plus a single transient GitHub 503 would alert one grace period
     later. Dropping `/start` is what makes rule 1 true. Do not "complete" the
     ping set.

  3. THE DASHBOARD IS IDENTIFIED POSITIVELY, BY TITLE. Renovate opens other
     non-pull-request issues from the same account -- most importantly "Action
     Required: Fix Renovate Configuration", during which it stops proposing pull
     requests entirely. "The renovate[bot] issue that is not a PR" would read
     that as a healthy dashboard and report green while Renovate is halted.

  4. NO REMOTE STRING EVER REACHES THE PING BODY. Every emitted value is the
     result of `int()` on something this script derived, or a member of the
     VERDICTS enum below. A pull-request title is unvalidated remote text and the
     body is read verbatim into every notification transport this account has
     configured. `make check-ping-bodies` enforces it against an explicit
     allowlist of the names below.

Exit status is ALWAYS 0. The exit code would conflate "the job worked" with
"there is nothing to do", and a non-zero exit would trigger a `backoffLimit`
re-run that double-pings for no benefit. Retries live in-script; "the job did not
run at all" is covered by period-plus-grace silence.

ENVIRONMENT VARIABLES ARE RENAMED ON PURPOSE. The CronJob manifest sets
`HC_UUID` from the allowlisted placeholder, and this file names only `HC_UUID`.
Generator files ride the same envsubst stream as every manifest and envsubst
rewrites the bare `$NAME` form too, so naming an `ENVSUBST_VAR_NAMES` entry here
-- even in a comment -- would publish its value inside a ConfigMap. `make
check-script-substitution` enforces the rename; do not "simplify" it away. The
placeholder's real name is in homelab/ops/update-watch.yaml, where substitution
is what is meant to happen.
"""

import json
import os
import re
import sys
import time
import urllib.error
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
# 3). If it ever changes, the check goes red as `dashboard-missing` -- loud, not
# silent, which is the safe failure direction.
DASHBOARD_TITLE = "Dependency Dashboard"

# RED ONLY WHEN A SESSION HAS PLAINLY BEEN SKIPPED. The estate updates in a
# session every 4 to 6 weeks, so an open Renovate pull request is the NORMAL
# state for weeks at a time. The original rule -- red on any open pull request --
# makes red the steady state under that cadence, and an alarm that is normally
# red is not an alarm: it trains the operator to ignore the one time it means
# something. 45 days is a session and a half.
PR_AGE_RED_DAYS = 45

# THE LIVENESS THRESHOLD, ON THIS SAME CHECK. The verdict becomes
# `renovate-stale` when the Dependency Dashboard issue has not been touched in
# this many days. The dashboard's `updated_at` is a stable API field; nothing
# here parses its markdown.
#
# THIS USED TO BE A SECOND CHECK WITH ITS OWN UUID. The argument for splitting
# was that healthchecks.io notifies on status FLIPS and this check was
# permanently red under the old any-open-pull-request rule, so a folded-in
# signal could never fire. `updates-waiting` removes the permanent red, so the
# one check flips on a Renovate death exactly as a second one would have. One
# UUID, one enum. (Ruled 2026-08-26; the account is capped at 20 checks.)
#
# NOT ARMED FROM OBSERVATION YET -- THIS IS THE FLOOR, AND THAT IS DELIBERATE.
# The arming rule is twice the maximum `dash_age_days` seen across the last 30
# ping bodies, floored at 14 days. Read 2026-08-26: this job shipped 2026-08-24
# and its check had logged 6 pings in total, fewer than the 14 bodies the rule
# needs, so there is no observed maximum to double and the floor stands. The
# bodies could not be read individually either -- the only healthchecks.io API
# key in the vault is read-only, and `/api/v3/checks/<uuid>/pings/` refuses it.
# Re-read the ping log after a month of data and re-arm this: a threshold
# tighter than the quiet periods is red every fortnight, and one looser than a
# month lets Renovate die unnoticed.
RENOVATE_ALIVE_MAX_DAYS = 14

# Every verdict this check can emit. A ping body may carry a member of this set
# and nothing else that is not an int.
V_OK = "ok"
V_UPDATES_WAITING = "updates-waiting"
V_UPDATES_PENDING = "updates-pending"
V_RENOVATE_STALE = "renovate-stale"
V_DASHBOARD_MISSING = "dashboard-missing"
V_CONFIG_ERROR = "renovate-config-error"
V_RATE_LIMITED = "rate-limited"
V_SECONDARY_LIMIT = "secondary-limit"
V_REPO_UNREACHABLE = "repo-unreachable"
V_API_ERROR = "api-error"

VERDICTS = frozenset({
    V_OK, V_UPDATES_WAITING, V_UPDATES_PENDING, V_RENOVATE_STALE,
    V_DASHBOARD_MISSING, V_CONFIG_ERROR, V_RATE_LIMITED, V_SECONDARY_LIMIT,
    V_REPO_UNREACHABLE, V_API_ERROR,
})

# The verdicts that mean "the repo was read successfully". Everything else is
# indeterminate and pings /log (rule 1).
DETERMINATE = frozenset({
    V_OK, V_UPDATES_WAITING, V_UPDATES_PENDING, V_RENOVATE_STALE,
    V_DASHBOARD_MISSING, V_CONFIG_ERROR,
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
# Keep them one line, printable ASCII, and short: the body travels verbatim into
# every notification transport this account has configured, and an alert email
# that needs scrolling is an alert nobody reads. The substring `confirm` is
# banned estate-wide (it drives a healthchecks.io UI nag) -- say "check" instead.
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


# --- healthchecks.io ping body ----------------------------------------------
# Same accumulator shape as the health namespace's ingest job: a module-level
# summary slot plus a list of key=value lines, so line 1 of the body is always
# `summary=` whatever order things were emitted in.
#
# NEVER EMIT A PULL-REQUEST TITLE, A RESPONSE BODY OR repr(exc) HERE. See rule 4.

_UNPRINTABLE = re.compile(r"[^\040-\176]")
SUMMARY = ["summary=update-watch FAILED - see pod log"]
BODY_LINES = []


def _clean(text):
    """One line, printable ASCII. Mirrors the shell emitters' `tr -cd`."""
    return _UNPRINTABLE.sub("", str(text))


def hc_summary(text):
    SUMMARY[0] = "summary=" + _clean(text)


def hc_emit(key_value):
    BODY_LINES.append(_clean(key_value))


def hc_body():
    return "\n".join(SUMMARY + BODY_LINES) + "\n"


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

    if dashboard is not None:
        dash_age = age_days(parse_github_time(dashboard.get("updated_at")), now)
        if dash_age is not None:
            facts["dash_age_days"] = dash_age

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

    `log` records an event and changes nothing: it cannot postpone, suppress or
    trigger an alert, and with no `/start` ping in play it cannot arm a failure
    timer either (rules 1 and 2).
    """
    if verdict in GREEN:
        return "0"
    if verdict in DETERMINATE:
        return "fail"
    return "log"


# --- healthchecks.io --------------------------------------------------------

def make_pinger(uuid):
    """Dead-man's-switch pinger. A ping must never be able to fail the job, and
    a body must never cost a ping."""
    def ping(suffix, body=None):
        if not uuid:
            return
        url = "https://hc-ping.com/%s/%s" % (uuid, suffix)
        if body:
            try:
                data = body.encode("ascii", "replace")
                urllib.request.urlopen(url, data=data, timeout=10).close()
                return
            except Exception as exc:               # noqa: BLE001 - best effort
                # FIXED TEXT PLUS A CLASS NAME. Never the URL: the ping URL is
                # the check's own write credential.
                log("healthchecks.io body POST failed (ignored): %s"
                    % type(exc).__name__)
        try:
            urllib.request.urlopen(url, timeout=10).close()
        except Exception as exc:                   # noqa: BLE001 - best effort
            log("healthchecks.io ping failed (ignored): %s"
                % type(exc).__name__)
    return ping


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
    log("read %d open issue(s): verdict %s" % (len(items), verdict))
    return verdict, facts, len(items)


if __name__ == "__main__":
    # `-1` is the "not observed on this run" sentinel throughout this block. An
    # indeterminate run omits the count fields entirely rather than emitting a
    # placeholder string like `unknown`, which would break the
    # integer-or-enum-literal rule the ping-body guard enforces.
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

    # Every value below is the result of `int()` on something this script
    # derived, or a member of VERDICTS. Nothing from GitHub reaches here (rule 4).
    prs_open = int(facts.get("prs_open", -1))
    oldest_pr = int(facts.get("oldest_pr", -1))
    oldest_pr_days = int(facts.get("oldest_pr_days", -1))
    dash_age_days = int(facts.get("dash_age_days", -1))
    config_issues = int(facts.get("config_issues", -1))
    http = int(facts.get("http", -1))

    hc_summary("update-watch rc=0 verdict=%s" % verdict)
    if prs_open >= 0:
        hc_emit("prs_open=%d" % prs_open)
    if oldest_pr >= 0:
        hc_emit("oldest_pr=%d" % oldest_pr)
    if oldest_pr_days >= 0:
        hc_emit("oldest_pr_days=%d" % oldest_pr_days)
    if dash_age_days >= 0:
        hc_emit("dash_age_days=%d" % dash_age_days)
    if config_issues >= 0:
        hc_emit("config_issues=%d" % config_issues)
    if http >= 0:
        hc_emit("http=%d" % http)
    hc_emit("pr_age_red_days=%d" % PR_AGE_RED_DAYS)
    # The liveness threshold this run was judged against, so an alert says what
    # `renovate-stale` was measured with rather than making the reader look it
    # up in the source.
    hc_emit("renovate_alive_max_days=%d" % RENOVATE_ALIVE_MAX_DAYS)
    # A silence-triggered alert carries the PREVIOUS run's body, so every body
    # carries its own run timestamp: an old `run_epoch=` in an alert means "this
    # body is not about this alert; the watcher has gone quiet".
    hc_emit("run_epoch=%d" % run_epoch)
    # Last, so it is the line the eye lands on: the command to run next. A fixed
    # literal selected by the verdict -- see NEXT_ACTIONS.
    next_action = next_action_for(verdict)
    hc_emit("next=" + next_action)

    make_pinger(os.environ.get("HC_UUID", ""))(ping_suffix(verdict), hc_body())
    sys.exit(0)
