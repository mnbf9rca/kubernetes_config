#!/bin/sh
# Update the Hermes agent and its two venv passengers together, prove the result
# still serves, and roll back to last-good when it does not.
#
# DELIBERATELY NOT SCHEDULED. `hermes update` sometimes carries manual steps
# (config schema migrations), so this runs on demand — by hand, or from the
# 4-to-6-week update session — never from a timer. There is no .timer unit for
# THIS script and adding one is a design change, not a tidy-up. (The sibling
# hermes-app-alive.sh does have a timer; it is a heartbeat, not an update.)
#
# Canonical copy: hermes-vm/scripts/hermes-update.sh in
# github.com/mnbf9rca/kubernetes_config. Runbook:
# docs/operations/hermes-vm-updates.md. Installed on VM 103 at
# /home/hermes/bin/hermes-update.sh, run as the `hermes` user — normally through
# /usr/local/bin/hermes-update, which starts the systemd unit that runs this.
set -eu

# ---- pure helpers (unit-tested by test-hermes-update.sh) -------------------
# NO CONSTANTS IN THIS BLOCK. They arrive in the next append, beside their
# consumers: a constant with no reader is SC2034 and would make
# `make check-vm-scripts` fail on this very commit.

# True when $1 is exactly a 40-character lowercase hex object name — the shape
# `git rev-parse` prints. This is the webui phase's FAIL-CLOSED gate and it is
# what replaced the retired newest-tag policy: the checkout only ever moves to
# something that passed this. A failed fetch, a renamed default branch or a ref
# name instead of a SHA all fail it, and the caller exits BEFORE the tree moves.
#
# `case` matches the WHOLE argument, which is the point. The grep this replaced
# matched per LINE, so 'junk<newline>3b9c...' passed it — strictly more than the
# contract above allows, and enough to put a newline into the ping body that
# Task 2's `untaint` marker promises is 40 hex characters.
#
# The character list is enumerated rather than written as an `a-f` RANGE because
# range endpoints in a bracket expression are collation-dependent, and under
# some locales that range takes in uppercase letters. git never prints
# uppercase, so this must never accept it.
valid_sha40() {
  _s=${1:-}
  case $_s in
    '' | *[!0123456789abcdef]*) return 1 ;;
  esac
  [ ${#_s} -eq 40 ]
}

# True when $1 is exactly X.Y.Z with no prefix, suffix or fourth component, each
# component one or more digits. Whole-argument match for the same reason as
# valid_sha40: the grep this replaced accepted a conforming line inside
# multi-line text. The cases reject, in order, an empty string or any character
# outside digits and dots (a newline included), a fourth component, and anything
# without exactly two dots; the second `case` then rejects an empty component —
# a leading dot, a trailing dot, or '1..2'.
valid_semver() {
  _sv=${1:-}
  case $_sv in
    '' | *[!0123456789.]*) return 1 ;;
    *.*.*.*) return 1 ;;
    *.*.*) ;;
    *) return 1 ;;
  esac
  case $_sv in
    .* | *. | *..*) return 1 ;;
  esac
  return 0
}

# Seconds since the epoch of $1's mtime. GNU stat first, BSD stat second, so the
# tests run on the operator's macOS as well as on the VM.
file_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"
}

# Whole days between $1's mtime and the epoch second $2. 9999 when $1 is absent,
# which is the same signal as "far too old" and keeps the caller branch-free.
apt_age_days() {
  if [ ! -f "$1" ]; then
    printf '9999\n'
    return 0
  fi
  _m=$(file_mtime "$1")
  printf '%s\n' "$(( ( $2 - _m ) / 86400 ))"
}

# Print the value of key $1 from $LAST_GOOD. Returns 1 when the file or the key
# is absent. The file is PARSED, never sourced: it is state this script wrote,
# but sourcing it would execute whatever a future bug puts there.
lg_get() {
  [ -f "$LAST_GOOD" ] || return 1
  _v=$(sed -n "s/^$1=\\(.*\\)\$/\\1/p" "$LAST_GOOD" | head -n 1)
  [ -n "$_v" ] || return 1
  printf '%s\n' "$_v"
}

# ---- paths and constants ---------------------------------------------------
# Defined HERE, not in the helpers block above, so that every one of them has a
# consumer in the same commit: shellcheck raises SC2034 for a constant nothing
# reads, and `make check-vm-scripts` would fail on the helpers-only tree.
HERMES_BIN=/home/hermes/.local/bin/hermes
HERMES_HOME=/home/hermes/.hermes
AGENT_DIR=$HERMES_HOME/hermes-agent
VENV=$AGENT_DIR/venv/bin
WEBUI_DIR=/home/hermes/hermes-webui
# The passenger tracks origin/master. The newest-tag policy was RETIRED on
# 2026-08-26 against observed upstream practice: upstream stopped tagging five
# weeks earlier and the newest tag was 560 commits BEHIND the deployed master,
# so a tag rule's first act would have been a rollback of the origin the Hermex
# iOS app talks to. Do not reinstate it, and do not invent a "newest tag, or
# master if master is ahead" hybrid. Task 0 Step 3 re-confirms both names.
WEBUI_BRANCH=master
WEBUI_REMOTE=origin
LAST_GOOD=$HERMES_HOME/hermes-update.last-good
WEBUI_LAST_GOOD=$HERMES_HOME/webui.last-good
# The agent's own env file. Read for exactly two non-secret-shaped decisions and
# one secret that never leaves this process: API_SERVER_ENABLED, and the default
# profile's API_SERVER_KEY for the health chat turn. NEVER cat this file — it
# also holds OP_SERVICE_ACCOUNT_TOKEN.
AGENT_ENV=$HERMES_HOME/.env

# The stamp `unattended-upgrade` writes for ITSELF, in write_stamp_file():
# open(os.path.join(statedir, "unattended-upgrades-stamp"), "w"). The other
# files in that directory are apt.systemd.daily's and are not this. There is no
# "-success" file and nothing creates one: gating on that name would return
# 9999 forever and make this check permanently red after every SUCCESSFUL run.
# VERIFIED on VM 103, 2026-08-26: /var/lib/apt/periodic/ holds exactly four
# files - download-upgradeable-stamp, unattended-upgrades-stamp, update-stamp
# and upgrade-stamp - and the one below was written that morning. The constant
# is correct as written; do not change it.
APT_STAMP=/var/lib/apt/periodic/unattended-upgrades-stamp
APT_MAX_AGE_DAYS=14

HINDSIGHT_URL=https://hindsight.cynexia.net
WEBUI_HEALTH=http://127.0.0.1:8787/health
# The DEFAULT profile's own API server, on loopback. UNPREFIXED: there is no
# probe profile and nothing is published. /p/<profile>/ routing exists in the
# code but is inert here (gateway.multiplex_profiles is off), so a prefix would
# reach this same listener anyway — the plain path says what is meant.
CHAT_URL=http://127.0.0.1:8642/v1/chat/completions
UNITS="hermes-gateway hermes-gateway-emh hermes-gateway-hal hermes-dashboard hermes-webui"
UNIT_COUNT=5

# Scratch under $HERMES_HOME at mode 0700, NEVER /tmp. Every agent session on
# this VM runs as this same user with an unfiltered os.environ.copy()
# (homelab.md:582-591), and the VM's documented posture is "the LAN is a trusted
# zone". With a fixed /tmp path, a pre-planted symlink at the response file
# makes `curl -o` overwrite an arbitrary file, and a symlink at the ping-body
# file makes `emit`'s >> append to it AND makes ping_hc POST its contents to
# healthchecks.io — an exfiltration channel through the one mechanism this repo
# has a dedicated guard to keep clean. The cluster scripts' /tmp/hc-body is safe
# only because a pod filesystem is private; this is a shared host.
# PrivateTmp= is NOT relied on: user units cannot always use it, and the script
# is also run directly over ssh.
RUNDIR=$HERMES_HOME/hermes-update.run
HC_BODY=$RUNDIR/hc-body
CHAT_REQ=$RUNDIR/chat-req.json
CHAT_RESP=$RUNDIR/chat-resp.json

# ---- argument parsing ------------------------------------------------------
# `run`  — update, assert, roll back on failure, ping.
# `seed` — assert health and record last-good. No update, no ping. Used once at
#          install, so the very first real run has a rollback target that has
#          already passed the assertion.
parse_mode() {
  case "${1:-}" in
    '')     printf 'run\n' ;;
    --seed) printf 'seed\n' ;;
    *)      echo "usage: hermes-update.sh [--seed]" >&2; exit 64 ;;
  esac
}

# `ok` for any 2xx, `bad` for everything else including the empty string.
classify_chat() {
  case "${1:-}" in
    2??) printf 'ok\n' ;;
    *)   printf 'bad\n' ;;
  esac
}

# ---- healthchecks.io -------------------------------------------------------
# Same contract as the cluster jobs that use healthchecks.io: /start plus the
# exit code, the exit ping from an EXIT trap, so a failure can never be silence.
#
# NEVER EMIT A COMMAND'S OUTPUT. A failing curl quotes the ping URL, which IS
# this check's write credential, and a chat response carries whatever the agent
# said. Everything emitted below is a counter, a digit-gated HTTP status, a
# shape-gated version string or object name, or a verdict from a fixed enum.
hc_reset() { true 2>/dev/null > "$HC_BODY" || true; }
emit() { { printf '%s' "$*" | LC_ALL=C tr -cd '\040-\176'; printf '\n'; } 2>/dev/null >> "$HC_BODY" || true; }

ping_hc() {
  _sf=${1:-}
  _u="https://hc-ping.com/$HERMES_UPDATE_HC_UUID"
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

# ---- body values -----------------------------------------------------------
# VERDICT is a fixed enum and starts at the failure that is true before anything
# has run; each successful phase narrows it. CHAT_MODE is a second fixed enum
# recording WHICH assertion was performed - `chat` when a real turn was made,
# `skipped-api-disabled` or `skipped-no-literal-key` when it degraded to the
# three free checks. A green run that skipped the turn is visibly weaker than
# one that made it, and the body has to say so. The sentinels keep `set -u`
# harmless inside the trap.
VERDICT=update-failed
AGENT_CHANGED=no
WEBUI_CHANGED=no
CLIENT_CHANGED=no
ROLLBACK_SOURCE=none
UNITS_ACTIVE=0
CHAT_HTTP=000
CHAT_MODE=not-attempted
WEBUI_SHA=unknown
CLIENT_VERSION=unknown
APT_AGE=9999
RUN_EPOCH=0

# ---- phases ----------------------------------------------------------------

restart_units() {
  # `hermes update` restarts the three GATEWAY units itself (its --plan output
  # says so). It knows nothing about hermes-webui or hermes-dashboard, because
  # neither runs the hermes entry point. So this is a second restart of the
  # gateways and the ONLY restart of the other two. Documented rather than left
  # to be discovered from a journal.
  echo "==> restarting the five hermes user units"
  # shellcheck disable=SC2086 # UNITS is a deliberate word-split list of unit names
  systemctl --user restart $UNITS
  # The gateways bind their listeners after systemd reports the unit started.
  sleep 15
}

install_webui_requirements() {
  # The revision must actually carry the file. Checked rather than assumed:
  # a fetch that landed on an unexpected tree fails here rather than installing
  # nothing and reporting success.
  if [ ! -f "$WEBUI_DIR/requirements.txt" ]; then
    echo "ERROR: no requirements.txt at this hermes-webui revision" >&2
    return 1
  fi
  # Constrain the install to what the venv already has. This venv is what all
  # five units execute from; without -c, an upstream requirements.txt that
  # raised a floor past one of hermes-agent's pyproject pins would silently
  # mutate their runtime and pip would report success.
  _c=$(mktemp)
  # `|| true` then an emptiness check, NOT a bare pipeline: there is no pipefail
  # in POSIX sh, so the pipeline's status is grep's, and grep exits 1 on no
  # match. Under errexit inside rollback() that would abort the rollback midway
  # — after `git reset --hard` and `git checkout` had already run — leaving the
  # agent restored and the webui dependencies not.
  "$VENV/pip" freeze --local | grep -E '^[A-Za-z0-9._-]+==' > "$_c" || true
  if [ ! -s "$_c" ]; then
    rm -f "$_c"
    echo "ERROR: pip freeze produced no pinned requirements - refusing to install unconstrained" >&2
    return 1
  fi
  if ! "$VENV/pip" install -q -r "$WEBUI_DIR/requirements.txt" -c "$_c"; then
    rm -f "$_c"
    return 1
  fi
  rm -f "$_c"
}

# The deployed hindsight server's version, over plain HTTP. /health/live is
# unauthenticated — the cluster's own probes call it with no credential — and it
# describes what is RUNNING, which is the whole point: the VM holds no
# kubeconfig and the repo pin is intent, not state.
deployed_hindsight_version() {
  curl -fsS -m 15 "$HINDSIGHT_URL/health/live" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))'
}

installed_client_version() {
  "$VENV/python" -c 'import importlib.metadata as m; print(m.version("hindsight-client"))' 2>/dev/null || printf '\n'
}

# The value of KEY in the agent's own .env, with surrounding quotes stripped.
# Printed nowhere: the caller shape-gates it and never emits it.
agent_env_value() {
  sed -n "s/^$1=//p" "$AGENT_ENV" 2>/dev/null | head -n 1 \
    | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}

# 0 when the app is genuinely working. Four assertions, the last of which is
# conditional:
#   1. all five user units active;
#   2. the shared venv can import run_agent — the module the WebUI's
#      api/agent_runtime.py imports and api/streaming.py instantiates, which
#      pulls in dotenv, httpx and openai. This is the ONLY cheap check that
#      catches the documented silent failure: a venv missing those leaves every
#      unit active and /health answering `status: ok` while the iOS app says
#      `AIAgent not available` (homelab.md, "Do not give it a venv of its own").
#      Importing hermes_cli.main instead would NOT catch it;
#   3. the WebUI answers its own /health on loopback;
#   4. a real chat turn against the DEFAULT profile's local API server — WHEN
#      that server is enabled and its key is a usable literal. When it is not,
#      the turn is skipped, the run still passes on 1-3, and CHAT_MODE records
#      which fallback fired. That degradation is deliberate (operator ruling,
#      2026-08-26) and must stay visible in the body rather than silent.
assert_health() {
  UNITS_ACTIVE=0
  for _u in $UNITS; do
    if systemctl --user is-active --quiet "$_u"; then
      UNITS_ACTIVE=$(( UNITS_ACTIVE + 1 ))
    fi
  done
  if [ "$UNITS_ACTIVE" -ne "$UNIT_COUNT" ]; then
    echo "ERROR: $UNITS_ACTIVE of $UNIT_COUNT hermes units active" >&2
    return 1
  fi

  if ! "$VENV/python" -c 'import run_agent' >/dev/null 2>&1; then
    echo "ERROR: the agent venv cannot import run_agent" >&2
    return 1
  fi

  if ! curl -fsS -m 15 -o /dev/null "$WEBUI_HEALTH"; then
    echo "ERROR: webui /health did not answer" >&2
    return 1
  fi

  # Rollback-drill hook. Documented in docs/operations/hermes-vm-updates.md and
  # exercised by Task 9 of this plan; unset in every ordinary run.
  if [ "${HERMES_UPDATE_FORCE_HEALTH_FAIL:-0}" = "1" ]; then
    echo "ERROR: forced health failure (HERMES_UPDATE_FORCE_HEALTH_FAIL=1)" >&2
    CHAT_HTTP=000
    CHAT_MODE=forced-fail
    return 1
  fi

  _enabled=$(agent_env_value API_SERVER_ENABLED) || _enabled=""
  if [ "$_enabled" != "true" ]; then
    CHAT_MODE=skipped-api-disabled
    echo "NOTE: API_SERVER_ENABLED is not true - skipping the chat turn" >&2
    return 0
  fi

  # Shape gate on the key BEFORE it is interpolated into a curl config file.
  # It must stay in [A-Za-z0-9_-]: a value holding a double quote or backslash
  # would silently truncate or mangle the header and produce a 401 with no clue
  # why, and an UNRESOLVED `op://...` reference fails this gate on its `:` and
  # `/` — which is exactly the right outcome, because it degrades to the
  # documented fallback instead of sending a meaningless bearer token.
  _k=$(agent_env_value API_SERVER_KEY) || _k=""
  case "$_k" in
    *[!A-Za-z0-9_-]*|'')
      CHAT_MODE=skipped-no-literal-key
      echo "NOTE: no usable literal API_SERVER_KEY - skipping the chat turn" >&2
      return 0 ;;
  esac

  # The `model` field below is IGNORED by this server today, so the name is a
  # label rather than a routing decision: _handle_chat_completions passes
  # allow_bare_model=self._direct_model_requests, that flag defaults off, and
  # _request_agent_overrides then drops the value. `kairos` is the value of
  # API_SERVER_MODEL_NAME in the agent's own environment file — the virtual
  # model this server ADVERTISES — and NOT the operator's configured inference
  # model, so changing which model the agent thinks with does not move it. It is
  # the advertised name rather than an arbitrary one because if
  # direct_model_requests were ever turned on, the server nulls a model equal to
  # its own virtual model and honours one that differs: this request stays inert
  # under that change, where any other string would suddenly be taken as a real
  # model to execute and turn the health assertion red over a working VM.
  cat > "$CHAT_REQ" <<'JSON'
{"model":"kairos","messages":[{"role":"user","content":"Reply with the single word: pong"}],"max_tokens":16,"stream":false}
JSON

  # The key reaches curl through a config file on stdin written by `printf`, a
  # shell BUILT-IN, so it is never an argument to an executed program and never
  # appears in a process listing.
  CHAT_HTTP=$(printf 'header = "Authorization: Bearer %s"\n' "$_k" \
    | curl -sS -m 120 -K - -o "$CHAT_RESP" -w '%{http_code}' \
      -X POST "$CHAT_URL" \
      -H 'Content-Type: application/json' \
      --data-binary @"$CHAT_REQ") || CHAT_HTTP=000
  case "$CHAT_HTTP" in ''|*[!0-9]*) CHAT_HTTP=000 ;; esac
  # check-ping-bodies: untaint CHAT_HTTP - curl's %{http_code}, gated to digits by the case above; the response body goes to a file and is never emitted
  if [ "$(classify_chat "$CHAT_HTTP")" != "ok" ]; then
    echo "ERROR: the chat turn returned HTTP $CHAT_HTTP" >&2
    CHAT_MODE=chat-failed
    return 1
  fi

  # The assertion is "an assistant message with non-empty text came back", not
  # "the agent said pong". Model wording is not a contract; a populated
  # choices[0].message.content proves the agent class loaded, the provider
  # answered and the venv is whole. The content is never printed.
  if ! python3 - "$CHAT_RESP" <<'PY'
import json, sys
try:
    content = json.load(open(sys.argv[1]))["choices"][0]["message"]["content"]
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(content, str) and content.strip() else 1)
PY
  then
    echo "ERROR: the chat turn returned no assistant content" >&2
    CHAT_MODE=chat-empty
    return 1
  fi
  CHAT_MODE=chat
  return 0
}

write_last_good() {
  umask 077
  cat > "$LAST_GOOD" <<EOF
agent_sha=$(git -C "$AGENT_DIR" rev-parse HEAD)
webui_sha=$(git -C "$WEBUI_DIR" rev-parse HEAD)
client_version=$(installed_client_version)
stamp=$(date -u +%s)
EOF
  # Keep the pre-existing webui.last-good in step: the manual rollback runbook
  # in docs/operations/homelab.md reads that file and predates this script.
  git -C "$WEBUI_DIR" rev-parse HEAD > "$WEBUI_LAST_GOOD"
}

# Restore the last state that passed the assertion. The recorded file wins over
# the pre-run state because it is only ever written after a green assertion; the
# pre-run state is the fallback for the first run.
rollback() {
  if RB_AGENT=$(lg_get agent_sha) && RB_WEBUI=$(lg_get webui_sha) && RB_CLIENT=$(lg_get client_version); then
    ROLLBACK_SOURCE=last-good
  else
    RB_AGENT=$PREV_AGENT
    RB_WEBUI=$PREV_WEBUI
    RB_CLIENT=$PREV_CLIENT
    ROLLBACK_SOURCE=pre-run
  fi
  echo "==> rolling back (source: $ROLLBACK_SOURCE)"
  git -C "$AGENT_DIR" reset --hard "$RB_AGENT"
  # Braces, not "$AGENT_DIR[all]": the bare form is semantically correct POSIX
  # ([ cannot begin a name) but shellcheck rejects it as SC1087, an ERROR, and
  # `make check-vm-scripts` would fail.
  "$VENV/pip" install -q -e "${AGENT_DIR}[all]"
  # -B, not a bare checkout: it restores the recorded SHA while leaving HEAD
  # ATTACHED to master, which is the state the tracking rule maintains. A
  # detached rollback would look healthy and then quietly stop following
  # upstream at the next run.
  git -C "$WEBUI_DIR" checkout -q -B "$WEBUI_BRANCH" "$RB_WEBUI"
  install_webui_requirements
  if [ -n "$RB_CLIENT" ]; then
    "$VENV/pip" install -q "hindsight-client==$RB_CLIENT"
  fi
  restart_units
}

# ---- the exit trap ---------------------------------------------------------
# shellcheck disable=SC2329 # invoked by `trap ... EXIT` below, not by name.
on_exit() {
  _xrc=$?
  trap - EXIT
  rm -f "$CHAT_REQ" "$CHAT_RESP" 2>/dev/null || true
  hc_reset
  if [ "$_xrc" -eq 0 ]; then
    emit "summary=ok - agent, webui and hindsight-client current; chat=$CHAT_MODE"
  else
    emit "summary=FAILED rc=$_xrc - hermes-update"
  fi
  emit "rc=$_xrc"
  emit "verdict=$VERDICT"
  emit "agent_changed=$AGENT_CHANGED"
  emit "webui_changed=$WEBUI_CHANGED"
  emit "client_changed=$CLIENT_CHANGED"
  emit "webui_sha=$WEBUI_SHA"
  emit "client_version=$CLIENT_VERSION"
  emit "rollback_source=$ROLLBACK_SOURCE"
  emit "units_active=$UNITS_ACTIVE"
  emit "chat_mode=$CHAT_MODE"
  emit "chat_http=$CHAT_HTTP"
  emit "apt_age_days=$APT_AGE"
  emit "run_epoch=$RUN_EPOCH"
  emit "next=read the run with: journalctl --user -u hermes-update -n 500 --no-pager"
  ping_hc "$_xrc"
  exit "$_xrc"
}

# ---- main ------------------------------------------------------------------
main() {
  MODE=$(parse_mode "${1:-}")

  (umask 077; mkdir -p "$RUNDIR")
  chmod 0700 "$RUNDIR"

  PREV_AGENT=$(git -C "$AGENT_DIR" rev-parse HEAD)
  PREV_WEBUI=$(git -C "$WEBUI_DIR" rev-parse HEAD)
  PREV_CLIENT=$(installed_client_version)

  if [ "$MODE" = "seed" ]; then
    echo "==> seed: asserting health without updating anything"
    assert_health
    write_last_good
    echo "==> seed: recorded last-good"
    cat "$LAST_GOOD"
    return 0
  fi

  : "${HERMES_UPDATE_HC_UUID:?set HERMES_UPDATE_HC_UUID (see /home/hermes/.hermes/hermes-update.env)}"

  RUN_EPOCH=$(date -u +%s)
  case "$RUN_EPOCH" in ''|*[!0-9]*) RUN_EPOCH=0 ;; esac
  # check-ping-bodies: untaint RUN_EPOCH - `date -u +%s`, gated to digits by the case above

  # Signal traps BEFORE the EXIT trap. In POSIX sh an untrapped SIGTERM
  # terminates the shell WITHOUT running the EXIT trap, so without these three
  # a TimeoutStartSec expiry, a `systemctl --user stop`, or the 04:45 automatic
  # reboot landing on a long run would ping NOTHING: no body, no failure, just
  # silence, on the one channel that is watching. Converting each signal to an
  # exit lets on_exit run and report.
  trap 'exit 143' TERM
  trap 'exit 130' INT
  trap 'exit 129' HUP
  trap on_exit EXIT
  hc_reset
  emit "summary=starting"
  emit "run_epoch=$RUN_EPOCH"
  ping_hc start

  # ---- 1. the agent ---------------------------------------------------------
  echo "==> hermes update"
  "$HERMES_BIN" update < /dev/null
  if [ "$(git -C "$AGENT_DIR" rev-parse HEAD)" != "$PREV_AGENT" ]; then
    AGENT_CHANGED=yes
  fi

  # ---- 2. passenger one: the webui checkout ---------------------------------
  VERDICT=webui-failed
  echo "==> hermes-webui"
  # FAIL CLOSED BEFORE THE TREE MOVES. Everything from the checkout below
  # onwards must be restorable: an aborted run that leaves the checkout at a new
  # revision with a partial dependency set looks perfectly healthy — the units
  # are still serving the old code from memory — until the next restart or the
  # 04:45 automatic reboot. Up to that point, a failure exits with
  # rollback_source=none, which is what distinguishes the two webui-failed
  # cases in the runbook table.
  if ! git -C "$WEBUI_DIR" fetch --quiet --prune "$WEBUI_REMOTE"; then
    echo "ERROR: could not fetch $WEBUI_REMOTE for hermes-webui - refusing to move the checkout" >&2
    exit 1
  fi
  WEBUI_SHA=$(git -C "$WEBUI_DIR" rev-parse "$WEBUI_REMOTE/$WEBUI_BRANCH") || WEBUI_SHA=""
  if ! valid_sha40 "$WEBUI_SHA"; then
    WEBUI_SHA=unreadable
    echo "ERROR: $WEBUI_REMOTE/$WEBUI_BRANCH did not resolve to a 40-hex object name - refusing to guess" >&2
    exit 1
  fi
  # check-ping-bodies: untaint WEBUI_SHA - a git object name, gated to 40 hex characters by valid_sha40 above; a commit SHA is a tier-3 identifier
  # From here the working tree MOVES. `checkout -B` force-sets the local master
  # to the fetched SHA and keeps HEAD attached, so `git checkout master && git
  # pull --ff-only` — the runbook operators already have in their fingers — now
  # AGREES with this script instead of defeating it. It also discards
  # uncommitted changes in this checkout, which Task 0 Step 3 warns about.
  # The `if ... then : else` shape is what suppresses errexit so an `else` arm
  # exists at all.
  if git -C "$WEBUI_DIR" checkout -q -B "$WEBUI_BRANCH" "$WEBUI_SHA" && install_webui_requirements; then
    :
  else
    echo "==> the webui checkout or its constrained install failed" >&2
    rollback
    exit 1
  fi
  if [ "$(git -C "$WEBUI_DIR" rev-parse HEAD)" != "$PREV_WEBUI" ]; then
    WEBUI_CHANGED=yes
  fi

  # ---- 3. passenger two: hindsight-client -----------------------------------
  VERDICT=client-failed
  echo "==> hindsight-client"
  CLIENT_VERSION=$(deployed_hindsight_version) || CLIENT_VERSION=""
  if ! valid_semver "$CLIENT_VERSION"; then
    CLIENT_VERSION=unreadable
    echo "ERROR: could not read a X.Y.Z version from $HINDSIGHT_URL/health/live" >&2
    rollback
    exit 1
  fi
  # check-ping-bodies: untaint CLIENT_VERSION - the hindsight server's own version, gated to X.Y.Z by valid_semver above; a version string is a tier-3 identifier
  if ! "$VENV/pip" install -q "hindsight-client==$CLIENT_VERSION"; then
    echo "==> pinning hindsight-client failed" >&2
    rollback
    exit 1
  fi
  if [ "$(installed_client_version)" != "$PREV_CLIENT" ]; then
    CLIENT_CHANGED=yes
  fi

  # ---- 4. restart and assert ------------------------------------------------
  VERDICT=health-failed
  restart_units
  if assert_health; then
    write_last_good
    VERDICT=ok
  else
    echo "==> health assertion failed"
    VERDICT=rollback-failed
    rollback
    if assert_health; then
      VERDICT=rolled-back
    fi
    exit 1
  fi

  # ---- 5. the operating system's own updater --------------------------------
  # An automatic reboot that stopped happening is invisible from inside this
  # script's own success, so a stale apt stamp turns this check red even when
  # every hermes component is perfect. Without it a dead apt timer would ping
  # green forever.
  APT_AGE=$(apt_age_days "$APT_STAMP" "$(date -u +%s)")
  case "$APT_AGE" in ''|*[!0-9]*) APT_AGE=9999 ;; esac
  # check-ping-bodies: untaint APT_AGE - an integer day count this script computed from two epoch seconds, gated to digits by the case above
  if [ "$APT_AGE" -gt "$APT_MAX_AGE_DAYS" ]; then
    VERDICT=apt-stale
    echo "ERROR: $APT_STAMP is $APT_AGE days old (limit $APT_MAX_AGE_DAYS)" >&2
    exit 1
  fi

  return 0
}

if [ "${HERMES_UPDATE_LIB_ONLY:-0}" != "1" ]; then
  main "$@"
fi
