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

# health namespace — healthchecks.io ping UUIDs
HEALTH_HC_APPLE_UUID=op://Homelab/health-healthchecks/apple-uuid
HEALTH_HC_GARMIN_UUID=op://Homelab/health-healthchecks/garmin-uuid
HEALTH_HC_BACKUP_UUID=op://Homelab/health-healthchecks/backup-uuid

# health namespace — InfluxDB (admin creds + generated tokens; ingester/read
# tokens are PENDING until minted in Task 9 and pasted back into 1Password)
HEALTH_INFLUX_ADMIN_PASSWORD=op://Homelab/health-influxdb/admin-password
HEALTH_INFLUX_ADMIN_TOKEN=op://Homelab/health-influxdb/admin-token
HEALTH_INFLUX_GARMIN_V1_PASSWORD=op://Homelab/health-influxdb/garmin-v1-password
HEALTH_INFLUX_INGESTER_TOKEN=op://Homelab/health-influxdb/ingester-token
HEALTH_INFLUX_READ_TOKEN=op://Homelab/health-influxdb/read-token

# health namespace — Health Auto Export (HAE) ingest auth token
HEALTH_HAE_AUTH_TOKEN=op://Homelab/health-hae/auth-token

# health namespace — Garmin credentials (created by the operator, not by automation)
HEALTH_GARMIN_EMAIL=op://Homelab/health-garmin/email
HEALTH_GARMIN_B64_PASSWORD=op://Homelab/health-garmin/b64-password

# health namespace — Pomerium (Google OAuth client is PENDING until the operator
# creates it in Google Cloud console; cookie/shared secrets are generated)
HEALTH_POMERIUM_GOOGLE_CLIENT_ID=op://Homelab/health-pomerium/google-client-id
HEALTH_POMERIUM_GOOGLE_CLIENT_SECRET=op://Homelab/health-pomerium/google-client-secret
HEALTH_POMERIUM_COOKIE_SECRET=op://Homelab/health-pomerium/cookie-secret
HEALTH_POMERIUM_SHARED_SECRET=op://Homelab/health-pomerium/shared-secret

# health namespace — Grafana admin password
HEALTH_GRAFANA_ADMIN_PASSWORD=op://Homelab/health-grafana/admin-password

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

# karakeep — meilisearch master key + NextAuth signing secret + OpenAI key for AI tagging/summarization
KARAKEEP_MEILI_MASTER_KEY=op://VPS/karakeep/meili-master-key
KARAKEEP_NEXTAUTH_SECRET=op://VPS/karakeep/nextauth-secret
KARAKEEP_OPENAI_API_KEY=op://VPS/karakeep/openai_secret_key

# karakeep admin API key for scripts/karakeep-tag-*.py (NOT used in any manifest — shell env only)
KARAKEEP_CLEANUP_API_KEY=op://VPS/karakeep/cleanup_api_key
