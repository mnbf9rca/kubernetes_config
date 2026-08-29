#!/bin/sh
# Weekly sandbox refresh for the Hermes VM's docker terminal backend. ZERO
# MODEL TOKENS.
#
# The docker terminal backend creates ONE SANDBOX CONTAINER PER (PROFILE, TASK)
# - labelled hermes-profile:<name> and hermes-task-id:<id>, named hermes-<hex> -
# and KEEPS it, because the backend runs with container_persistent: true. A
# container therefore pins the image it was created from for as long as it
# lives, so a profile that has been busy for a month is still executing last
# month's base image, CVEs included. Nothing else on this VM moves it:
# unattended-upgrades patches the host, not an image inside docker. One profile
# can own several containers, so this is per container and not per profile.
#
# The mechanism is REMOVAL, not restart. hermes recreates a sandbox lazily on
# the profile's next terminal call, from the local image as it stands at that
# moment (verified live), so pulling the image and deleting the stale containers
# is the whole of the refresh. Nothing needs restarting and no gateway is
# touched.
#
# Canonical copy: hermes-vm/scripts/hermes-sandbox-refresh.sh in
# github.com/mnbf9rca/kubernetes_config. Installed on the VM at
# /home/hermes/.hermes/scripts/hermes-sandbox-refresh.sh and run at 05:15 UTC
# every Sunday by a hermes `no_agent` cron job, which runs it as a HOST
# subprocess of the default gateway and records its stdout against the job run.
# The install path is not a choice, for the same reason it is not one for
# hermes-app-alive.sh: `hermes cron create --script` takes a BARE FILENAME
# resolved under $HERMES_HOME/scripts/ and rejects an absolute path.
#
# IT PUSHES TO NO MONITOR, deliberately. A sandbox a week stale is not an
# outage, the job's stdout is delivered to zero targets, and a non-zero exit is
# recorded against the run as an error - which is the report. Empty stdout would
# be a silent success; the summary line below is one line for somebody reading
# the job history.
#
# THE SELF-CASE. This runs INSIDE the default gateway, which is itself a
# docker-backend profile, so the run can be asked to remove a container
# belonging to the profile hosting it. That is safe: a `no_agent` cron run
# creates no session row and does not bump the persisted active_agents counter
# (verified), so this run does not report itself busy. A chat turn finishing
# near the poll can still leave a stale +1, which reads as busy and skips the
# removal - the SAFE DIRECTION, and the container is picked up again next Sunday
# by the per-container image comparison below. Both of those are UPSTREAM
# IMPLEMENTATION DETAILS rather than a contract: if a later hermes release
# counts differently this script gets more conservative, never less.
set -eu

HERMES_HOME=/home/hermes/.hermes
HERMES_BIN=/home/hermes/.local/bin/hermes
PROFILE_DIR=$HERMES_HOME/profiles
STATUS_URL=http://127.0.0.1:9119/api/status

# Every command that talks to the docker daemon or to the loopback API carries
# its OWN bound. The scheduler caps the whole run at 3600s, which is not a
# per-command bound: a wedged daemon blocks until that cap and reports one
# timeout for a run that did nothing, rather than skipping the one call that
# hung. The pull bound is generous because it is a real network transfer of a
# multi-hundred-megabyte devcontainers image over a cold cache.
PULL_TIMEOUT=900
DOCKER_TIMEOUT=60
CLI_TIMEOUT=60
CURL_TIMEOUT=10

# DANGLING IMAGES ONLY: `docker image prune` without `-a` cannot touch an image
# any container still references, so the images the kept containers run on are
# safe by construction rather than by the age filter. The age filter is there so
# that an image pulled minutes ago and not yet used by a lazily-recreated
# sandbox is not collected out from under it.
PRUNE_AGE=168h

# ---- counters --------------------------------------------------------------
# skipped_busy counts every container this run declined to remove: one that
# failed an idle check, one whose docker query errored, and one whose `rm -f`
# did not complete. All three mean the same thing to a reader - the container is
# still there running an old image - and all three self-heal next Sunday,
# because the image comparison below re-derives staleness from scratch.
PROFILE_COUNT=0
PULLED=0
REMOVED=0
SKIPPED=0
ERRORS=0

# ---- diagnostics -----------------------------------------------------------
# The one diagnostic sink, and it writes to STDERR, which lands in the gateway
# journal where triage starts. STDOUT carries the key=value summary and nothing
# else: no command output, no URL, no token.
#
# IT IS DELIBERATELY NOT NAMED emit, say_err, fatal, hc_emit OR hc_summary.
# scripts/check-ping-bodies.py classifies a sink BY FUNCTION NAME, and those
# five names mean "this text reaches a healthchecks.io body or an uptime-kuma
# heartbeat message". This script has no push sink at all, so taking one of
# those names would file ordinary journal output under a rule set it does not
# belong to - and would teach the next reader that the names are decorative.
log() { printf '%s\n' "$*" >&2; }

# ---- hermes CLI ------------------------------------------------------------
# The profile list and both config values are read AT RUN TIME. A hardcoded
# profile list is the failure this design exists to avoid: hal is still on the
# local backend and moves to docker later, and a list written today would either
# skip it forever or start refreshing it before it has a container to refresh.
#
# THE VALUE IS THE LAST LINE OF THE OUTPUT, never the first and never all of it.
# The CLI prints a "1Password: applied N secrets" startup line, and it is not
# reliably on stderr, so stderr is folded in and the tail is taken. Whitespace
# is stripped because both values read here are single tokens - a backend name
# and an image reference - and a trailing carriage return would fail an exact
# comparison silently, which reads as "no docker profiles" and does nothing.
config_get() {
  _cg_profile=$1
  _cg_key=$2
  # The default profile takes NO -p flag; passing `-p default` is not the same
  # command.
  if [ "$_cg_profile" = default ]; then
    _cg_out=$(timeout "$CLI_TIMEOUT" "$HERMES_BIN" config get "$_cg_key" 2>&1) \
      || return 1
  else
    _cg_out=$(timeout "$CLI_TIMEOUT" "$HERMES_BIN" -p "$_cg_profile" \
      config get "$_cg_key" 2>&1) || return 1
  fi
  printf '%s\n' "$_cg_out" | tail -n 1 | tr -d '[:space:]'
}

# ---- idle check A: the platform ---------------------------------------------
# THE jq TRAP IS REAL AND WAS VERIFIED LIVE: `.field // default` fires on FALSE
# as well as on null, so `.gateway_busy // true` reads an idle gateway as busy
# and, worse, the same idiom applied the other way round reads a missing field
# as idle. Every field is therefore compared explicitly, and a null or missing
# field fails its comparison and lands on BUSY.
#
# jq -e exits non-zero when the last output is false or null and exits 5 on
# unparseable input, so a malformed body is busy too. curl's own stderr is
# discarded rather than logged: the estate's rule is that a failing curl quotes
# the URL it was handed, and while this one is loopback and carries no
# credential, the habit is what keeps a token out of a log somewhere else.
platform_idle() {
  _pi_profile=$1
  _pi_json=$(curl -fsS -m "$CURL_TIMEOUT" --get \
    --data-urlencode "profile=$_pi_profile" "$STATUS_URL" 2>/dev/null) || return 1
  printf '%s' "$_pi_json" | jq -e '
      .gateway_running == true
      and .gateway_state == "running"
      and .gateway_busy == false
      and .active_agents == 0
      and .active_sessions == 0' >/dev/null 2>&1
}

# ---- idle check B: the container --------------------------------------------
# THE PLATFORM COUNTERS DELIBERATELY EXCLUDE background terminal work: a
# `terminal(background=true)` process keeps running inside the sandbox while the
# gateway reports itself idle (confirmed in source). Removing the container
# under it would kill that work silently, so the container is asked directly.
#
# The devcontainers image idles with EXACTLY ONE process, so `docker top` on an
# idle sandbox prints its header and one row: two non-blank lines. Anything else
# - more rows, no rows, an error from a container that stopped between the list
# and this call - is busy.
container_idle() {
  _ci_id=$1
  _ci_top=$(timeout "$DOCKER_TIMEOUT" docker top "$_ci_id" 2>/dev/null) || return 1
  _ci_lines=$(printf '%s\n' "$_ci_top" | awk 'NF > 0 { c = c + 1 } END { print c + 0 }')
  case "$_ci_lines" in ''|*[!0-9]*) return 1 ;; esac
  [ "$_ci_lines" -eq 2 ]
}

# ---- removal ---------------------------------------------------------------
# A failed `rm -f` is not fatal and is not an error worth failing the run over -
# the usual cause is the container having gone away already, and the outcome
# either way is that this run did not remove it. It counts as skipped, which is
# what a reader of the summary needs to know.
remove_stale() {
  _rs_id=$1
  _rs_profile=$2
  if timeout "$DOCKER_TIMEOUT" docker rm -f "$_rs_id" >/dev/null 2>&1; then
    REMOVED=$((REMOVED + 1))
    log "removed stale container $_rs_id (profile $_rs_profile)"
  else
    log "WARNING: could not remove stale container $_rs_id (profile $_rs_profile); kept"
    SKIPPED=$((SKIPPED + 1))
  fi
}

main() {
  # Without the CLI there is no way to learn which profiles are docker-backed,
  # and every config read below would fail identically - which would render as
  # "no docker profiles, nothing to do" and exit 0 forever. Fail loudly instead.
  if [ ! -x "$HERMES_BIN" ]; then
    log "ERROR: $HERMES_BIN is not executable, so no profile can be read"
    exit 1
  fi

  # The default profile has no directory of its own; the others are directories
  # under $PROFILE_DIR. A directory literally named `default` is skipped rather
  # than added, because it would process the default profile twice - once with
  # -p and once without - and pull the same image twice.
  profiles=default
  if [ -d "$PROFILE_DIR" ]; then
    for _dir in "$PROFILE_DIR"/*; do
      [ -d "$_dir" ] || continue
      _name=${_dir##*/}
      if [ "$_name" != default ]; then
        profiles="$profiles $_name"
      fi
    done
  fi

  # pairs holds `profile=image` tokens, images holds the deduplicated image
  # references. Two space-separated lists rather than a data structure, because
  # POSIX sh has no arrays; `=` is safe as the separator because an image
  # reference cannot contain one, and space is safe because neither a profile
  # name nor an image reference can contain one either.
  pairs=
  images=
  for _profile in $profiles; do
    if ! _backend=$(config_get "$_profile" terminal.backend); then
      log "ERROR: cannot read terminal.backend for profile $_profile"
      ERRORS=$((ERRORS + 1))
      continue
    fi
    # Exact match only. A backend this script does not know is left alone.
    if [ "$_backend" != docker ]; then
      continue
    fi
    if ! _image=$(config_get "$_profile" terminal.docker_image); then
      log "ERROR: cannot read terminal.docker_image for profile $_profile"
      ERRORS=$((ERRORS + 1))
      continue
    fi
    if [ -z "$_image" ]; then
      log "ERROR: terminal.docker_image is empty for profile $_profile"
      ERRORS=$((ERRORS + 1))
      continue
    fi
    PROFILE_COUNT=$((PROFILE_COUNT + 1))
    pairs="$pairs $_profile=$_image"
    case " $images " in
      *" $_image "*) ;;
      *) images="$images $_image" ;;
    esac
  done

  if [ "$PROFILE_COUNT" -eq 0 ] && [ "$ERRORS" -eq 0 ]; then
    printf 'verdict=noop profiles=0\n'
    return 0
  fi

  # EVERY RUN PULLS. There is deliberately no "the digest has not moved since
  # last week, exit early" shortcut: an early exit makes a week in which every
  # container was busy PERMANENT, because the following week's digest matches
  # the one recorded and the stale containers are never revisited. Pulling an
  # unchanged image is a cheap manifest check.
  for _image in $images; do
    # Progress goes to stderr, so the journal keeps it and stdout stays clean.
    if ! timeout "$PULL_TIMEOUT" docker pull "$_image" >&2; then
      log "ERROR: docker pull failed for $_image"
      printf 'verdict=pull-failed image=%s\n' "$_image"
      return 1
    fi
    PULLED=$((PULLED + 1))
  done

  for _pair in $pairs; do
    _profile=${_pair%%=*}
    _image=${_pair#*=}

    # THE PER-CONTAINER IMAGE GATE. Comparing each container's image id against
    # the id the reference now resolves to is what makes a skipped week
    # self-healing: staleness is re-derived from the live state every Sunday
    # rather than remembered, so a container skipped as busy is a candidate
    # again next week with no state carried between runs.
    if ! _target=$(timeout "$DOCKER_TIMEOUT" docker image inspect -f '{{.Id}}' \
      "$_image" 2>/dev/null) || [ -z "$_target" ]; then
      log "ERROR: cannot read the image id of $_image; profile $_profile left alone"
      ERRORS=$((ERRORS + 1))
      continue
    fi

    if ! _list=$(timeout "$DOCKER_TIMEOUT" docker ps -a \
      --filter "label=hermes-profile=$_profile" \
      --format '{{.ID}} {{.State}}' 2>/dev/null); then
      log "ERROR: cannot list containers for profile $_profile"
      ERRORS=$((ERRORS + 1))
      continue
    fi

    # The platform status is per profile, not per container, so it is read at
    # most once per profile and only when a running stale container asks for it.
    _profile_idle=unknown

    # shellcheck disable=SC2086 # the word split IS the parse: two columns, no
    # field of which can contain a space, consumed below two at a time.
    set -- $_list
    while [ $# -ge 2 ]; do
      _cid=$1
      _cstate=$2
      shift 2

      # ANY DOCKER ERROR IS BUSY, NEVER REMOVABLE. An unreadable container is
      # one this run knows nothing about, and the safe reading of "I do not
      # know" is "leave it alone".
      if ! _cimg=$(timeout "$DOCKER_TIMEOUT" docker inspect -f '{{.Image}}' \
        "$_cid" 2>/dev/null) || [ -z "$_cimg" ]; then
        log "WARNING: cannot read the image of container $_cid; treated as busy"
        SKIPPED=$((SKIPPED + 1))
        continue
      fi

      if [ "$_cimg" = "$_target" ]; then
        continue
      fi

      # A CONTAINER THAT IS NOT RUNNING HAS NO PROCESSES BY DEFINITION, so it
      # takes no idle check. This case is not an edge: the VM reboots for
      # unattended-upgrades and every sandbox is left `exited`, so on a Sunday
      # following a reboot EVERY container is in this state. Treating a stopped
      # container as busy - which an idle check would, since `docker top` fails
      # on one - would deadlock the refresh on exactly those weeks.
      if [ "$_cstate" != running ]; then
        remove_stale "$_cid" "$_profile"
        continue
      fi

      if [ "$_profile_idle" = unknown ]; then
        if platform_idle "$_profile"; then
          _profile_idle=yes
        else
          _profile_idle=no
          log "profile $_profile is not idle by the platform check"
        fi
      fi

      # BOTH checks must pass. Either one failing keeps the container.
      if [ "$_profile_idle" = yes ] && container_idle "$_cid"; then
        remove_stale "$_cid" "$_profile"
      else
        log "container $_cid (profile $_profile) is stale but busy; kept"
        SKIPPED=$((SKIPPED + 1))
      fi
    done
  done

  # Only after a removal, and only ever dangling layers. Its failure is ignored:
  # disk that was not reclaimed is not a reason to report the refresh failed.
  if [ "$REMOVED" -gt 0 ]; then
    timeout "$DOCKER_TIMEOUT" docker image prune -f --filter "until=$PRUNE_AGE" >&2 \
      || log "WARNING: image prune did not complete; ignored"
  fi

  if [ "$ERRORS" -gt 0 ]; then
    printf 'verdict=error pulled=%s removed=%s skipped_busy=%s profiles=%s errors=%s\n' \
      "$PULLED" "$REMOVED" "$SKIPPED" "$PROFILE_COUNT" "$ERRORS"
    return 1
  fi

  verdict=refreshed
  if [ "$REMOVED" -eq 0 ]; then
    verdict=noop
  fi
  printf '%s pulled=%s removed=%s skipped_busy=%s profiles=%s\n' \
    "verdict=$verdict" "$PULLED" "$REMOVED" "$SKIPPED" "$PROFILE_COUNT"
  return 0
}

main "$@"
