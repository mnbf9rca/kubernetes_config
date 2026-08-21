#!/bin/sh
# This job DELIBERATELY always exits 0. The alerting signal is the ABSENT
# healthchecks.io ping (dead-man's-switch), not the job's exit code — a stale
# source must not also show up as a failed Job. Do not "fix" this into a
# non-zero exit. `set -e` is deliberately absent for the same reason: it would
# abort the run before the second source was ever checked.
#
# DO NOT reintroduce group()/last()/keep() into the FRESHNESS query. The
# "tidier" form
#   from(bucket:"B") |> range(start:-24h) |> group() |> last() |> keep(columns:["_time"])
# errors on BOTH buckets, because group() merges tables whose _value types differ:
#   {"code":"invalid","message":"runtime error @1:75-1:98: keep: schema collision:
#    cannot group float and string types together"}      <- apple_metrics
#   {"code":"invalid","message":"runtime error @1:68-1:91: keep: schema collision:
#    cannot group integer and string types together"}    <- garmin
# The previous script grepped that error payload for "_time", found none, and
# reported STALE on every run forever: health-garmin-ingest sat DOWN for 25 days
# and health-apple-ingest never pinged once, while both buckets were being
# written normally the whole time. Asking only "does any point exist in the
# window" needs no schema agreement and so cannot collide.
#
# newest_time() below DOES use last(), and that is safe: last() alone never
# merges tables, and the collision came from group()+keep(), not from last().
# It is also a SECOND, INDEPENDENT query whose failure degrades the ping BODY
# and nothing else. The freshness query above remains the sole input to the
# ping decision, and nothing here goes near it.
set -u
INFLUX=http://influxdb.health.svc.cluster.local:8086
WINDOW=24h              # the freshness window - the ONLY input to the ping decision
LOOKBACK=30d            # how far back the STALE body looks for the last point

# ---- healthchecks.io ping with an optional body -------------------------
# EACH PING CARRIES A BODY - a short key=value summary of what this run
# observed. The motivating incident: health-apple-ingest was GREEN while
# Apple Health data had been stale for five days. Nothing malfunctioned; a
# ping is simply one bit, and green could not distinguish "fresh" from
# "stale but the window has not expired yet". The body carries the number
# the job had already computed and thrown away.
#
# THE UUID IS AN ARGUMENT, NOT A GLOBAL. This script pings TWO different
# checks from one process and has no single check UUID; a global $HC_UUID
# would abort it under `set -u` before either bucket was checked, taking
# BOTH checks red on every run - the 25-day false-STALE incident above,
# re-created by its own fix.
#
# hc_ping RESETS the body after every ping, including the empty-UUID early
# return. That is what makes the two-check sequence safe by construction:
# no call site can leak apple's body onto the garmin check by forgetting a
# reset, and an operator can never read "fresh - apple_metrics ..." on the
# garmin check while garmin is stale.
#
# A PING MUST NEVER FAIL THE JOB, AND A BODY MUST NEVER COST A PING.
# NEVER EMIT A COMMAND'S OUTPUT - `make check-ping-bodies` enforces it, and
# spec section 9.2 says why. A BARE TRAILING SLASH IS AN HTTP 400, so the
# URL is built conditionally. `true >`, not `: >`: a redirection error on a
# POSIX special built-in aborts the shell even behind `|| true`.
HC_BODY=/tmp/hc-body
hc_reset() { true > "$HC_BODY" 2>/dev/null || true; }
emit() { { printf '%s' "$*" | LC_ALL=C tr -cd '\040-\176'; printf '\n'; } >> "$HC_BODY" 2>/dev/null || true; }

# hc_ping UUID [SUFFIX] - SUFFIX is "" | log. Always returns 0.
hc_ping() {
  _uu=${1:-}; _sf=${2:-}
  [ -n "$_uu" ] || { hc_reset; return 0; }
  _u="https://hc-ping.com/$_uu"
  [ -z "$_sf" ] || _u="$_u/$_sf"
  if [ -s "$HC_BODY" ]; then
    if curl -fsS -m 15 -o /dev/null --data-binary @"$HC_BODY" "$_u"; then
      hc_reset; return 0
    fi
    echo "hc: body POST failed, retrying without a body" >&2
  fi
  # Fixed text. No URL and no tool output: for a ping the URL IS the write
  # credential, and a pod log is not a place to put one either. Losing this
  # diagnostic would blind the one channel that reports on the reporting
  # mechanism - the trailing-slash 400 would have shipped undiscovered.
  curl -fsS -m 15 -o /dev/null "$_u" || echo "hc: ping not delivered" >&2
  hc_reset
  return 0
}

# ---- freshness query - the DECISION path, unchanged ----------------------
# Classification is recorded in FAIL_* for the body; the pod-log echoes are
# exactly as they were, raw response included. The RAW RESPONSE NEVER
# REACHES THE BODY: only the classification, the HTTP code after a
# digits-only gate, and a positive-match extraction of InfluxDB's own error
# `code` field.
FAIL_KIND=""
FAIL_HTTP=""
FAIL_CURL_RC=""
INFLUX_CODE=""

# Positive-match extraction of InfluxDB's error `code`. Bounded character
# class, bounded length, drawn from the error envelope's own field. This is
# the ONE extraction from a remote response this design permits (spec 9.3);
# it replaces an earlier "<=200-byte excerpt", which was withdrawn because
# the classifier that would gate it has a documented 25-day misfire history
# and newest_time()'s response is CSV carrying real health values. A
# positive match into [a-z ]{1,32} cannot be a CSV row.
influx_code() {
  _ic=$(printf '%s' "$1" | sed -n 's/.*"code":"\([a-z ]\{1,32\}\)".*/\1/p' | head -n 1)
  case "$_ic" in
    ''|*[!a-z\ ]*) _ic=unparsed ;;
  esac
  printf '%s' "$_ic"
}

fresh() {  # $1 bucket → 0 if a point exists in the last 24h, else log why and return 1
  FAIL_KIND=""; FAIL_HTTP=""; FAIL_CURL_RC=""; INFLUX_CODE=""
  OUT=$(curl -sS -m 30 -w '\n%{http_code}' "$INFLUX/api/v2/query?org=cynexia" \
    -H "Authorization: Token $TOKEN" -H 'Content-Type: application/vnd.flux' \
    -d "from(bucket:\"$1\") |> range(start:-$WINDOW) |> limit(n:1)" 2>&1)
  RC=$?
  CODE=$(printf '%s\n' "$OUT" | tail -n1)
  BODY=$(printf '%s\n' "$OUT" | sed '$d')
  # A FAILED QUERY IS NOT STALE DATA. Previously an InfluxDB error, a bad token,
  # a DNS failure and genuinely stale data all produced the same silent outcome.
  # Each now logs the raw response so the reason is visible to whoever looks.
  # Ping behaviour is unchanged and fail-safe: no success ping on any of them.
  if [ "$RC" -ne 0 ]; then
    echo "$1: QUERY FAILED (curl rc=$RC): $BODY"
    FAIL_KIND=curl
    case "$RC" in ''|*[!0-9]*) FAIL_CURL_RC=unparsed ;; *) FAIL_CURL_RC=$RC ;; esac
    # check-ping-bodies: untaint FAIL_CURL_RC - curl's exit status, gated to digits by the case above
    return 1
  fi
  case "$CODE" in
    2??) ;;
    *) echo "$1: QUERY FAILED (HTTP $CODE): $BODY"
       FAIL_KIND=http
       case "$CODE" in ''|*[!0-9]*) FAIL_HTTP=unparsed ;; *) FAIL_HTTP=$CODE ;; esac
       # check-ping-bodies: untaint FAIL_HTTP - curl's %{http_code}, gated to digits by the case above
       INFLUX_CODE=$(influx_code "$BODY")
       # check-ping-bodies: untaint INFLUX_CODE - positive-match [a-z ]{1,32} extraction, spec 9.3; never a slice of the response
       return 1 ;;
  esac
  if printf '%s\n' "$BODY" | grep -q '"code":"'; then
    echo "$1: QUERY FAILED (InfluxDB error): $BODY"
    FAIL_KIND=influx
    INFLUX_CODE=$(influx_code "$BODY")
    # check-ping-bodies: untaint INFLUX_CODE - as above
    return 1
  fi
  # Annotated-CSV data rows begin ",_result," — match that, not a bare "_time",
  # which an error payload could in principle also contain.
  if printf '%s\n' "$BODY" | grep -q '^,_result,'; then
    return 0
  fi
  echo "$1: STALE — query succeeded but returned no points in the last 24h"
  FAIL_KIND=stale
  return 1
}

# ---- body query - SECOND, INDEPENDENT, and degradable --------------------
# Bounded at -m 10, SHORTER than the freshness query's -m 30, because this
# one is degradable and that one is not. A timeout, an error or an
# unparseable response yields last_point=unknown and the ping proceeds
# unchanged.
#
# ONLY _time IS READ OUT. The annotated-CSV rows this returns carry _value -
# an actual weight, heart rate or sleep score. The column index is taken
# from each table's own header row, because _start and _stop are also
# ISO-8601 and sit to the LEFT of _time.
newest_time() {  # $1 bucket, $2 lookback → newest ISO-8601 _time, or nothing
  curl -sS -m 10 "$INFLUX/api/v2/query?org=cynexia" \
    -H "Authorization: Token $TOKEN" -H 'Content-Type: application/vnd.flux' \
    -d "from(bucket:\"$1\") |> range(start:-$2) |> last()" 2>/dev/null \
  | awk 'BEGIN { FS = ","; ti = 0 }
         { sub(/\r$/, "") }
         /^,result,table,/ { ti = 0; for (i = 1; i <= NF; i++) if ($i == "_time") ti = i; next }
         ti && /^,_result,/ { if ($ti ~ /^[0-9][0-9][0-9][0-9]-/) print $ti }' \
  | sort | tail -n 1
}

epoch_of() {  # $1 ISO-8601 Z → epoch seconds, or nothing
  [ -n "${1:-}" ] || return 0
  # GNU date first, busybox's -D form second. If neither parses it, the body
  # simply omits last_point_age; that is a designed degradation.
  _ep=$(date -u -d "$1" +%s 2>/dev/null) \
    || _ep=$(date -u -D '%Y-%m-%dT%H:%M:%SZ' -d "$1" +%s 2>/dev/null) \
    || return 0
  case "$_ep" in ''|*[!0-9]*) return 0 ;; esac
  printf '%s' "$_ep"
}

human_age() {  # $1 seconds → NdNhNm
  _hd=$(( $1 / 86400 ))
  _hh=$(( ($1 % 86400) / 3600 ))
  _hm=$(( ($1 % 3600) / 60 ))
  if   [ "$_hd" -gt 0 ]; then printf '%dd%dh' "$_hd" "$_hh"
  elif [ "$_hh" -gt 0 ]; then printf '%dh%dm' "$_hh" "$_hm"
  else                        printf '%dm' "$_hm"
  fi
}

age_of() {  # $1 ISO-8601 Z → NdNhNm, or nothing
  _ae=$(epoch_of "$1") || return 0
  [ -n "$_ae" ] || return 0
  _an=$(date -u +%s 2>/dev/null) || return 0
  case "$_an" in ''|*[!0-9]*) return 0 ;; esac
  [ "$_an" -ge "$_ae" ] || return 0
  human_age $(( _an - _ae ))
}

# ---- per-bucket driver ---------------------------------------------------
# $1 bucket, $2 check UUID, $3 check name.
#
# FRESH  → the existing success ping, now with a body. Unchanged decision.
# STALE  → NO success ping (unchanged), plus a /log ping carrying why.
# FAILED → NO success ping (unchanged), plus a /log ping carrying which
#          failure it was.
#
# /log is INERT: it sets no last_ping, no last_start and no status, and
# alert_after is recomputed from unchanged inputs. The check still goes red
# by silence at exactly the same instant it would have; the difference is
# that the events log already says why. Converting these two checks to
# /fail was considered and rejected: it trades a 36-hour tolerance for a
# 6-hour one on a signal that depends on the operator syncing a watch.
check_bucket() {
  _b=$1; _uuid=$2; _name=$3
  hc_reset
  if fresh "$_b"; then
    _ts=$(newest_time "$_b" "$WINDOW")
    _age=$(age_of "$_ts")
    [ -n "$_ts" ] || _ts=unknown
    # check-ping-bodies: untaint _ts - newest_time reads only the _time column, never _value; spec 7.5
    # check-ping-bodies: untaint _age - derived from _ts by age_of, which emits only NdNhNm digits
    if [ -n "$_age" ]; then
      emit "summary=fresh - $_b last point $_age ago"
    else
      emit "summary=fresh - $_b has a point inside $WINDOW"
    fi
    emit "check=$_name"
    emit "bucket=$_b"
    emit "last_point=$_ts"
    [ -z "$_age" ] || emit "last_point_age=$_age"
    emit "window=$WINDOW"
    hc_ping "$_uuid"
    return 0
  fi

  # Preserved verbatim from the original: the one line that says a ping was
  # withheld. hc_ping prints its own fixed diagnostic if a ping fails.
  echo "$_b: NOT pinged"
  hc_reset
  if [ "$FAIL_KIND" = stale ]; then
    _ts=$(newest_time "$_b" "$LOOKBACK")
    _age=$(age_of "$_ts")
    [ -n "$_ts" ] || _ts=unknown
    # check-ping-bodies: untaint _ts - as above
    # check-ping-bodies: untaint _age - as above
    emit "summary=STALE - no $_b points in $WINDOW; success ping withheld"
    emit "check=$_name"
    emit "bucket=$_b"
    emit "window=$WINDOW"
    emit "last_point=$_ts"
    [ -z "$_age" ] || emit "last_point_age=$_age"
    emit "lookback=$LOOKBACK"
  else
    # A FAILED QUERY IS NOT STALE DATA, and the body must keep them apart
    # just as the script does. Note that the ALERT still cannot: both
    # produce a red at 36h with opposite remedies (sync a watch vs fix a
    # token). Separating them at the alerting layer needs a second check,
    # which is out of scope.
    emit "summary=QUERY FAILED - $_b not verified; success ping withheld"
    emit "check=$_name"
    emit "bucket=$_b"
    emit "failure=$FAIL_KIND"
    [ -z "$FAIL_HTTP" ]    || emit "http_code=$FAIL_HTTP"
    [ -z "$FAIL_CURL_RC" ] || emit "curl_rc=$FAIL_CURL_RC"
    [ -z "$INFLUX_CODE" ]  || emit "influx_code=$INFLUX_CODE"
  fi
  hc_ping "$_uuid" log
  return 0
}

check_bucket apple_metrics "$HC_APPLE"  health-apple-ingest
check_bucket garmin        "$HC_GARMIN" health-garmin-ingest
exit 0
