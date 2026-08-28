# kubernetes_config

Personal Kubernetes configuration for two [Talos Linux](https://www.talos.dev/)
clusters managed by [Omni](https://omni.siderolabs.com/) — a single-node homelab and a
three-node VPS control plane:

| | Homelab | VPS |
|---|---|---|
| Runs on | Proxmox VM at home | Three Hetzner cloud instances |
| Domain | `*.cynexia.net` (Route53) | `*.cynexia.com` (Cloudflare) |
| Exposure | Private — LAN and Tailscale only | Public, via a cloudflared tunnel behind Cloudflare Access |
| Ingress | Traefik + cert-manager wildcard | cloudflared only |
| Workloads | Media stack, personal health-data pipeline, backups | RSS, bookmarks, automation, uptime monitoring, analytics |

Everything is plain manifests plus kustomize — no Helm releases, no GitOps controller.
Applies are deliberately manual and guarded.

## Layout

```
homelab/          Homelab cluster: talos/ bootstrap/ workloads/ secrets/ health/ backup/
vps/              VPS cluster, same shape
docs/operations/  Runbooks and cluster documentation — start here
AGENTS.md         Repo conventions and rules (also loaded by Claude Code via CLAUDE.md)
Makefile          build / diff / apply per cluster, plus secret and bootstrap helpers
.env.tpl          1Password references (op://...) — never real values
```

## Applying

```bash
make check-tools                # verify the required CLIs are installed
make diff-homelab               # server-side dry run against the cluster
make apply-homelab              # apply (asserts the kubectl context first)
```

`make help` lists every target. The VPS equivalents are `diff-vps` / `apply-vps`.

`build-*` targets are **preview only** — their output has secret values masked, so never
render to a file and apply that file.

## Secrets

No secret values live in this repository. Manifests carry `${VAR}` placeholders;
`.env.tpl` maps those names to `op://` references; and the apply targets resolve them
per-command through `op run`, so secrets never sit in your shell environment. `.envrc`
exports only a 1Password service-account token.

See [`docs/operations/apply-workflow.md`](docs/operations/apply-workflow.md) for the full
mechanism, including the hazards worth knowing before you change it.

## Documentation

| Document | Covers |
|---|---|
| [omni-access.md](docs/operations/omni-access.md) | **Start here on a new machine** — bootstrapping omnictl, kubectl and talosctl from nothing |
| [apply-workflow.md](docs/operations/apply-workflow.md) | The secret pipeline, every Makefile target, and how to add a new secret |
| [homelab.md](docs/operations/homelab.md) | Homelab cluster: stack, storage, networking, DNS, encryption, gotchas |
| [homelab-health.md](docs/operations/homelab-health.md) | The `health` namespace — Apple Health and Garmin ingest, InfluxDB, Grafana, MCP |
| [vps.md](docs/operations/vps.md) | VPS cluster: workloads, tunnel, Access, backups |
| [monitoring.md](docs/operations/monitoring.md) | What is monitored, how, and — importantly — what these checks do **not** catch |

## Legacy

`legacy-microk8s/` and `no_longer_used/` are frozen references from the previous microk8s
cluster. Nothing there is deployed, and nothing new should be added to them; they exist to
be deleted once nothing is being cross-referenced out of them.
