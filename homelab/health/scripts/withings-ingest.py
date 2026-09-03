#!/usr/bin/env python3
"""Pull one Withings account's body measurements into InfluxDB.

The scale produces body-composition detail that Apple Health never receives.
This job copies the account's measure groups into the `withings` bucket every 15
minutes. Weight arrives here AND in apple_metrics; that is deliberate and is
not deduplicated.

THE THREE RULES THIS SCRIPT EXISTS TO ENFORCE. Read them before changing
anything.

  1. THE DATA IS ITS OWN WATERMARK. The resume point is max(_time) over the
     `withings` bucket, read back on every run, so the token file holds a
     credential and nothing else. A stored position is a second copy of a fact
     InfluxDB already holds, and a second copy can disagree.

  2. PERSIST THE ROTATED REFRESH TOKEN BEFORE USING THE NEW ACCESS TOKEN.
     Withings rotates the refresh token on every refresh, and the old one
     survives for 8 hours after the new one is issued OR until the new access
     token is first used, whichever comes first. Persisting first turns every
     crash in that window into a retry; persisting last turns one into a
     permanent unlink that only a browser can repair.

  3. NEVER WRITE THE FILE ON A BAD RESPONSE. A non-zero Withings `status`, a
     non-2xx, a body that is not JSON, or a body missing `access_token` or
     `refresh_token` all raise before write_state is reached. This is the
     withings-sync bug, written as a rule: a Withings 5xx overwrote its token
     file with nulls.

A FAILED RESUME QUERY IS NOT AN EMPTY ONE, and the query runs BEFORE the
refresh, so an InfluxDB outage costs no token rotation. An empty bucket seeds
from FIRST_RUN_START; anything else raises.

NO TOKEN IS EVER LOGGED. The refresh token appears in exactly two places: the
refresh request body and the state file. Every error path logs a class name and
a stage rather than a response body.

Idempotency comes from InfluxDB point overwrite: same measurement, same tag set,
same field key and same timestamp replaces. Every run rewinds OVERLAP_SECONDS
behind the resume point, so re-running the job is always safe.
"""

import json
import os
import re
import secrets
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# --- constants --------------------------------------------------------------
# The token path, the person tag, the redirect URI and the scope are constants
# rather than environment variables: none has a second possible value, and a
# constant cannot be set to something the script does not expect. INFLUX_URL,
# INFLUX_ORG and INFLUX_BUCKET stay environment variables only because
# cloudflare-analytics sets them that way and this job copies that manifest.

WITHINGS_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
WITHINGS_MEASURE_URL = "https://wbsapi.withings.net/measure"
AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
REDIRECT_URI = "https://withings.cynexia.net/oauth-callback"
TOKEN_FILE = "/state/withings_tokens.json"
PERSON = "rob"

# EVERY REQUEST SETS AN EXPLICIT TIMEOUT. The tool this job replaces sets none,
# which is how a wedged socket becomes a hung job.
HTTP_TIMEOUT = 30

# Rewind this far behind the resume point on every run: clock skew, and a group
# modified while a run was in flight. It costs nothing, because an identical
# point overwrites itself.
OVERLAP_SECONDS = 7200

# Pagination bound. `more` that never clears fails the run rather than looping.
MAX_PAGES = 200

# Where an empty bucket seeds from. Withings predates this by nothing that
# matters; the first run pages the whole account once and never again.
FIRST_RUN_START = "2009-01-01"

# user.activity is requested so sleep and activity can be added later without a
# second browser round trip. No endpoint here calls either.
SCOPE = "user.metrics,user.activity"


class IngestFailed(Exception):
    """The run did not produce a trustworthy answer.

    Distinct from "the call succeeded and there was nothing new". Withings
    reports a failed call with a non-zero `status` rather than an empty result,
    so zero measure groups is a real answer and is a success.
    """


def log(msg):
    print(msg, flush=True)


# --- heartbeat message ------------------------------------------------------
# A short key=value summary sent as the `msg` of the uptime-kuma heartbeat, so
# an alert answers "what did it see?" without a pod log that may have aged out.
# One line, cut at 200 characters, so counters come FIRST and `failure=` last.
#
# NEVER PUT A RESPONSE BODY, A URL OR repr(exc) IN HERE. `make check-ping-bodies`
# checks every hc_emit/hc_summary argument against an explicit value allowlist,
# and it recognises a sink by FUNCTION NAME, never by the destination host.
#
# SUMMARY is a one-element list rather than a `global` so the FIRST token is
# always `verdict=` - and it defaults to a failure, so a run that never reaches a
# verdict cannot report success.
_UNPRINTABLE = re.compile(r"[^\040-\176]")
VERDICTS = ("ok", "failed")
SUMMARY = ["verdict=failed"]
BODY_LINES = []

# The stage the run is in, read by the exit handler so every failure is
# classified by where it died. A one-element list for the same reason SUMMARY is
# one. It starts at `refresh` because the token file IS the refresh credential:
# a missing or unparseable one has the same remedy as a rejected refresh.
STAGES = ("resume", "refresh", "fetch", "write", "token_persist")
STAGE = ["refresh"]


def _clean(text):
    """One line, printable ASCII. Mirrors the shell emitters' `tr -cd`."""
    return _UNPRINTABLE.sub("", str(text))


def hc_summary(text):
    """Set the run's verdict. The argument MUST be a member of VERDICTS.

    A drifted verdict is coerced to `failed` and logged: it must not raise,
    because a message may never cost a push, and `failed` is the safe direction
    for a value nobody can classify.
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

    THE CUT LANDS ON A TOKEN BOUNDARY, NEVER MID-TOKEN.
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


# --- HTTP -------------------------------------------------------------------

def http_post(url, body, headers, timeout=HTTP_TIMEOUT):
    """POST and return (status, body_text). Transport errors raise IngestFailed.

    urllib raises HTTPError for non-2xx, which is still a response worth
    reading: Withings and InfluxDB both put the diagnostic in the error body.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:                       # noqa: BLE001 - transport
        raise IngestFailed("transport error contacting %s: %r" % (url, exc))


# --- Withings ---------------------------------------------------------------

def withings_post(url, fields, access_token=None):
    """Form-encode, POST, and return the response's `body` member.

    THE 200-WITH-STATUS TRAP. Withings answers a failed call with HTTP 200 and a
    non-zero `status` field. Checking the status line alone turns a rejected
    token into "no measures", and on the refresh path it would turn a rejection
    into a state write that destroys the only working credential.

    No message raised here carries a response body: the caller logs the class
    name and the stage, and this endpoint's request body carries the refresh
    token.
    """
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if access_token:
        headers["Authorization"] = "Bearer %s" % access_token
    status, text = http_post(url, urllib.parse.urlencode(fields).encode(),
                             headers)
    if status < 200 or status >= 300:
        raise IngestFailed("withings http %d" % status)
    try:
        doc = json.loads(text)
    except ValueError:
        raise IngestFailed("withings body is not JSON (http %d)" % status)
    if not isinstance(doc, dict):
        raise IngestFailed("withings body is not a JSON object")
    if doc.get("status") != 0:
        raise IngestFailed("withings rejected the call, status %r"
                           % doc.get("status"))
    body = doc.get("body")
    if not isinstance(body, dict):
        raise IngestFailed("withings body member is not an object")
    return body


def token_request(client_id, client_secret, **grant):
    """POST action=requesttoken with the client credentials and a grant.

    The refresh passes grant_type=refresh_token and refresh_token; --auth passes
    grant_type=authorization_code, code and redirect_uri. Raises unless both
    access_token and refresh_token come back non-empty, so nothing downstream
    can persist a half-answer.
    """
    fields = {"action": "requesttoken",
              "client_id": client_id,
              "client_secret": client_secret}
    fields.update(grant)
    body = withings_post(WITHINGS_TOKEN_URL, fields)
    for field in ("access_token", "refresh_token"):
        if not body.get(field):
            raise IngestFailed("token response missing %s" % field)
    return body


# --- token file -------------------------------------------------------------

def write_state(state):
    """Replace TOKEN_FILE atomically. THE ONLY CODE THAT TOUCHES THAT FILE.

    The temporary file is created in the SAME DIRECTORY: one in /tmp would make
    os.replace a cross-device move and lose atomicity outright. On any exception
    the temporary file is unlinked and the exception re-raised, so a failure at
    any point leaves the previous contents intact and valid - the original is
    never opened for writing.

    The containing directory is fsync'd after the replace, so the rename itself
    is durable and not only the bytes.
    """
    directory = os.path.dirname(TOKEN_FILE) or "."
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".withings_tokens.",
                                suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, TOKEN_FILE)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --- line protocol ----------------------------------------------------------

def esc_tag(value):
    """Escape a tag key or value: comma, equals, space, backslash.

    Control characters are STRIPPED before escaping, not escaped. Line protocol
    has no escape for a newline inside a tag value: a newline ends the point, so
    one arriving in a tag would split the body and inject an arbitrary extra
    point. str.isprintable() keeps the ASCII space, which is escaped below.
    """
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return (text
            .replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace("=", "\\=")
            .replace(" ", "\\ ")) or "unknown"


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
    # partial failure still leaves earlier measurements durably stored. That
    # second property depends on `lines` arriving oldest-first - see points().
    for i in range(0, len(lines), 5000):
        batch = "\n".join(lines[i:i + 5000]).encode()
        status, text = http_post(url, batch, headers)
        if status < 200 or status >= 300:
            raise IngestFailed("influx write http %d: %s" % (status, text[:500]))


# --- uptime-kuma push -------------------------------------------------------

# Any non-default agent will do; what matters is that it is not urllib's own.
PUSH_USER_AGENT = "kubernetes-config-withings-ingest"


def make_pusher(push_url):
    """Heartbeat pusher for an uptime-kuma PUSH monitor.

    A push must never be able to fail the job, and a message must never cost a
    push.

    THERE IS NO `start` PUSH. The push API has two states and no third kind, so
    activeDeadlineSeconds is the whole of the hang bound and the monitor's
    interval plus retry is the silence bound. Every path in this job is
    determinate, so it always pushes exactly one heartbeat, `up` or `down`.
    """
    def push(status, msg=""):
        if not push_url:
            return
        try:
            # THE ENCODE IS INSIDE THE TRY: evaluated on the line before
            # urlopen, an encoding error would propagate past sys.exit(rc) and
            # the heartbeat would be lost.
            query = urllib.parse.urlencode(
                {"status": status, "msg": str(msg)[:200]})
            # THE User-Agent IS LOAD-BEARING. uptime-kuma sits behind
            # Cloudflare, which answers urllib's default `Python-urllib/3.x`
            # agent with HTTP 403 and `error code: 1010` before the request ever
            # reaches kuma. A push failure is swallowed by design, so the only
            # symptom would be a monitor that never goes up. Do not drop it.
            request = urllib.request.Request(
                push_url + "?" + query, headers={"User-Agent": PUSH_USER_AGENT})
            urllib.request.urlopen(request, timeout=10).close()
        except Exception as exc:               # noqa: BLE001 - best effort
            # THE CLASS NAME ONLY, never repr(exc), and never the URL: the push
            # URL carries the monitor's token as its last path segment.
            log("uptime-kuma push %r failed (ignored): %s"
                % (status, type(exc).__name__))
    return push


# --- resume point -----------------------------------------------------------

def resume_point(cfg):
    """Newest point in the bucket, or None if it has never been written.

    DO NOT "tidy" the filter away. This bucket carries a float field, `value`,
    and a string field, `grpid`, so a bare group() over every field merges
    tables whose _value types differ and InfluxDB answers `schema collision:
    cannot group float and string types together`. That error body parses as
    "no data" to a naive reader, which is how ingest-freshness reported STALE
    for 25 straight days. Filtering to the one float field before grouping is
    what makes the merge type-safe, and `_field == "value"` covers every point
    this job writes.

    NEVER READ _value FROM THESE ROWS. It is a body measurement, and the pod log
    is not where one belongs. keep() drops it, so the last column is _time.
    """
    flux = (
        'from(bucket:"%s")\n'
        '  |> range(start: 1970-01-01T00:00:00Z)\n'
        '  |> filter(fn: (r) => r._field == "value")\n'
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
        raise IngestFailed("resume query http %d" % status)
    # An InfluxDB error arrives as a JSON body even on some 2xx paths.
    if text.lstrip().startswith("{") and '"code"' in text:
        raise IngestFailed("resume query returned an InfluxDB error object")

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
            raise IngestFailed("resume row has unparseable _time")
        parsed = parsed.astimezone(timezone.utc)
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def window_start(newest, now):
    """Where this run's lastupdate window begins.

    THE CLAMP TO NOW IS ONE LINE AND IT CLOSES THE ONLY PATH THAT SKIPS DATA. A
    scale with a wrong clock can write a point dated in the future, which would
    otherwise push lastupdate past the present and silently skip everything
    modified in between.
    """
    if newest is None:
        return datetime.fromisoformat(FIRST_RUN_START).replace(
            tzinfo=timezone.utc)
    return min(newest, now) - timedelta(seconds=OVERLAP_SECONDS)


# --- Withings measures ------------------------------------------------------

def fetch_measures(access_token, lastupdate):
    """Every measure group modified since `lastupdate`, following `more`.

    `meastypes` is deliberately NOT sent, so every measure group and every type
    code arrives untouched - including a code newer firmware invents. `category`
    is 1: real measurements, not user objectives.

    `lastupdate` filters on MODIFICATION time, not measurement date. A weight
    recorded three days ago and synced this morning is returned, and the point
    lands at its own measurement date.
    """
    groups = []
    offset = 0
    for page in range(1, MAX_PAGES + 1):
        fields = {"action": "getmeas", "category": 1, "lastupdate": lastupdate}
        if offset:
            fields["offset"] = offset
        try:
            body = withings_post(WITHINGS_MEASURE_URL, fields, access_token)
        except IngestFailed as exc:
            raise IngestFailed("getmeas failed at page %d: %s" % (page, exc))
        page_groups = body.get("measuregrps")
        if not isinstance(page_groups, list):
            raise IngestFailed("getmeas page %d has no measuregrps list" % page)
        log("page %d: %d group(s)" % (page, len(page_groups)))
        groups.extend(page_groups)
        if not body.get("more"):
            return groups
        offset = body.get("offset") or 0
    raise IngestFailed("more flag still set after %d pages" % MAX_PAGES)


# --- shaping ----------------------------------------------------------------

def scaled(value, unit):
    """`value * 10 ** unit`, fixed-point and exact.

    Decimal, not float: 74850 at unit -3 is 74.850 kg, and a float multiply
    renders 74.85000000000001 for some inputs. format(..., "f") is what keeps a
    large positive unit out of scientific notation, which line protocol would
    reject.
    """
    return format(Decimal(value).scaleb(unit), "f")


def esc_field(value):
    """Escape a quoted string field value: backslash, then double quote.

    `grpid` is a Withings integer today, so this is a guard rather than a
    transformation - but an unescaped quote would end the field early and the
    rest of the line would be parsed as something else.
    """
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text.replace("\\", "\\\\").replace('"', '\\"')


def group_date(group):
    """The group's measurement time, or raise.

    A GROUP WITH NO USABLE `date` IS A FAILED RUN, not a point at epoch 0. The
    timestamp is the point's identity, so a defaulted one writes a 1970 outlier
    that every panel shows and that no later run corrects - the same reasoning
    that makes a measure missing `value` raise below.
    """
    try:
        return int(group["date"])
    except (KeyError, TypeError, ValueError):
        raise IngestFailed("measure group is missing a usable date")


def points(groups):
    """Line protocol for every measure, OLDEST FIRST.

    The ordering is load-bearing, not cosmetic: influx_write sends these in
    batches, so a later batch can fail with earlier ones already durably stored.
    Oldest-first makes any partial write a PREFIX, which the next run's overlap
    covers.

    ONE MEASUREMENT WITH A `type` TAG, not one measurement per type. The type
    space is open - newer hardware adds codes - so an unknown code is one more
    tag value in a series that already exists, and every panel is a filter on
    `type`. `grpid` is a FIELD and `deviceid` is a TAG: deviceid has a handful of
    values for the life of the account, grpid has one per weigh-in forever and as
    a tag would multiply series cardinality by the number of weigh-ins.
    """
    lines = []
    for group in sorted(groups, key=group_date):
        ts = group_date(group)
        grpid = group.get("grpid", "")
        deviceid = group.get("deviceid") or "unknown"
        for measure in group.get("measures") or []:
            try:
                value = measure["value"]
                unit = measure["unit"]
                mtype = measure["type"]
            except (KeyError, TypeError):
                raise IngestFailed(
                    "measure group is missing value, unit or type")
            lines.append(
                'withings_measure,person=%s,type=%s,deviceid=%s '
                'grpid="%s",value=%s %d'
                % (esc_tag(PERSON), esc_tag(mtype), esc_tag(deviceid),
                   esc_field(grpid), scaled(value, unit), ts))
    return lines


# --- authorization ----------------------------------------------------------

def run_auth():
    """One-time interactive authorization. The only mode that talks to a human.

    Run from a one-off pod by the operator; see the runbook in
    docs/operations/homelab-health.md. It pushes NO heartbeat: it is an operator
    action, not a scheduled run, and a push from it would be a heartbeat for a
    job that did not run.
    """
    client_id = env("WITHINGS_CLIENT_ID")
    client_secret = env("WITHINGS_CLIENT_SECRET")
    state = secrets.token_urlsafe(16)
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })
    log("")
    log("Open this URL, sign in, approve, then paste the WHOLE redirect URL")
    log("from the address bar back here. THE CODE LIVES 30 SECONDS, so this")
    log("prompt must already be waiting before you open the URL.")
    log("")
    log(url)
    log("")
    pasted = input("redirect URL: ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    # Comparing `state` is a real check because the whole URL was pasted, not
    # just the code: an eyeball comparison is not a check.
    if query.get("state", [""])[0] != state:
        log("FATAL: state does not match; nothing written")
        return 1
    code = query.get("code", [""])[0]
    if not code:
        log("FATAL: no code in that URL; nothing written")
        return 1
    body = token_request(client_id, client_secret,
                         grant_type="authorization_code",
                         code=code, redirect_uri=REDIRECT_URI)
    write_state({"refresh_token": body["refresh_token"],
                 "userid": str(body.get("userid", ""))})
    # BY SHAPE, NEVER BY VALUE.
    log("wrote %s (refresh token, userid)" % TOKEN_FILE)
    return 0


# --- main -------------------------------------------------------------------

def main():
    influx = {
        "url": os.environ.get(
            "INFLUX_URL", "http://influxdb.health.svc.cluster.local:8086"),
        "org": os.environ.get("INFLUX_ORG", "cynexia"),
        "bucket": os.environ.get("INFLUX_BUCKET", "withings"),
        "token": env("INFLUX_TOKEN"),
    }
    client_id = env("WITHINGS_CLIENT_ID")
    client_secret = env("WITHINGS_CLIENT_SECRET")

    # 1. READ STATE. A missing or unparseable file is fatal before any network
    # call: the fix is a re-authorization, not a retry. STAGE is already
    # `refresh`, because the file IS the refresh credential.
    try:
        with open(TOKEN_FILE) as handle:
            state = json.load(handle)
        refresh_token = state["refresh_token"]
        if not refresh_token:
            raise ValueError("empty refresh_token")
    except Exception as exc:                       # noqa: BLE001 - classify
        log("FATAL: no usable token file at %s; re-authorize (%s)"
            % (TOKEN_FILE, type(exc).__name__))
        hc_emit("failure=%s" % STAGE[0])
        return 1

    now = datetime.now(timezone.utc).replace(microsecond=0)

    # 2. READ THE RESUME POINT, BEFORE THE REFRESH, so an InfluxDB outage costs
    # no token rotation.
    STAGE[0] = "resume"
    newest = resume_point(influx)
    start = window_start(newest, now)
    if newest is None:
        log("resume: bucket is empty, seeding from %s" % iso(start))
    else:
        log("resume: newest point %s, window starts %s"
            % (iso(newest), iso(start)))

    # 3. REFRESH.
    STAGE[0] = "refresh"
    body = token_request(client_id, client_secret,
                         grant_type="refresh_token",
                         refresh_token=refresh_token)

    # 4. PERSIST THE ROTATED TOKEN BEFORE THE NEW ACCESS TOKEN IS USED FOR
    # ANYTHING. A crash after this is a retry; a crash before it, with the
    # access token already used, is a permanent unlink only a browser repairs.
    # The 8 hours is Withings' own figure, not an estimate: the developer guide
    # page "Access and refresh tokens" (developer.withings.com/developer-guide/
    # v3/integration-guide/public-health-data-api/get-access/
    # access-and-refresh-tokens-no-recover/) states that the refresh token
    # rotates on every refresh and that the old one expires 8 hours after the
    # new one is issued or once the new access token is used, and support
    # article 360018514178, "API - Improving the refresh token expiration", is
    # where that grace was introduced.
    STAGE[0] = "token_persist"
    try:
        write_state({"refresh_token": body["refresh_token"],
                     "userid": str(body.get("userid", ""))})
    except Exception as exc:                       # noqa: BLE001 - classify
        log("FATAL: could not persist token: %s" % type(exc).__name__)
        log("The OLD refresh token is still on disk and is valid for 8 hours "
            "from the refresh above. Fix the volume and force a run inside "
            "that window, or the fix is a browser re-authorization.")
        hc_emit("failure=%s" % STAGE[0])
        return 1
    log("rotated refresh token persisted")

    # 5. FETCH.
    STAGE[0] = "fetch"
    groups = fetch_measures(body["access_token"], int(start.timestamp()))
    groups_total = len(groups)
    hc_emit("groups=%d" % groups_total)

    # 6. WRITE. There is no watermark to persist, so there is no step after it:
    # a run either stored points, which moves the resume point by itself, or it
    # did not, and the next run asks the same question.
    lines = points(groups)
    # THE STAGE MOVES AFTER points() RETURNS, not before it. points() raises on
    # a malformed getmeas body - a group with no usable date, a measure missing
    # value, unit or type - and that is a fault in what the fetch returned, so
    # it must report `failure=fetch`. Setting the stage first attributed a bad
    # Withings response to InfluxDB.
    STAGE[0] = "write"
    points_written = len(lines)
    hc_emit("points=%d" % points_written)
    influx_write(influx, lines)

    if groups_total == 0:
        # A real answer, unlike the Cloudflare job's empty result: Withings
        # reports a failed call with a non-zero status, so "nothing in the
        # window" is unambiguous and is a success.
        log("RUN OK: nothing new")
    else:
        log("RUN OK: %d groups, %d points" % (groups_total, points_written))
    hc_summary("ok")
    return 0


if __name__ == "__main__":
    # `sys.argv` decides; there is no argument parser for one flag.
    if "--auth" in sys.argv[1:]:
        sys.exit(run_auth())

    push = make_pusher(os.environ.get("PUSH_URL", ""))
    try:
        rc = main()
    except SystemExit as exc:
        # THE NAME, NEVER THE VALUE. env() raises SystemExit carrying a fixed
        # literal plus the NAME of the unset variable, so this is safe to log
        # and is the only place it is said. Dropping it left the pod log
        # reading "exiting 1" while the heartbeat said `failure=refresh`, whose
        # documented remedy is a browser re-authorization - the wrong repair
        # for a Secret key that was never wired.
        # Logged BARE: env() already prefixes its message with "FATAL: ", and
        # a second one reads as a bug in the reporting rather than the config.
        if not isinstance(exc.code, int):
            log(str(exc.code))
        rc = exc.code if isinstance(exc.code, int) else 1
        log("FATAL: exiting %d" % rc)
        hc_summary("failed")
        hc_emit("failure=%s" % STAGE[0])
    except IngestFailed as exc:
        log("FATAL: %s" % exc)
        rc = 1
        hc_summary("failed")
        hc_emit("failure=%s" % STAGE[0])
    except Exception as exc:                       # noqa: BLE001 - report, red
        import traceback
        traceback.print_exc()
        # THE CLASS NAME ONLY. repr(exc) is banned because the exception in hand
        # may carry a response body.
        log("FATAL: unhandled %s" % type(exc).__name__)
        rc = 1
        hc_summary("failed")
        hc_emit("failure=%s" % STAGE[0])
        hc_emit("exception=%s" % type(exc).__name__)
    # EVERY LINE TO THE POD LOG, then the cut-down one-liner to kuma.
    log("heartbeat message (full):\n" + hc_body())
    push("up" if rc == 0 else "down", kuma_msg())
    sys.exit(rc)
