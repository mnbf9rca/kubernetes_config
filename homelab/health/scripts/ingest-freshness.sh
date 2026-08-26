#!/bin/sh
# This job DELIBERATELY always exits 0. The alerting signal is the ABSENT
# heartbeat (dead-man's-switch), not the job's exit code — a stale source must
# not also show up as a failed Job. Do not "fix" this into a non-zero exit.
# `set -e` is deliberately absent for the same reason: it would abort the run
# before the second source was ever checked.
#
# ONE MONITOR, TWO BUCKETS, AND SUCCESS-ONLY SEMANTICS — read this before
# changing what it pushes. Since 2026-08-26 this job drives the single
# uptime-kuma push monitor `health-ingest` rather than two healthchecks.io
# checks. It pushes `up` only when BOTH buckets are fresh, and pushes NOTHING on
# every other path: a stale bucket, a failed query, a dead InfluxDB. That is the
# same contract the two checks had, and it is not negotiable — a `down` push on
# a stale bucket would flip the monitor at the first 6-hourly run that found
# nothing, trading a ~36-hour tolerance for a 6-hour one, on a signal that
# depends on the operator syncing a watch. The absent heartbeat is the alarm.
#
# THE MERGE COSTS PER-PATH RESOLUTION AT THE MOMENT OF THE ALARM. Two checks
# told you WHICH ingest path went stale; one monitor tells you that one of them
# did. The recovery is the `msg` on the LAST heartbeat before the silence, which
# carries BOTH ages — a monitor that goes DOWN with a last message of
# `apple_age_h=3 garmin_age_h=22` names garmin without ambiguity — plus this
# pod's log, which carries the full per-bucket verdict. That is why both ages
# are written on every `up` push. Do not trim one.
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
# It is also a SECOND, INDEPENDENT query whose failure degrades the reported AGE
# and nothing else. The freshness query above remains the sole input to the
# push decision, and nothing here goes near it.
set -u
INFLUX=http://influxdb.health.svc.cluster.local:8086
WINDOW=24h              # the freshness window - the ONLY input to the push decision
LOOKBACK=30d            # how far back a STALE bucket's pod-log line looks for the last point

# ---- uptime-kuma push with a short message ------------------------------
# THE HEARTBEAT CARRIES BOTH AGES - a short key=value summary of what this
# run observed. The motivating incident: health-apple-ingest was GREEN while
# Apple Health data had been stale for five days. Nothing malfunctioned; a
# heartbeat is simply one bit, and green could not distinguish "fresh" from
# "stale but the window has not expired yet". The message carries the numbers
# the job had already computed and thrown away.
#
# ONE PUSH PER RUN, AT THE END, AND ONLY WHEN BOTH BUCKETS ARE FRESH. The
# earlier two-check shape needed a UUID argument per call because it drove two
# checks from one process; there is one monitor now, so the URL is a global and
# `check_bucket` no longer sends anything at all. It classifies, and the single
# call site below decides. That removes the whole class of bug the argument
# existed to prevent: there is no second body to leak onto.
#
# A PUSH MUST NEVER FAIL THE JOB, AND A MESSAGE MUST NEVER COST A PUSH.
# NEVER EMIT A COMMAND'S OUTPUT - `make check-ping-bodies` enforces it, and
# spec section 9.2 says why. It applies unchanged to a push: a failing curl
# quotes the URL, and a push URL carries the monitor's token as its last path
# segment. `emit` deliberately keeps its name - that guard recognises a body
# sink by FUNCTION NAME and never by the ping host.
#
# THE TOKEN REACHES THIS SCRIPT AS `PUSH_URL`, NOT AS ITS REAL NAME. Generated
# scripts ride the same envsubst stream as every manifest and envsubst
# substitutes the bare $NAME form as well as ${NAME}, so naming the allowlisted
# variable here - even in a comment - would publish the token inside a
# ConfigMap. `make check-script-substitution` enforces the rename; the real name
# is in homelab/health/ingest-freshness.yaml.
#
# `true >`, not `: >`: a redirection error on a POSIX special built-in aborts
# the shell even behind `|| true`.
MSG_FILE=/tmp/kuma-msg
# The stderr redirection PRECEDES the message redirection in both. Redirections
# are applied left to right, so `>> "$MSG_FILE" 2>/dev/null` cannot suppress the
# shell's own "cannot create" diagnostic - only this order can (verified in dash
# and busybox 1.36.1). The `|| true` is what keeps the job alive on that day;
# this is what keeps its log readable.
msg_reset() { true 2>/dev/null > "$MSG_FILE" || true; }
emit() { { printf '%s ' "$*" | LC_ALL=C tr -cd '\040-\176'; } 2>/dev/null >> "$MSG_FILE" || true; }

# GET https://uptime.cynexia.com/api/push/<token>?status=up&msg=<short>
# `-G --data-urlencode` builds the query safely: the message never has to be
# escaped by hand, and no value is interpolated into the URL string. The msg is
# capped at 200 characters because kuma stores it in one column.
#
# Called with `up` and nothing else, ever. This job has no `down` path by
# design; see the success-only paragraph in the header.
push_kuma() {
  _st=$1
  _m=$(cut -c1-200 "$MSG_FILE" 2>/dev/null) || _m=""
  # Fixed text on failure. No URL and no tool output: the push URL carries the
  # monitor's token, and a pod log is not a place to put one either. Losing this
  # diagnostic would blind the one channel that reports on the reporting
  # mechanism - the healthchecks.io trailing-slash 400 would have shipped
  # undiscovered without its equivalent.
  curl -fsS -m 15 -o /dev/null -G \
    --data-urlencode "status=$_st" \
    --data-urlencode "msg=$_m" \
    "$PUSH_URL" || echo "kuma: push not delivered" >&2
  msg_reset
  return 0
}

# ---- freshness query - the DECISION path, unchanged ----------------------
# Classification is recorded in FAIL_* for the pod log; the pod-log echoes are
# exactly as they were, raw response included. NONE OF IT REACHES THE
# HEARTBEAT: the only things pushed are the two ages, and only on the path
# where both buckets are fresh and no FAIL_* is set at all.
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

# ---- age query - SECOND, INDEPENDENT, and degradable ---------------------
# Bounded at -m 10, SHORTER than the freshness query's -m 30, because this
# one is degradable and that one is not. A timeout, an error or an
# unparseable response yields an age of `unknown` and the run proceeds
# unchanged - the freshness decision never depends on it.
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
  # TRUNCATE THE FRACTION FIRST. InfluxDB formats _time as RFC3339Nano and drops
  # the fractional part only when it is exactly zero, so a point written with
  # sub-second precision arrives as 2026-08-21T05:03:00.123456789Z - and NEITHER
  # form below parses that (verified in curlimages/curl:8.14.1: the GNU form and
  # the busybox -D form both return "invalid date", because -D's strptime wants a
  # literal Z immediately after %S). Without this the run silently loses the age
  # and degrades to "has a point inside 24h", which is the one bit the ages exist
  # to replace. Truncating to whole seconds is lossless for an age rendered in
  # whole hours; the pod log still carries the raw timestamp.
  _es=${1%%.*}
  case "$_es" in *Z) ;; *) _es="${_es}Z" ;; esac
  # GNU date first, busybox's -D form second. If neither parses it, the age
  # reads `unknown`; that is a designed degradation.
  _ep=$(date -u -d "$_es" +%s 2>/dev/null) \
    || _ep=$(date -u -D '%Y-%m-%dT%H:%M:%SZ' -d "$_es" +%s 2>/dev/null) \
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

# ---- age in whole hours --------------------------------------------------
# The heartbeat carries hours, not the NdNhNm form: `apple_age_h=3` beside
# `garmin_age_h=22` is comparable at a glance and against the 24h window, which
# is the whole point of writing both on every push. human_age() is kept for the
# pod-log lines, where the wider form reads better on a bucket that is days old.
age_hours_of() {  # $1 ISO-8601 Z -> whole hours since $1, or nothing
  _he=$(epoch_of "$1") || return 0
  [ -n "$_he" ] || return 0
  _hn=$(date -u +%s 2>/dev/null) || return 0
  case "$_hn" in ''|*[!0-9]*) return 0 ;; esac
  [ "$_hn" -ge "$_he" ] || return 0
  printf '%d' $(( (_hn - _he) / 3600 ))
}

# ---- per-bucket driver ---------------------------------------------------
# $1 bucket, $2 label for the pod log. The labels name the INGEST PATH, not a
# monitor: both paths now report through the one `health-ingest` monitor, and
# the two healthchecks.io check names they used to carry are being deleted.
#
# IT CLASSIFIES; IT DOES NOT PUSH. Returns 0 when the bucket is fresh and
# non-zero otherwise, and leaves the bucket's age in whole hours in AGE_H
# (`unknown` if the age query degraded). The single push decision is made by
# the call site below, because there is one monitor for both buckets.
#
# FRESH  -> return 0, AGE_H set. The caller pushes only if BOTH return 0.
# STALE  -> return 1. Nothing is pushed by anyone; the pod log says why.
# FAILED -> return 1. Same, and the pod log keeps "query failed" apart from
#           "no data", which the single monitor's colour cannot.
check_bucket() {
  _b=$1; _name=$2
  AGE_H=unknown
  if fresh "$_b"; then
    _ts=$(newest_time "$_b" "$WINDOW")
    _age=$(age_of "$_ts")
    _ah=$(age_hours_of "$_ts")
    [ -n "$_ts" ] || _ts=unknown
    [ -n "$_age" ] || _age=unknown
    [ -z "$_ah" ] || AGE_H=$_ah
    # check-ping-bodies: untaint AGE_H - whole hours from age_hours_of, which prints one %d and nothing else; never a slice of the response
    echo "$_b: FRESH ($_name) - last point $_ts, age $_age (${AGE_H}h), window $WINDOW"
    return 0
  fi

  # Preserved from the original: the one line that says a heartbeat was
  # withheld. There is no per-bucket push to withhold any more, so it says what
  # it now means - this run will not push at all.
  echo "$_b: NOT pushed ($_name)"
  if [ "$FAIL_KIND" = stale ]; then
    _ts=$(newest_time "$_b" "$LOOKBACK")
    _age=$(age_of "$_ts")
    [ -n "$_ts" ] || _ts=unknown
    [ -n "$_age" ] || _age=unknown
    echo "$_b: STALE - no points in $WINDOW; last point $_ts, age $_age," \
         "looked back $LOOKBACK"
  else
    # A FAILED QUERY IS NOT STALE DATA, and the pod log must keep them apart
    # just as the script does. Note that the ALERT still cannot: both
    # produce a DOWN at interval-plus-retry with opposite remedies (sync a
    # watch vs fix a token). Separating them at the alerting layer needs a
    # second monitor, which is out of scope.
    echo "$_b: QUERY FAILED - not verified; failure=$FAIL_KIND" \
         "http_code=${FAIL_HTTP:-none} curl_rc=${FAIL_CURL_RC:-none}" \
         "influx_code=${INFLUX_CODE:-none}"
  fi
  return 1
}

# ---- the one push decision -----------------------------------------------
# BOTH buckets are checked before anything is decided: `set -e` is absent and
# neither call short-circuits the other, so a stale apple bucket never hides
# garmin's state from the pod log.
AGE_H=unknown
APPLE_OK=0
GARMIN_OK=0
if check_bucket apple_metrics apple-ingest; then APPLE_OK=1; fi
APPLE_AGE_H=$AGE_H
# check-ping-bodies: untaint APPLE_AGE_H - copied from AGE_H, already untainted above
if check_bucket garmin garmin-ingest; then GARMIN_OK=1; fi
GARMIN_AGE_H=$AGE_H
# check-ping-bodies: untaint GARMIN_AGE_H - copied from AGE_H, already untainted above

if [ "$APPLE_OK" -eq 1 ] && [ "$GARMIN_OK" -eq 1 ]; then
  msg_reset
  emit "verdict=fresh"
  # BOTH AGES, ALWAYS. This is the merged monitor's only per-path resolution:
  # the last message before the silence is what names which bucket was ageing.
  emit "apple_age_h=$APPLE_AGE_H"
  emit "garmin_age_h=$GARMIN_AGE_H"
  emit "window=$WINDOW"
  push_kuma up
else
  echo "==> not pushing: apple_fresh=$APPLE_OK garmin_fresh=$GARMIN_OK." \
       "Silence is the alarm; the monitor goes DOWN at its interval plus retry."
fi

# ALWAYS 0. See the header: a stale source must not also show up as a failed Job.
exit 0
