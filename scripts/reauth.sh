#!/bin/sh
# Re-authenticate every tool an investigation session needs, in one go:
# omnictl, kubectl and talosctl against both clusters, ssh to the hermes VM,
# and 1Password from a non-interactive shell in every worktree.
#
# Omni keys (SideroV1 PGP for omnictl/talosctl, OIDC for kubectl) expire daily.
# The first authenticated call on an expired key opens the browser sign-in and
# mints a new key, so each call below is that trigger. Run this while you are
# at the keyboard: an unattended run parks every expired tool on a browser tab.
#
# Checks the caller's token first, then runs every check and prints one verdict
# per check. Exits 1 if any check fails.
# Usage: scripts/reauth.sh [--seat] (from the repo root or any worktree)

# If the main .envrc itself changed, allow it there before restarting this seat.
if [ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]; then
  echo 'FAIL: this shell has no service account token. Stop, and ask for this agent to be restarted from a shell where direnv has loaded. Remember to authorize new worktrees first with scripts/reauth.sh from the main checkout.'
  exit 1
fi
echo 'OK:   service account token present'

case "$*" in
  '') check_worktrees=1 ;;
  --seat) check_worktrees=0 ;;
  *) echo 'FAIL: usage: scripts/reauth.sh [--seat]'; exit 1 ;;
esac

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

# 1Password. `op run` needs OP_SERVICE_ACCOUNT_TOKEN in the environment; without
# it every op call falls back to the desktop app and prompts the operator once
# per call. The calling shell must already have inherited the token before
# this script runs; a child cannot set its parent's environment.
check "op service account token valid" op whoami
if [ "$check_worktrees" -eq 1 ]; then
  # The plain operator run loads .envrc in worktrees; --seat skips this loop
  # so a seat cannot prompt through the desktop app for every worktree.
  repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
  # A for loop, not a pipeline into `while read`: a pipeline runs the loop in a
  # subshell and the rc=1 below would be lost. Worktree paths here carry no spaces.
  for wt in $(git -C "$repo" worktree list --porcelain | awk '/^worktree /{print $2}'); do
    [ -f "$wt/.envrc" ] || continue
    if cmp -s "$repo/.envrc" "$wt/.envrc"; then
      direnv allow "$wt/.envrc" >/dev/null 2>&1
    else
      echo "FAIL: $wt/.envrc differs from $repo/.envrc; not allowing it"
      rc=1
      continue
    fi
    # shellcheck disable=SC2016  # the $-expressions are for the child sh, deliberately
    if ! env -u OP_SERVICE_ACCOUNT_TOKEN sh -c 'cd "$1" && eval "$(direnv export bash 2>/dev/null)" && [ -n "$OP_SERVICE_ACCOUNT_TOKEN" ]' _ "$wt"; then
      # Say why. direnv's stderr never carries the token (that goes to stdout,
      # which is discarded), but redact anything token-shaped as a backstop.
      reason=$(env -u OP_SERVICE_ACCOUNT_TOKEN sh -c 'cd "$1" && direnv export bash 2>&1 >/dev/null' _ "$wt" \
        | sed -E 's/ops_[A-Za-z0-9_-]+/ops_<redacted>/g' | grep -m1 -E 'ERROR|error' | cut -c1-200)
      echo "FAIL: op token from a non-interactive shell in $wt"
      echo "      ${reason:-direnv exported no token and printed no error}"
      rc=1
    else
      echo "OK:   op token from a non-interactive shell in $wt"
    fi
  done
fi

exit "$rc"
