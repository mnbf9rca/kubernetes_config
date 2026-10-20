#!/usr/bin/env python3
"""Pull Cloudflare edge analytics into InfluxDB.

Cloudflare's Free plan keeps 8 days of per-hostname analytics and rejects any
GraphQL query wider than 1 day. This job copies that window into InfluxDB before
it expires, so the record outlives Cloudflare's retention.

THE FOUR RULES THIS SCRIPT EXISTS TO ENFORCE. Read them before changing anything.

  1. THE DATA IS ITS OWN WATERMARK. The resume point is max(_time) PER ZONE
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
     lose them permanently once Cloudflare's 8 days roll past. Zero rows from a
     clean response is a real answer and may advance the watermark; anything else
     may not.

  3. AN UNRECOVERABLE GAP IS LOUD. If the resume point is older than Cloudflare's
     retention, those hours are gone and no future run can get them. The job logs
     the exact range, writes an `ingest_gap` marker so the hole is visible in
     Grafana instead of reading as zero traffic, and exits non-zero so the
     `homelab-cloudflare-analytics` monitor is pushed DOWN. Quietly resuming
     would be the same bug as a probe that stays green through an outage.

  4. COMMIT IN ORDER, AND A FAILURE STOPS ONLY ITS ZONE. Each zone's chunks
     are processed oldest first; a failed chunk ends that zone for this run and
     the other zones carry on, so a zone that fails every run cannot starve the
     zones sorted after it. A zone's watermark is its own, so the failed zone
     resumes from its last committed chunk next run; no zone's watermark can
     move past hours it never fetched. The run still exits non-zero.

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

# Cloudflare rejects any query wider than 1 day, so chunks must be strictly
# under 24h. 23h leaves an hour of headroom against boundary interpretation.
CHUNK_HOURS = 23

# Cloudflare Free keeps 8 days. Beyond 8 chunks there is nothing left to fetch,
# so a runaway backfill loop would only burn rate limit. 8 * 23h = 184h, a little
# short of the 191h retention floor below; a catch-up run therefore converges
# over two hourly runs rather than one, which only ever happens after an outage.
MAX_CHUNKS = 8

# Rewind this far behind the watermark on every run. The final hour of the
# previous run was almost certainly still in progress when it was written.
OVERLAP_HOURS = 2

# Cloudflare's stated retention, less an hour of margin: the boundary is not
# sharp and a query that straddles it returns silently truncated data.
RETENTION_HOURS = 8 * 24 - 1

# httpRequestsAdaptiveGroups caps a single response at this many rows. A chunk
# that returns exactly this many is assumed truncated and is subdivided.
GRAPHQL_ROW_LIMIT = 10000

# Floor for subdivision. Below this the halving cannot help and the run reports
# an unavoidably truncated window instead of looping forever.
MIN_SUBDIVIDE_SECONDS = 60

# Hard ceiling on GraphQL calls per run. Subdivision is recursive, so a
# pathological chunk could fan out to ~2000 queries and trip Cloudflare's
# 300-queries-per-5-minutes user limit — at which point every remaining query
# fails and the rate limit is burned for the next several hourly runs too.
# Exhausting the budget is a loud failure, not a silent truncation: the
# watermark stays put and the next run retries.
MAX_GRAPHQL_CALLS = 180

CF_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
HTTP_TIMEOUT = 60

QUERY = """
query ZoneHourly($zoneTag: string!, $start: Time!, $end: Time!, $limit: Int!) {
  viewer {
    zones(filter: {zoneTag: $zoneTag}) {
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
        }
      }
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


def rewind(watermark):
    """Where a run resumes: OVERLAP_HOURS behind the watermark, FLOORED TO THE HOUR.

    Cloudflare applies datetime_geq to the raw request time and only then
    groups by datetimeHour, so a start of 18:37 returns hour 18 as the
    18:37..19:00 slice, and the write then overwrites that hour's full count
    with the slice (measured 2026-10-20: 1982 requests from :00, 568 from :37).
    The watermark is the previous run's wall-clock minute, so without the floor
    every hour two behind a run was cut down to its last 23 minutes.
    """
    return (watermark - timedelta(hours=OVERLAP_HOURS)).replace(
        minute=0, second=0, microsecond=0)


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
    """Every zone the token can see, as [(name, tag), ...] sorted by name.

    Enumerated on every run rather than configured, so a zone added to the
    account is ingested from its next run without anyone editing a list; the
    token therefore carries Zone:Read beside Zone.Analytics:Read. Pagination is
    honoured because an account can outgrow one page. Anything short of a
    clean, complete listing raises QueryFailed: a silently shortened list would
    skip a zone, which is the same loss as treating a failed query as empty.
    """
    headers = {"Authorization": "Bearer %s" % token}
    zones = []
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
            if not name or not tag:
                raise QueryFailed(
                    "zone listing entry without name or id: %s"
                    % json.dumps(zone)[:200])
            zones.append((name, tag))
        info = doc.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            break
        page += 1
    if not zones:
        raise QueryFailed("the token can see no zones at all")
    return sorted(zones)


def cf_query(token, zone_tag, start, end, limit, budget):
    """Return the raw group rows for one zone and window.

    Raises QueryFailed for every outcome that is not a clean, complete answer.
    """
    if budget["used"] >= MAX_GRAPHQL_CALLS:
        raise QueryFailed(
            "GraphQL call budget of %d exhausted; refusing to keep querying"
            % MAX_GRAPHQL_CALLS)
    budget["used"] += 1

    payload = json.dumps({
        "query": QUERY,
        "variables": {
            "zoneTag": zone_tag,
            "start": iso(start),
            "end": iso(end),
            "limit": limit,
        },
    }).encode()
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
        zones = doc["data"]["viewer"]["zones"]
    except (KeyError, TypeError):
        raise QueryFailed("unexpected response shape: %s" % text[:500])

    if not isinstance(zones, list):
        raise QueryFailed("`zones` is not a list: %s" % text[:500])
    if not zones:
        # An empty zones list means the token cannot see this zone at all, which
        # is a configuration fault, not an absence of traffic.
        raise QueryFailed(
            "zone %s returned no zone object — token cannot read it" % zone_tag)

    rows = zones[0].get("httpRequestsAdaptiveGroups")
    if rows is None:
        raise QueryFailed("no httpRequestsAdaptiveGroups in %s" % text[:500])
    return rows


def cf_fetch(token, zone_tag, start, end, warnings, budget):
    """Fetch a window, subdividing when the response looks truncated.

    Cloudflare offers no cursor for this dataset, so the only way past the row
    cap is a narrower window. Halving works because the caller aggregates: two
    half-windows summed give the same per-hour totals as one whole window.
    """
    rows = cf_query(token, zone_tag, start, end, GRAPHQL_ROW_LIMIT, budget)
    if len(rows) < GRAPHQL_ROW_LIMIT:
        return rows

    span = int((end - start).total_seconds())
    if span <= MIN_SUBDIVIDE_SECONDS:
        warnings.append(
            "TRUNCATED: zone %s window %s..%s returned the %d-row cap at the "
            "%ds subdivision floor; this window's data is incomplete"
            % (zone_tag, iso(start), iso(end), GRAPHQL_ROW_LIMIT, span))
        return rows

    mid = start + timedelta(seconds=span // 2)
    log("  row cap hit for %s %s..%s — subdividing at %s"
        % (zone_tag, iso(start), iso(end), iso(mid)))
    return (cf_fetch(token, zone_tag, start, mid, warnings, budget)
            + cf_fetch(token, zone_tag, mid, end, warnings, budget))


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
            int(hour.timestamp()),
        )
        count = int(row.get("count") or 0)
        interval = float((row.get("avg") or {}).get("sampleInterval") or 1.0)
        total, weighted = acc.get(key, (0, 0.0))
        acc[key] = (total + count, weighted + count * interval)


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
         bot_category, security_source, client, ts) = key
        interval = (weighted / count) if count else 1.0
        yield ("http_requests,zone=%s,host=%s,path=%s,status=%s,country=%s,"
               "method=%s,bot_category=%s,security_source=%s,client=%s "
               "count=%di,sample_interval=%.6f %d"
               % (esc_tag(zone), esc_tag(host), esc_tag(path),
                  esc_tag(status), esc_tag(country), esc_tag(method),
                  esc_tag(bot_category), esc_tag(security_source),
                  esc_tag(client), count, interval, ts))


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
    """{zone: newest point with that zone tag}; {} if the bucket was never written.

    The key "" is the newest point carrying NO zone tag: the global
    ingest_status marker written before 2026-10-20, when the watermark was one
    value for every zone. main() reads it once as a floor for zones that
    already have points.

    DO NOT "tidy" this into a bare group()/keep() over every field. That form
    collides whenever two fields in the bucket have different value types
    ("schema collision: cannot group float and string types together") and the
    resulting error body parses as "no data", which is how ingest-freshness
    reported STALE for 25 straight days. Filtering to a single integer field
    before grouping is what makes the merge type-safe.

    `_field == "count"` covers both http_requests and ingest_status, so an hour
    with genuinely zero traffic still moves a zone's watermark. It deliberately
    does NOT cover ingest_gap's `missing_hours`: a gap marker records a hole,
    it must never claim the hole was filled.
    """
    flux = (
        'from(bucket:"%s")\n'
        '  |> range(start: 1970-01-01T00:00:00Z)\n'
        '  |> filter(fn: (r) => r._field == "count")\n'
        '  |> group(columns: ["zone"])\n'
        '  |> max(column: "_time")\n'
        '  |> keep(columns: ["zone", "_time"])\n' % cfg["bucket"]
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
    # (",result,table,...") because the zone-less group has no `zone` column at
    # all. Columns are therefore read per header, never by position.
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
        zone = cells.get("zone", "").strip()
        if zone not in newest or parsed > newest[zone]:
            newest[zone] = parsed
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
    retention_floor = now - timedelta(hours=RETENTION_HOURS)
    budget = {"used": 0}

    zones = cf_zones(cf_token)                    # raises -> run fails, no writes
    log("%d zone(s) visible to the token" % len(zones))
    watermarks = influx_watermarks(influx)        # raises -> run fails, no writes
    # Before 2026-10-20 the ingest_status marker carried no zone tag and was
    # one watermark for every zone then configured, so it proves every zone
    # that has points was covered through it. A zone whose own newest point is
    # older than that marker resumes from the marker, not from its last
    # traffic: otherwise a quiet zone would be re-fetched and, having gained
    # the `method` tag since, double-counted. A zone with no points at all is
    # new and is seeded from Cloudflare's whole retention. The marker is never
    # written again, so this floor is a constant.
    legacy = watermarks.pop("", None)

    warnings = []
    gaps = []                      # (zone_name, g_start, g_end, missing_hours)
    failure = None                 # the first failed chunk, for the log
    failures = 0                   # zones that failed a chunk this run
    chunks_done = 0
    rows_total = 0
    series_total = 0
    behind = 0                     # zones with chunks still outstanding
    committed_through = None       # newest chunk end any zone committed
    gap_marker = "not-needed"

    for zone_name, zone_tag in zones:
        watermark = watermarks.get(zone_name)
        if watermark is not None and legacy is not None:
            watermark = max(watermark, legacy)
        if watermark is None:
            # New zone, or a restored-from-empty bucket. Nothing was lost,
            # because nothing was ever held. Start at the oldest data
            # Cloudflare still has.
            start = retention_floor
            log("%s: no watermark, seeding from %s" % (zone_name, iso(start)))
        else:
            start = rewind(watermark)
            log("%s: watermark %s, rewound %dh and floored to %s"
                % (zone_name, iso(watermark), OVERLAP_HOURS, iso(start)))
            if start < retention_floor:
                missing_hours = int(
                    (retention_floor - start).total_seconds() // 3600)
                gaps.append((zone_name, start, retention_floor, missing_hours))
                log("!!! %s: UNRECOVERABLE GAP %s .. %s (%d hours) is older "
                    "than Cloudflare's %d-hour retention and can never be "
                    "fetched; writing an ingest_gap marker and exiting "
                    "non-zero so the monitor is pushed down"
                    % (zone_name, iso(start), iso(retention_floor),
                       missing_hours, RETENTION_HOURS))
                start = retention_floor

        cursor = start
        zone_chunks = 0
        zone_failure = None
        while cursor < now and zone_chunks < MAX_CHUNKS:
            chunk_end = min(cursor + timedelta(hours=CHUNK_HOURS), now)
            acc = {}
            try:
                rows = cf_fetch(
                    cf_token, zone_tag, cursor, chunk_end, warnings, budget)
            except QueryFailed as exc:
                zone_failure = "%s chunk %s..%s: %s" % (
                    zone_name, iso(cursor), iso(chunk_end), exc)
                log("  FAILED: %s" % exc)
                break
            rows_total += len(rows)
            aggregate(rows, zone_name, full_path_hosts, acc)

            lines = list(points(acc))
            # An empty chunk is a real answer and must still move this zone's
            # watermark, otherwise eight genuinely quiet days would be
            # indistinguishable from eight days of broken ingestion and would
            # raise a false gap alarm. Appended AFTER every data point: see
            # points() for why the order matters.
            lines.append(
                "ingest_status,source=cloudflare,zone=%s "
                "count=%di,chunk_seconds=%di %d"
                % (esc_tag(zone_name), len(acc),
                   int((chunk_end - cursor).total_seconds()),
                   int(chunk_end.timestamp()) - 1))
            try:
                influx_write(influx, lines)
            except QueryFailed as exc:
                zone_failure = "%s write %s..%s: %s" % (
                    zone_name, iso(cursor), iso(chunk_end), exc)
                log("  WRITE FAILED: %s" % exc)
                break

            log("  %s: %s .. %s: %d rows, %d series"
                % (zone_name, iso(cursor), iso(chunk_end), len(rows), len(acc)))
            series_total += len(acc)
            chunks_done += 1
            zone_chunks += 1
            if committed_through is None or chunk_end > committed_through:
                committed_through = chunk_end
            cursor = chunk_end
            if cursor < now:
                time.sleep(1)          # stay clear of the GraphQL rate limit
        if zone_failure:
            # THE OTHER ZONES CARRY ON. Each zone's watermark is its own, so a
            # zone that fails on every run cannot starve the zones sorted after
            # it; this zone resumes from its last committed chunk next run. The
            # run still exits non-zero, and reports the FIRST failure.
            failures += 1
            failure = failure or zone_failure
            continue
        if cursor < now:
            behind += 1
            log("  %s: chunk cap reached with %s .. %s outstanding; the next "
                "run continues from this zone's watermark"
                % (zone_name, iso(cursor), iso(now)))

    if gaps:
        try:
            influx_write(influx, [
                "ingest_gap,source=cloudflare,reason=retention,zone=%s "
                "missing_hours=%di,gap_end=%di %d"
                % (esc_tag(name), hours, int(g_end.timestamp()),
                   int(g_start.timestamp()))
                for name, g_start, g_end, hours in gaps
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
        log("committed through %s (%d chunk(s) across %d zone(s))"
            % (iso(committed_through), chunks_done, len(zones)))

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
    if committed_through:
        lag_minutes = int((now - committed_through).total_seconds() // 60)
        hc_emit("lag=%dm" % lag_minutes)
        hc_emit("committed_through=%s" % iso(committed_through))

    if failure:
        log("RUN FAILED: %d zone(s) failed a chunk; first: %s"
            % (failures, failure))
        hc_summary("failed")
        hc_emit("failure=queryfailed")
        hc_emit("detail=see pod log")
        return 1
    if gaps:
        # The summary names the FAULT (a zone had not ingested for over a
        # week) rather than only the symptom (which hours were lost), and
        # cause=unknown stops the body reading as a complete account. The
        # message carries the WORST gap; every zone's range is in the log
        # above, which is the only record of itself. Ceiling, not floor:
        # RETENTION_HOURS is `8 * 24 - 1`, an hour of margin under
        # Cloudflare's stated 8 days, and `// 24` would round that to
        # "7-day" - understating the window everything else in this repo
        # calls eight days.
        _zone, g_start, g_end, missing_hours = max(gaps, key=lambda g: g[3])
        log("GAP: %d zone(s) had not ingested for over %d days; the worst "
            "since %s" % (len(gaps), (RETENTION_HOURS + 23) // 24, iso(g_start)))
        hc_summary("gap")
        gap_zones = len(gaps)      # an int: the tuples carry zone names
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
        log("RUN OK: %d chunks over %d zones, %d series, committed through %s"
            % (chunks_done, len(zones), series_total, iso(committed_through)))
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
