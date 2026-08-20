# 1Password-backed env var template for both clusters.
#
# This file is read by `op run --env-file=.env.tpl -- <command>`, which resolves
# the `op://` references and makes the values available as environment variables
# to that command only, for the duration of the subprocess. The Makefile's
# apply/diff/build targets wrap themselves in it; see the header of the Makefile
# and docs/operations/apply-workflow.md.
#
# `.envrc` deliberately does NOT export these. It exports only a 1Password
# service-account token, which is what lets `op run` resolve these references
# non-interactively (no biometric prompt, works headless). Secrets are therefore
# resolved per-command and never sit in your ambient shell environment.
#
# `op run` masks secrets in the child's stdout/stderr, NOT in its environment —
# corrected 2026-08-20; the previous warning in this file claimed the opposite
# and was a misdiagnosis (the `len=${#VAR}` returning 24 tell was a coincidence:
# that value is genuinely 24 characters long). The real hazard is rendering to a
# file: `make build-homelab > out.yaml` writes the literal mask string into the
# Secrets. Never render-then-apply; use `make apply-*`, whose pipeline is a
# single process and carries real values throughout.
#
# Per-service secrets are commented out and should be uncommented as
# each workload is migrated.

# --- Platform secrets (needed from Phase 2 onward) ---

# Restic / Backblaze B2 (Phase 3 backup system)
B2_ACCOUNT_ID=op://Homelab/b2-restic/account-id
B2_ACCOUNT_KEY=op://Homelab/b2-restic/account-key
RESTIC_PASSWORD=op://Homelab/b2-restic/repo-password
RESTIC_REPOSITORY=op://Homelab/b2-restic/repository
# healthchecks.io dead-man's-switch for the nightly restic CronJob
RESTIC_HC_UUID=op://Homelab/b2-restic/healthcheck-uuid

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
HEALTH_HC_CLOUDFLARE_UUID=op://Homelab/health-healthchecks/cloudflare-uuid

# health namespace — InfluxDB (admin creds + generated tokens; ingester/read
# tokens are minted via `make health-influx-bootstrap` and pasted into 1Password)
HEALTH_INFLUX_ADMIN_PASSWORD=op://Homelab/health-influxdb/admin-password
HEALTH_INFLUX_ADMIN_TOKEN=op://Homelab/health-influxdb/admin-token
HEALTH_INFLUX_GARMIN_V1_PASSWORD=op://Homelab/health-influxdb/garmin-v1-password
HEALTH_INFLUX_INGESTER_TOKEN=op://Homelab/health-influxdb/ingester-token
HEALTH_INFLUX_READ_TOKEN=op://Homelab/health-influxdb/read-token
HEALTH_INFLUX_CLOUDFLARE_TOKEN=op://Homelab/health-influxdb/cloudflare-token

# health namespace — Cloudflare analytics ingest. The API token must carry
# Zone.Analytics:Read and NOTHING else (the job never writes to Cloudflare).
# zone-tags is a single field holding `name=zonetag,name=zonetag`; zone IDs are
# kept out of this public repo because they identify the account.
HEALTH_CF_API_TOKEN=op://Homelab/cloudflare-analytics/api-token
HEALTH_CF_ZONE_TAGS=op://Homelab/cloudflare-analytics/zone-tags

# health namespace — Health Auto Export (HAE) ingest auth token
HEALTH_HAE_AUTH_TOKEN=op://Homelab/health-hae/auth-token

# health namespace — Garmin credentials (created by the operator, not by automation)
HEALTH_GARMIN_EMAIL=op://Homelab/health-garmin/email
HEALTH_GARMIN_B64_PASSWORD=op://Homelab/health-garmin/b64-password

# health namespace — Pomerium (Google OAuth client created by the operator in
# the Google Cloud console; cookie/shared secrets are generated locally)
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
# healthchecks.io dead-man's-switch for the nightly restic CronJob
VPS_RESTIC_HC_UUID=op://VPS/b2-restic/healthcheck-uuid

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
