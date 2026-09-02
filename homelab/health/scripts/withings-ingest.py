#!/usr/bin/env python3
"""Pull one Withings account's body measurements into InfluxDB.

The scale produces body-composition detail that Apple Health never receives.
This job copies the account's measure groups into the `withings` bucket every
six hours. Weight arrives here AND in apple_metrics; that is deliberate and is
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
