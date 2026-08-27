#!/bin/sh
# Failure-path harness for hermes-update.sh: drive the WHOLE script, not just
# its pure helpers, and assert what the ping body says on each route through it.
#
# WHY THIS EXISTS
# ---------------
# test-hermes-update.sh covers the pure helpers. Everything that decides whether
# a bad update is survivable - which failures roll back, which report that
# nothing moved, whether a rollback that dies half way through says so - lives in
# `main` and in the four functions it calls, and none of that had ever executed
# anywhere. Two Critical and seven Important defects were found in that code by
# driving it against stubs during review, and the evidence for their fixes lived
# only in a scratch directory. This is that harness, committed, so the fixes stay
# fixed. It is the repository's own rule - logic lives in a file so it can be
# imported, unit-tested, linted and diffed - applied to the riskiest code here.
#
# WHAT IT TOUCHES: one `mktemp -d` directory and nothing else. No network, no
# VM, no ssh. `git` and `python3` are the real ones and the git repositories are
# real, because the git behaviour (fetch, `reset --hard`, `checkout -f -B`) is
# part of what is being tested; every remote is a local path. Everything that
# would leave the machine - curl, systemctl, pip, the venv python, the `hermes`
# entry point, timeout, flock - is a stub on a PATH this script controls.
#
# THE SAFETY INTERLOCK. The script under test names /home/hermes and
# /var/lib/apt by absolute path, so a copy is made with those five constants
# redirected into the scratch root. rewrite_script() then ASSERTS all five were
# redirected and that no constant still names the real /home/hermes, and aborts
# if not. That check is not decoration: if a constant is renamed upstream and the
# rewrite silently stops matching, this harness would run `git reset --hard` and
# `pip install` against a real installation. Never weaken it.
#
# Run: sh hermes-vm/scripts/test-hermes-update-paths.sh   (or `make check-vm-scripts`)
set -u

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
SCRIPT=$HERE/hermes-update.sh
[ -f "$SCRIPT" ] || { printf 'FATAL: no hermes-update.sh beside this harness\n' >&2; exit 2; }

FAILED=0
SCEN=setup
RC=0

pass() { printf 'ok   - %s\n' "$1"; }
bad()  { printf 'FAIL - %s\n' "$1"; FAILED=1; }

WORK=$(mktemp -d) || exit 2
trap 'rm -rf "$WORK"' EXIT
trap 'rm -rf "$WORK"; exit 130' INT
trap 'rm -rf "$WORK"; exit 143' TERM

# The five units the script counts. Kept here rather than derived from the
# script so that a change to UNITS shows up as a failing count rather than as
# two files agreeing with each other about the wrong thing.
ALL_UNITS='hermes-gateway hermes-gateway-emh hermes-gateway-hal hermes-dashboard hermes-webui'

# git needs an identity to commit, and the `hermes` stub commits too. Set here
# so neither depends on the operator's ~/.gitconfig.
GIT_AUTHOR_NAME=harness
GIT_AUTHOR_EMAIL=harness@invalid
GIT_COMMITTER_NAME=harness
GIT_COMMITTER_EMAIL=harness@invalid
# AND THE CONFIGURATION ITSELF IS NEUTRALISED, not just the identity. Setting
# only the four variables above left this harness reading the operator's global
# and system git config, which can carry settings that make a commit BLOCK
# rather than fail: `commit.gpgsign = true` with a gpg agent that wants a
# passphrase hangs indefinitely, and `make check-vm-scripts` has no timeout, so
# the repository's own gate wedges with no output and no explanation of why.
# Demonstrated, not theorised. /dev/null is an empty, readable, unwritable
# config file, which is exactly the "no configuration at all" this wants.
GIT_CONFIG_GLOBAL=/dev/null
GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
export GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM

# ---- the stubs -------------------------------------------------------------
# Written once into $WORK/stubs, which goes on the front of PATH. Each one reads
# its behaviour from files under the scenario's own $HUT_STATE directory, so a
# scenario is configured by creating files rather than by editing a stub.
STUBS=$WORK/stubs
mkdir -p "$STUBS"

cat > "$STUBS/timeout" <<'STUB'
#!/bin/sh
# timeout -k GRACE LIMIT cmd...  -> run cmd with no limit at all. The harness
# tests routing, not the bounds; a real timeout would only add wall clock.
if [ "$1" = "-k" ]; then shift 2; fi
shift
exec "$@"
STUB

cat > "$STUBS/flock" <<'STUB'
#!/bin/sh
# The single-instance guard. It succeeds unless a scenario says the lock is
# already held, which is how the "another run is in progress" route is reached
# without actually starting two runs.
if [ -n "${HUT_STATE:-}" ] && [ -f "$HUT_STATE/lock-held" ]; then exit 1; fi
exit 0
STUB

cat > "$STUBS/curl" <<'STUB'
#!/bin/sh
# Never reaches a network. Dispatches on the URL and records every ping.
S=${HUT_STATE:?}
url=; out=; data=; readcfg=0
while [ $# -gt 0 ]; do
  case $1 in
    -o) out=$2; shift 2 ;;
    -w|-m|-H|-X) shift 2 ;;
    -K) readcfg=1; shift 2 ;;
    --data-binary) data=${2#@}; shift 2 ;;
    http://*|https://*) url=$1; shift ;;
    *) shift ;;
  esac
done
if [ "$readcfg" = 1 ]; then cat >/dev/null; fi
case $url in
  *hc-ping.com*)
    tail=${url##*/}
    { printf -- '--- ping %s\n' "$tail"
      if [ -n "$data" ] && [ -f "$data" ]; then cat "$data"; fi
    } >> "$S/pings"
    if [ "$tail" != start ]; then
      if [ -n "$data" ] && [ -f "$data" ]; then cp "$data" "$S/final-body"
      else : > "$S/final-body"; fi
    fi
    exit 0 ;;
  */health/live)
    if [ -f "$S/hindsight-json" ]; then cat "$S/hindsight-json"; exit 0; fi
    exit 22 ;;
  *8787/health)
    if [ -f "$S/webui-health-down" ]; then exit 22; fi
    exit 0 ;;
  *chat/completions)
    if [ -f "$S/chat-fail-count" ]; then
      n=$(cat "$S/chat-fail-count")
      if [ "$n" -gt 0 ]; then
        printf '%s\n' "$((n - 1))" > "$S/chat-fail-count"
        if [ -n "$out" ]; then printf '{"error":"stub"}\n' > "$out"; fi
        printf '500'
        exit 0
      fi
    fi
    if [ -n "$out" ]; then cat "$S/chat-body" > "$out"; fi
    printf '200'
    exit 0 ;;
esac
exit 22
STUB

cat > "$STUBS/systemctl" <<'STUB'
#!/bin/sh
# --user is-active --quiet UNIT, and --user restart UNIT...
S=${HUT_STATE:?}
mode=
for a in "$@"; do
  case $a in
    is-active) mode=is-active ;;
    restart) mode=restart ;;
  esac
done
case $mode in
  is-active)
    for u in "$@"; do :; done
    rc=3
    for a in $(cat "$S/active-units" 2>/dev/null); do
      if [ "$a" = "$u" ]; then rc=0; fi
    done
    exit "$rc" ;;
  restart)
    printf 'restart\n' >> "$S/systemctl.log"
    if [ -f "$S/restart-fail-count" ]; then
      n=$(cat "$S/restart-fail-count")
      if [ "$n" -gt 0 ]; then
        printf '%s\n' "$((n - 1))" > "$S/restart-fail-count"
        : > "$S/active-units"
        exit 1
      fi
    fi
    cat "$S/all-units" > "$S/active-units"
    exit 0 ;;
esac
exit 0
STUB

cat > "$STUBS/pip" <<'STUB'
#!/bin/sh
# The venv pip. Records every invocation, and a successful `hindsight-client==X`
# install is what moves the recorded installed version, so the client pin and
# the version read back are genuinely coupled rather than two constants.
S=${HUT_STATE:?}
printf '%s\n' "$*" >> "$S/pip.log"
if [ "${1:-}" = freeze ]; then
  if [ -f "$S/pip-freeze-empty" ]; then exit 0; fi
  printf 'hindsight-client==%s\n' "$(cat "$S/client-version")"
  printf 'flask==1.0\nrequests==2.0\n'
  exit 0
fi
kind=other
want=
for a in "$@"; do
  case $a in
    -e) kind=editable ;;
    -r) kind=requirements ;;
    hindsight-client==*) kind=client; want=${a#hindsight-client==} ;;
  esac
done
f=$S/pip-$kind-fail-count
if [ -f "$f" ]; then
  n=$(cat "$f")
  if [ "$n" -gt 0 ]; then printf '%s\n' "$((n - 1))" > "$f"; exit 1; fi
fi
if [ "$kind" = client ]; then printf '%s' "$want" > "$S/client-version"; fi
exit 0
STUB

cat > "$STUBS/venv-python" <<'STUB'
#!/bin/sh
# The venv python: `import run_agent` and the hindsight-client version lookup.
S=${HUT_STATE:?}
case "$*" in
  *run_agent*)
    if [ -f "$S/import-fail-count" ]; then
      n=$(cat "$S/import-fail-count")
      if [ "$n" -gt 0 ]; then printf '%s\n' "$((n - 1))" > "$S/import-fail-count"; exit 1; fi
    fi
    if [ -f "$S/import-broken" ]; then exit 1; fi
    exit 0 ;;
  *hindsight-client*)
    v=$(cat "$S/client-version" 2>/dev/null)
    if [ -z "$v" ]; then exit 1; fi
    printf '%s\n' "$v"
    exit 0 ;;
esac
exit 0
STUB

cat > "$STUBS/hermes-bin" <<'STUB'
#!/bin/sh
# The `hermes` entry point. Whether it MOVES the agent checkout and what it
# exits with are set independently, because telling those two apart is the whole
# of the "nothing moved, so nothing to roll back" branch.
S=${HUT_STATE:?}
A=${HUT_AGENT:?}
printf '%s\n' "$*" >> "$S/hermes.log"
if [ -f "$S/hermes-moves" ]; then
  printf 'updated by the stub\n' > "$A/UPDATED"
  git -C "$A" add -A
  git -C "$A" commit -q -m 'stub hermes update'
fi
if [ -f "$S/hermes-rc" ]; then exit "$(cat "$S/hermes-rc")"; fi
exit 0
STUB

chmod +x "$STUBS"/*

# ---- scenario scaffolding --------------------------------------------------
# new_root NAME builds a complete, healthy scratch installation: two real git
# repositories, a stub venv, an agent .env, an apt stamp with today's mtime, and
# the state files every stub reads. A scenario then perturbs exactly one thing.
new_root() {
  ROOT=$WORK/$1
  STATE=$ROOT/state
  HH=$ROOT/home/hermes/.hermes
  AGENT=$HH/hermes-agent
  VENVB=$AGENT/venv/bin
  WEBUI=$ROOT/home/hermes/hermes-webui
  UP=$ROOT/upstream/hermes-webui
  mkdir -p "$STATE" "$HH" "$VENVB" "$ROOT/home/hermes/.local/bin" \
    "$ROOT/var/lib/apt/periodic" "$ROOT/upstream"

  git -c init.defaultBranch=master init -q "$AGENT"
  printf 'venv/\n' > "$AGENT/.gitignore"
  printf 'agent v1\n' > "$AGENT/VERSION"
  git -C "$AGENT" add -A
  git -C "$AGENT" commit -q -m init

  git -c init.defaultBranch=master init -q "$UP"
  printf 'flask==1.0\n' > "$UP/requirements.txt"
  printf 'webui v1\n' > "$UP/VERSION"
  git -C "$UP" add -A
  git -C "$UP" commit -q -m init
  git clone -q "$UP" "$WEBUI"

  cp "$STUBS/venv-python" "$VENVB/python"
  cp "$STUBS/pip" "$VENVB/pip"
  cp "$STUBS/hermes-bin" "$ROOT/home/hermes/.local/bin/hermes"
  chmod +x "$VENVB/python" "$VENVB/pip" "$ROOT/home/hermes/.local/bin/hermes"

  # A fixture, not a credential: it never leaves the scratch directory and the
  # only thing that reads it is the stub curl two directories away. It is in the
  # [A-Za-z0-9_-] shape on purpose, so assert_health's gate lets it through and
  # the chat branch is the one under test.
  printf 'API_SERVER_ENABLED=true\nAPI_SERVER_KEY=harness-fixture-not-a-key\n' > "$HH/.env"
  touch "$ROOT/var/lib/apt/periodic/unattended-upgrades-stamp"

  printf '%s\n' "$ALL_UNITS" > "$STATE/all-units"
  printf '%s\n' "$ALL_UNITS" > "$STATE/active-units"
  printf '1.0.0' > "$STATE/client-version"
  printf '{"version":"2.0.0"}\n' > "$STATE/hindsight-json"
  printf '{"choices":[{"message":{"content":"pong"}}]}\n' > "$STATE/chat-body"
}

# Advance the webui remote so the forward path has something to move to.
advance_webui() {
  printf 'webui v2\n' > "$UP/VERSION"
  git -C "$UP" commit -q -a -m 'webui v2'
}

# Record the current state as last-good, so rollback has a recorded target
# rather than falling back to the pre-run capture.
seed_last_good() {
  {
    printf 'agent_sha=%s\n' "$(git -C "$AGENT" rev-parse HEAD)"
    printf 'webui_sha=%s\n' "$(git -C "$WEBUI" rev-parse HEAD)"
    printf 'client_version=%s\n' "$(cat "$STATE/client-version")"
    printf 'stamp=%s\n' "$(date -u +%s)"
  } > "$HH/hermes-update.last-good"
}

# Copy the script with its five absolute constants redirected into $ROOT, then
# PROVE the redirection happened. See the safety-interlock note at the top: this
# is what stands between a unit test and `git reset --hard` on a real machine.
rewrite_script() {
  sed \
    -e "s#^HERMES_BIN=/#HERMES_BIN=$ROOT/#" \
    -e "s#^HERMES_HOME=/#HERMES_HOME=$ROOT/#" \
    -e "s#^WEBUI_DIR=/#WEBUI_DIR=$ROOT/#" \
    -e "s#^HERMES_USER_HOME=/#HERMES_USER_HOME=$ROOT/#" \
    -e "s#^APT_STAMP=/#APT_STAMP=$ROOT/#" \
    "$SCRIPT" > "$ROOT/hermes-update.sh"
  for _c in HERMES_BIN HERMES_HOME WEBUI_DIR HERMES_USER_HOME APT_STAMP; do
    if ! grep -q "^$_c=$ROOT/" "$ROOT/hermes-update.sh"; then
      printf 'FATAL: %s was not redirected into the scratch root.\n' "$_c" >&2
      printf 'The constant was renamed or moved in hermes-update.sh. Fix the\n' >&2
      printf 'sed in rewrite_script() in the same commit - do NOT delete this\n' >&2
      printf 'check: without it this harness mutates the real installation.\n' >&2
      exit 2
    fi
  done
  # Every constant whose value is an ABSOLUTE PATH must now be under $ROOT.
  # This used to grep only for /home/hermes, which would have missed a new
  # constant naming any other real root - $APT_STAMP is in the rewrite list
  # above only because somebody remembered it, and the next one might not be
  # remembered. Checking "absolute and not under the scratch root" needs nobody
  # to enumerate anything. The URL constants are unaffected: they start `http`,
  # not `/`.
  #
  # THE OPTIONAL QUOTE IS THE WHOLE POINT OF THE sed. An earlier version matched
  # `^NAME=/` only, so a constant written `NAME="/home/hermes/x"` matched neither
  # this check nor the rewrite above it: it escaped both, and the harness would
  # have run `git reset --hard` and `pip install` against the real installation
  # while reporting nothing wrong. Quoting a path is ordinary shell, so that was
  # a gap the claim above ("needs nobody to enumerate anything") did not cover.
  # The sed strips one leading `'` or `"` before the `/` so that BOTH halves see
  # the path: the -Fv exclusion below then recognises a rewritten quoted
  # constant the same way it recognises an unquoted one. A quoted constant the
  # sed at the top of this function does not redirect still fails here, which is
  # the intended outcome - it must be added to that sed.
  _stray=$(sed -n 's#^\([A-Z_][A-Z_]*\)=["'\'']\{0,1\}\(/.*\)$#\1=\2#p' \
    "$ROOT/hermes-update.sh" | grep -Fv -- "=$ROOT/") || _stray=''
  if [ -n "$_stray" ]; then
    printf 'FATAL: a constant still names an absolute path outside the scratch\n' >&2
    printf 'root after the rewrite:\n%s\n' "$_stray" >&2
    printf 'Add it to the sed in rewrite_script() in the same commit - or, if it\n' >&2
    printf 'genuinely must stay absolute, to a stated exemption here. Do NOT\n' >&2
    printf 'delete this check: without it this harness mutates the real\n' >&2
    printf 'installation.\n' >&2
    exit 2
  fi
}

# Run the rewritten script under the stub PATH. Extra `NAME=value` arguments are
# passed through to the run's environment.
run_script() {
  rewrite_script
  env "PATH=$STUBS:$PATH" \
      "HUT_STATE=$STATE" \
      "HUT_AGENT=$AGENT" \
      HERMES_UPDATE_HC_UUID=harness-fixture-no-real-check \
      "$@" \
      sh "$ROOT/hermes-update.sh" > "$ROOT/out.log" 2>&1
  RC=$?
}

# ---- assertions ------------------------------------------------------------
bval() { sed -n "s/^$1=//p" "$STATE/final-body" 2>/dev/null | head -n 1; }

expect() {  # key want
  _got=$(bval "$1")
  if [ "$_got" = "$2" ]; then pass "$SCEN: $1=$2"
  else bad "$SCEN: $1 (want '$2', got '$_got')"; fi
}

expect_rc() {  # want
  if [ "$RC" = "$1" ]; then pass "$SCEN: exit rc=$1"
  else bad "$SCEN: exit rc (want $1, got $RC)"; fi
}

expect_started() {
  if grep -q -- '--- ping start' "$STATE/pings" 2>/dev/null; then
    pass "$SCEN: the start ping was sent"
  else bad "$SCEN: no start ping was sent"; fi
}

expect_no_ping() {
  if [ ! -f "$STATE/pings" ]; then pass "$SCEN: nothing was pinged"
  else bad "$SCEN: pinged when it should have stayed silent"; fi
}

expect_epoch() {
  case "$(bval run_epoch)" in
    ''|0|*[!0-9]*) bad "$SCEN: run_epoch is not a real epoch second" ;;
    *) pass "$SCEN: run_epoch is a real epoch second" ;;
  esac
}

expect_file_has() {  # file needle label
  if grep -q -- "$2" "$1" 2>/dev/null; then pass "$SCEN: $3"
  else bad "$SCEN: $3"; fi
}

# ===========================================================================
# 1. The happy path.
# Everything moves, everything asserts green, last-good is recorded. If this
# scenario fails, nothing below it means anything.
# ===========================================================================
SCEN='happy path'
new_root happy
advance_webui
touch "$STATE/hermes-moves"
run_script
expect_rc 0
expect_started
expect verdict ok
expect agent_changed yes
expect webui_changed yes
expect client_changed yes
expect update_rc 0
expect backup requested
expect rollback_source none
expect rollback_state none
expect post_rollback not-attempted
expect units_active 5
expect chat_mode chat
expect chat_http 200
expect client_version 2.0.0
expect apt_age_days 0
expect_epoch
expect webui_sha "$(git -C "$WEBUI" rev-parse HEAD)"
expect_file_has "$HH/hermes-update.last-good" '^client_version=2\.0\.0$' \
  'last-good records the newly pinned client version'
expect_file_has "$STATE/hermes.log" 'update --backup' \
  'hermes update was asked for a pre-update backup'

# ===========================================================================
# 2. `hermes update` fails BEFORE the agent checkout moved.
# The one case that must report that nothing was touched. Guessing `yes` here
# would send a destructive rollback at a tree that never moved.
# ===========================================================================
SCEN='update fails, tree did not move'
new_root pre-mutation-update
advance_webui
printf '3\n' > "$STATE/hermes-rc"
run_script
expect_rc 1
expect verdict update-failed
expect update_rc 3
expect agent_changed no
expect rollback_source none
expect rollback_state none
expect post_rollback not-attempted
expect backup requested
expect_file_has "$ROOT/out.log" 'nothing to roll back' \
  'the log says nothing was rolled back'

# ===========================================================================
# 3. `hermes update` fails AFTER the agent checkout moved.
# Post-mutation, so it must roll back and say so. rollback_source=last-good
# proves the recorded target won over the pre-run capture.
# ===========================================================================
SCEN='update fails, tree moved'
new_root post-mutation-update
advance_webui
seed_last_good
touch "$STATE/hermes-moves"
printf '124\n' > "$STATE/hermes-rc"
run_script
expect_rc 1
expect verdict update-failed
expect update_rc 124
expect rollback_source last-good
expect rollback_state complete
expect post_rollback healthy
expect agent_changed no
expect units_active 5
expect_file_has "$ROOT/out.log" 'after the agent tree moved' \
  'the log names the post-mutation branch'

# ===========================================================================
# 4. Pre-mutation: the deployed hindsight version cannot be read.
# hindsight.cynexia.net is a different machine, so this must be a clean exit
# with nothing moved - and units_active must read `not-counted`, not 0, because
# 0 reads as "all five units are down" on a run where nothing counted them.
# ===========================================================================
SCEN='hindsight version unreadable'
new_root pre-mutation-hindsight
rm -f "$STATE/hindsight-json"
run_script
expect_rc 1
expect verdict client-failed
expect client_version unreadable
expect agent_changed no
expect update_rc 0
expect backup not-attempted
expect rollback_source none
expect rollback_state none
expect units_active not-counted
expect_started

# ===========================================================================
# 5. The webui remote cannot be fetched AND the agent did not move.
# `hermes update` found nothing to do, so genuinely nothing has moved and the
# body must say so. This is the benign half of the pair; scenario 6 is the
# other, and the two together are what the shared routing helper decides
# between.
# ===========================================================================
SCEN='webui fetch fails, agent did not move'
new_root pre-mutation-webui
rm -rf "$UP"
run_script
expect_rc 1
expect verdict webui-failed
expect agent_changed no
expect webui_changed no
expect rollback_source none
expect rollback_state none
expect post_rollback not-attempted
expect_file_has "$ROOT/out.log" 'nothing to roll back' \
  'the log says nothing was rolled back'

# ===========================================================================
# 6. The webui remote cannot be fetched AND the agent DID move.
# THE COMMON CASE, and the one that was broken. `hermes update` succeeded,
# which moves the agent checkout, restarts three of the five units onto new
# code, applies forward-only migrations and installs into the shared venv -
# and then the fetch fails. This used to exit with `rollback_source=none`,
# whose meaning in the runbook table is "nothing moved", over exactly that
# state. It must roll back and the body must say it did.
#
# The rollback needs no remote: it re-checkouts the webui to a LOCAL recorded
# object name, so the failure that got here cannot also break the recovery.
# That is why the remote is still absent for the whole scenario.
# ===========================================================================
SCEN='webui fetch fails, agent moved'
new_root post-mutation-webui
seed_last_good
touch "$STATE/hermes-moves"
rm -rf "$UP"
run_script
expect_rc 1
expect verdict webui-failed
expect update_rc 0
expect rollback_source last-good
expect rollback_state complete
expect post_rollback healthy
expect agent_changed no
expect webui_changed no
expect units_active 5
expect_file_has "$ROOT/out.log" 'the hermes-webui fetch failed after the agent tree moved' \
  'the log names the post-mutation branch of the shared router'

# ===========================================================================
# 7. The units do not come back after a good update.
# Post-mutation. The rollback's own restart succeeds, so this ends rolled back
# and healthy - but the verdict stays `restart-failed`, because "they would not
# come back" and "they came back broken" want different first moves.
# ===========================================================================
SCEN='restart fails after the update'
new_root restart-failure
advance_webui
seed_last_good
touch "$STATE/hermes-moves"
printf '1\n' > "$STATE/restart-fail-count"
run_script
expect_rc 1
expect verdict restart-failed
expect update_rc 0
expect rollback_source last-good
expect rollback_state complete
expect post_rollback healthy
expect units_active 5

# ===========================================================================
# 8. Health fails after a good update; the rollback completes and the result
# is healthy. The chat turn fails once, so the post-rollback re-assertion
# passes on the second call.
# ===========================================================================
SCEN='rollback completes'
new_root rollback-complete
advance_webui
seed_last_good
touch "$STATE/hermes-moves"
printf '1\n' > "$STATE/chat-fail-count"
PREV_WEBUI_SHA=$(git -C "$WEBUI" rev-parse HEAD)
run_script
expect_rc 1
expect verdict rolled-back
expect rollback_source last-good
expect rollback_state complete
expect post_rollback healthy
# refresh_reported_state recomputes these from the machine as it IS after the
# rollback. Reporting the SHA the run had just rolled AWAY from was a real
# defect; these three assertions are what keeps it fixed.
expect agent_changed no
expect webui_changed no
expect client_changed no
expect webui_sha "$PREV_WEBUI_SHA"
expect client_version 1.0.0
expect chat_mode chat
expect chat_http 200

# ===========================================================================
# 9. The rollback stops part way through.
# The agent's editable reinstall fails. Every later step must STILL run - a
# rollback that aborted at its first command used to leave the machine half
# restored while the body said `rollback_source=last-good`, which reads as
# "restored". Note what this locks in: the verdict is `rolled-back` because the
# result did pass the re-assertion, and `rollback_state` is the ONLY thing that
# says the restore did not finish. Those are two different questions.
# ===========================================================================
SCEN='rollback stops part way'
new_root rollback-partial
advance_webui
seed_last_good
touch "$STATE/hermes-moves"
printf '1\n' > "$STATE/chat-fail-count"
printf '1\n' > "$STATE/pip-editable-fail-count"
run_script
expect_rc 1
expect verdict rolled-back
expect rollback_source last-good
expect rollback_state failed-agent-install
expect post_rollback healthy
# The steps after the failed one must have run anyway.
expect_file_has "$STATE/pip.log" 'hindsight-client==1.0.0' \
  'the client pin still ran after the agent reinstall failed'
expect webui_sha "$(git -C "$WEBUI" rev-parse HEAD)"
expect client_version 1.0.0

# ===========================================================================
# 10. The rollback completes and the restored state is STILL unhealthy.
# Driven through HERMES_UPDATE_FORCE_HEALTH_FAIL, which is the hook the live
# rollback drill uses - so this also proves the drill's hook still works
# without touching the VM. The hook stays set through the re-assertion on
# purpose, so post_rollback must read `unhealthy` and the verdict must stay
# `rollback-failed`.
# ===========================================================================
SCEN='rollback ends unhealthy'
new_root rollback-unhealthy
advance_webui
seed_last_good
touch "$STATE/hermes-moves"
run_script HERMES_UPDATE_FORCE_HEALTH_FAIL=1
expect_rc 1
expect verdict rollback-failed
expect rollback_source last-good
expect rollback_state complete
expect post_rollback unhealthy
expect chat_mode forced-fail
expect chat_http 000

# ===========================================================================
# 11. No last-good on disk: the rollback target falls back to the pre-run
# capture. This is the first-ever-run case, and it must not silently do
# nothing.
# ===========================================================================
SCEN='rollback with no last-good'
new_root rollback-pre-run
advance_webui
touch "$STATE/hermes-moves"
printf '1\n' > "$STATE/chat-fail-count"
run_script
expect_rc 1
expect verdict rolled-back
expect rollback_source pre-run
expect rollback_state complete
expect post_rollback healthy
expect client_version 1.0.0

# ===========================================================================
# 12. The chat turn degrades because the API server is switched off.
# A green run that skipped the turn is weaker than one that made it, and the
# body has to say which happened rather than looking identical.
# ===========================================================================
SCEN='chat skipped, api disabled'
new_root chat-degraded
advance_webui
touch "$STATE/hermes-moves"
printf 'API_SERVER_ENABLED=false\n' > "$HH/.env"
run_script
expect_rc 0
expect verdict ok
expect chat_mode skipped-api-disabled
expect chat_http 000

# ===========================================================================
# 13. Another run already holds the lock.
# The one route that pings NOTHING, deliberately: a duplicate invocation that
# pinged would either reset the check's timer with a /start or mark it red,
# both describing a run that never happened while the real one is still
# working. The traps are up by this point, so the EXIT trap is REMOVED to get
# that silence rather than never registered - which is a thing that can be
# broken by an edit, and until now nothing tested it.
# ===========================================================================
SCEN='another run holds the lock'
new_root lock-held
touch "$STATE/lock-held"
run_script
expect_rc 75
expect_no_ping
expect_file_has "$ROOT/out.log" 'already-running' \
  'the log names the already-running verdict'

# ===========================================================================
# 14. The unit file's own contract, asserted from here because nothing else
# reads it: the wrapper's longest single foreground child must fit inside
# TimeoutStopSec, or a `systemctl stop` becomes a SIGKILL and the EXIT trap
# never reports. A POSIX sh trap cannot run while a foreground child is
# blocked, so this is the whole of that guarantee.
# ===========================================================================
SCEN='unit bounds'
UNIT=$HERE/../systemd/hermes-update.service
if [ -f "$UNIT" ]; then
  _lim=$(sed -n 's/^TO_HERMES_UPDATE=//p' "$SCRIPT" | head -n 1)
  _gr=$(sed -n 's/^TO_HERMES_GRACE=//p' "$SCRIPT" | head -n 1)
  _stop=$(sed -n 's/^TimeoutStopSec=//p' "$UNIT" | head -n 1)
  _start=$(sed -n 's/^TimeoutStartSec=//p' "$UNIT" | head -n 1)
  _longest=$(( _lim + _gr ))
  if [ "$_stop" -gt "$_longest" ]; then
    pass "$SCEN: TimeoutStopSec=$_stop exceeds the longest child ($_longest s)"
  else
    bad "$SCEN: TimeoutStopSec=$_stop does not cover the longest child ($_longest s)"
  fi
  if [ "$_start" -ge 9040 ]; then
    pass "$SCEN: TimeoutStartSec=$_start covers the 9040 s worst-case route"
  else
    bad "$SCEN: TimeoutStartSec=$_start is below the 9040 s worst-case route"
  fi
  if grep -q '^Restart=' "$UNIT"; then
    bad "$SCEN: the unit has a Restart= - a failed run must stay failed"
  else
    pass "$SCEN: no Restart= - a failed run stays failed"
  fi
  if grep -q '^WorkingDirectory=/home/hermes$' "$UNIT"; then
    pass "$SCEN: WorkingDirectory matches the pin in assert_health"
  else
    bad "$SCEN: WorkingDirectory does not match HERMES_USER_HOME in the script"
  fi
else
  bad "$SCEN: no hermes-update.service beside hermes-vm/scripts"
fi

if [ "$FAILED" -ne 0 ]; then
  printf '\nFAILED\n'
  exit 1
fi
printf '\nAll failure-path scenarios passed.\n'
