#!/bin/sh
# The hindsight canary. Runs every 15 minutes in the `hindsight-canary` CronJob
# and is the ONLY thing that notices two of this design's failures:
#
#   F1  the API, the database or the whole cluster is down. Hermes is fail-open at
#       four layers: turns proceed with no memories and retains are dropped with a
#       logger.warning, so a dead server looks like an agent with amnesia rather
#       than an outage. Nothing client-side will ever complain.
#
#   F12 retains failing against a HEALTHY server — a rotated or mistyped tenant
#       key, a client timeout, a contract break after an image bump. `/health`
#       checks database connectivity, not auth validity, so every server-side
#       signal stays green while every write is dropped. This is the
#       garmin-grafana failure class, and an unauthenticated probe cannot see it.
#
# So the canary AUTHENTICATES WITH THE REAL TENANT KEY and exercises the real
# write path. A cluster that is down never sends the start ping, so the
# healthchecks.io dead-man's-switch covers scheduling loss too.
#
# WHY NOT uptime-kuma: kuma runs on the VPS cluster and probes from the Hetzner
# IP; every *.cynexia.net record resolves to a private LAN address, so the VPS has
# no route to it and the monitor would be permanently down. See
# docs/operations/monitoring.md.
#
# THE KEY REACHES THIS SCRIPT AS `CANARY_API_KEY`, NOT `HINDSIGHT_TENANT_API_KEY`.
# Generated scripts ride the same envsubst stream as every manifest and envsubst
# substitutes the bare $NAME form as well as ${NAME}, so naming an
# ENVSUBST_VAR_NAMES variable here would publish the tenant key in plaintext inside
# a ConfigMap. `make check-script-substitution` enforces it.
#
# THE KEY NEVER TOUCHES argv. It is written into a curl config on stdin by a shell
# BUILT-IN (`printf`), so it exists in no process's command line and in no file.
set -eu

# hindsight-api is reached over the in-cluster Service, not through Traefik: the
# canary must fail when the API is broken, not when DNS or the ingress is.
API=http://hindsight-api.hindsight.svc.cluster.local:8888
# The tenant path segment. Self-hosted Hindsight serves a single tenant named
# `default`; verify against /docs at rollout step 6 before trusting a red.
TENANT=default
# A dedicated bank, so the canary can never appear in a Hermes profile's recall
# and its writes can never grow a real bank. Banks auto-create on first write.
BANK=canary
REQ=/tmp/canary-retain.json
QRY=/tmp/canary-recall.json
RESP=/tmp/canary-response.json

# ---- healthchecks.io ping with a body ------------------------------------
# Same contract as every other scheduled job here: /start plus the exit code, from
# an EXIT trap, so a failure can never be silence.
#
# NEVER EMIT A COMMAND'S OUTPUT. The recall response carries memory text — the
# thing this whole system exists to keep private — and a failing curl quotes the
# ping URL, which IS the check's write credential. Everything emitted below is a
# digit-gated HTTP status, a count, or a verdict from a fixed enum.
# `true >`, not `: >`: a redirection error on a POSIX special built-in aborts the
# shell even behind `|| true`. A bare trailing slash is an HTTP 400, so the URL is
# built conditionally.
HC_BODY=/tmp/hc-body
# The stderr redirection PRECEDES the body redirection, so the shell's own "cannot
# create" diagnostic reaches the pod log instead of being swallowed.
hc_reset() { true 2>/dev/null > "$HC_BODY" || true; }
emit() { { printf '%s' "$*" | LC_ALL=C tr -cd '\040-\176'; printf '\n'; } 2>/dev/null >> "$HC_BODY" || true; }

ping_hc() {
  _sf=${1:-}
  _u="https://hc-ping.com/$HC_UUID"
  [ -z "$_sf" ] || _u="$_u/$_sf"
  if [ -s "$HC_BODY" ]; then
    if curl -fsS -m 15 -o /dev/null --data-binary @"$HC_BODY" "$_u"; then
      hc_reset; return 0
    fi
    echo "hc: body POST failed, retrying without a body" >&2
  fi
  # Fixed text. No URL and no tool output.
  curl -fsS -m 15 -o /dev/null "$_u" || echo "hc: ping not delivered" >&2
  hc_reset
  return 0
}

# ---- body values ----------------------------------------------------------
# VERDICT is a fixed enum and starts at the failure that is true before anything
# has run; each successful phase narrows it. `unknown` sentinels keep `set -u`
# harmless inside the trap.
VERDICT=retain-failed
RETAIN_HTTP=unknown
RECALL_HTTP=unknown
RESULTS=unknown

# shellcheck disable=SC2329 # invoked by `trap ... EXIT` below, not by name.
on_exit() {
  _xrc=$?
  trap - EXIT
  rm -f "$REQ" "$QRY" "$RESP" 2>/dev/null || true
  hc_reset
  if [ "$_xrc" -eq 0 ]; then
    emit "summary=ok - retain and recall both succeeded against bank $BANK"
  else
    emit "summary=FAILED rc=$_xrc - hindsight-canary"
  fi
  emit "rc=$_xrc"
  emit "verdict=$VERDICT"
  emit "retain_http=$RETAIN_HTTP"
  emit "recall_http=$RECALL_HTTP"
  emit "results=$RESULTS"
  ping_hc "$_xrc"
  exit "$_xrc"
}
trap on_exit EXIT

hc_reset
emit "summary=starting"
ping_hc start

# ---- the request bodies ---------------------------------------------------
# A FIXED sentinel fact, so Hindsight's dedup (present since 0.5.0, satisfied by
# the image pin) keeps this bank at one memory however many thousand times the
# canary runs. `"async": false` makes retain synchronous, so the recall below
# tests the same write this run performed rather than a previous one's.
cat > "$REQ" <<'JSON'
{"items":[{"content":"The hindsight canary runs every fifteen minutes and writes this sentence to prove the write path is alive.","context":"hindsight canary"}],"async":false}
JSON
cat > "$QRY" <<'JSON'
{"query":"What does the hindsight canary write?","budget":"mid"}
JSON

# ---- auth ------------------------------------------------------------------
# `printf` is a shell built-in, so the key is never an argument to an executed
# program and never reaches a process listing. curl reads it as a config file from
# stdin, which is why neither request may use `@-` for its body.
auth_config() { printf 'header = "Authorization: Bearer %s"\n' "$CANARY_API_KEY"; }

# ---- 1. retain -------------------------------------------------------------
echo "==> retain into bank $BANK"
RETAIN_HTTP=$(auth_config | curl -sS -m 60 -K - \
  -o /dev/null -w '%{http_code}' \
  -X POST "$API/v1/$TENANT/banks/$BANK/memories" \
  -H 'Content-Type: application/json' \
  --data-binary @"$REQ") || RETAIN_HTTP=000
case "$RETAIN_HTTP" in ''|*[!0-9]*) RETAIN_HTTP=000 ;; esac
# check-ping-bodies: untaint RETAIN_HTTP - curl's %{http_code}, gated to digits by the case above; the response body is discarded to /dev/null
case "$RETAIN_HTTP" in
  2??) ;;
  *) echo "ERROR: retain returned HTTP $RETAIN_HTTP" >&2; exit 1 ;;
esac

# ---- 2. recall -------------------------------------------------------------
VERDICT=recall-failed
echo "==> recall from bank $BANK"
RECALL_HTTP=$(auth_config | curl -sS -m 60 -K - \
  -o "$RESP" -w '%{http_code}' \
  -X POST "$API/v1/$TENANT/banks/$BANK/memories/recall" \
  -H 'Content-Type: application/json' \
  --data-binary @"$QRY") || RECALL_HTTP=000
case "$RECALL_HTTP" in ''|*[!0-9]*) RECALL_HTTP=000 ;; esac
# check-ping-bodies: untaint RECALL_HTTP - curl's %{http_code}, gated to digits by the case above
case "$RECALL_HTTP" in
  2??) ;;
  *) echo "ERROR: recall returned HTTP $RECALL_HTTP" >&2; exit 1 ;;
esac

# ---- 3. assert the recall found something ----------------------------------
# THE ASSERTION IS "AT LEAST ONE RESULT", NOT "THE SENTENCE CAME BACK VERBATIM".
# Retain runs the extraction LLM over the content, so what lands in the bank is a
# rewritten memory, not the input string; asserting on the input's own words would
# make the canary red whenever the model phrased things differently. What this
# canary exists to prove is the SILENT WRITE PATH — auth validity, the retain
# endpoint, the database, and the local embedding and reranking runtime that
# recall exercises. Extraction QUALITY is an accepted residual (R3) that no
# automated check here judges.
#
# num_results is read by positive match into a bounded digit class from the
# response's own trace field. That is the one extraction from a remote response
# this estate permits, and it cannot be a slice of memory text.
RESULTS=$(sed -n 's/.*"num_results"[[:space:]]*:[[:space:]]*\([0-9]\{1,9\}\).*/\1/p' "$RESP" | head -n 1)
case "$RESULTS" in ''|*[!0-9]*) RESULTS=0 ;; esac
# check-ping-bodies: untaint RESULTS - positive-match [0-9]{1,9} extraction of the response's own num_results field, gated to digits by the case above; never a slice of the response
if [ "$RESULTS" -lt 1 ]; then
  VERDICT=recall-miss
  echo "ERROR: recall succeeded but returned no results from bank $BANK" >&2
  exit 1
fi

VERDICT=ok
echo "==> done - $RESULTS result(s)"
exit 0
