#!/usr/bin/env python3
"""Pull Cloudflare edge analytics into InfluxDB.

Cloudflare's Free plan keeps 8 days of per-hostname analytics and rejects any
GraphQL query wider than 1 day. This job copies that window into InfluxDB before
it expires, so the record outlives Cloudflare's retention.

THE FOUR RULES THIS SCRIPT EXISTS TO ENFORCE. Read them before changing anything.

  1. THE DATA IS ITS OWN WATERMARK. The resume point is max(_time) over the
     `cloudflare` bucket, read back from InfluxDB on every run. There is no state
     file, no PVC and no ConfigMap cursor, because all three can disagree with
     what was actually stored — after a restore, after a manual delete, after a
     partial write. This cannot.

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
     healthchecks.io check goes red. Quietly resuming would be the same bug as a
     probe that stays green through an outage.

  4. COMMIT IN ORDER, STOP AT THE FIRST FAILURE. Chunks are processed oldest
     first and a chunk is committed only when EVERY zone succeeded for it. Commit
     a chunk where one zone failed and the watermark jumps past hours that zone
     never covered.

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
          edgeResponseStatus
          clientCountryName
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


# --- healthchecks.io ping body ----------------------------------------------
# A short key=value summary of what this run observed, POSTed with the
# exit-code ping so the Events log answers "what did it see?" without a pod log
# that may have aged out. Same format as the four shell emitters; one format
# across the estate is worth more than one job's convenience.
#
# NEVER PUT A QueryFailed MESSAGE, A RESPONSE BODY OR repr(exc) IN HERE.
# QueryFailed is raised at ten sites below and those messages splice in
# `zone_tag` - which comes from CF_ZONE_TAGS, a secretKeyRef whose own manifest
# comment says a zone ID identifies the account - plus up to 800 bytes of raw
# Cloudflare and InfluxDB response (`text[:500]`, `json.dumps(errors)[:800]`).
# `make check-ping-bodies` checks every hc_emit/hc_summary argument against an
# explicit value allowlist.
#
# The plumbing is a module-level accumulator that main() appends to and the
# __main__ block joins, because ping(str(rc)) is called at module scope while
# every emittable value lives in main()'s locals - and main() must keep
# returning an int, which the test suite asserts on. Do not refactor main() to
# return a tuple.
#
# SUMMARY is a one-element list rather than a `global` so that line 1 of the
# body is always `summary=`, whatever order things were emitted in.
_UNPRINTABLE = re.compile(r"[^\040-\176]")
SUMMARY = ["summary=FAILED - see pod log"]
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


def env(name, default=None):
    val = os.environ.get(name, default)
    if val is None or val == "":
        raise SystemExit("FATAL: %s is unset" % name)
    return val


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_zone_tags(raw):
    """`name=tag,name=tag` -> [(name, tag), ...], order preserved."""
    zones = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise SystemExit(
                "FATAL: CF_ZONE_TAGS entry %r is not name=zonetag" % entry)
        name, tag = entry.split("=", 1)
        name, tag = name.strip(), tag.strip()
        if not name or not tag:
            raise SystemExit(
                "FATAL: CF_ZONE_TAGS entry %r has an empty half" % entry)
        zones.append((name, tag))
    if not zones:
        raise SystemExit("FATAL: CF_ZONE_TAGS resolved to no zones")
    return zones


# --- HTTP -------------------------------------------------------------------

def http_post(url, body, headers, timeout=HTTP_TIMEOUT):
    """POST and return (status, body_text). Transport errors raise QueryFailed.

    urllib raises HTTPError for non-2xx, which is still a response worth reading:
    Cloudflare and InfluxDB both put the useful diagnostic in the error body.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:                       # noqa: BLE001 - transport
        raise QueryFailed("transport error contacting %s: %r" % (url, exc))


# --- Cloudflare -------------------------------------------------------------

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
        key = (
            zone_name,
            host,
            truncate_path(dims.get("clientRequestPath"), keep_full),
            str(dims.get("edgeResponseStatus", "unknown")),
            dims.get("clientCountryName") or "unknown",
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
        zone, host, path, status, country, ts = item[0]
        return (ts, zone, host, path, status, country)

    for key, (count, weighted) in sorted(acc.items(), key=oldest_first):
        zone, host, path, status, country, ts = key
        interval = (weighted / count) if count else 1.0
        yield ("http_requests,zone=%s,host=%s,path=%s,status=%s,country=%s "
               "count=%di,sample_interval=%.6f %d"
               % (esc_tag(zone), esc_tag(host), esc_tag(path),
                  esc_tag(status), esc_tag(country), count, interval, ts))


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


def influx_watermark(cfg):
    """Newest point in the bucket, or None if the bucket has never been written.

    DO NOT "tidy" this into a bare group()/keep() over every field. That form
    collides whenever two fields in the bucket have different value types
    ("schema collision: cannot group float and string types together") and the
    resulting error body parses as "no data", which is how ingest-freshness
    reported STALE for 25 straight days. Filtering to a single integer field
    before grouping is what makes the merge type-safe.

    `_field == "count"` covers both http_requests and ingest_status, so an hour
    with genuinely zero traffic still moves the watermark. It deliberately does
    NOT cover ingest_gap's `missing_hours`: a gap marker records a hole, it must
    never claim the hole was filled.
    """
    flux = (
        'from(bucket:"%s")\n'
        '  |> range(start: 1970-01-01T00:00:00Z)\n'
        '  |> filter(fn: (r) => r._field == "count")\n'
        '  |> group()\n'
        '  |> max(column: "_time")\n'
        '  |> keep(columns: ["_time"])\n' % cfg["bucket"]
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

    newest = None
    for line in text.splitlines():
        # Annotated CSV data rows start with ",_result,"; annotations start "#".
        if not line.startswith(",_result,"):
            continue
        cell = line.rstrip("\r").split(",")[-1].strip()
        if not cell:
            continue
        try:
            parsed = datetime.fromisoformat(cell.replace("Z", "+00:00"))
        except ValueError:
            raise QueryFailed("watermark row has unparseable _time %r" % cell)
        parsed = parsed.astimezone(timezone.utc)
        if newest is None or parsed > newest:
            newest = parsed
    return newest


# --- healthchecks.io --------------------------------------------------------

def make_pinger(uuid):
    """Dead-man's-switch pinger. A ping must never be able to fail the job,
    and a body must never cost a ping."""
    def ping(suffix, body=None):
        if not uuid:
            return
        # This pinger is only ever called with "start" or str(rc), never with
        # an empty suffix, so it cannot build the trailing-slash URL that
        # hc-ping.com answers with HTTP 400. Keep it that way.
        url = "https://hc-ping.com/%s/%s" % (uuid, suffix)
        if body:
            try:
                # THE ENCODE IS INSIDE THE TRY. Evaluated on the line before
                # urlopen, a UnicodeEncodeError would propagate out of ping()
                # past sys.exit(rc) and the exit-code ping would never be sent
                # - a body costing a ping, in the one emitter with an exception
                # mechanism to do it with.
                data = body.encode("ascii", "replace")
                urllib.request.urlopen(url, data=data, timeout=10).close()
                return
            except Exception as exc:               # noqa: BLE001 - best effort
                # THE CLASS NAME ONLY, never repr(exc). The exception in hand
                # may be a QueryFailed whose message carries a zone tag or a
                # response body.
                log("healthchecks.io body POST %r failed (ignored): %s"
                    % (suffix, type(exc).__name__))
        try:
            urllib.request.urlopen(url, timeout=10).close()
        except Exception as exc:                   # noqa: BLE001 - best effort
            log("healthchecks.io ping %r failed (ignored): %s"
                % (suffix, type(exc).__name__))
    return ping


# --- main -------------------------------------------------------------------

def main():
    cf_token = env("CF_API_TOKEN")
    zones = parse_zone_tags(env("CF_ZONE_TAGS"))
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

    watermark = influx_watermark(influx)          # raises -> run fails, no writes
    gap = None

    if watermark is None:
        # First run, or a restored-from-empty bucket. Nothing was lost, because
        # nothing was ever held. Start at the oldest data Cloudflare still has.
        start = retention_floor
        log("no watermark: bucket is empty, seeding from %s" % iso(start))
    else:
        start = watermark - timedelta(hours=OVERLAP_HOURS)
        log("watermark %s, rewound %dh to %s"
            % (iso(watermark), OVERLAP_HOURS, iso(start)))
        if start < retention_floor:
            missing_hours = int(
                (retention_floor - start).total_seconds() // 3600)
            gap = (start, retention_floor, missing_hours)
            log("")
            log("!!! UNRECOVERABLE GAP !!!")
            log("!!! %s .. %s (%d hours) is older than Cloudflare's %d-hour"
                % (iso(start), iso(retention_floor), missing_hours,
                   RETENTION_HOURS))
            log("!!! retention and can never be fetched. Writing an ingest_gap")
            log("!!! marker and exiting non-zero so the check goes red.")
            log("")
            start = retention_floor

    if start >= now:
        # Deliberately NOT an early return. Falling through leaves the loop with
        # nothing to do but still runs the gap-marker write and the exit-code
        # logic below, so no code path can decide there is a permanent hole and
        # then return before recording it.
        log("watermark is already current; no chunks to fetch")

    warnings = []
    budget = {"used": 0}
    chunks_done = 0
    committed_through = None
    failure = None
    cursor = start
    rows_total = 0
    series_total = 0
    gap_marker = "not-needed"

    while cursor < now and chunks_done < MAX_CHUNKS:
        chunk_end = min(cursor + timedelta(hours=CHUNK_HOURS), now)
        log("chunk %d/%d: %s .. %s"
            % (chunks_done + 1, MAX_CHUNKS, iso(cursor), iso(chunk_end)))

        acc = {}
        try:
            # ALL zones or none. Committing a chunk in which one zone failed
            # advances the watermark past hours that zone never covered, and
            # Cloudflare's retention then deletes them.
            for zone_name, zone_tag in zones:
                rows = cf_fetch(
                    cf_token, zone_tag, cursor, chunk_end, warnings, budget)
                rows_total += len(rows)
                log("  %s: %d rows" % (zone_name, len(rows)))
                aggregate(rows, zone_name, full_path_hosts, acc)
        except QueryFailed as exc:
            failure = "chunk %s..%s: %s" % (iso(cursor), iso(chunk_end), exc)
            log("  FAILED: %s" % exc)
            break

        lines = list(points(acc))
        # An empty chunk is a real answer and must still move the watermark,
        # otherwise eight genuinely quiet days would be indistinguishable from
        # eight days of broken ingestion and would raise a false gap alarm.
        lines.append(
            "ingest_status,source=cloudflare count=%di,chunk_seconds=%di %d"
            % (len(acc), int((chunk_end - cursor).total_seconds()),
               int(chunk_end.timestamp()) - 1))

        try:
            influx_write(influx, lines)
        except QueryFailed as exc:
            failure = "write for %s..%s: %s" % (iso(cursor), iso(chunk_end), exc)
            log("  WRITE FAILED: %s" % exc)
            break

        series_total += len(acc)
        log("  wrote %d series (%d line-protocol lines)" % (len(acc), len(lines)))
        committed_through = chunk_end
        cursor = chunk_end
        chunks_done += 1
        if cursor < now:
            time.sleep(1)          # stay clear of the GraphQL rate limit

    if gap:
        g_start, g_end, missing_hours = gap
        try:
            influx_write(influx, [
                "ingest_gap,source=cloudflare,reason=retention "
                "missing_hours=%di,gap_end=%di %d"
                % (missing_hours, int(g_end.timestamp()),
                   int(g_start.timestamp()))
            ])
            gap_marker = "written"
            log("wrote ingest_gap marker at %s" % iso(g_start))
        except QueryFailed as exc:
            gap_marker = "failed"
            log("could not write the ingest_gap marker: %s" % exc)

    for warning in warnings:
        log(warning)

    log("%d GraphQL call(s) of a %d budget"
        % (budget["used"], MAX_GRAPHQL_CALLS))
    if committed_through:
        log("committed through %s (%d chunk(s))"
            % (iso(committed_through), chunks_done))
    if cursor < now and not failure:
        log("chunk cap reached with %s .. %s still outstanding; the next hourly "
            "run continues from the new watermark"
            % (iso(cursor), iso(now)))

    # --- ping body ----------------------------------------------------------
    # Counts, timestamps and classified verdicts only. Nothing derived from a
    # QueryFailed message, a response body or an exception's repr - see the
    # comment above hc_emit.
    if watermark is not None:
        hc_emit("watermark=%s" % iso(watermark))
        hc_emit("rewound_to=%s" % iso(start))
    hc_emit("chunks=%d/%d" % (chunks_done, MAX_CHUNKS))
    hc_emit("rows=%d" % rows_total)
    hc_emit("series=%d" % series_total)
    if committed_through:
        lag_minutes = int((now - committed_through).total_seconds() // 60)
        hc_emit("committed_through=%s" % iso(committed_through))
        hc_emit("lag=%dm" % lag_minutes)

    if failure:
        log("RUN FAILED: %s" % failure)
        hc_summary("FAILED - query or write failed; see pod log")
        hc_emit("failure=queryfailed")
        hc_emit("detail=see pod log")
        return 1
    if gap:
        g_start, g_end, missing_hours = gap
        # The summary names the FAULT (the job had not ingested for over a
        # week) rather than only the symptom (which hours were lost), and
        # cause=unknown stops the body reading as a complete account. This
        # branch can only fire after the check has been red for eight days with
        # nobody acting, and it fires ONCE, so the body is the only record.
        # Ceiling, not floor: RETENTION_HOURS is `8 * 24 - 1`, an hour of
        # margin under Cloudflare's stated 8 days, and `// 24` would round that
        # to "7-day" - understating the window everything else in this repo
        # calls eight days.
        hc_summary("GAP - job had not ingested since %s; %dh now past "
                   "Cloudflare's %d-day retention"
                   % (iso(g_start), missing_hours, (RETENTION_HOURS + 23) // 24))
        hc_emit("gap_start=%s" % iso(g_start))
        hc_emit("gap_end=%s" % iso(g_end))
        hc_emit("gap_hours=%d" % missing_hours)
        hc_emit("gap_marker=%s" % gap_marker)
        hc_emit("cause=unknown - see pod log")
        return 1
    if warnings:
        # Bound to an int BEFORE it reaches a sink. `warnings` itself must never
        # be on check-ping-bodies.py's value allowlist: each element splices in
        # zone_tag, from the CF_ZONE_TAGS Secret, so allowlisting the name would
        # pass `warnings[0]` as well as `len(warnings)`. An int cannot carry a
        # zone ID.
        truncated = len(warnings)
        log("RUN INCOMPLETE: %d truncated window(s)" % truncated)
        hc_summary("INCOMPLETE - %d truncated window(s)" % truncated)
        hc_emit("truncated_windows=%d" % truncated)
        hc_emit("detail=see pod log")
        return 1
    if committed_through:
        hc_summary("ok - %d chunks, %d series, committed through %s"
                   % (chunks_done, series_total, iso(committed_through)))
    else:
        hc_summary("ok - nothing new to ingest")
    return 0


if __name__ == "__main__":
    ping = make_pinger(os.environ.get("HC_UUID", ""))
    ping("start", "summary=starting\n")
    try:
        rc = main()
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
        log("FATAL: exiting %d" % rc)
        hc_summary("FAILED - startup check failed; see pod log")
        hc_emit("failure=fatal")
        hc_emit("detail=see pod log")
    except QueryFailed as exc:
        log("FATAL: %s" % exc)
        rc = 1
        hc_summary("FAILED - query failed; see pod log")
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
        hc_summary("FAILED - unhandled exception; see pod log")
        hc_emit("failure=unhandled")
        hc_emit("exception=%s" % type(exc).__name__)
        hc_emit("detail=see pod log")
        rc = 1
    ping(str(rc), hc_body())
    sys.exit(rc)
