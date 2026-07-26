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
| 2026-07-26 | op://Homelab/health-influxdb/admin-password | `op item get --format json` printed plaintext into local subagent transcript | Task 9 implementer agent | pending |
| 2026-07-26 | op://Homelab/health-influxdb/admin-token | same event as above | Task 9 implementer agent | pending |
| 2026-07-26 | op://Homelab/health-influxdb/garmin-v1-password | same event as above | Task 9 implementer agent | pending |
