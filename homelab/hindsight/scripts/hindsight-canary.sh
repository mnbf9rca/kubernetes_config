#!/bin/sh
# The hindsight canary. Runs hourly in the `hindsight-canary` CronJob
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
# write path. A cluster that is down runs no CronJob, so it pushes nothing at all
# and the monitor goes DOWN at its heartbeat interval plus retry: the
# dead-man's-switch covers scheduling loss too, without needing a start signal.
#
# WHY NOT AN uptime-kuma HTTP MONITOR, which is a different question from where
# this run REPORTS to. kuma runs on the VPS cluster and probes from the Hetzner
# IP; every *.cynexia.net record resolves to a private LAN address, so the VPS
# has no route in and an inbound probe would be permanently down. A PUSH monitor
# reverses the direction - this pod calls out to uptime.cynexia.com through the
# Access bypass - which is why the reporting side moved to kuma on 2026-08-26
# while the probing side could not. See docs/operations/monitoring.md and
# docs/operations/uptime-kuma.md.
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

# ---- uptime-kuma push with a short message --------------------------------
# Same contract as every other scheduled job here: the exit code, from an EXIT
# trap, so a failure can never be silence. Since 2026-08-26 it drives the
# `hindsight-canary` uptime-kuma PUSH monitor rather than a healthchecks.io
# check. There is NO /start push - the push API has no such concept, so
# activeDeadlineSeconds is the whole of the hang bound and the monitor's
# heartbeat interval plus retry is the silence bound - and the message is one
# short line, with the detail in this pod's log.
#
# NEVER EMIT A COMMAND'S OUTPUT. The recall response carries memory text — the
# thing this whole system exists to keep private — and a failing curl quotes the
# URL, which for a push carries the monitor's token as its last path segment.
# Everything emitted below is a digit-gated HTTP status, a count, or a verdict
# from a fixed enum. `emit` keeps its name: `make check-ping-bodies` recognises a
# body sink by FUNCTION NAME and never by the ping host.
#
# THE TOKEN REACHES THIS SCRIPT AS `PUSH_URL`, NOT AS ITS REAL NAME, for the same
# envsubst reason as CANARY_API_KEY above.
#
# `true >`, not `: >`: a redirection error on a POSIX special built-in aborts the
# shell even behind `|| true`. Tokens are space-separated on ONE line, because a
# kuma msg is one line.
MSG_FILE=/tmp/kuma-msg
# The stderr redirection PRECEDES the message redirection, so the shell's own
# "cannot create" diagnostic reaches the pod log instead of being swallowed.
msg_reset() { true 2>/dev/null > "$MSG_FILE" || true; }
# shellcheck disable=SC2329 # called only from on_exit, which runs from the EXIT trap.
emit() { { printf '%s ' "$*" | LC_ALL=C tr -cd '\040-\176'; } 2>/dev/null >> "$MSG_FILE" || true; }

# GET https://uptime.cynexia.com/api/push/<token>?status=up|down&msg=<short>
# curl, because this container is curlimages/curl:8.14.1 (re-probed in-cluster
# 2026-08-26: curl present). `-G --data-urlencode` builds the query safely, so
# the message never has to be escaped by hand and no value is interpolated into
# the URL string. Capped at 200 characters, the width kuma stores.
# shellcheck disable=SC2329 # called only from on_exit, which runs from the EXIT trap.
push_kuma() {
  _st=$1
  _m=$(cut -c1-200 "$MSG_FILE" 2>/dev/null) || _m=""
  curl -fsS -m 15 -o /dev/null -G \
    --data-urlencode "status=$_st" \
    --data-urlencode "msg=$_m" \
    "$PUSH_URL" || echo "kuma: push not delivered" >&2
  msg_reset
  return 0
}

# ---- message values --------------------------------------------------------
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
  # The full detail goes to the pod log; the verdict and the two HTTP statuses
  # travel with the alert, which is what tells retain-failed from recall-failed
  # without opening the pod.
  echo "detail: rc=$_xrc verdict=$VERDICT bank=$BANK retain_http=$RETAIN_HTTP" \
       "recall_http=$RECALL_HTTP results=$RESULTS"
  msg_reset
  emit "verdict=$VERDICT"
  emit "retain_http=$RETAIN_HTTP"
  emit "recall_http=$RECALL_HTTP"
  emit "results=$RESULTS"
  if [ "$_xrc" -eq 0 ]; then
    push_kuma up
  else
    push_kuma down
  fi
  exit "$_xrc"
}
trap on_exit EXIT

msg_reset

# ---- the request bodies ---------------------------------------------------
# A FIXED sentinel fact. NOTHING DEDUPLICATES IT: every run adds one memory, so
# the bank grows without bound at 24 a day (253 units on 2026-09-03), and that
# costs nothing worth acting on because consolidation is windowed at about 7,000
# input tokens per run whatever the bank holds (measured the same day).
# `"async": false` makes retain synchronous, so the recall below tests the same
# write this run performed rather than a previous one's.
cat > "$REQ" <<'JSON'
{"items":[{"content":"The hindsight canary writes this sentence on every run to prove the write path is alive.","context":"hindsight canary"}],"async":false}
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
# The result count is derived structurally: count occurrences of the per-result
# "scores" key (verified at rollout 2026-08-24 — the 0.9.1 response is
# {"results":[{...,"scores":...},...],"entities":{...}} and carries no
# num_results trace field; "scores" appears exactly once per result object and
# nowhere else). A count is a number this estate permits in a ping body; no
# slice of the response ever leaves this pipeline.
RESULTS=$(grep -c '"scores"' "$RESP")
case "$RESULTS" in ''|*[!0-9]*) RESULTS=0 ;; esac
# check-ping-bodies: untaint RESULTS - grep -c occurrence count of a fixed literal key, gated to digits by the case above; never a slice of the response
if [ "$RESULTS" -lt 1 ]; then
  VERDICT=recall-miss
  echo "ERROR: recall succeeded but returned no results from bank $BANK" >&2
  exit 1
fi

VERDICT=ok
echo "==> done - $RESULTS result(s)"
exit 0
