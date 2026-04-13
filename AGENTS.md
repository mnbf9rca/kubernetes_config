# Kubernetes Config Repository

Personal Kubernetes cluster config for a home media/downloads stack being rebuilt on **Talos Linux**, managed by Omni, with a VPS cluster to follow.

## Target State

The repository is being rebuilt from a drifted microk8s cluster to a clean greenfield Talos setup. See the design document at `docs/superpowers/specs/2026-04-11-talos-homelab-rebuild-design.md` and the implementation plan at `docs/superpowers/plans/2026-04-11-talos-homelab-rebuild.md` (both are local-only under the gitignored `docs/superpowers/` tree).

## Repo Layout

```
kubernetes_config/
├── .envrc                    # direnv entrypoint: `set -a; eval "$(op inject -i .env.tpl)"; set +a`
├── .env.tpl                  # op-template with VAR=op://... lines (committed; no real secret values)
├── Makefile                  # apply/diff/build/check-tools/apply-talos/create-jotta-secret targets
├── homelab/                  # new Talos homelab cluster
│   ├── kustomization.yaml    # top-level: bootstrap + secrets + workloads + backup
│   ├── talos/                # Omni ConfigPatches resources (applied via `make apply-talos`)
│   ├── bootstrap/            # platform: namespaces (with PSA labels), local-path, NFS CSI, cert-manager, traefik, keel
│   ├── workloads/            # application workloads (one file per service, --- separated, no ns override)
│   ├── secrets/              # Secret manifests with ${VAR} envsubst placeholders
│   └── backup/               # restic init Job + nightly CronJob (hostPath /var/mnt/local-path-provisioner)
├── vps/                      # planned Phase 2 (Hetzner Talos), not yet populated
├── legacy-microk8s/          # frozen reference copies of the old microk8s manifests
└── docs/
    └── superpowers/          # gitignored: specs and implementation plans
```

## Apply Workflow

Secrets flow from 1Password into the shell environment via direnv. The `.envrc` is:

```bash
set -a
eval "$(op inject -i .env.tpl)"
set +a
```

`.env.tpl` contains `VAR=op://Homelab/item/field` lines. `op inject` replaces the `op://` references with real values and outputs plain `VAR=value` assignments. `set -a` (bash allexport) ensures every assignment is exported so direnv and child processes see them — **this is load-bearing**; without `set -a` the vars are shell-local and direnv won't pick them up. Launch Claude or any shell from a directory where direnv is active and the vars are inherited automatically.

> **Do NOT use `op run --env-file=.env.tpl -- claude`.** `op run`'s masking implementation sets child-process env vars to the literal 24-character string `<concealed by 1Password>` instead of real values. envsubst then substitutes the placeholder into Kubernetes Secret manifests and kubectl stores garbage — silent corruption. Diagnostic tell: `echo "len=${#VAR}"` returns 24.

Targets:

```bash
make check-tools              # verify kubectl, kustomize, envsubst, op, talosctl, omnictl
make build-homelab            # render kustomize + envsubst to stdout (preview)
make diff-homelab             # kubectl diff against current cluster state
make apply-homelab            # apply to the current kubeconfig context
make apply-talos              # envsubst + omnictl apply every file in homelab/talos/machineconfig-patches/
make create-jotta-secret      # imperative secret creation for jottacloud-backup (multi-line rclone config)
```

`make apply-homelab` runs `kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)' | kubectl apply -f -` and asserts `kubectl current-context == cynexia-homelab` via the `check-context` target before any cluster write. Secrets are substituted from direnv-loaded env vars at apply time; no plaintext secret values live in git.

**`ENVSUBST_VARS` is an explicit allowlist, passed single-quoted** — never call envsubst without one. With no allowlist, envsubst substitutes every `${VAR}` token in the stream, including shell variables embedded in upstream manifests (e.g. `$VOL_DIR` inside local-path-provisioner's helper-pod setup script), breaking them silently. With double-quoted args, the shell expands `${VAR}` before envsubst sees them, producing garbage arguments. Single-quoting preserves the literal tokens. When you add a new secret placeholder to a manifest, add both its line to `.env.tpl` and its token to `ENVSUBST_VARS` in the Makefile.

**Multi-line secrets cannot go through the envsubst pipeline** — multi-line values (like `rclone.conf`) break YAML parsing after substitution. Escape hatch: a dedicated Makefile target that calls `op read` + `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -`. See `make create-jotta-secret` for the canonical pattern. Only use this for secrets that genuinely can't be single-line; everything else should flow through envsubst.

**`op inject` resolves commented lines.** `#TAILSCALE_AUTH_KEY=op://...` in `.env.tpl` still gets resolved — shell `#` comments don't short-circuit op's template substitution. Be careful when grepping `op inject` output during debugging; secrets can surface from "disabled" lines.

## Cluster Stack (Target)

- **Talos Linux** single-node VM on Proxmox, managed by **Omni**
- **Tailscale** as a Talos system extension on the host (subnet router + remote `talosctl`/`kubectl` access)
- **Traefik** as a hostNetwork DaemonSet for ingress on :80/:443 (no MetalLB)
- **cert-manager** + Let's Encrypt with **Route53 DNS-01** solver; single wildcard `*.cynexia.net` cert
- **local-path-provisioner** on the node's SSD (user volume mount)
- **NFS CSI driver** for NFS-backed media from the Proxmox ZFS pool
- **keel** for image auto-updates (with `keel.sh/match-tag: "true"` required on every Deployment — without it keel silently downgrades `:latest` via OCI version label)
- **restic** nightly CronJob to Backblaze B2 (`b2:homelab-restic-d5e15f22`) backing up `/var/mnt/local-path-provisioner`. 7d/4w/6m retention.
- **jottacloud-backup** CronJob in its own namespace: rclone syncs Jottacloud → NFS, kopia backs that up to a separate B2 bucket (`cloud-files-backup`). Reports to healthchecks.io.
- Apps' own scheduled backups (sonarr, radarr, emby, sabnzbd) should write zips to **`/config/Backups/`** so restic catches them. Do NOT rely on the sonarr/radarr sqlite quiesce sidecar pattern from earlier drafts of the plan — it's redundant because the app's own zip backup handles DB consistency.

## Domain

Homelab services resolve on `*.cynexia.net` (Route53). The homelab cluster is **not exposed to the public internet**. Remote access to homelab services goes via Tailscale. The VPS cluster (Phase 2) uses `cynexia.com` (Cloudflare DNS) with Cloudflare Access / Zero Trust for public auth.

## Workload List

| Namespace | Purpose | Services (after rebuild) |
|---|---|---|
| `downloads` | Media management | sonarr, radarr, sabnzbd, hydra2, emby, tinyproxy |
| `jottacloud-backup` | Cloud backup | jottacloud-backup CronJob (own namespace) |
| `cert-manager` | TLS | cert-manager controller |
| `traefik` | Ingress | Traefik DaemonSet (PSA privileged — hostNetwork) |
| `keel` | Auto-updates | keel controller |
| `backup` | Backup | restic init Job + nightly CronJob (PSA privileged — hostPath) |

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
| `10.10.10.1` | `/tank/video/` (emby + *arr media), `/tank/largeappdata/jottacloud` (jottacloud rclone sync target) |
| `fs.cynexia.net` | `/tank/appdata/*`, `/tank/largeappdata/*` |

## Node network

The Talos node has three relevant interfaces:

| Interface | IP | Purpose |
|---|---|---|
| `ens18` (LAN) | `10.100.0.100` | All `*.cynexia.net` A records point here. Reserved on the router. |
| `ens19` (storage) | `10.10.10.10` | NFS traffic to `10.10.10.1`. Kubernetes reports this as `InternalIP` which is misleading. |
| `tailscale0` | `100.85.18.48` | Remote access via Tailscale mesh |

**Do not use `10.10.10.10` as a DNS target** — it's the storage NIC and isn't reachable from the home LAN. Route53 A records for `*.cynexia.net` must use `10.100.0.100`.

## DNS (Route53)

- Hosted zone for `cynexia.net`: `Z3409TNW35PGSS`
- AWS CLI is authenticated on the user's workstation — manage DNS directly:
  ```bash
  aws route53 change-resource-record-sets --hosted-zone-id Z3409TNW35PGSS \
    --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"<host>.cynexia.net","Type":"A","TTL":60,"ResourceRecords":[{"Value":"10.100.0.100"}]}}]}'
  ```
- After updating a DNS record, browsers usually need a hard refresh (Cmd+Shift+R) to pick up the new target because of 60s TTL caching.

## Operational gotchas (learned during Phase 3/4)

- **`homelab/workloads/kustomization.yaml` must NOT set `namespace:` at the top level.** It would rewrite the namespace on every resource, breaking services that live outside `downloads` (e.g. jottacloud-backup). Each workload manifest declares its own namespace explicitly.
- **`backup` and `traefik` namespaces are PSA `privileged`**, set via labels directly in `homelab/bootstrap/namespaces.yaml`. Any workload using `hostPath` or `hostNetwork` violates the cluster-wide `baseline` PSA enforce level and needs its namespace elevated this way.
- **NFS PVs retain their `claimRef` after the PVC is deleted** (reclaim policy `Retain`). They stay in `Released` state and won't auto-bind to a new PVC until you `kubectl patch pv <name> --type=json -p='[{"op":"remove","path":"/spec/claimRef"}]'`.
- **Linuxserver image `host_whitelist`:** fresh sabnzbd blocks unknown hostnames with a 403 "Access denied - Hostname verification failed". Edit `/config/sabnzbd.ini` via `kubectl exec` to add the external hostname, then restart the pod.
- **Traefik wildcard TLS as default cert:** Traefik serves the `wildcard-cynexia-net-tls` cert as its default via `homelab/bootstrap/traefik/traefik.yaml`'s file provider ConfigMap. Ingresses don't need a `tls:` block — just declare the `host:` rule and Traefik handles HTTPS termination automatically. This also avoids the cross-namespace TLS secret replication problem.
- **Alpine DNS workaround:** linuxserver (Alpine-based) images have DNS resolution issues inside Kubernetes' default DNS policy. Every Deployment uses:
  ```yaml
  dnsPolicy: None
  dnsConfig:
    nameservers: ["8.8.8.8", "8.8.4.4"]
  ```
- **Services migrated in Phase 4** were deployed fresh with empty PVCs. The user exported app-level backups from the old cluster via each service's own UI, then imported them into the new instance via the same UI. No rsync-from-old-cluster data seeding was needed — simpler than the original plan.
- **Old cluster's jottacloud-backup CronJob is suspended** (`kubectl --context=microk8s -n jottacloud-backup patch cronjob jottacloud-backup-scheduled -p '{"spec":{"suspend":true}}'`) to avoid overlap with the new cluster.

## Legacy Reference

`legacy-microk8s/` contains the original flat-layout microk8s manifests. This directory is **frozen reference only** — do not add new files here. It will be removed once the Talos rebuild is fully operational (see Phase 5.3 of the plan).

## When Editing

- Keep the one-file-per-service pattern in `homelab/workloads/`.
- Put all resources for a service (Deployment, Service, Ingress, PVCs) in a single file with `---` separators.
- Every resource in the manifest must declare its own `namespace:` explicitly — do NOT rely on the kustomization-level namespace override.
- Every new Deployment must include the full set of keel annotations above.
- Never commit plaintext secret values. Use `${VAR}` placeholders + direnv + envsubst. For multi-line secrets (rclone.conf etc.), create a dedicated `make <service>-secret` target using the `op read` + `kubectl create secret --dry-run=client -o yaml | kubectl apply -f -` pattern.
- After adding a new secret placeholder: add it to `.env.tpl`, add the token to `ENVSUBST_VARS` in the `Makefile`, and `direnv reload` in your shell.
- For new `hostPath`/`hostNetwork` workloads: elevate their namespace to PSA `privileged` in `homelab/bootstrap/namespaces.yaml`.
