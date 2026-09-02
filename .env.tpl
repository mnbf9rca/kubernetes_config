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

# uptime-kuma push token for the nightly hermes-pull CronJob (Hermes VM backup;
# the SSH key itself is multi-line and goes through
# `make create-hermes-ssh-secret`, not through this pipeline).
HERMES_KUMA_TOKEN=op://Homelab/hermes-backup/kuma-push-token
# The healthchecks.io UUID this job used until 2026-08-26. It is kept resolvable
# only until the operator deletes the retired check; see the note at the foot of
# this section.
HERMES_HC_UUID=op://Homelab/hermes-backup/healthcheck-uuid

# uptime-kuma push monitor for the daily Renovate update watcher (ops
# namespace). `up` while updates simply wait, `down` on a determinate red,
# nothing at all on an indeterminate run; see docs/operations/monitoring.md.
OPS_KUMA_UPDATE_TOKEN=op://Homelab/update-watch/kuma-push-token
OPS_HC_UPDATE_UUID=op://Homelab/update-watch/healthcheck-uuid

# uptime-kuma push token for the daily keel dead-man's-switch (ops namespace).
# The monitor goes DOWN when keel's registry poll counter stops moving; see
# docs/operations/uptime-kuma.md.
OPS_KUMA_KEEL_TOKEN=op://Homelab/keel-fresh/kuma-push-token

# Route53 credentials for cert-manager DNS-01 (Task 2.5)
ROUTE53_ACCESS_KEY_ID=op://Homelab/route53-cert-manager/access-key-id
ROUTE53_SECRET_ACCESS_KEY=op://Homelab/route53-cert-manager/secret-access-key

# ACME contact email for Let's Encrypt (Task 2.5)
ACME_EMAIL=op://Homelab/acme/email

# Jottacloud backup heartbeat. The container environment key is still called
# HEALTHCHECK_UUID because the image reads that name, but the value it now
# carries is a kuma push token — see homelab/workloads/jottacloud-backup.yaml.
JOTTACLOUD_KUMA_TOKEN=op://Homelab/jottacloud-backup/kuma-push-token
HEALTHCHECK_UUID=op://Homelab/jottacloud-backup/HEALTHCHECK_UUID

# health namespace — uptime-kuma push tokens
HEALTH_KUMA_BACKUP_TOKEN=op://Homelab/health-healthchecks/backup-kuma-push-token
HEALTH_KUMA_CLOUDFLARE_TOKEN=op://Homelab/health-healthchecks/cloudflare-kuma-push-token
# ONE token for both ingest buckets: a single CronJob checks apple and garmin in
# one process, so two monitors would be one signal counted twice.
HEALTH_KUMA_INGEST_TOKEN=op://Homelab/health-healthchecks/ingest-kuma-push-token

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
HEALTH_CF_API_TOKEN=op://Homelab/cloudflare/api-token
HEALTH_CF_ZONE_TAGS=op://Homelab/cloudflare/zone-ids

# health namespace — Health Auto Export (HAE) ingest auth token
HEALTH_HAE_AUTH_TOKEN=op://Homelab/health-hae/auth-token

# health namespace — Garmin credentials (created by the operator, not by automation)
HEALTH_GARMIN_EMAIL=op://Homelab/health-garmin/email
HEALTH_GARMIN_B64_PASSWORD=op://Homelab/health-garmin/b64-password

# health namespace — Grafana admin password
HEALTH_GRAFANA_ADMIN_PASSWORD=op://Homelab/health-grafana/admin-password

# hindsight namespace — the self-hosted memory backend for the Hermes profiles.
#
# pg-password must be generated URL-SAFE (alphanumeric): it is interpolated into
# the database DSN in homelab/secrets/hindsight.yaml, and a character needing
# percent-encoding there is a debugging session nobody needs.
#
# The extraction-LLM key's 1Password field is named for the provider it currently
# holds (`openai-api-key`), while the k8s Secret key it lands in stays the
# provider-neutral `llm-api-key`. Switching provider is two env lines in
# hindsight.yaml plus a new field here.
#
# tenant-api-key is shared by the API, the control plane and the canary. The Hermes
# gateways on VM 103 send the same value but do NOT read it from here: they resolve
# a second vault copy, op://hermes/hindsight/tenant-api-key, because the VM's
# service account can see only the `hermes` vault. Rotating it means updating BOTH
# vault items, restarting the three gateways, and re-running the VM-side smoke test
# — see docs/operations/hindsight.md.
HINDSIGHT_PG_PASSWORD=op://Homelab/hindsight/pg-password
HINDSIGHT_LLM_API_KEY=op://Homelab/hindsight/openai-api-key
HINDSIGHT_TENANT_API_KEY=op://Homelab/hindsight/tenant-api-key
HINDSIGHT_CP_ACCESS_KEY=op://Homelab/hindsight/cp-access-key
# uptime-kuma push tokens: the nightly pg_dump and the hourly canary
HINDSIGHT_KUMA_TOKEN=op://Homelab/hindsight/kuma-push-token
HINDSIGHT_CANARY_KUMA_TOKEN=op://Homelab/hindsight/canary-kuma-push-token
HINDSIGHT_HC_UUID=op://Homelab/hindsight/healthcheck-uuid
HINDSIGHT_CANARY_HC_UUID=op://Homelab/hindsight/canary-healthcheck-uuid

# THE NINE `*_HC_*` / `HEALTHCHECK_UUID` LINES ABOVE ARE RETIRED BUT STILL WIRED.
# On 2026-08-26 nine routine heartbeats moved from healthchecks.io to uptime-kuma
# push monitors, and no manifest references those UUIDs any more. They stay in
# this file, in REQUIRED_VARS and in ENVSUBST_VAR_NAMES until the operator has
# deleted the corresponding checks: red at healthchecks.io next to green in kuma
# is the proof the migration took, and until then the old credentials must stay
# resolvable in case anything needs backing out. Removing them is a follow-up
# commit. Two exceptions are NOT retired — RESTIC_HC_UUID and
# VPS_RESTIC_HC_UUID still drive live checks, because a restic ping body is the
# triage runbook and a one-line kuma message cannot carry it.

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

# uptime-kuma push token for the daily keel dead-man's-switch on the VPS
# cluster. Its own token and its own vault item: a homelab job must not hold a
# VPS credential, and a shared monitor could not tell the two clusters apart.
VPS_OPS_KUMA_KEEL_TOKEN=op://VPS/keel-fresh/kuma-push-token

# Cloudflare Access service token for proxy.cynexia.com, the homelab egress
# proxy hostname. Read by the `homelab-proxy` cloudflared client on this
# cluster. Token `vps-proxy-access`, expires 2031-09-01 - see
# docs/operations/uptime-kuma.md.
VPS_HOMELAB_PROXY_ACCESS_CLIENT_ID=op://VPS/homelab-proxy/access-client-id
VPS_HOMELAB_PROXY_ACCESS_CLIENT_SECRET=op://VPS/homelab-proxy/access-client-secret

# karakeep admin API key for scripts/karakeep-tag-*.py (NOT used in any manifest — shell env only)
KARAKEEP_CLEANUP_API_KEY=op://VPS/karakeep/cleanup_api_key
