#!/bin/sh
# Nightly pull of the Hermes VM's application-state backup.
#
# Runs from the `hermes-pull` CronJob at 02:00 UTC, one hour before the restic
# sweep picks the result up from the hermes-dumps PVC. It SSHes to the hermes
# VM with a dedicated forced-command key (Secret `hermes-ssh`, created by
# `make create-hermes-ssh-secret`), triggers `hermes backup` through the
# wrapper installed on the VM, streams the zip onto the PVC, verifies it, and
# prunes local copies to seven. The wrapper's two verbs are `backup` (run the
# backup, CRC-test it on the VM, stream it on stdout) and `sum` (SHA-256 of
# the staged copy). Design and the VM-side wrapper: the "Hermes VM restore"
# section of docs/operations/homelab.md.
#
# The pulled zip contains the VM's live secrets in plaintext (that is what a
# Hermes backup is). It must exist only on the PVC (LUKS-encrypted SSD) and in
# restic/B2 (client-side encrypted). Never copy it anywhere else from here.
#
# This file passes through envsubst on its way into a ConfigMap, so it must
# not name any allowlisted variable, bare or braced, even in a comment. The
# healthchecks UUID therefore arrives pre-renamed: the CronJob's `env:` block
# is where the allowlisted placeholder lives, and only the runtime name
# HC_UUID reaches this file. See `make check-script-substitution`.
#
# shellcheck disable=SC3040 # `set -o pipefail` is not POSIX, but this image's
# /bin/sh is busybox ash, which implements it. If a future image did not, this
# line would fail and the job would stop loudly rather than silently
# swallowing a broken pipeline.
set -eu
set -o pipefail

# The image runs as uid 1999 with a one-line /etc/passwd mounted from a
# ConfigMap; HOME must exist and be writable (it is this pod's emptyDir) or
# ssh's config probing fails. Exported before anything else runs.
export HOME=/tmp

# ---- healthchecks.io plumbing -------------------------------------
# Same contract as the two restic scripts and influx-backup.sh: /start plus
# exit code, a key=value body, a ping never fails the job, a body never costs
# a ping, and NOTHING CAPTURED FROM A COMMAND is ever emitted. This image has
# wget (busybox) and no curl, so the POST uses --post-file like the restic
# jobs, not influx-backup's curl.
HC_BODY=/tmp/hc-body
# `true >`, not `: >`; stderr redirection precedes the body redirection. Both
# properties are load-bearing — see the long-form comments in
# vps/backup/scripts/restic-backup.sh, which this block mirrors.
hc_reset() { true 2>/dev/null > "$HC_BODY" || true; }
emit() { { printf '%s' "$*" | LC_ALL=C tr -cd '\040-\176'; printf '\n'; } 2>/dev/null >> "$HC_BODY" || true; }

HC="https://hc-ping.com/${HC_UUID}"
# ping_hc [SUFFIX] - "" | start | <exit-status>. A bare trailing slash is an
# HTTP 400, so the URL is built conditionally. A failed ping prints FIXED
# text: for a ping the URL IS the write credential.
ping_hc() {
  _sf=${1:-}
  _u=$HC
  [ -z "$_sf" ] || _u="$HC/$_sf"
  if [ -s "$HC_BODY" ]; then
    if wget -q -T 10 -O- --post-file="$HC_BODY" "$_u" >/dev/null 2>&1; then
      hc_reset; return 0
    fi
    echo "hc: body POST failed, retrying without a body" >&2
  fi
  wget -q -T 10 -O- "$_u" >/dev/null 2>&1 || echo "hc: ping not delivered" >&2
  hc_reset
  return 0
}

# STEP names the phase for failed_step=. `step` also clears FATAL_MSG, so a
# FATAL from an earlier phase can never be reported against a later one.
STEP=startup
FATAL_MSG=""
step() { STEP=$1; FATAL_MSG=""; echo "==> $STEP"; }

# ONE SINK PER DIAGNOSTIC: `fatal` writes the message to the pod log and holds
# it for the ping body from a single call.
fatal() {
  FATAL_MSG=$*
  echo "FATAL: $*" >&2
  exit 1
}

# Body values. `unknown` sentinels so `set -u` cannot bite in the trap if the
# run dies before a measurement ran, and so a missing measurement reads as
# missing rather than as zero.
ZIP_KIB=unknown
SHA_MATCH=unknown
LOCAL_COPIES=unknown
PRUNED=unknown

# THE TRAP'S FIRST ACTION IS CAPTURING $?. It is armed before the first thing
# that can fail. An activeDeadlineSeconds SIGKILL skips this trap entirely —
# that shows on healthchecks.io as started-but-never-finished, which is the
# intended signature for a hang.
# shellcheck disable=SC2329 # invoked by `trap ... EXIT` below, not by name.
on_exit() {
  _xrc=$?
  trap - EXIT
  # Defense in depth: the key copy dies with the trap, not with the pod (a
  # completed Job's pod - and its emptyDir - is retained for ttl days).
  rm -f /tmp/id_ed25519 2>/dev/null || true
  hc_reset
  if [ "$_xrc" -eq 0 ]; then
    emit "summary=ok - zip verified, $ZIP_KIB KiB, sha256 $SHA_MATCH, pruned $PRUNED"
    emit "zip_kib=$ZIP_KIB"
    emit "sha256_match=$SHA_MATCH"
    emit "local_copies=$LOCAL_COPIES"
    emit "pruned=$PRUNED"
  else
    emit "summary=FAILED rc=$_xrc - $STEP"
    emit "failed_step=$STEP"
    # FATAL_MSG is empty unless `fatal` set it, and every fatal message is a
    # literal plus digits-gated numbers. Checksum values, ssh output and
    # hermes output never reach a sink: output this script did not construct
    # cannot be classified at runtime.
    [ -z "$FATAL_MSG" ] || emit "error=$FATAL_MSG"
  fi
  ping_hc "$_xrc"
  exit "$_xrc"
}
trap on_exit EXIT

hc_reset
emit "summary=starting"
ping_hc start

# One function, not a multi-word variable (SC2086-clean). The ServerAlive pair
# makes a peer that dies mid-stream fail in ~2 minutes with a real exit code
# and a proper exit ping, instead of hanging until activeDeadlineSeconds
# SIGKILLs the pod and skips the EXIT trap. The host key is pinned: a
# mismatch (VM rebuilt) fails closed, and the fix is re-keyscanning into the
# hermes-known-hosts ConfigMap, never StrictHostKeyChecking=no.
run_ssh() {
  ssh -i /tmp/id_ed25519 \
      -o UserKnownHostsFile=/known-hosts/known_hosts \
      -o GlobalKnownHostsFile=/dev/null \
      -o StrictHostKeyChecking=yes \
      -o IdentitiesOnly=yes \
      -o BatchMode=yes \
      -o ConnectTimeout=15 \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=4 \
      hermes@hermes.cynexia.net "$@"
}

# ASSERT THE KEY IS ACTUALLY THERE, BEFORE USING IT (the shipped-as-text
# lesson from influx-backup.sh: never let a missing input degrade into a
# quieter failure than it deserves). The copy exists because fsGroup makes
# the mounted Secret group-readable and OpenSSH refuses group-readable
# identity files; /tmp is this pod's emptyDir.
step key
[ -s /ssh-key/id_ed25519 ] || fatal "/ssh-key/id_ed25519 is missing or empty - run make create-hermes-ssh-secret"
cp /ssh-key/id_ed25519 /tmp/id_ed25519
chmod 600 /tmp/id_ed25519

# Pull into a date-stamped partial file. The leading rm is the cleanup for
# every previous failed night: a partial never matches the hermes-*.zip glob,
# so neither the prune below nor the restic gate can mistake it for a backup,
# but without the rm a failed run's ~200 MB orphan would sit on the PVC
# forever and be swept into B2 nightly. Any non-zero ssh exit (VM down,
# wrapper lock contention, hermes failure, CRC failure on the VM) aborts here
# via set -e with failed_step=pull.
step pull
DATE=$(date +%F)
PART="/dumps/hermes-$DATE.zip.partial"
FINAL="/dumps/hermes-$DATE.zip"
rm -f /dumps/hermes-*.zip.partial
run_ssh backup > "$PART"

# Three checks, all fatal. The VM already CRC-tested every member before
# streaming; these prove the TRANSFER was faithful.
step verify
SIZE=$(stat -c %s "$PART") || fatal "could not stat the pulled zip"
case "$SIZE" in ''|*[!0-9]*) fatal "could not read the pulled zip's size" ;; esac
# check-ping-bodies: untaint SIZE - gated to digits by the case above; a byte count this script measured, never tool output
[ "$SIZE" -ge 104857600 ] || fatal "pulled zip is $SIZE bytes, below the 104857600-byte floor - a quick-shaped or truncated backup"
MAGIC=$(head -c4 "$PART" | od -An -tx1 | tr -d ' ')
[ "$MAGIC" = "504b0304" ] || fatal "pulled file is not a zip - bad magic; something other than zip bytes reached stdout"
REMOTE_SHA=$(run_ssh sum)
case "$REMOTE_SHA" in ''|*[!0-9a-f]*) fatal "remote checksum was not a hex digest" ;; esac
LOCAL_SHA=$(sha256sum "$PART" | awk '{print $1}')
if [ "$REMOTE_SHA" = "$LOCAL_SHA" ]; then
  SHA_MATCH=yes
else
  SHA_MATCH=no
  # The digests themselves go nowhere: they are command output. The verdict
  # is the fixed enum SHA_MATCH.
  fatal "sha256 mismatch between the VM's staged zip and the pulled copy"
fi

# Atomic on the PVC: rename within one filesystem.
step publish
mv "$PART" "$FINAL"

# Keep the seven newest. Names are date-stamped, so the POSIX-sorted glob
# expansion is already chronological and $1 is always the oldest — no ls
# pipeline, no command substitution, nothing tainted. Local retention mirrors
# restic's --keep-daily 7 window so a same-day restore never needs B2.
step prune
PRUNED=0
while :; do
  set -- /dumps/hermes-*.zip
  [ -e "$1" ] || fatal "prune: nothing matches /dumps/hermes-*.zip after publish - the rename above did not land"
  [ "$#" -gt 7 ] || break
  echo "pruning $1"
  rm -f "$1"
  PRUNED=$(( PRUNED + 1 ))
done
LOCAL_COPIES=$#

# A measurement must never fail a pull that succeeded.
step measure
ZIP_KIB=$(du -sk "$FINAL" | awk '{print $1}') || ZIP_KIB=unknown
case "$ZIP_KIB" in ''|*[!0-9]*) ZIP_KIB=unknown ;; esac
# check-ping-bodies: untaint ZIP_KIB - gated to digits by the case above; a size this script measured, never tool output

step finished
