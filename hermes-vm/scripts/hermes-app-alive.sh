#!/bin/sh
# Daily liveness check for the Hermes VM's app stack. ZERO MODEL TOKENS.
#
# Three assertions, all local, all free: the agent package deep-imports from the
# current PM environment, the WebUI answers /health on loopback, and all three user
# units are active. The verdict is pushed to ONE uptime-kuma push monitor.
#
# Canonical copy: hermes-vm/scripts/hermes-app-alive.sh in
# github.com/mnbf9rca/kubernetes_config. Runbook:
# docs/operations/hermes-vm.md. Installed on VM 103 at
# /home/hermes/.hermes/scripts/hermes-app-alive.sh and run at 05:45 UTC daily
# by a hermes `no_agent` cron job, which runs the script as a subprocess of the
# default gateway and delivers its stdout without involving the model. It used
# to be a systemd user timer reading its token from an installed environment
# file; both were deleted on 2026-08-27.
#
# THE INSTALL PATH IS NOT A CHOICE, and /home/hermes/bin - where an earlier
# draft of the runbook put this - cannot work. `hermes cron create --script`
# takes a BARE FILENAME resolved under $HERMES_HOME/scripts/ and rejects an
# absolute path at the API boundary (_validate_cron_script_path in
# tools/cronjob_tools.py), a guard that exists to stop prompt injection aiming a
# job at an arbitrary file. Installing anywhere else leaves a script that runs
# by hand and a job that cannot be created at all.
#
# Living here also puts the script INSIDE the nightly `hermes backup` zip, which
# inverts the old reasoning favourably: the script and the cron store that names
# it are now backed up and restored together, rather than the store surviving a
# rebuild while the file it points at does not.
set -eu

HERMES_HOME=/home/hermes/.hermes
IMPORT_CHECK=$HERMES_HOME/scripts/hermes-webui-runtime.py
WEBUI_HEALTH=http://127.0.0.1:8787/health
UNITS="hermes-gateway hermes-dashboard hermes-webui"
# Derive the expected count from the same list that the loop checks.
# shellcheck disable=SC2086 # unit names intentionally split into words
UNIT_COUNT=$(set -- $UNITS; printf '%s\n' "$#")
# check-ping-bodies: untaint UNIT_COUNT - word count of the literal UNITS list above; no external input
if [ "$UNIT_COUNT" -lt 1 ]; then
  echo "ERROR: UNITS is empty, so this check would verify nothing and report ok" >&2
  exit 1
fi

# ---- push URL --------------------------------------------------------------
# THE TOKEN ARRIVES AS AN INJECTED ENVIRONMENT VARIABLE and is stored nowhere on
# this VM. hermes's own 1Password secrets provider resolves
# `HERMES_APP_ALIVE_PUSH_TOKEN` from the default profile's
# `secrets.onepassword.env` at gateway start, and the cron subprocess inherits
# it. The name is deliberately one no provider registry knows, because the
# subprocess sanitiser strips by NAME: a registry name, an `AUXILIARY_*` or a
# `GATEWAY_RELAY_*` would be removed before the script ever ran.
#
# THE URL IS ASSEMBLED HERE rather than injected whole, so the variable holding
# a credential is the only thing crossing the boundary. KUMA_PUSH_BASE is an
# ordinary identifier - a hostname and an endpoint path - and carries nothing.
KUMA_PUSH_BASE=https://uptime.cynexia.com/api/push

# Scratch under $HERMES_HOME at mode 0700, never /tmp: every agent session on
# this VM runs as this same user, and a symlink planted at a fixed /tmp path
# would make this script's `>>` append to a file of someone else's choosing AND
# make push_kuma send its contents to uptime-kuma.
RUNDIR=$HERMES_HOME/hermes-app-alive.run
MSG_FILE=$RUNDIR/kuma-msg

# ---- uptime-kuma push ------------------------------------------------------
# NO /start PING: the push API has no such concept. The silence bound is the
# monitor's 24-hour heartbeat interval plus its retry. THE HANG BOUND IS NOW
# WHATEVER THE HERMES SCHEDULER IMPOSES ON A CRON SUBPROCESS, which is not
# recorded here and is a weaker guarantee than the systemd unit's
# TimeoutStartSec=120 it replaced. Every command below that could block carries
# its own `-m 15`, so the exposure is a wedged `systemctl` or a wedged python
# import rather than an unbounded network wait.
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
#   units-down          fewer than the expected hermes user units are active
#   import-failed       the shared PM environment cannot import the WebUI and run_agent
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
  # exit trap could not report anyway: with no token there is nowhere to push,
  # so NO FALSE `down` IS POSSIBLE HERE. It fails loudly on stderr, the cron run
  # exits non-zero, and uptime-kuma sees silence - which its heartbeat reports on
  # its own schedule. `:?` covers unset and empty alike.
  # No apostrophe in that message: shellcheck cannot parse one inside `${x:?...}`
  # even though every shell accepts it.
  : "${HERMES_APP_ALIVE_PUSH_TOKEN:?not injected - add it to secrets.onepassword.env in the default profile, then restart hermes-gateway}"
  PUSH_URL=$KUMA_PUSH_BASE/$HERMES_APP_ALIVE_PUSH_TOKEN

  # Signal traps before the EXIT trap: in POSIX sh an untrapped signal ends the
  # shell WITHOUT running the EXIT trap, so a scheduler killing a slow run would
  # push nothing at all and read as a healthy silent day until the heartbeat
  # lapsed.
  trap 'exit 143' TERM
  trap 'exit 130' INT
  trap 'exit 129' HUP
  trap on_exit EXIT

  # The scratch setup sits BELOW the traps deliberately. It used to sit above
  # them, so an unwritable or un-chmod-able $RUNDIR exited under `set -e` with
  # no EXIT trap installed and pushed NOTHING - silence, when a `down` was
  # available and was the right answer. Nothing is lost by moving it: msg_reset
  # and emit both end in `|| true` and tolerate a missing $RUNDIR, so a run that
  # dies here still pushes, with an empty message.
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

  # Health alone misses a broken agent import. Use upstream's selected PM
  # environment for the same WebUI APIs and run_agent used by serving chat.
  # The helper guards lazy completion and isolates import-time UI settings.
  # Keep stderr for local diagnosis; the push carries only the verdict enum.
  VERDICT=import-failed
  if ! (cd / && /usr/bin/python3 "$IMPORT_CHECK" --check-import) >/dev/null; then
    echo "ERROR: the PM environment cannot import the WebUI and agent" >&2
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
