# Documentation

Operational documentation for this repo.
`AGENTS.md` at the repo root holds background and conventions only — everything cluster-specific or procedural lives here.

| Document | Covers |
|---|---|
| [operations/omni-access.md](operations/omni-access.md) | Bootstrapping a new workstation: omnictl/kubectl/talosctl from zero, where config and auth keys land, Omni access troubleshooting |
| [operations/apply-workflow.md](operations/apply-workflow.md) | The secret pipeline (1Password → `op run` → envsubst → kubectl), rotation, full Makefile target reference, Talos config patches, Tailscale bootstrap |
| [operations/homelab.md](operations/homelab.md) | Homelab cluster: platform stack, namespaces/workloads, NFS and storage, node network, DNS, encryption at rest, operational gotchas |
| [operations/homelab-health.md](operations/homelab-health.md) | The `health` namespace: ingest pipeline, image-pin rationale, InfluxDB bootstrap, backups/restore, Garmin re-auth, the 2026-08-18 Pomerium wedge and probe rationale |
| [operations/hindsight.md](operations/hindsight.md) | The `hindsight` namespace: the memory backend for the Hermes profiles — topology, auth, upgrade and restore runbooks, the restore drill, key rotation, the hindsight-specific profile deltas (the wiring contract lives in homelab.md), the removal path |
| [operations/vps.md](operations/vps.md) | VPS (Hetzner) cluster: shape, workloads, Cloudflare tunnel/Access, DB decisions, backups |
| [operations/monitoring.md](operations/monitoring.md) | How failures get noticed: the triage table, the four detection layers, probe policy and inventory, CronJob deadlines, the backup verification gates, the five healthchecks.io checks and the eleven uptime-kuma push monitors, the disclosure rules for both, and what none of it catches |
| [operations/uptime-kuma.md](operations/uptime-kuma.md) | Layer 3/4 runbook: creating uptime-kuma monitors by hand, per-monitor HTTP settings, the Cloudflare Access trap, the self-monitor |

`docs/superpowers/` is gitignored — local-only specs and implementation plans, not part of this tree.
