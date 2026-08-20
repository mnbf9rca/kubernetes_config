#!/bin/sh
# One-shot initialisation of the VPS restic repository in B2.
#
# DELIBERATELY NOT SHARED WITH homelab/backup/scripts/restic-init.sh, even
# though the two are near-identical. Both files pass through envsubst on their
# way into a ConfigMap, and the two clusters use different allowlists:
# `RESTIC_REPOSITORY` is inert under vps/ but live under homelab/. A single
# shared file would be safe in one tree and would publish the real B2 URL in a
# ConfigMap in the other, and nothing about the file would say which. Keeping
# them separate makes that impossible rather than merely unlikely.
#
# Credentials and the repository URL arrive in the environment from the
# `restic-b2` Secret (envFrom). The repository URL is echoed below through
# REPO_DISPLAY, a second env var pointed at the same Secret key, precisely so
# this file never has to write `RESTIC_REPOSITORY`. See
# `make check-script-substitution`.
#
# shellcheck disable=SC3040 # `set -o pipefail` is not POSIX, but the
# restic/restic:0.17.3 image's /bin/sh is busybox ash, which implements it. If
# a future image did not, this line would fail under `set -e` and the Job would
# stop loudly rather than silently swallowing a broken pipeline.
set -euo pipefail
echo "Initializing restic repo at $REPO_DISPLAY"
# Idempotent: the Job's TTL deletes it, so every later apply recreates
# it. A bare `restic init` then fails against the existing repo and
# leaves a Failed Job behind on each apply. Probing first makes a
# re-run a clean no-op, and a genuine init failure still fails loudly.
if restic cat config >/dev/null 2>&1; then
  echo "Repo already initialized - nothing to do."
else
  restic init
fi
echo "Done."
