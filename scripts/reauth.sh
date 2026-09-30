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

# 1Password. `op run` needs OP_SERVICE_ACCOUNT_TOKEN in the environment; without
# it every op call falls back to the desktop app and prompts the operator once
# per call. direnv exports the token, but only from an allowed .envrc and only
# in a shell that runs its hook: Codex runs commands as `zsh -lc`, which is not
# interactive, so a seat there has to `eval "$(direnv export zsh)"` itself.
# Allow every worktree's .envrc that matches the main checkout's, then prove a
# non-interactive shell in each one can reach the token without inheriting it.
check "op service account token valid" op whoami
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
  check "op token from a non-interactive shell in $wt" \
    env -u OP_SERVICE_ACCOUNT_TOKEN sh -c 'cd "$1" && eval "$(direnv export bash 2>/dev/null)" && [ -n "$OP_SERVICE_ACCOUNT_TOKEN" ]' _ "$wt"
done

exit "$rc"
