#!/bin/sh
# Daily liveness check for the Hermes VM's app stack. ZERO MODEL TOKENS.
#
# Three assertions, all local, all free: the agent package deep-imports from the
# shared venv, the WebUI answers its own /health on loopback, and all five user
# units are active. The verdict is pushed to ONE uptime-kuma push monitor.
#
# Canonical copy: hermes-vm/scripts/hermes-app-alive.sh in
# github.com/mnbf9rca/kubernetes_config. Runbook:
# docs/operations/hermes-vm-updates.md. Installed on VM 103 at
# /home/hermes/bin/hermes-app-alive.sh and run by hermes-app-alive.timer at
# 05:45 UTC daily.
set -eu

HERMES_HOME=/home/hermes/.hermes
AGENT_DIR=$HERMES_HOME/hermes-agent
VENV=$AGENT_DIR/venv/bin
WEBUI_HEALTH=http://127.0.0.1:8787/health
UNITS="hermes-gateway hermes-gateway-emh hermes-gateway-hal hermes-dashboard hermes-webui"
# DERIVED from UNITS, never written beside it. The count cannot pass with the
# WRONG five - the names are distinct - but one desync direction is silent: add
# a sixth unit to the list above and leave a hand-written 5 here, and a
# five-of-six machine reports a healthy `ok`. That is the single failure this
# check exists to catch. hermes-update.sh derives its copy the same way.
# shellcheck disable=SC2086 # the word split IS the measurement
UNIT_COUNT=$(set -- $UNITS; printf '%s\n' "$#")
# check-ping-bodies: untaint UNIT_COUNT - the word count of the literal UNITS list two lines above, computed by this script with `set --`; no external command runs and nothing outside this file reaches it

# Scratch under $HERMES_HOME at mode 0700, never /tmp: every agent session on
# this VM runs as this same user, and a symlink planted at a fixed /tmp path
# would make this script's `>>` append to a file of someone else's choosing AND
# make push_kuma send its contents to uptime-kuma.
RUNDIR=$HERMES_HOME/hermes-app-alive.run
MSG_FILE=$RUNDIR/kuma-msg

# ---- uptime-kuma push ------------------------------------------------------
# NO /start PING: the push API has no such concept. The hang bound is the
# service unit's TimeoutStartSec; the silence bound is the monitor's 24-hour
# heartbeat interval plus its retry.
#
# NEVER EMIT A COMMAND'S OUTPUT. A failing curl quotes the URL, and a push URL
# carries the monitor's token as its last path segment. Everything emitted below
# is a digit-gated integer or a verdict from the fixed enum.
msg_reset() { true 2>/dev/null > "$MSG_FILE" || true; }
emit() { { printf '%s ' "$*" | LC_ALL=C tr -cd '\040-\176'; } 2>/dev/null >> "$MSG_FILE" || true; }

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

# ---- verdict enum ----------------------------------------------------------
#   units-down          fewer than five hermes user units are active
#   import-failed       the shared venv cannot import run_agent
#   webui-unreachable   the WebUI's own /health did not answer 200
#   ok                  all three passed
VERDICT=units-down
UNITS_ACTIVE=0
WEBUI_HTTP=000

# shellcheck disable=SC2329 # invoked by `trap ... EXIT` below, not by name.
on_exit() {
  _xrc=$?
  trap - EXIT
  msg_reset
  emit "verdict=$VERDICT"
  emit "units=$UNITS_ACTIVE/$UNIT_COUNT"
  emit "webui_http=$WEBUI_HTTP"
  if [ "$_xrc" -eq 0 ]; then
    push_kuma up
  else
    push_kuma down
  fi
  exit "$_xrc"
}

main() {
  # This one assertion stays ABOVE the traps, because it is the one failure an
  # exit trap could not report anyway: with no PUSH_URL there is nowhere to
  # push. It fails loudly in the journal and the unit goes `failed`.
  : "${PUSH_URL:?set PUSH_URL (see /home/hermes/.hermes/hermes-app-alive.env)}"

  # Signal traps before the EXIT trap: in POSIX sh an untrapped signal ends the
  # shell WITHOUT running the EXIT trap, so a TimeoutStartSec expiry would push
  # nothing at all and read as a healthy silent day until the heartbeat lapsed.
  trap 'exit 143' TERM
  trap 'exit 130' INT
  trap 'exit 129' HUP
  trap on_exit EXIT

  # The scratch setup sits BELOW the traps deliberately. It used to sit above
  # them, so an unwritable or un-chmod-able $RUNDIR exited under `set -e` with
  # no EXIT trap installed and pushed NOTHING - silence, when a `down` was
  # available and was the right answer. Nothing is lost by moving it: msg_reset
  # and emit both end in `|| true` and tolerate a missing $RUNDIR, so a run that
  # dies here still pushes, with an empty message. hermes-update.sh orders its
  # prepare_rundir the same way and for the same reason.
  (umask 077; mkdir -p "$RUNDIR")
  chmod 0700 "$RUNDIR"
  msg_reset

  for _u in $UNITS; do
    if systemctl --user is-active --quiet "$_u"; then
      UNITS_ACTIVE=$(( UNITS_ACTIVE + 1 ))
    fi
  done
  if [ "$UNITS_ACTIVE" -ne "$UNIT_COUNT" ]; then
    echo "ERROR: $UNITS_ACTIVE of $UNIT_COUNT hermes units active" >&2
    exit 1
  fi

  # THE DEEP IMPORT IS THE POINT. `run_agent` is what the WebUI's
  # api/agent_runtime.py imports and what api/streaming.py instantiates for
  # every chat turn, and it pulls in dotenv, httpx and openai. A venv missing
  # any of them leaves every unit `active` and /health answering `status: ok`
  # while the iOS app answers `AIAgent not available` (homelab.md, "Do not give
  # it a venv of its own"). Importing hermes_cli.main instead would NOT catch
  # that: it is the CLI entry point, not the WebUI's dependency path.
  VERDICT=import-failed
  # STDOUT is discarded because it is only a sentinel - a successful import
  # prints nothing. STDERR IS DELIBERATELY NOT DISCARDED: python writes the
  # traceback there, and WHICH import failed is the whole answer to "what
  # broke". It used to be thrown away, leaving the journal with only the
  # generic line below. There is no disclosure question - the traceback goes to
  # the journal and never near the pushed message, which carries the fixed
  # `verdict=import-failed` and nothing else.
  if ! "$VENV/python" -c 'import run_agent' >/dev/null; then
    echo "ERROR: the agent venv cannot import run_agent" >&2
    exit 1
  fi

  VERDICT=webui-unreachable
  WEBUI_HTTP=$(curl -sS -m 15 -o /dev/null -w '%{http_code}' "$WEBUI_HEALTH") || WEBUI_HTTP=000
  case "$WEBUI_HTTP" in ''|*[!0-9]*) WEBUI_HTTP=000 ;; esac
  # check-ping-bodies: untaint WEBUI_HTTP - curl's %{http_code}, gated to digits by the case above; the response body is discarded to /dev/null
  case "$WEBUI_HTTP" in
    2??) ;;
    *) echo "ERROR: webui /health returned HTTP $WEBUI_HTTP" >&2; exit 1 ;;
  esac

  VERDICT=ok
  return 0
}

main "$@"
