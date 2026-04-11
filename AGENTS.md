# Kubernetes Config Repository

Personal Kubernetes cluster config for a home media/downloads stack being rebuilt on **Talos Linux**, managed by Omni, with a VPS cluster to follow.

## Target State

The repository is being rebuilt from a drifted microk8s cluster to a clean greenfield Talos setup. See the design document at `docs/superpowers/specs/2026-04-11-talos-homelab-rebuild-design.md` and the implementation plan at `docs/superpowers/plans/2026-04-11-talos-homelab-rebuild.md` (both are local-only under the gitignored `docs/superpowers/` tree).

## Repo Layout

```
kubernetes_config/
├── .envrc                    # direnv + 1Password CLI; loads secrets into env vars at shell entry
├── Makefile                  # apply/diff/build/check-tools targets
├── homelab/                  # new Talos homelab cluster
│   ├── kustomization.yaml    # top-level
│   ├── talos/                # Talos machine config patches (applied via Omni)
│   ├── bootstrap/            # platform: namespaces, storage, ingress, TLS, keel
│   ├── workloads/            # application workloads (one file per service, --- separated)
│   ├── secrets/              # Secret manifests with ${VAR} envsubst placeholders
│   └── backup/               # restic CronJob
├── vps/                      # planned Phase 2 (Hetzner Talos), not yet populated
├── legacy-microk8s/          # frozen reference copies of the old microk8s manifests
└── docs/
    └── superpowers/          # gitignored: specs and implementation plans
```

## Apply Workflow

Secrets are resolved from 1Password at process start via `op run`:

```bash
# Launch a shell (or Claude) with resolved env vars
op run --env-file=.env.tpl -- bash
op run --env-file=.env.tpl -- claude

# Or run any make target ad-hoc
op run --env-file=.env.tpl -- make apply-homelab
```

Inside the launched process, env vars are pre-populated — no direnv, no sourcing. The `.env.tpl` file contains `VAR=op://vault/item/field` lines; `op run` resolves them and injects the values into the child process's environment.

Targets:

```bash
make check-tools              # verify kubectl, kustomize, envsubst, op, talosctl, omnictl
make build-homelab            # render kustomize + envsubst to stdout (preview)
make diff-homelab             # kubectl diff against current cluster state
make apply-homelab            # apply to the current kubeconfig context
```

`make apply-homelab` runs `kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)' | kubectl apply -f -` and asserts `kubectl current-context == cynexia-homelab` via the `check-context` target before any cluster write. Secrets are substituted from `op run`-injected env vars at apply time; no plaintext secret values live in git.

**`ENVSUBST_VARS` is an explicit allowlist, passed single-quoted** — never call envsubst without one. With no allowlist, envsubst substitutes every `${VAR}` token in the stream, including shell variables embedded in upstream manifests (e.g. `$VOL_DIR` inside local-path-provisioner's helper-pod setup script), breaking them silently. With double-quoted args, the shell expands `${VAR}` before envsubst sees them, producing garbage arguments. Single-quoting preserves the literal tokens. When you add a new secret placeholder to a manifest, add both its line to `.env.tpl` and its token to `ENVSUBST_VARS` in the Makefile.

## Cluster Stack (Target)

- **Talos Linux** single-node VM on Proxmox, managed by **Omni**
- **Tailscale** as a Talos system extension on the host (subnet router + remote `talosctl`/`kubectl` access)
- **Traefik** as a hostNetwork DaemonSet for ingress on :80/:443 (no MetalLB)
- **cert-manager** + Let's Encrypt with **Route53 DNS-01** solver; single wildcard `*.cynexia.net` cert
- **local-path-provisioner** on the node's SSD (user volume mount)
- **NFS CSI driver** for NFS-backed media from the Proxmox ZFS pool
- **keel** for image auto-updates (with `keel.sh/match-tag: "true"` required on every Deployment — without it keel silently downgrades `:latest` via OCI version label)
- **restic** nightly CronJob to Backblaze B2 for cluster-wide backup. Services are atomic — each one embeds its own backup-prep logic where needed (sqlite quiesce sidecar for sonarr/radarr; Emby's own Premier Backup plugin for emby).

## Domain

Homelab services resolve on `*.cynexia.net` (Route53). The homelab cluster is **not exposed to the public internet**. Remote access to homelab services goes via Tailscale. The VPS cluster (Phase 2) uses `cynexia.com` (Cloudflare DNS) with Cloudflare Access / Zero Trust for public auth.

## Workload List

| Namespace | Purpose | Services (after rebuild) |
|---|---|---|
| `downloads` | Media management | sonarr, radarr, sabnzbd, hydra2, emby, tinyproxy, jottacloud-backup |
| `cert-manager` | TLS | cert-manager controller |
| `traefik` | Ingress | Traefik DaemonSet |
| `keel` | Auto-updates | keel controller |
| `backup` | Backup | restic init Job + nightly CronJob |

Retired in the rebuild: immich, ollama, open-webui, komga, jellyfin, mylar3, lazylibrarian, caddy, cloudflared (homelab — VPS keeps its own), postgresql.

## File Conventions

- Each service is **one YAML file** under `homelab/workloads/` containing its Deployment, Service, Ingress, and PVCs separated by `---`.
- Services use `PUID=1999` / `PGID=1999` for file ownership on shared media (verified against the current sonarr/emby manifests).
- Secret manifests under `homelab/secrets/` contain only `${VAR}` placeholders. Real values come from 1Password via direnv at apply time.
- NFS PVs and their PVCs live in the same service file.
- Every Deployment with auto-updates carries the full keel annotation set:
  ```yaml
  keel.sh/policy: force
  keel.sh/match-tag: "true"   # REQUIRED — without this keel silently downgrades :latest
  keel.sh/trigger: poll
  keel.sh/pollSchedule: "@every 6h"
  ```

## NFS Servers

| Server | Typical paths |
|---|---|
| `10.10.10.1` | `/tank/video/` (emby + *arr media) |
| `fs.cynexia.net` | `/tank/appdata/*`, `/tank/largeappdata/*` |

## Legacy Reference

`legacy-microk8s/` contains the original flat-layout microk8s manifests. This directory is **frozen reference only** — do not add new files here. It will be removed once the Talos rebuild is fully operational (see Phase 5.3 of the plan).

## When Editing

- Keep the one-file-per-service pattern in `homelab/workloads/`.
- Put all resources for a service (Deployment, Service, Ingress, PVCs) in a single file with `---` separators.
- Match the namespace of existing similar services.
- Every new Deployment must include the full set of keel annotations above.
- Never commit plaintext secret values. Use `${VAR}` placeholders + direnv + envsubst.
