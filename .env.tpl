# 1Password-backed env var template for the homelab cluster.
#
# This file is read by `op inject -i .env.tpl`, which resolves the 1Password refs
# to their real values and outputs plain VAR=value lines. `.envrc` wraps that
# in `set -a` so every resulting assignment is exported into the shell
# environment for direnv (and therefore for any child process, including
# `make apply-homelab`, `kubectl`, `omnictl`, and interactive shells).
#
# Launch Claude (or any shell) from a directory where direnv is active and
# the vars are inherited automatically. No manual sourcing required.
#
# Do NOT use `op run --env-file=.env.tpl -- <command>`. `op run`'s masking
# implementation sets child-process env vars to the literal 24-character
# string `<concealed by 1Password>` instead of real values. envsubst then
# substitutes that placeholder into Kubernetes Secret manifests and kubectl
# stores garbage — silent corruption. See AGENTS.md "Apply Workflow" for
# the diagnostic tell (`echo "len=${#VAR}"` returns 24).
#
# Per-service secrets are commented out and should be uncommented as
# each workload is migrated (Phase 4).

# --- Platform secrets (needed from Phase 2 onward) ---

# Restic / Backblaze B2 (Phase 3 backup system)
B2_ACCOUNT_ID=op://Homelab/b2-restic/account-id
B2_ACCOUNT_KEY=op://Homelab/b2-restic/account-key
RESTIC_PASSWORD=op://Homelab/b2-restic/repo-password
RESTIC_REPOSITORY=op://Homelab/b2-restic/repository

# Route53 credentials for cert-manager DNS-01 (Task 2.5)
ROUTE53_ACCESS_KEY_ID=op://Homelab/route53-cert-manager/access-key-id
ROUTE53_SECRET_ACCESS_KEY=op://Homelab/route53-cert-manager/secret-access-key

# ACME contact email for Let's Encrypt (Task 2.5)
ACME_EMAIL=op://Homelab/acme/email

# Jottacloud backup healthcheck (Phase 4)
HEALTHCHECK_UUID=op://Homelab/jottacloud-backup/HEALTHCHECK_UUID

# --- VPS cluster secrets (Phase 2) ---

# Restic / Backblaze B2 for VPS (separate bucket, separate repo, separate password)
VPS_B2_ACCOUNT_ID=op://VPS/b2-restic/account-id
VPS_B2_ACCOUNT_KEY=op://VPS/b2-restic/account-key
VPS_RESTIC_PASSWORD=op://VPS/b2-restic/repo-password
VPS_RESTIC_REPOSITORY=op://VPS/b2-restic/repository

# n8n credential encryption key — load-bearing, extracted from old VPS
N8N_ENCRYPTION_KEY=op://VPS/n8n/encryption-key

# umami postgres credentials + app secret
UMAMI_DB_PASSWORD=op://VPS/umami/db-password
UMAMI_APP_SECRET=op://VPS/umami/app-secret

# karakeep — meilisearch master key + NextAuth signing secret
KARAKEEP_MEILI_MASTER_KEY=op://VPS/karakeep/meili-master-key
KARAKEEP_NEXTAUTH_SECRET=op://VPS/karakeep/nextauth-secret
