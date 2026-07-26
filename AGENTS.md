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
│   └── backup/               # restic init Job + nightly CronJob (hostPath /var/mnt/ssd/local-path-provisioner)
├── vps/                      # Phase 2 Hetzner Talos cluster — live, see "VPS Cluster (Phase 2)" below
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
- **Encryption at rest** (2026-07-26): both pve3 NVMe partitions backing the Talos VM (vmdata LVM = OS disk/etcd, and the user volume) are LUKS2, auto-unlocked at host boot by the TPM via clevis (PCR 7, requires Secure Boot enabled). VM 100's disks point at `/dev/mapper/vmdata_crypt` LVs and `/dev/mapper/talos_ssd_crypt`. If VMs don't autostart after a pve3 boot, the TPM refused (firmware/SB change): `cryptsetup open` each volume with the recovery passphrases in 1Password (`op://Homelab/TPM/...`), `vgchange -ay`, `systemctl start pve-guests`, then re-run `clevis luks bind`. The jottacloud staging copy on the HDD pool is separately encrypted via rclone crypt (`DEST_REMOTE` in the workload ConfigMap).
- **NFS CSI driver** for NFS-backed media from the Proxmox ZFS pool
- **keel** for image auto-updates (with `keel.sh/match-tag: "true"` required on every Deployment — without it keel silently downgrades `:latest` via OCI version label)
- **restic** nightly CronJob to Backblaze B2 (`b2:homelab-restic-d5e15f22`) backing up `/var/mnt/ssd/local-path-provisioner`. 7d/4w/6m retention.
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

`10.10.10.1` and `fs.cynexia.net` are the **same physical host** — a single `tank` zpool, folders exported over NFS and consumed by the cluster as static PV/PVCs. Two hostnames for the one box (storage-NIC IP vs. general LAN name), not two servers. All the data that matters lives in zpool folders on that host; the Talos node's own SSD partitions (vmdata LVM, user volume) are for VM/system lifecycle — etcd and local-path PVCs — not for NFS-backed data. The homelab `restic`/B2 job only backs up that SSD (`/var/mnt/ssd/local-path-provisioner`); it is the only backup destination this repo manages, and it does **not** cover the NFS zpool — that has its own backup story on the NAS side, outside this repo.

| Server | Typical paths |
|---|---|
| `10.10.10.1` | `/tank/video/` (emby + *arr media), `/tank/largeappdata/jottacloud` (jottacloud rclone sync target) |
| `fs.cynexia.net` | `/tank/appdata/*`, `/tank/largeappdata/*` |

## Node network

The Talos node has three relevant interfaces:

| Interface | IP | Purpose |
|---|---|---|
| `ens18` (LAN) | `10.100.0.100` | All `*.cynexia.net` A records point here. Statically assigned in `homelab/talos/machineconfig-patches/305-homelab-lan-network.yaml`; OPNsense Kea reservation kept as defense in depth. |
| `ens19` (storage) | `10.10.10.10` | NFS traffic to `10.10.10.1`. Kubernetes reports this as `InternalIP` which is misleading. |
| `tailscale0` | `100.85.18.48` | Remote access via Tailscale mesh |

**Do not use `10.10.10.10` as a DNS target** — it's the storage NIC and isn't reachable from the home LAN. Route53 A records for `*.cynexia.net` must use `10.100.0.100`.

## talosctl / Omni access

- talosctl goes through the Omni proxy: context `cynexia-homelab`, node addressed by **node name** (`talosctl -n talos-5yn-s9u ...`) — never by IP (`-n 10.100.0.100` fails with "node not found, cannot resolve its management address").
- Auth is SideroV1 PGP keys in `~/.talos/keys/`, one per context+user, minted via an Omni browser sign-in on first use. If a command fails with `Could not authenticate: open ~/.talos/keys/<ctx>-<user>.pgp`: remove the stale context(s) (`talosctl config remove <ctx> -y`, switching current context first if needed), refetch with `omnictl talosconfig --cluster homelab` (merges into `~/.talos/config`), then run any talosctl command and complete the browser sign-in. kubectl auth (Omni OIDC) is separate and unaffected.
- Omni cluster names are `homelab` and `vps` (`omnictl get clusters`); the node name is discoverable via `kubectl get nodes -o name`.

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
- **Static-IP the LAN NIC, don't trust DHCP for stable nodes.** Talos v1.12's controller-runtime DHCP4 client can NAK-loop on renewal if the boot lease didn't land on the reserved IP. Kea OFFERs the reservation cleanly but the client REQUEST in the same transaction asks for the cached dynamic-pool IP, so the loop never converges. The DHCP retry storm also trips RFC 5905 KoD rate-limit on the gateway's NTP, surfacing as `time.SyncController` errors that look like a clock issue. `homelab/talos/machineconfig-patches/305-homelab-lan-network.yaml` puts `ens18` on a static config and moves NTP to public servers (time.cloudflare.com / time.google.com / pool.ntp.org) so the cluster doesn't depend on the gateway for either. OPNsense Kea reservation kept as defense in depth.

## Health namespace

Personal health-data pipeline (Apple Health + Garmin → InfluxDB → Grafana, plus a Claude MCP connector), added Phase 0/1 as its own `health` namespace in the homelab cluster. Design docs live in the separate `~/Downloads/git/HealthRecords` repo — `docs/superpowers/specs/2026-07-11-health-platform-vision.md` and `docs/superpowers/specs/2026-07-11-health-records-ingestion-design.md`. Phase 2 (facade, records store, multi-person registry) is scoped there, not here.

- **No keel — every image is version/digest-pinned.** This is a data pipeline; auto-upgrading it is not wanted. `namespaces.yaml` marks `health` PSA `baseline` (nothing here needs hostPath/hostNetwork), but every current workload already trips `restricted`-level PSA warnings — a hardening pass to `restricted` is a queued follow-up. Renovate is scoped to `homelab/health/**` only (with `pinDigests`) to propose bumps instead of keel auto-applying them.
- **Ingress:** a dedicated `cloudflared` Deployment (tunnel `cynexia-health`, creds in 1Password as the **DOCUMENT** item `health-cloudflared` — use `op document get`, not `op read`, document items don't expose a plain field) fronts two public `*.cynexia.com` hostnames: `hae.cynexia.com` (Health Auto Export ingest) and `mcp.cynexia.com` (Claude MCP connector via Pomerium). `authenticate.cynexia.com` rides the same tunnel as Pomerium's Google-OAuth callback. After changing hostnames in `homelab/health/cloudflared.yaml`, run `make route-health-dns`; recreate the creds Secret with `make create-health-cloudflared-secret`. Grafana is **not** on this tunnel — it's private, Traefik-fronted at `grafana-health.cynexia.net` like every other homelab service (LAN/Tailscale only).
- **MCP is a sidecar, not a standalone Deployment+Service+NetworkPolicy** (deviation from the original design): the cluster's flannel CNI doesn't enforce NetworkPolicy, so a "pomerium-only" NetworkPolicy in front of a standalone MCP server would have been inert fencing — the MCP server is authless in HTTP mode. Instead it runs as a second container in the `pomerium` pod, reached over `localhost:3000`, using the kernel-enforced loopback netns as the real isolation boundary. Residual risk: upstream (`ghcr.io/mnbf9rca/influxdb-mcp-server`, built multi-arch from source — no official image, and Mac-local `docker buildx` alone only produces arm64 while the node is amd64) binds `0.0.0.0` with no `--bind` flag, so pod-IP:3000 is still reachable in-cluster; documented in `pomerium.yaml`. Queued: a bind-flag patch, and reinstating a NetworkPolicy if the CNI is ever swapped to Cilium. Pomerium is pinned to **v0.33.0**, not v0.32.1 — needs `mcp_allowed_client_id_domains: [claude.ai, claude.com]` or claude.ai's OAuth dynamic client registration 401s.
- **`make health-influx-bootstrap`** creates the InfluxDB buckets, the v1 DBRP mapping + v1-compat auth user (garmin-grafana needs InfluxDB 1.x-style auth), and prints two scoped tokens (ingester write-only, MCP+Grafana read-only) for one-time manual paste into 1Password — InfluxDB 2.9 hash-stores tokens server-side, so the printed value is the only copy, ever. Token extraction is `--json | jq -r .token`, not `--hide-headers` + awk column-parsing (multi-word `-d` descriptions shift awk's column and silently capture a description fragment instead of the token) — **`jq` is a hard dependency**, in `check-tools`.
- **Backups:** the `influx-backup` CronJob (02:30 daily, ahead of the 03:00 restic sweep) writes a native `influx backup` (14 generations) plus a per-bucket, 8-day-windowed line-protocol export (60 generations, gzip) to the `health-dumps` PVC on `local-path`. Because that PVC lives on the node's SSD, the existing hostPath restic→B2 CronJob picks it up for free — no separate off-cluster wiring needed. Restore drill: `influx restore --full` self-defeats (clobbers its own auth mid-restore) — use scoped `influx restore --bucket <name>` instead; first drill passed 2026-07-26. Quarterly drills should also exercise the untested DR path: `--full` onto a brand-new, never-`setup` instance.
- **Garmin re-auth is annual** (tokens on the `garmin-tokens` PVC last ~1yr): scale `garmin-grafana` to 0 *before* redoing the interactive login pod, then back to 1 — a crashlooping pod with an expired token fires an MFA SMS at the operator on every restart. The login pod needs `enableServiceLinks: false` (the influxdb Service's injected `INFLUXDB_PORT=tcp://...` otherwise crashes the script's `int()` parse) plus the full InfluxDB v1 env block, since the script demo-writes to InfluxDB before it shows the login prompt. **Keep `replicas: 0` committed while paused** — `make apply-homelab` resurrects any uncommitted scale-down.
- **Monitoring:** three healthchecks.io checks (`health-apple-ingest`, `health-garmin-ingest` 1d/12h; `health-influx-backup` 1d/6h; UUIDs in 1Password item `health-healthchecks`). The backup CronJob pings unconditionally; the separate `ingest-freshness` CronJob (every 6h) pings the apple/garmin checks only when that source's InfluxDB data is actually <24h old — so a real ingest gap shows up as a healthchecks.io alert instead of being masked by an unrelated cron firing on schedule.
- **Rotation** per `health-*` 1Password item: edit the item → `direnv reload` → `make apply-homelab` → restart the consuming pod. InfluxDB tokens specifically: mint the replacement via the `health-influx-bootstrap` pattern, update 1Password, apply, then delete the old auth server-side. See `secrets-to-rotate.md` (repo root) for the disclosure honesty-box rules already covered in the main body of this file.
- **Encryption status:** secretbox-at-rest verified 2026-07-26 (canary: plaintext sentinel absent, `k8s:enc:secretbox:v1:` prefix present in etcd). Disk encryption is superseded-not-skipped — pve3 NVMe LUKS2+TPM2 (since 2026-07-26, see "Encryption at rest" above) already covers etcd and every local-path PVC, including `health-dumps`, `influxdb-data`, `grafana-data`, and `garmin-tokens`.
- **Tech debt / deferred:** Garmin points can't carry a `person` tag (upstream limitation of the v1-compat write path); Apple points get a hardcoded `person=rob` static tag instead of a real multi-person model — the Phase 2 facade/person-registry design is expected to absorb this. Also deferred: Cloudflare Access service-token in front of the tunnel hostnames (Bearer token + Pomerium email-allowlist suffice for now), and Grafana alert rules (Phase 3, pending data accumulation).
- **Verified working 2026-07-26:** the Claude.ai connector (read queries succeed; a write probe correctly 403s — the MCP read-token has no write scope, server-log-verified) and the HAE ingest path (`https://hae.cynexia.com/api/healthautoexport/v1/influxdb/ingest?target=iphone-rob`, bearer token `op://Homelab/health-hae/auth-token`, JSON, Batch Requests ON for large exports; hourly aggregates cover 2020–2025, raw data from 2026-01-01 — keep the same URL/tags on every export or you get duplicate series).

## Legacy Reference

`legacy-microk8s/` contains the original flat-layout microk8s manifests. This directory is **frozen reference only** — do not add new files here. It will be removed once the Talos rebuild is fully operational (see Phase 5.3 of the plan).

## When Editing

- Keep the one-file-per-service pattern in `homelab/workloads/`.
- Put all resources for a service (Deployment, Service, Ingress, PVCs) in a single file with `---` separators.
- Every resource in the manifest must declare its own `namespace:` explicitly — do NOT rely on the kustomization-level namespace override.
- Every new Deployment must include the full set of keel annotations above.
- When creating 1Password items via `op item create`, explicitly type the fields: bare `field=value` defaults to **concealed**, so mark non-secret fields (emails, IDs, UUIDs, hostnames) as `field[text]=value` and keep only actual secrets concealed. Wrongly-concealed non-secrets make the vault harder to debug; visible secrets are worse.
- **Secret disclosure → honesty box.** If you (human or agent) expose a real secret value anywhere it doesn't belong — terminal output, an agent report/transcript, chat, a log or scratch file — do two things immediately: (1) tell the operator in your next message, and (2) append a row to `secrets-to-rotate.md` at the repo root identifying the secret by its `op://` reference or k8s secret/key, how it was disclosed, and by whom. Identifiers only — never the value. Err on the side of logging: a false-positive row costs one unnecessary rotation; a silent disclosure costs the assumption of confidentiality. This applies even when the exposure feels harmless (short-lived token, local-only transcript, immediately-cleared scrollback).
- Never commit plaintext secret values. Use `${VAR}` placeholders + direnv + envsubst. For multi-line secrets (rclone.conf etc.), create a dedicated `make <service>-secret` target using the `op read` + `kubectl create secret --dry-run=client -o yaml | kubectl apply -f -` pattern.
- After adding a new secret placeholder: add it to `.env.tpl`, add the token to `ENVSUBST_VARS` in the `Makefile`, and `direnv reload` in your shell.
- For new `hostPath`/`hostNetwork` workloads: elevate their namespace to PSA `privileged` in `homelab/bootstrap/namespaces.yaml`.

## VPS Cluster (Phase 2)

Public-internet-facing cluster on Hetzner for personal web services.

- **Context:** `cynexia-vps` (Omni-managed, same Omni instance as homelab)
- **Host:** Hetzner CX43 in `fsn1`, Talos single-node, 75 GB Cloud Volume as user volume at `/var/mnt/data`
- **Network:** Hetzner Private Network `10.0.0.0/24`; no public :80/:443 on the node; Hetzner Cloud Firewall drops public inbound
- **Ingress:** cloudflared tunnel only (`cynexia-vps` named tunnel). No Traefik, no cert-manager, no MetalLB, no NFS CSI.
- **TLS/auth:** terminated at Cloudflare edge. Cloudflare Access with email-OTP in front of every hostname. umami `/script.js` + `/api/send/*` and n8n `/webhook/*` are Access-bypassed for public ingestion.
- **Domain:** `*.cynexia.com` (Cloudflare-hosted zone, not Route53). Homelab's `cynexia.net` is separate and unrelated.
- **Namespace:** `vps` for all workloads (no per-service namespaces, no top-level kustomize namespace override)
- **Backups:** separate B2 bucket, separate restic repo, sqlite quiesce sidecars for n8n/freshrss/karakeep/uptime-kuma, pg_dumpall sidecar for umami's dedicated postgres
- **Apply:** `make apply-vps` (with `check-vps-context` preflight asserting current kubectl context is `cynexia-vps`)
- **Secrets:** 1Password `VPS` vault, referenced via `VPS_*` / workload-specific vars in `.env.tpl`. `N8N_ENCRYPTION_KEY` is load-bearing and was extracted from the old n8n container during the rebuild.
- **DB shape:** per-service sqlite except umami which needs postgres. Shared postgres was researched and rejected — karakeep is sqlite-only (issue #1782), uptime-kuma v2 is sqlite/MariaDB only (issue #5674), and the consolidation saving didn't justify the upgrade-coupling cost.
