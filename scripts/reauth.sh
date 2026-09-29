#!/bin/sh
# Re-authenticate every tool an investigation session needs, in one go:
# omnictl, kubectl and talosctl against both clusters, and ssh to the hermes VM.
#
# Omni keys (SideroV1 PGP for omnictl/talosctl, OIDC for kubectl) expire daily.
# The first authenticated call on an expired key opens the browser sign-in and
# mints a new key, so each call below is that trigger. Run this while you are
# at the keyboard: an unattended run parks every expired tool on a browser tab.
#
# Prints one OK/FAIL line per check, runs them all, exits 1 if any failed.
# Usage: scripts/reauth.sh          (from the repo root or any worktree)

rc=0

check() {
  # $1 label, rest: command. Output is discarded; only the verdict is printed.
  label=$1; shift
  if "$@" >/dev/null 2>&1; then
    echo "OK:   $label"
  else
    echo "FAIL: $label"
    rc=1
  fi
}

first_node() {
  kubectl --context "$1" get nodes -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

check "omnictl (Omni API)" omnictl get clusters

for ctx in cynexia-homelab cynexia-vps; do
  check "kubectl $ctx" kubectl --context "$ctx" get nodes
  node=$(first_node "$ctx")
  if [ -n "$node" ]; then
    check "talosctl $ctx ($node)" talosctl --context "$ctx" -n "$node" version
  else
    echo "FAIL: talosctl $ctx (no node name from kubectl)"
    rc=1
  fi
done

check "ssh hermes@hermes.cynexia.net" ssh -o BatchMode=yes -o ConnectTimeout=10 hermes@hermes.cynexia.net true

exit "$rc"
