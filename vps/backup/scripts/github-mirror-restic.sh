#!/bin/sh
# Second half of the github-mirror CronJob: snapshot the mirror PVC to the
# hide-only B2 repository, keep a year of daily snapshots, verify, and push the
# combined verdict to uptime-kuma. The first half (github-mirror.py, the init
# container) wrote /data/.status. Design and runbooks:
# docs/operations/github-mirror.md.
#
# THIS RUNNER NEVER RUNS `restic init`. Hidden versions are invisible to
# listing, so after a mass hide the bucket looks exactly like a fresh one; a
# runner that initialised on "repository does not exist" would build a new
# empty repository beside the hidden one and push `up`. The repository was
# initialised once, by hand (docs/operations/github-mirror.md); exit code 10
# here means "the repository is gone", and that is a `down`.
#
# This file passes through envsubst on its way into a ConfigMap, so it must
# not name any allowlisted variable, bare or braced, even in a comment. The
# uptime-kuma push token arrives pre-assembled as PUSH_URL from the CronJob's
# env block. See `make check-script-substitution`.
#
# shellcheck disable=SC3040 # `set -o pipefail` is not POSIX, but the
# restic/restic image's /bin/sh is busybox ash, which implements it.
set -eu
set -o pipefail

DATA=${MIRROR_DATA:-/data}

# ---- uptime-kuma push plumbing (same contract as hermes-pull.sh) ----------
# wget, not curl: restic/restic is Alpine with busybox wget and no curl.
MSG_FILE=/tmp/kuma-msg
msg_reset() { true 2>/dev/null > "$MSG_FILE" || true; }
# shellcheck disable=SC2329 # called only from on_exit, which runs from the EXIT trap.
emit() { { printf '%s ' "$*" | LC_ALL=C tr -cd '\040-\176'; } 2>/dev/null >> "$MSG_FILE" || true; }
# shellcheck disable=SC2329 # called only from on_exit.
push_kuma() {
  _st=$1
  _m=$(cut -c1-200 "$MSG_FILE" 2>/dev/null | tr -d '\n' \
       | LC_ALL=C tr -c 'A-Za-z0-9=._:/-' '+') || _m=""
  # stderr discarded and fixed text printed: wget quotes the URL it was
  # handed, and the URL carries the monitor's token.
  wget -q -T 15 -O /dev/null "$PUSH_URL?status=$_st&msg=$_m" >/dev/null 2>&1 \
    || echo "kuma: push not delivered" >&2
  msg_reset
  return 0
}

STEP=startup
step() { STEP=$1; echo "==> $STEP"; }

# Values read from the mirror step's status file. `unknown` sentinels so a
# missing measurement reads as missing, never as zero.
VERDICT=unknown
REPOS=unknown
FORCED=unknown
DELETED=unknown

# shellcheck disable=SC2329 # invoked by `trap ... EXIT`, not by name.
on_exit() {
  _xrc=$?
  trap - EXIT
  echo "detail: rc=$_xrc step=$STEP mirror_verdict=$VERDICT repos=$REPOS" \
       "forced_default=$FORCED deleted_default=$DELETED"
  msg_reset
  if [ "$_xrc" -eq 0 ]; then
    emit "verdict=ok"
  else
    if [ "$STEP" = verdict ]; then
      emit "verdict=default-branch-changed"
    elif [ "$VERDICT" = ok ]; then
      emit "verdict=restic-failed"
    else
      emit "verdict=$VERDICT"
    fi
  fi
  emit "repos=$REPOS"
  emit "forced_default=$FORCED"
  emit "deleted_default=$DELETED"
  # LAST: the only token that varies in length.
  [ "$_xrc" -eq 0 ] || emit "failed_step=$STEP"
  if [ "$_xrc" -eq 0 ]; then push_kuma up; else push_kuma down; fi
  exit "$_xrc"
}
trap on_exit EXIT

# Read the status line: each token is key=value of a fixed enum or digits.
# The values are taken by a fixed-format parse, never by executing the file.
step read-status
[ -r "$DATA/.status" ] || { VERDICT=no-status; exit 1; }
for _kv in $(head -n1 "$DATA/.status"); do
  case $_kv in
    verdict=*)         VERDICT=${_kv#verdict=} ;;
    repos=*)           REPOS=${_kv#repos=} ;;
    forced_default=*)  FORCED=${_kv#forced_default=} ;;
    deleted_default=*) DELETED=${_kv#deleted_default=} ;;
  esac
done

# Snapshot whatever is there even when the mirror step failed: the mirrors on
# disk are intact and stale, and the daily cadence is worth keeping.
step cat-config
restic cat config >/dev/null            # exit 10 = repository gone: fatal, never init
step unlock
restic unlock                            # stale locks only; each pod has a fresh hostname
step backup
restic backup --quiet --tag nightly "$DATA"
step forget
# Prunes only when a snapshot was removed, so nothing prunes in year one.
restic forget --quiet --keep-within 1y --prune --max-unused unlimited
step check
restic check --quiet

step verdict
[ "$VERDICT" = ok ] || exit 1
# A force-push to, or deletion of, a default branch is the compromise signal
# this job exists to surface: down, and the counts say which.
[ "$FORCED" = 0 ] && [ "$DELETED" = 0 ] || exit 1
