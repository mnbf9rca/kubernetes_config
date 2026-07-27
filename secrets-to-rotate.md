# Secrets to rotate (honesty box)

When a real secret **value** is disclosed anywhere it doesn't belong — echoed to a
terminal, printed in an agent report or transcript, pasted into a chat, written to
a log or scratch file — the discloser records it here immediately, even when
unsure whether the exposure "counts". Entries are identifiers only: **never write
the secret value itself in this file.**

An entry here means: assume compromised, rotate at the next opportunity, then mark
it rotated. Rotation procedures per secret live in AGENTS.md / the relevant
service docs.

| Date | Secret (op:// reference or k8s secret/key) | How it was disclosed | Disclosed by | Status |
|------|--------------------------------------------|----------------------|--------------|--------|
| 2026-07-26 | op://Homelab/health-influxdb/admin-password | `op item get --format json` printed plaintext into local subagent transcript | Task 9 implementer agent | rotated 2026-07-26 |
| 2026-07-26 | op://Homelab/health-influxdb/admin-token | same event as above | Task 9 implementer agent | rotated 2026-07-26 |
| 2026-07-26 | op://Homelab/health-influxdb/garmin-v1-password | same event as above | Task 9 implementer agent | rotated 2026-07-26 |
| 2026-07-26 | op://Homelab/health-influxdb/admin-password | `op item get health-influxdb --fields label=admin-password --format json` run mid-rotation printed a partial (~29 char) plaintext fragment of the then-current admin-password value into the rotation agent's tool output/transcript, while checking whether a prior `op item edit` had landed | Rotation agent (this task) | rotated 2026-07-26 (superseded immediately by a freshly generated replacement password before this exposure was even logged) |
| 2026-07-26 | op://Homelab/health-healthchecks/backup-uuid (HEALTH_HC_BACKUP_UUID) | `make build-homelab` rendered output was grepped/printed to verify envsubst substitution while implementing Task 12; the resolved ping UUID appeared in the agent's tool output/transcript | Task 12 implementer agent | accepted, no rotation (operator decision 2026-07-27): ping UUIDs are spam-target identifiers, not secrets — they grant no access. The value appeared only in a local session transcript and has never been committed to this repo; keeping it out of GitHub (op:// reference only) is the actual control, and that holds. |
| 2026-07-26 | op://Homelab/health-influxdb/ingester-token | `op item get health-influxdb --fields label=ingester-token --reveal | head -c 200` printed the full field value into the agent's tool output/transcript during Attempt 3 of the token-recovery task (step 2 verification) — should have used command substitution into a shell variable only | Attempt 3 recovery agent | rotated 2026-07-26 — new InfluxDB auth minted (same write-only scope), 1Password field overwritten, old auth deleted server-side, new value verified live (200) |
