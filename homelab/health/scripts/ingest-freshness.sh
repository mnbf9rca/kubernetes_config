#!/bin/sh
# This job DELIBERATELY always exits 0. The alerting signal is the ABSENT
# healthchecks.io ping (dead-man's-switch), not the job's exit code — a stale
# source must not also show up as a failed Job. Do not "fix" this into a
# non-zero exit. `set -e` is deliberately absent for the same reason: it would
# abort the run before the second source was ever checked.
#
# DO NOT reintroduce group()/last()/keep() into the query. The "tidier" form
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
set -u
INFLUX=http://influxdb.health.svc.cluster.local:8086

fresh() {  # $1 bucket → 0 if a point exists in the last 24h, else log why and return 1
  OUT=$(curl -sS -m 30 -w '\n%{http_code}' "$INFLUX/api/v2/query?org=cynexia" \
    -H "Authorization: Token $TOKEN" -H 'Content-Type: application/vnd.flux' \
    -d "from(bucket:\"$1\") |> range(start:-24h) |> limit(n:1)" 2>&1)
  RC=$?
  CODE=$(printf '%s\n' "$OUT" | tail -n1)
  BODY=$(printf '%s\n' "$OUT" | sed '$d')
  # A FAILED QUERY IS NOT STALE DATA. Previously an InfluxDB error, a bad token,
  # a DNS failure and genuinely stale data all produced the same silent outcome.
  # Each now logs the raw response so the reason is visible to whoever looks.
  # Ping behaviour is unchanged and fail-safe: no ping on any of them.
  if [ "$RC" -ne 0 ]; then
    echo "$1: QUERY FAILED (curl rc=$RC): $BODY"; return 1
  fi
  case "$CODE" in
    2??) ;;
    *) echo "$1: QUERY FAILED (HTTP $CODE): $BODY"; return 1 ;;
  esac
  if printf '%s\n' "$BODY" | grep -q '"code":"'; then
    echo "$1: QUERY FAILED (InfluxDB error): $BODY"; return 1
  fi
  # Annotated-CSV data rows begin ",_result," — match that, not a bare "_time",
  # which an error payload could in principle also contain.
  if printf '%s\n' "$BODY" | grep -q '^,_result,'; then
    return 0
  fi
  echo "$1: STALE — query succeeded but returned no points in the last 24h"
  return 1
}

fresh apple_metrics && curl -fsS -m 15 "https://hc-ping.com/$HC_APPLE" >/dev/null || echo "apple: NOT pinged"
fresh garmin && curl -fsS -m 15 "https://hc-ping.com/$HC_GARMIN" >/dev/null || echo "garmin: NOT pinged"
exit 0
