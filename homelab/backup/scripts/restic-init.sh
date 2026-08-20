#!/bin/sh
# One-shot initialisation of the homelab restic repository in B2.
#
# Runs from the `restic-init` Job. Credentials and the repository URL arrive in
# the environment from the `restic-b2` Secret (envFrom), so nothing here names
# them — and nothing here may: RESTIC_REPOSITORY, RESTIC_PASSWORD,
# B2_ACCOUNT_ID and B2_ACCOUNT_KEY are all in the homelab envsubst allowlist,
# and this file passes through envsubst on its way into a ConfigMap. Writing
# any of those names would publish the real value in that ConfigMap. See
# `make check-script-substitution`.
#
# shellcheck disable=SC3040 # `set -o pipefail` is not POSIX, but the
# restic/restic:0.17.3 image's /bin/sh is busybox ash, which implements it. If
# a future image did not, this line would fail under `set -e` and the Job would
# stop loudly rather than silently swallowing a broken pipeline.
set -euo pipefail
if restic snapshots >/dev/null 2>&1; then
  echo "Repository already initialized."
else
  echo "Initializing restic repository..."
  # Idempotent: the Job's TTL deletes it, so every later apply recreates
  # it. A bare `restic init` then fails against the existing repo and
  # leaves a Failed Job behind on each apply. Probing first makes a
  # re-run a clean no-op, and a genuine init failure still fails loudly.
  if restic cat config >/dev/null 2>&1; then
    echo "Repo already initialized - nothing to do."
  else
    # Idempotent: the Job's TTL deletes it, so every later apply recreates
    # it. A bare `restic init` then fails against the existing repo and
    # leaves a Failed Job behind on each apply. Probing first makes a
    # re-run a clean no-op, and a genuine init failure still fails loudly.
    if restic cat config >/dev/null 2>&1; then
      echo "Repo already initialized - nothing to do."
    else
      restic init
    fi
  fi
fi
