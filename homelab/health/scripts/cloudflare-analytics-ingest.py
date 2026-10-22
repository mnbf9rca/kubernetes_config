#!/usr/bin/env python3
"""Pull Cloudflare edge and browser-beacon analytics into InfluxDB.

Cloudflare keeps a bounded window of analytics (31 days of per-zone edge
requests and 184 days of account-level page-load beacons, as measured on
2026-10-21; the job reads the live figure from the GraphQL settings node on
every run rather than assuming either). This job copies those windows into
InfluxDB before they expire, so the record outlives Cloudflare's retention.

Two streams share one bucket and one loop: one edge stream per zone
(`httpRequestsAdaptiveGroups` -> `http_requests`) and one beacon stream for the
account (`rumPageloadEventsAdaptiveGroups` -> `page_loads`).

THE FOUR RULES THIS SCRIPT EXISTS TO ENFORCE. Read them before changing anything.

  1. THE DATA IS ITS OWN WATERMARK. The resume point is max(_time) PER STREAM
     over the `cloudflare` bucket, read back from InfluxDB on every run. There
     is no state file, no PVC and no ConfigMap cursor, because all three can
     disagree with what was actually stored — after a restore, after a manual
     delete, after a partial write. This cannot. The zones themselves are
     enumerated from the Cloudflare API on every run, not configured, so a
     zone with no points at all is new and is seeded from Cloudflare's whole
     retention window.

  2. A FAILED QUERY IS NOT AN EMPTY ONE. Cloudflare answers a broken query with
     HTTP 200 and a non-empty `errors` array in the body. Treating that as "no
     traffic" would advance the watermark over hours that were never fetched and
     lose them permanently once Cloudflare's window rolls past. Zero rows from a
     clean response is a real answer and may advance the watermark; anything else
     may not.

  3. AN UNRECOVERABLE GAP IS LOUD. If an hour after the watermark is older than
     Cloudflare's retention, it is gone and no future run can get it. The job
     logs the exact range, writes an `ingest_gap` marker so the hole is visible
     in Grafana instead of reading as zero traffic, and exits non-zero so the
     `homelab-cloudflare-analytics` monitor is pushed DOWN. Quietly resuming
     would be the same bug as a probe that stays green through an outage.
     Hours AT OR BEFORE the watermark that have fallen behind the floor are not
     a gap: they are stored, and the fetch simply starts at the floor.

  4. COMMIT IN ORDER, AND A FAILURE STOPS ONLY ITS STREAM. Each stream's chunks
     are processed oldest first; a failed chunk ends that stream for this run and
     the other streams carry on, so a zone that fails every run cannot starve the
     zones sorted after it. A stream's watermark is its own, so the failed stream
     resumes from its last committed chunk next run; no watermark can move past
     hours it never fetched. The run still exits non-zero.

Idempotency comes from InfluxDB point overwrite: same measurement, same tag set
and same timestamp replaces. Every run deliberately rewinds OVERLAP_HOURS behind
the watermark so a partially written final hour is rewritten rather than
half-kept.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# --- tunables ---------------------------------------------------------------

# Chunk width for both streams. The settings node reports a 30-day maximum
# query span for the edge dataset and 93 days for the beacon (measured
# 2026-10-21), but chunks stay under a day for two reasons: the 10,000-row cap
# below, and ADAPTIVE SAMPLING COARSENS WITH THE SPAN. The same beacon week
# fetched as one span came back at sampleInterval 10 (44 rows), and as seven
# 1-day windows at sampleInterval 1 (353 rows, 398 loads). Narrow windows are
# what make the stored hours exact.
CHUNK_HOURS = 23

# Chunks per ZONE per run. 21 zones at 8 chunks is the whole call budget below,
# so no one zone can starve the rest; a catch-up longer than 8 chunks converges
# over successive hourly runs. The beacon stream is bounded by the budget alone.
MAX_CHUNKS = 8

# Rewind this far behind the watermark on every run. The final hour of the
# previous run was almost certainly still in progress when it was written.
OVERLAP_HOURS = 2

# Cloudflare's retention is READ FROM THE GRAPHQL SETTINGS NODE on every run
# (`notOlderThan`, in seconds), never assumed: it was 8 days when this job was
# written and 31 days when measured on 2026-10-21. The floor is that instant
# rounded DOWN to the hour plus this margin, so the first hour fetched is
# between one and two hours inside the window and a run that takes most of an
# hour never straddles it. A straddling query is refused loudly (`code:
# quota`), not truncated, but a refused chunk fails its stream for the run.
RETENTION_MARGIN_HOURS = 1

# Both datasets cap a single response at this many rows. A chunk that returns
# exactly this many is assumed truncated and is subdivided.
GRAPHQL_ROW_LIMIT = 10000

# Floor for subdivision. Below this the halving cannot help and the run reports
# an unavoidably truncated window instead of looping forever.
MIN_SUBDIVIDE_SECONDS = 60

# Hard ceiling on GraphQL calls per run. Subdivision is recursive, so a
# pathological chunk could fan out to ~2000 queries and trip Cloudflare's
# 300-queries-per-5-minutes user limit — at which point every remaining query
# fails and the rate limit is burned for the next several hourly runs too.
# Exhausting the budget MID-CHUNK is a loud failure, not a silent truncation:
# the watermark stays put and the next run retries.
MAX_GRAPHQL_CALLS = 180

# A stream does not START a chunk with fewer than this many calls left; it
# stops cleanly and reports itself as catching up instead, so a multi-run
# catch-up (21 zones after an outage, or the beacon's first 184 days) stays
# green. A chunk that fans out past the budget once started still fails loudly:
# a stream that cannot finish one chunk inside the reserve is stuck, and stuck
# must not read as catching up. Sized to one 23-hour chunk of the busiest zone
# after two halvings.
BUDGET_RESERVE = 8

CF_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
HTTP_TIMEOUT = 60

# The edge dataset. `clientRefererHost` is NOT requested: the plan refuses it
# (`code: authz`, "does not have access to the field 'clientrefererhost'",
# measured 2026-10-21) and it is absent from the dataset's availableFields.
# Referrers come from the beacon stream instead.
QUERY = """
query ZoneHourly($tag: string!, $start: Time!, $end: Time!, $limit: Int!) {
  viewer {
    zones(filter: {zoneTag: $tag}) {
      httpRequestsAdaptiveGroups(
        limit: $limit
        filter: {datetime_geq: $start, datetime_lt: $end}
        orderBy: [datetimeHour_ASC]
      ) {
        count
        avg { sampleInterval }
        dimensions {
          datetimeHour
          clientRequestHTTPHost
          clientRequestPath
          clientRequestHTTPMethodName
          edgeResponseStatus
          clientCountryName
          verifiedBotCategory
          securitySource
          userAgent
          edgeResponseContentTypeName
        }
      }
    }
  }
}
"""

# The browser-beacon dataset, account-scoped. `refererPath` is deliberately
# never requested: a referring page's path can carry a person's name or a
# search query, and the host alone answers "where do readers come from".
BEACON_QUERY = """
query AccountHourly($tag: string!, $start: Time!, $end: Time!, $limit: Int!) {
  viewer {
    accounts(filter: {accountTag: $tag}) {
      rumPageloadEventsAdaptiveGroups(
        limit: $limit
        filter: {datetime_geq: $start, datetime_lt: $end}
        orderBy: [datetimeHour_ASC]
      ) {
        count
        sum { visits }
        avg { sampleInterval }
        dimensions {
          datetimeHour
          requestHost
          requestPath
          refererHost
          countryName
          deviceType
        }
      }
    }
  }
}
"""

# One settings query answers both streams' retention. The zone half is read
# from the first zone and applied to all: every zone is on the same plan.
SETTINGS_QUERY = """
query Settings($zoneTag: string!, $accountTag: string!) {
  viewer {
    zones(filter: {zoneTag: $zoneTag}) {
      settings { httpRequestsAdaptiveGroups { notOlderThan } }
    }
    accounts(filter: {accountTag: $accountTag}) {
      settings { rumPageloadEventsAdaptiveGroups { notOlderThan } }
    }
  }
}
"""


class QueryFailed(Exception):
    """The query did not produce a trustworthy answer.

    Distinct from "the query succeeded and there was no traffic". Raising this
    must never advance the watermark.
    """


def log(msg):
    print(msg, flush=True)


# --- heartbeat message ------------------------------------------------------
# A short key=value summary of what this run observed, sent as the `msg` of the
# uptime-kuma heartbeat so an alert answers "what did it see?" without a pod log
# that may have aged out. Same format as the shell emitters; one format across
# the estate is worth more than one job's convenience.
#
# SINCE 2026-08-26 THIS IS ONE LINE, NOT A BODY. healthchecks.io stored an
# arbitrary body; kuma stores a single `msg` string, so the accumulated lines are
# joined with spaces and cut at 200 characters for the push, while the same
# lines are printed to the pod log in full. The counters therefore come FIRST
# and the ISO timestamps last: the cut takes the tail.
#
# NEVER PUT A QueryFailed MESSAGE, A RESPONSE BODY OR repr(exc) IN HERE.
# QueryFailed is raised at ten sites below and those messages splice in
# `zone_tag` - a zone ID from the Cloudflare zone listing, which identifies
# the account and this repo is public - plus up to 800 bytes of raw
# Cloudflare and InfluxDB response (`text[:500]`, `json.dumps(errors)[:800]`).
# `make check-ping-bodies` checks every hc_emit/hc_summary argument against an
# explicit value allowlist. Both sinks keep their names on purpose: that guard
# recognises a sink by FUNCTION NAME and never by the destination host.
#
# The plumbing is a module-level accumulator that main() appends to and the
# __main__ block joins, because the push is made at module scope while every
# emittable value lives in main()'s locals - and main() must keep returning an
# int, which the test suite asserts on. Do not refactor main() to return a
# tuple.
#
# SUMMARY is a one-element list rather than a `global` so that the FIRST token
# is always `verdict=`, whatever order things were emitted in - and it defaults
# to a failure, so a run that never reaches a verdict cannot report success.
#
# THE VERDICT IS A FIXED ENUM: ok | incomplete | gap | failed. hc_summary is
# called with a bare member of it and nothing else; the prose that used to live
# in the summary line is in the log() calls beside each branch.
_UNPRINTABLE = re.compile(r"[^\040-\176]")
VERDICTS = ("ok", "incomplete", "gap", "failed")
SUMMARY = ["verdict=failed"]
BODY_LINES = []


def _clean(text):
    """One line, printable ASCII. Mirrors the shell emitters' `tr -cd`."""
    return _UNPRINTABLE.sub("", str(text))


def hc_summary(text):
    """Set the run's verdict. The argument MUST be a member of VERDICTS.

    Enforced here rather than trusted, because the enum was otherwise dead in
    this module -- only the test suite read it, and nothing bound the seven call
    sites (covering four distinct verdicts, `failed` from four of them) to
    membership, so one could have drifted off the enum and every test would
    still have passed. A drifted verdict is coerced to `failed` and
    logged: it must not raise, because a message may never cost a push, and
    `failed` is the safe direction for a value nobody can classify.
    """
    verdict = _clean(text)
    if verdict not in VERDICTS:
        log("BUG: %r is not a member of VERDICTS; reporting failed" % verdict)
        verdict = "failed"
    SUMMARY[0] = "verdict=" + verdict


def hc_emit(key_value):
    BODY_LINES.append(_clean(key_value))


def hc_body():
    """Every line, for the pod log."""
    return "\n".join(SUMMARY + BODY_LINES) + "\n"


# What kuma stores in a heartbeat's `msg` column.
MSG_LIMIT = 200


def kuma_msg():
    """The same lines as ONE line, cut to what kuma stores.

    THE CUT LANDS ON A TOKEN BOUNDARY, NEVER MID-TOKEN, so a truncated message
    ends with the last whole `key=value` pair rather than with half of one.
    """
    joined = " ".join(SUMMARY + BODY_LINES)
    if len(joined) <= MSG_LIMIT:
        return joined
    return joined[:MSG_LIMIT].rsplit(" ", 1)[0]


def env(name, default=None):
    val = os.environ.get(name, default)
    if val is None or val == "":
        raise SystemExit("FATAL: %s is unset" % name)
    return val


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def rewind(watermark):
    """Where a run resumes: OVERLAP_HOURS behind the watermark, FLOORED TO THE HOUR.

    Cloudflare applies datetime_geq to the raw request time and only then
    groups by datetimeHour, so a start of 18:37 returns hour 18 as the
    18:37..19:00 slice, and the write then overwrites that hour's full count
    with the slice (measured 2026-10-20: 1982 requests from :00, 568 from :37).
    The watermark is the previous run's wall-clock minute, so without the floor
    every hour two behind a run was cut down to its last 23 minutes.
    """
    return floor_hour(watermark - timedelta(hours=OVERLAP_HOURS))


def retention_floor(now, not_older_than_seconds):
    """The first hour a run may fetch: Cloudflare's floor, rounded down to the
    hour, plus RETENTION_MARGIN_HOURS. Constant across a clock hour, so a
    delete-and-refetch can name it in advance."""
    return (floor_hour(now - timedelta(seconds=not_older_than_seconds))
            + timedelta(hours=RETENTION_MARGIN_HOURS))


def resume(watermark, floor):
    """(start, missing_hours) for one stream.

    A new stream seeds from the floor. An existing one rewinds, but never below
    the floor: hours at or before the watermark that have fallen behind it are
    STORED and stay as they are. Only hours strictly after the watermark's hour
    that are older than the floor are missing, and those are the gap (rule 3).
    Before 2026-10-21 any rewind below the floor was reported as a gap, which
    raised a false alarm whenever the two-hour overlap alone crossed it.
    """
    if watermark is None:
        return floor, 0
    next_hour = floor_hour(watermark) + timedelta(hours=1)
    if next_hour < floor:
        return floor, int((floor - next_hour).total_seconds() // 3600)
    return max(rewind(watermark), floor), 0


# --- HTTP -------------------------------------------------------------------

def _send(req, timeout):
    """Return (status, body_text). Transport errors raise QueryFailed.

    urllib raises HTTPError for non-2xx, which is still a response worth reading:
    Cloudflare and InfluxDB both put the useful diagnostic in the error body.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:                       # noqa: BLE001 - transport
        raise QueryFailed(
            "transport error contacting %s: %r" % (req.full_url, exc))


def http_post(url, body, headers, timeout=HTTP_TIMEOUT):
    return _send(urllib.request.Request(
        url, data=body, headers=headers, method="POST"), timeout)


def http_get(url, headers, timeout=HTTP_TIMEOUT):
    return _send(urllib.request.Request(
        url, headers=headers, method="GET"), timeout)


# --- Cloudflare -------------------------------------------------------------

CF_ZONES_URL = "https://api.cloudflare.com/client/v4/zones"


def cf_zones(token):
    """([(name, tag), ...] sorted by name, account_tag) for everything the token sees.

    Enumerated on every run rather than configured, so a zone added to the
    account is ingested from its next run without anyone editing a list; the
    token therefore carries Zone:Read beside Zone.Analytics:Read. Pagination is
    honoured because an account can outgrow one page. Anything short of a
    clean, complete listing raises QueryFailed: a silently shortened list would
    skip a zone, which is the same loss as treating a failed query as empty.

    The account tag comes from the same listing (`result[].account.id`); the
    beacon stream is account-scoped, and this job handles exactly one account.
    """
    headers = {"Authorization": "Bearer %s" % token}
    zones = []
    accounts = set()
    page = 1
    while True:
        status, text = http_get(
            "%s?per_page=50&page=%d" % (CF_ZONES_URL, page), headers)
        try:
            doc = json.loads(text)
        except ValueError:
            raise QueryFailed(
                "zone listing HTTP %s, body is not JSON: %s" % (status, text[:500]))
        if status < 200 or status >= 300 or not doc.get("success"):
            raise QueryFailed(
                "zone listing HTTP %s: %s"
                % (status, json.dumps(doc.get("errors"))[:800]))
        for zone in doc.get("result") or []:
            name, tag = zone.get("name"), zone.get("id")
            account = (zone.get("account") or {}).get("id")
            if not name or not tag or not account:
                raise QueryFailed(
                    "zone listing entry without name, id or account: %s"
                    % json.dumps(zone)[:200])
            zones.append((name, tag))
            accounts.add(account)
        info = doc.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            break
        page += 1
    if not zones:
        raise QueryFailed("the token can see no zones at all")
    if len(accounts) != 1:
        raise QueryFailed(
            "zones span %d accounts; the beacon stream expects exactly one"
            % len(accounts))
    return sorted(zones), accounts.pop()


def cf_graphql(token, query, variables, budget):
    """POST one GraphQL query and return its `viewer`, or raise QueryFailed."""
    if budget["used"] >= MAX_GRAPHQL_CALLS:
        raise QueryFailed(
            "GraphQL call budget of %d exhausted; refusing to keep querying"
            % MAX_GRAPHQL_CALLS)
    budget["used"] += 1

    payload = json.dumps({"query": query, "variables": variables}).encode()
    headers = {
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
    }

    status, text = http_post(CF_GRAPHQL_URL, payload, headers)

    try:
        doc = json.loads(text)
    except ValueError:
        raise QueryFailed(
            "HTTP %s, body is not JSON: %s" % (status, text[:500]))

    # THE 200-WITH-ERRORS TRAP. Cloudflare's GraphQL endpoint reports query
    # failures — bad token, unknown dimension, window too wide, rate limit — in
    # an `errors` array with a 200 status line. Checking the status code alone
    # turns every one of those into "no traffic this hour".
    errors = doc.get("errors")
    if errors:
        raise QueryFailed(
            "Cloudflare returned errors (HTTP %s): %s"
            % (status, json.dumps(errors)[:800]))

    if status < 200 or status >= 300:
        raise QueryFailed("HTTP %s: %s" % (status, text[:500]))

    try:
        viewer = doc["data"]["viewer"]
    except (KeyError, TypeError):
        raise QueryFailed("unexpected response shape: %s" % text[:500])
    if not isinstance(viewer, dict):
        raise QueryFailed("`viewer` is not an object: %s" % text[:500])
    return viewer


def _scope(viewer, scope, tag):
    """The one zone or account object under `viewer`, or raise QueryFailed."""
    objs = viewer.get(scope)
    if not isinstance(objs, list):
        raise QueryFailed("`%s` is not a list: %s"
                          % (scope, json.dumps(viewer)[:500]))
    if not objs:
        # An empty list means the token cannot see this zone or account at
        # all, which is a configuration fault, not an absence of traffic.
        raise QueryFailed(
            "%s %s returned no object — token cannot read it" % (scope, tag))
    return objs[0]


def cf_retention(token, zone_tag, account_tag, budget):
    """(edge_seconds, beacon_seconds): each dataset's `notOlderThan`."""
    viewer = cf_graphql(token, SETTINGS_QUERY,
                        {"zoneTag": zone_tag, "accountTag": account_tag}, budget)
    try:
        edge = int(_scope(viewer, "zones", zone_tag)["settings"]
                   ["httpRequestsAdaptiveGroups"]["notOlderThan"])
        beacon = int(_scope(viewer, "accounts", account_tag)["settings"]
                     ["rumPageloadEventsAdaptiveGroups"]["notOlderThan"])
    except (KeyError, TypeError, ValueError):
        raise QueryFailed("settings node is missing notOlderThan: %s"
                          % json.dumps(viewer)[:500])
    if edge <= 0 or beacon <= 0:
        raise QueryFailed("settings node reports a non-positive retention")
    return edge, beacon


# The two datasets: (query, scope under `viewer`, groups field).
EDGE = (QUERY, "zones", "httpRequestsAdaptiveGroups")
BEACON = (BEACON_QUERY, "accounts", "rumPageloadEventsAdaptiveGroups")


def cf_query(token, tag, start, end, limit, budget, dataset=EDGE):
    """Return the raw group rows for one zone (or account) and window.

    Raises QueryFailed for every outcome that is not a clean, complete answer.
    """
    query, scope, groups = dataset
    viewer = cf_graphql(token, query, {
        "tag": tag, "start": iso(start), "end": iso(end), "limit": limit,
    }, budget)
    rows = _scope(viewer, scope, tag).get(groups)
    if rows is None:
        raise QueryFailed("no %s in %s" % (groups, json.dumps(viewer)[:500]))
    return rows


def cf_fetch(token, tag, start, end, warnings, budget, dataset=EDGE):
    """Fetch a window, subdividing when the response looks truncated.

    Cloudflare offers no cursor for these datasets, so the only way past the
    row cap is a narrower window. Halving works because the caller aggregates:
    two half-windows summed give the same per-hour totals as one whole window.
    """
    rows = cf_query(token, tag, start, end, GRAPHQL_ROW_LIMIT, budget, dataset)
    if len(rows) < GRAPHQL_ROW_LIMIT:
        return rows

    span = int((end - start).total_seconds())
    if span <= MIN_SUBDIVIDE_SECONDS:
        warnings.append(
            "TRUNCATED: %s %s window %s..%s returned the %d-row cap at the "
            "%ds subdivision floor; this window's data is incomplete"
            % (dataset[1], tag, iso(start), iso(end), GRAPHQL_ROW_LIMIT, span))
        return rows

    mid = start + timedelta(seconds=span // 2)
    log("  row cap hit for %s %s..%s — subdividing at %s"
        % (tag, iso(start), iso(end), iso(mid)))
    return (cf_fetch(token, tag, start, mid, warnings, budget, dataset)
            + cf_fetch(token, tag, mid, end, warnings, budget, dataset))


# --- shaping ----------------------------------------------------------------

def truncate_path(path, keep_full):
    """Collapse a path to its first two segments unless the host is allowlisted.

    Path is the highest-cardinality dimension by far — karakeep alone emits a
    distinct path per asset UUID — and unbounded tag cardinality is how an
    InfluxDB instance dies. `/*` is appended when segments were dropped so that
    a request for exactly `/api/v1` stays distinguishable from one for something
    beneath it.
    """
    if not path:
        return "/"
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = "".join(ch for ch in path if ch.isprintable())
    if not path.startswith("/"):
        path = "/" + path
    if keep_full:
        return path[:256]
    parts = [p for p in path.split("/") if p != ""]
    if not parts:
        return "/"
    head = "/" + "/".join(parts[:2])
    if len(parts) > 2:
        head += "/*"
    return head[:256]


# Path prefixes, per zone NAME as Cloudflare lists it (`name` in GET /zones),
# whose next segment identifies a person. Everything beneath them collapses to
# `<prefix>*` before the series key is built, so the raw paths never reach
# InfluxDB and their counts sum into the one masked series.
PRIVATE_PATH_PREFIXES = {
    "family-foqos.app": ("/profile/", "/navigate/"),
}


def mask_private(zone_name, path):
    """Collapse a person-identifying path to its prefix for the zones above."""
    for prefix in PRIVATE_PATH_PREFIXES.get(zone_name, ()):
        if path.startswith(prefix):
            return prefix + "*"
    return path


def parse_hour(value):
    """Cloudflare's datetimeHour, e.g. 2026-08-20T10:00:00Z, to a UTC datetime."""
    if not value:
        raise QueryFailed("row is missing datetimeHour")
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        raise QueryFailed("unparseable datetimeHour %r" % value)


def aggregate(rows, zone_name, full_path_hosts, acc):
    """Fold Cloudflare rows into acc, keyed by the output series.

    Aggregation is not an optimisation, it is required twice over: path
    truncation merges several source paths into one series, and subdivision
    splits one hour across several responses. Both need summation.

    sample_interval is averaged weighted by count, because that is what an
    average sampling interval over a merged set of requests means. It is stored,
    never applied: whether `count` is already extrapolated is a property of
    Cloudflare's dataset, not something this job should silently assume. See
    docs/operations/homelab-health.md.
    """
    for row in rows:
        dims = row.get("dimensions") or {}
        hour = parse_hour(dims.get("datetimeHour"))
        host = dims.get("clientRequestHTTPHost") or "unknown"
        keep_full = host in full_path_hosts
        # The raw user agent is NEVER stored: it is unbounded, attacker-chosen
        # text and would be a cardinality bomb. It is collapsed here to the
        # one class the estate asks about - its own uptime-kuma probes - and
        # `other`. It is still requested as a dimension so Cloudflare splits
        # the groups by it, which is what makes the collapse possible.
        agent = dims.get("userAgent") or ""
        client = "uptime-kuma" if agent.startswith("Uptime-Kuma/") else "other"
        key = (
            zone_name,
            host,
            mask_private(zone_name, truncate_path(
                dims.get("clientRequestPath"), keep_full)),
            str(dims.get("edgeResponseStatus", "unknown")),
            dims.get("clientCountryName") or "unknown",
            dims.get("clientRequestHTTPMethodName") or "unknown",
            dims.get("verifiedBotCategory") or "none",
            dims.get("securitySource") or "unknown",
            client,
            dims.get("edgeResponseContentTypeName") or "none",
            int(hour.timestamp()),
        )
        count = int(row.get("count") or 0)
        interval = float((row.get("avg") or {}).get("sampleInterval") or 1.0)
        total, weighted = acc.get(key, (0, 0.0))
        acc[key] = (total + count, weighted + count * interval)


# Beacon hosts whose page loads are never stored, by operator ruling: the app
# is a family's, and its referrers and paths are theirs. Matched on the zone
# apex and every host beneath it.
BEACON_EXCLUDED_ZONES = ("family-foqos.app",)


def beacon_excluded(host):
    return any(host == z or host.endswith("." + z) for z in BEACON_EXCLUDED_ZONES)


def aggregate_beacon(rows, full_path_hosts, acc):
    """Fold beacon rows into acc, keyed by the page_loads series.

    Same shape as aggregate(): paths truncate the same way, counts and visits
    sum, sample_interval is count-weighted and stored, never applied. `count`
    is ALREADY EXTRAPOLATED by Cloudflare (measured 2026-10-21: a 7-day span
    returned 44 rows at sampleInterval 10 with count 440, and every
    single-sample row carries count == sampleInterval), so multiplying by the
    interval would apply the sampling twice. `refererPath` is never requested.
    """
    for row in rows:
        dims = row.get("dimensions") or {}
        hour = parse_hour(dims.get("datetimeHour"))
        host = dims.get("requestHost") or "unknown"
        if beacon_excluded(host):
            continue
        key = (
            host,
            truncate_path(dims.get("requestPath"), host in full_path_hosts),
            dims.get("refererHost") or "none",
            dims.get("countryName") or "unknown",
            dims.get("deviceType") or "unknown",
            int(hour.timestamp()),
        )
        count = int(row.get("count") or 0)
        visits = int((row.get("sum") or {}).get("visits") or 0)
        interval = float((row.get("avg") or {}).get("sampleInterval") or 1.0)
        total, seen, weighted = acc.get(key, (0, 0, 0.0))
        acc[key] = (total + count, seen + visits, weighted + count * interval)


# --- line protocol ----------------------------------------------------------

def esc_tag(value):
    """Escape a tag key or value: comma, equals, space, backslash.

    Control characters are STRIPPED before escaping, not escaped. Line
    protocol has no escape for a newline inside a tag value: a newline ends
    the point, so one arriving in a tag would split the body and inject an
    arbitrary extra point. `clientRequestHTTPHost` is derived from the
    client's Host header, which makes this attacker-influenced input, and a
    forged point is silent data corruption rather than a loud failure.
    str.isprintable() keeps the ASCII space, which is escaped below.
    """
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return (text
            .replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace("=", "\\=")
            .replace(" ", "\\ ")) or "unknown"


def points(acc):
    """Yield line protocol for every accumulated series, OLDEST FIRST.

    The timestamp ordering is load-bearing, not cosmetic. influx_write() sends
    these in batches, so a later batch can fail with earlier ones already
    durably stored; the watermark is max(_time) over what IS stored, and the
    next run rewinds only OVERLAP_HOURS behind it. Ordered by anything else —
    this previously sorted on the tag tuple, zone first and timestamp last — a
    surviving first batch carries points from the END of a 23-hour chunk while
    hours near its START go unwritten. The watermark then jumps past them, the
    2-hour rewind does not reach back far enough, and Cloudflare deletes them:
    silent permanent loss of exactly the hours this job exists to preserve.

    Sorting by timestamp makes any partial commit a PREFIX of the chunk, which
    is the one shape the rewind can recover from. The remaining key fields are
    kept in the sort only to make the output deterministic.
    """
    def oldest_first(item):
        return (item[0][-1],) + item[0][:-1]

    for key, (count, weighted) in sorted(acc.items(), key=oldest_first):
        (zone, host, path, status, country, method,
         bot_category, security_source, client, content_type, ts) = key
        interval = (weighted / count) if count else 1.0
        yield ("http_requests,zone=%s,host=%s,path=%s,status=%s,country=%s,"
               "method=%s,bot_category=%s,security_source=%s,client=%s,"
               "content_type=%s count=%di,sample_interval=%.6f %d"
               % (esc_tag(zone), esc_tag(host), esc_tag(path),
                  esc_tag(status), esc_tag(country), esc_tag(method),
                  esc_tag(bot_category), esc_tag(security_source),
                  esc_tag(client), esc_tag(content_type), count, interval, ts))


def beacon_points(acc):
    """Line protocol for the page_loads series, oldest first (see points()).

    `source=beacon` is on every point so the stream's watermark is its own
    data, exactly as a zone's is: influx_watermarks() groups on it.
    """
    def oldest_first(item):
        return (item[0][-1],) + item[0][:-1]

    for key, (count, visits, weighted) in sorted(acc.items(), key=oldest_first):
        host, path, referer_host, country, device, ts = key
        interval = (weighted / count) if count else 1.0
        yield ("page_loads,source=beacon,host=%s,path=%s,referer_host=%s,"
               "country=%s,device=%s count=%di,visits=%di,sample_interval=%.6f %d"
               % (esc_tag(host), esc_tag(path), esc_tag(referer_host),
                  esc_tag(country), esc_tag(device), count, visits, interval, ts))


# --- InfluxDB ---------------------------------------------------------------

def influx_write(cfg, lines):
    if not lines:
        return
    url = ("%s/api/v2/write?org=%s&bucket=%s&precision=s"
           % (cfg["url"], cfg["org"], cfg["bucket"]))
    headers = {
        "Authorization": "Token %s" % cfg["token"],
        "Content-Type": "text/plain; charset=utf-8",
    }
    # Batched so one oversized body cannot be rejected wholesale, and so a
    # partial failure still leaves earlier hours durably stored. That second
    # property depends entirely on `lines` arriving oldest-first — see points().
    # The caller appends its ingest_status marker last, after every data point,
    # for the same reason.
    for i in range(0, len(lines), 5000):
        batch = "\n".join(lines[i:i + 5000]).encode()
        status, text = http_post(url, batch, headers)
        if status < 200 or status >= 300:
            raise QueryFailed("InfluxDB write HTTP %s: %s" % (status, text[:500]))


def influx_watermarks(cfg):
    """{(zone, source): newest `count` point with those tags}; {} if never written.

    Edge data points carry `zone` and no `source`; edge markers carry both
    (`source=cloudflare`); the beacon's points and marker carry
    `source=beacon` and no `zone`. main() folds that into one watermark per
    zone and one for the beacon. The pair ("", "cloudflare") is the global
    ingest_status marker from before 2026-10-20 and is ignored.

    DO NOT "tidy" this into a bare group()/keep() over every field. That form
    collides whenever two fields in the bucket have different value types
    ("schema collision: cannot group float and string types together") and the
    resulting error body parses as "no data", which is how ingest-freshness
    reported STALE for 25 straight days. Filtering to a single integer field
    before grouping is what makes the merge type-safe.

    `_field == "count"` covers http_requests, page_loads and ingest_status, so
    an hour with genuinely zero traffic still moves a stream's watermark. It
    deliberately does NOT cover ingest_gap's `missing_hours`: a gap marker
    records a hole, it must never claim the hole was filled.
    """
    flux = (
        'from(bucket:"%s")\n'
        '  |> range(start: 1970-01-01T00:00:00Z)\n'
        '  |> filter(fn: (r) => r._field == "count")\n'
        '  |> group(columns: ["zone", "source"])\n'
        '  |> max(column: "_time")\n'
        '  |> keep(columns: ["zone", "source", "_time"])\n' % cfg["bucket"]
    )
    url = "%s/api/v2/query?org=%s" % (cfg["url"], cfg["org"])
    headers = {
        "Authorization": "Token %s" % cfg["token"],
        "Content-Type": "application/vnd.flux",
        "Accept": "application/csv",
    }
    status, text = http_post(url, flux.encode(), headers, timeout=60)
    if status < 200 or status >= 300:
        raise QueryFailed("watermark query HTTP %s: %s" % (status, text[:500]))
    # An InfluxDB error arrives as a JSON body even on some 2xx paths.
    if text.lstrip().startswith("{") and '"code"' in text:
        raise QueryFailed("watermark query returned an error: %s" % text[:500])

    # Annotated CSV: annotations start "#", and EVERY TABLE HAS ITS OWN HEADER
    # (",result,table,...") because a group lacking a tag has no column for it
    # at all. Columns are therefore read per header, never by position.
    newest = {}
    columns = None
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        cells = line.split(",")
        if len(cells) > 1 and cells[1] == "result":
            columns = cells
            continue
        if columns is None:
            continue
        cells = dict(zip(columns, cells))
        cell = cells.get("_time", "").strip()
        if not cell:
            continue
        try:
            parsed = datetime.fromisoformat(cell.replace("Z", "+00:00"))
        except ValueError:
            raise QueryFailed("watermark row has unparseable _time %r" % cell)
        parsed = parsed.astimezone(timezone.utc)
        key = (cells.get("zone", "").strip(), cells.get("source", "").strip())
        if key not in newest or parsed > newest[key]:
            newest[key] = parsed
    return newest


# --- uptime-kuma push -------------------------------------------------------

# Any non-default agent will do; what matters is that it is not urllib's own.
# See the comment inside push() for the Cloudflare rule that makes it matter.
PUSH_USER_AGENT = "kubernetes-config-cloudflare-analytics"


def make_pusher(push_url):
    """Heartbeat pusher for an uptime-kuma PUSH monitor.

    A push must never be able to fail the job, and a message must never cost a
    push - the same two properties the healthchecks.io pinger this replaces was
    built around, and the reason neither is three lines.

    THERE IS NO `start` PUSH AND NO `log` PUSH. The push API has two states and
    no third kind, so `activeDeadlineSeconds` is the whole of the hang bound and
    the monitor's heartbeat interval plus retry is the silence bound. Every path
    in this job is determinate - it either ingested or it did not - so it always
    pushes exactly one heartbeat, `up` or `down`.
    """
    def push(status, msg=""):
        if not push_url:
            return
        try:
            # THE ENCODE IS INSIDE THE TRY, for the same reason it was in the
            # pinger: evaluated on the line before urlopen, an encoding error
            # would propagate out of push() past sys.exit(rc) and the heartbeat
            # would be lost - a message costing a push, in the one emitter with
            # an exception mechanism to do it with.
            query = urllib.parse.urlencode(
                {"status": status, "msg": str(msg)[:200]})
            # THE User-Agent IS LOAD-BEARING AND IS NOT COSMETIC. uptime-kuma
            # sits behind Cloudflare, which answers urllib's DEFAULT
            # `Python-urllib/3.x` agent with HTTP 403 and `error code: 1010`
            # before the request ever reaches kuma. Measured in-cluster on
            # 2026-08-26: the default agent got 403/1010 and a named one got
            # kuma's own 404 for a bogus token, from the same URL in the same
            # process. Every shell runner in the estate pushes with curl or
            # wget and is unaffected, so this trap is Python-only - and it is
            # SILENT, because a push failure is swallowed by design. Do not
            # drop this header.
            request = urllib.request.Request(
                push_url + "?" + query, headers={"User-Agent": PUSH_USER_AGENT})
            urllib.request.urlopen(request, timeout=10).close()
        except Exception as exc:               # noqa: BLE001 - best effort
            # THE CLASS NAME ONLY, never repr(exc), and never the URL: the
            # exception in hand may be a QueryFailed whose message carries a
            # zone tag or a response body, and the push URL carries the
            # monitor's token as its last path segment.
            log("uptime-kuma push %r failed (ignored): %s"
                % (status, type(exc).__name__))
    return push


# --- main -------------------------------------------------------------------

def main():
    cf_token = env("CF_API_TOKEN")
    full_path_hosts = {
        h.strip() for h in os.environ.get("FULL_PATH_HOSTS", "").split(",")
        if h.strip()
    }
    influx = {
        "url": os.environ.get(
            "INFLUX_URL", "http://influxdb.health.svc.cluster.local:8086"),
        "org": os.environ.get("INFLUX_ORG", "cynexia"),
        "bucket": os.environ.get("INFLUX_BUCKET", "cloudflare"),
        "token": env("INFLUX_TOKEN"),
    }

    now = datetime.now(timezone.utc).replace(microsecond=0)
    budget = {"used": 0}

    zones, account_tag = cf_zones(cf_token)       # raises -> run fails, no writes
    log("%d zone(s) visible to the token" % len(zones))
    edge_seconds, beacon_seconds = cf_retention(
        cf_token, zones[0][1], account_tag, budget)
    edge_floor = retention_floor(now, edge_seconds)
    beacon_floor = retention_floor(now, beacon_seconds)
    log("retention: edge %dh (floor %s), beacon %dh (floor %s)"
        % (edge_seconds // 3600, iso(edge_floor),
           beacon_seconds // 3600, iso(beacon_floor)))

    marks = influx_watermarks(influx)             # raises -> run fails, no writes
    # Fold (zone, source) pairs into one watermark per stream. The pair
    # ("", "cloudflare") is the zone-less ingest_status marker from before
    # 2026-10-20, when the watermark was one value for every zone. It is NOT a
    # floor for any zone: it was used as one on the first enumerated run and
    # lifted the 19 new zones past hours their first run had not reached
    # (14:46 to 19:00 UTC that day, never fetched). Only a stream's own points
    # may say where it resumes.
    watermarks = {}
    for (zone, source), newest in marks.items():
        key = "beacon" if source == "beacon" else zone
        if key and (key not in watermarks or newest > watermarks[key]):
            watermarks[key] = newest

    # One stream per zone, then the beacon. `tags` is the tag set of the
    # stream's ingest_status and ingest_gap markers, and the field in which
    # its watermark is found again.
    streams = [{
        "name": zone_name, "tag": zone_tag, "dataset": EDGE,
        "floor": edge_floor, "chunk_cap": MAX_CHUNKS,
        "tags": "source=cloudflare,zone=%s" % esc_tag(zone_name),
    } for zone_name, zone_tag in zones]
    streams.append({
        "name": "beacon", "tag": account_tag, "dataset": BEACON,
        "floor": beacon_floor, "chunk_cap": MAX_GRAPHQL_CALLS,
        "tags": "source=beacon",
    })

    warnings = []
    gaps = []                      # (stream, g_start, g_end, missing_hours)
    failure = None                 # the first failed chunk, for the log
    failures = 0                   # streams that failed a chunk this run
    chunks_done = 0
    rows_total = 0
    series_total = 0
    behind = 0                     # streams with chunks still outstanding
    committed_through = None       # newest chunk end any stream committed
    gap_marker = "not-needed"
    beacon_state = "ok"            # ok | behind | failed, a fixed enum

    for stream in streams:
        name, floor = stream["name"], stream["floor"]
        watermark = watermarks.get(name)
        start, missing_hours = resume(watermark, floor)
        if watermark is None:
            # New stream, or a restored-from-empty bucket. Nothing was lost,
            # because nothing was ever held. Start at the oldest data
            # Cloudflare still has.
            log("%s: no watermark, seeding from %s" % (name, iso(start)))
        else:
            log("%s: watermark %s, resuming from %s"
                % (name, iso(watermark), iso(start)))
        if missing_hours:
            g_start = floor_hour(watermark) + timedelta(hours=1)
            gaps.append((stream, g_start, floor, missing_hours))
            log("!!! %s: UNRECOVERABLE GAP %s .. %s (%d hours) is older than "
                "Cloudflare's retention and can never be fetched; writing an "
                "ingest_gap marker and exiting non-zero so the monitor is "
                "pushed down" % (name, iso(g_start), iso(floor), missing_hours))

        cursor = start
        stream_chunks = 0
        stream_failure = None
        while cursor < now and stream_chunks < stream["chunk_cap"]:
            if budget["used"] > MAX_GRAPHQL_CALLS - BUDGET_RESERVE:
                log("  %s: budget reserve reached; stopping cleanly" % name)
                break
            chunk_end = min(cursor + timedelta(hours=CHUNK_HOURS), now)
            acc = {}
            try:
                rows = cf_fetch(cf_token, stream["tag"], cursor, chunk_end,
                                warnings, budget, stream["dataset"])
            except QueryFailed as exc:
                stream_failure = "%s chunk %s..%s: %s" % (
                    name, iso(cursor), iso(chunk_end), exc)
                log("  FAILED: %s" % exc)
                break
            rows_total += len(rows)
            if stream["dataset"] is BEACON:
                aggregate_beacon(rows, full_path_hosts, acc)
                lines = list(beacon_points(acc))
            else:
                aggregate(rows, name, full_path_hosts, acc)
                lines = list(points(acc))
            # An empty chunk is a real answer and must still move this
            # stream's watermark, otherwise a genuinely quiet month would be
            # indistinguishable from a month of broken ingestion and would
            # raise a false gap alarm. Appended AFTER every data point: see
            # points() for why the order matters.
            lines.append(
                "ingest_status,%s count=%di,chunk_seconds=%di %d"
                % (stream["tags"], len(acc),
                   int((chunk_end - cursor).total_seconds()),
                   int(chunk_end.timestamp()) - 1))
            try:
                influx_write(influx, lines)
            except QueryFailed as exc:
                stream_failure = "%s write %s..%s: %s" % (
                    name, iso(cursor), iso(chunk_end), exc)
                log("  WRITE FAILED: %s" % exc)
                break

            log("  %s: %s .. %s: %d rows, %d series"
                % (name, iso(cursor), iso(chunk_end), len(rows), len(acc)))
            series_total += len(acc)
            chunks_done += 1
            stream_chunks += 1
            if committed_through is None or chunk_end > committed_through:
                committed_through = chunk_end
            cursor = chunk_end
            if cursor < now:
                time.sleep(1)          # stay clear of the GraphQL rate limit
        if stream_failure:
            # THE OTHER STREAMS CARRY ON. Each stream's watermark is its own,
            # so one that fails on every run cannot starve those sorted after
            # it; it resumes from its last committed chunk next run. The run
            # still exits non-zero, and reports the FIRST failure.
            failures += 1
            failure = failure or stream_failure
            if stream["dataset"] is BEACON:
                beacon_state = "failed"
            continue
        if cursor < now:
            behind += 1
            if stream["dataset"] is BEACON:
                beacon_state = "behind"
            log("  %s: %s .. %s outstanding; the next run continues from "
                "this stream's watermark" % (name, iso(cursor), iso(now)))

    if gaps:
        try:
            influx_write(influx, [
                "ingest_gap,%s,reason=retention "
                "missing_hours=%di,gap_end=%di %d"
                % (stream["tags"], hours, int(g_end.timestamp()),
                   int(g_start.timestamp()))
                for stream, g_start, g_end, hours in gaps
            ])
            gap_marker = "written"
            log("wrote %d ingest_gap marker(s)" % len(gaps))
        except QueryFailed as exc:
            gap_marker = "failed"
            log("could not write the ingest_gap marker(s): %s" % exc)

    for warning in warnings:
        log(warning)

    log("%d GraphQL call(s) of a %d budget"
        % (budget["used"], MAX_GRAPHQL_CALLS))
    if committed_through:
        log("committed through %s (%d chunk(s) across %d stream(s))"
            % (iso(committed_through), chunks_done, len(streams)))

    # --- heartbeat message ----------------------------------------------------
    # Counts, timestamps and classified verdicts only. Nothing derived from a
    # QueryFailed message, a response body or an exception's repr - and no
    # zone NAME, which is a hostname and so remote-controlled text - see the
    # comment above hc_emit.
    #
    # COUNTERS FIRST, ONE TIMESTAMP LAST. The message is cut at 200 characters
    # for the push, so the cheapest and most-read values go first. Per-zone
    # watermarks are deliberately NOT emitted: the log() calls above print
    # every one.
    zone_count = len(zones)
    hc_emit("zones=%d" % zone_count)
    hc_emit("chunks=%d" % chunks_done)
    hc_emit("rows=%d" % rows_total)
    hc_emit("series=%d" % series_total)
    hc_emit("catching_up=%d" % behind)
    hc_emit("beacon=%s" % beacon_state)
    if committed_through:
        lag_minutes = int((now - committed_through).total_seconds() // 60)
        hc_emit("lag=%dm" % lag_minutes)
        hc_emit("committed_through=%s" % iso(committed_through))

    if failure:
        log("RUN FAILED: %d stream(s) failed a chunk; first: %s"
            % (failures, failure))
        hc_summary("failed")
        hc_emit("failure=queryfailed")
        hc_emit("detail=see pod log")
        return 1
    if gaps:
        # The summary names the FAULT (a stream had not ingested for longer
        # than Cloudflare keeps data) rather than only the symptom (which hours
        # were lost), and cause=unknown stops the body reading as a complete
        # account. The message carries the WORST gap; every stream's range is
        # in the log above, which is the only record of itself.
        _stream, g_start, g_end, missing_hours = max(gaps, key=lambda g: g[3])
        log("GAP: %d stream(s) fell behind Cloudflare's retention; the worst "
            "since %s" % (len(gaps), iso(g_start)))
        hc_summary("gap")
        gap_zones = len(gaps)      # an int: the tuples carry stream names
        hc_emit("gap_zones=%d" % gap_zones)
        hc_emit("gap_hours=%d" % missing_hours)
        hc_emit("gap_start=%s" % iso(g_start))
        hc_emit("gap_end=%s" % iso(g_end))
        hc_emit("gap_marker=%s" % gap_marker)
        hc_emit("cause=unknown - see pod log")
        return 1
    if warnings:
        # Bound to an int BEFORE it reaches a sink. `warnings` itself must never
        # be on check-ping-bodies.py's value allowlist: each element splices in
        # zone_tag, from the Cloudflare zone listing, so allowlisting the name
        # would pass `warnings[0]` as well as `len(warnings)`. An int cannot
        # carry a zone ID.
        truncated = len(warnings)
        log("RUN INCOMPLETE: %d truncated window(s)" % truncated)
        hc_summary("incomplete")
        hc_emit("truncated_windows=%d" % truncated)
        hc_emit("detail=see pod log")
        return 1
    if committed_through:
        log("RUN OK: %d chunks over %d streams, %d series, committed through %s"
            % (chunks_done, len(streams), series_total, iso(committed_through)))
    else:
        log("RUN OK: nothing new to ingest")
    hc_summary("ok")
    return 0


if __name__ == "__main__":
    push = make_pusher(os.environ.get("PUSH_URL", ""))
    try:
        rc = main()
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
        log("FATAL: exiting %d" % rc)
        hc_summary("failed")
        hc_emit("failure=fatal")
        hc_emit("detail=see pod log")
    except QueryFailed as exc:
        log("FATAL: %s" % exc)
        rc = 1
        hc_summary("failed")
        hc_emit("failure=queryfailed")
        hc_emit("detail=see pod log")
    except Exception as exc:                       # noqa: BLE001 - report, then red
        import traceback
        traceback.print_exc()
        # THE CLASS NAME ONLY. repr(exc) is banned not because urllib
        # exceptions carry the URL (they do not - `<HTTPError 400: 'Bad
        # Request'>`), but because the exception in hand may be a QueryFailed
        # whose message carries a zone tag or a response body.
        log("FATAL: unhandled %s" % type(exc).__name__)
        hc_summary("failed")
        hc_emit("failure=unhandled")
        hc_emit("exception=%s" % type(exc).__name__)
        hc_emit("detail=see pod log")
        rc = 1
    # EVERY LINE TO THE POD LOG, then the cut-down one-liner to kuma. The full
    # set is what the healthchecks.io body used to carry; read it here first.
    log("heartbeat message (full):\n" + hc_body())
    push("up" if rc == 0 else "down", kuma_msg())
    sys.exit(rc)
