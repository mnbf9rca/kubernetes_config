# Homelab cluster reference

Single-node Talos Linux VM on Proxmox (`pve3`), managed by Omni, serving the media /
downloads stack and the health-data pipeline. Not exposed to the public internet —
remote access is via Tailscale. Kubectl context: `cynexia-homelab`.

## Platform stack

| Component | Notes |
|---|---|
| Talos Linux | Single-node VM on Proxmox, managed by Omni (cluster name `homelab`) |
| Tailscale | Talos system extension on the host: subnet router + remote `talosctl`/`kubectl` |
| Traefik | hostNetwork DaemonSet on :80/:443. No MetalLB. |
| cert-manager | Let's Encrypt, Route53 DNS-01 solver, single wildcard `*.cynexia.net` cert |
| local-path-provisioner | Backed by the node's SSD user volume (`/var/mnt/ssd`) |
| NFS CSI driver | Static PV/PVCs against the Proxmox host's ZFS pool |
| keel | Image auto-updates — **except** the `health` namespace, which forbids it |
| restic | Nightly CronJob (03:00 UTC) → Backblaze B2 `b2:homelab-restic-d5e15f22`, 7 daily / 4 weekly / 6 monthly. Pings healthchecks.io on start and exit code — see [monitoring.md](monitoring.md#the-restic-ping-wrapper) |
| jottacloud-backup | Own namespace; rclone Jottacloud → NFS, then kopia → B2 `cloud-files-backup`; reports to healthchecks.io |

PSA: the cluster enforces `baseline` by default. `traefik` (hostNetwork/hostPort) and
`backup` (hostPath) are elevated to `privileged` by labels in
`homelab/bootstrap/namespaces.yaml`. Any new hostPath/hostNetwork workload needs the
same treatment.

`cert-manager` and `local-path-storage` namespaces are created by their upstream
manifests, so they are deliberately absent from `namespaces.yaml` (kustomize rejects
duplicates). `keel`'s namespace **is** declared there, because upstream keel moved to
Helm-only distribution and `homelab/bootstrap/keel/keel.yaml` is hand-written.

## Namespaces and workloads

| Namespace | Purpose | Services |
|---|---|---|
| `downloads` | Media management | sonarr, radarr, sabnzbd, hydra2, emby |
| `jottacloud-backup` | Cloud backup | jottacloud-backup CronJob (own namespace) |
| `cert-manager` | TLS | cert-manager controller |
| `traefik` | Ingress | Traefik DaemonSet (PSA privileged — hostNetwork) |
| `keel` | Auto-updates | keel controller |
| `backup` | Backup | restic init Job + nightly CronJob (PSA privileged — hostPath) |
| `health` | Personal health data pipeline | influxdb, apple-health-ingester, garmin-grafana, grafana, pomerium + MCP sidecar, cloudflared, backup + freshness CronJobs — see [homelab-health.md](homelab-health.md) |

Ingress hostnames are `*.cynexia.net` (Route53), Traefik-fronted, LAN/Tailscale only:
`sonarr`, `radarr`, `sab`, `hydra`, `emby`, `grafana-health`.

Retired in the rebuild: immich, ollama, open-webui, komga, jellyfin, mylar3,
lazylibrarian, caddy, postgresql, **tinyproxy**. cloudflared was retired from the
downloads-era stack but is not retired homelab-wide — the `health` namespace runs its own
dedicated `cynexia-health` tunnel, separate from the VPS cluster's `cynexia-vps` tunnel.

## Storage and NFS

`10.10.10.1` and `fs.cynexia.net` are the **same physical host** — one `tank` zpool,
folders exported over NFS and consumed by the cluster as static PV/PVCs. Two hostnames
for one box (storage-NIC IP vs. general LAN name), not two servers.

| Address | Typical paths |
|---|---|
| `10.10.10.1` | `/tank/video/` (emby + *arr media), `/tank/largeappdata/jottacloud` (rclone sync target) |
| `fs.cynexia.net` | `/tank/appdata/*`, `/tank/largeappdata/*` |

All the data that matters lives on that zpool. The Talos node's own SSD partitions
(vmdata LVM, user volume) are for VM/system lifecycle — etcd and local-path PVCs — not
for NFS-backed data. The homelab restic/B2 job backs up **only** that SSD
(`/var/mnt/ssd/local-path-provisioner`); it is the only backup destination this repo
manages and it does **not** cover the NFS zpool, which has its own backup story on the
NAS side, outside this repo.

Applications' own scheduled backups (sonarr, radarr, emby, sabnzbd) should write zips to
`/config/Backups/` so restic catches them. The sqlite-quiesce sidecar pattern from
earlier drafts of the plan is **not** used here — it's redundant when the app's own zip
backup already handles DB consistency. (Consequently the homelab restic job has no
backup verification gate; the VPS one does, because the VPS *does* run quiesce sidecars.)

Until 2026-08 neither restic CronJob reported anywhere and neither had a runtime ceiling,
so a hung run would have blocked every following night silently and been discovered at
restore time. Both now ping healthchecks.io and carry `activeDeadlineSeconds` —
[monitoring.md](monitoring.md#scheduled-work-deadlines-and-dead-mans-switches).

## Node network

| Interface | IP | Purpose |
|---|---|---|
| `ens18` (LAN) | `10.100.0.100` | All `*.cynexia.net` A records point here |
| `ens19` (storage) | `10.10.10.10` | NFS traffic to `10.10.10.1`; Kubernetes misleadingly reports this as `InternalIP` |
| `tailscale0` | `100.85.18.48` | Remote access via the Tailscale mesh |

**Never use `10.10.10.10` as a DNS target** — the storage NIC is not reachable from the
home LAN.

### Why the LAN NIC is static

Talos v1.12's controller-runtime DHCP4 client can NAK-loop on renewal if the boot lease
didn't land on the reserved IP: Kea OFFERs the reservation cleanly, but the client
REQUEST in the same transaction asks for the cached dynamic-pool address, so the loop
never converges. The resulting retry storm also trips RFC 5905 KoD rate-limiting on the
gateway's NTP, which surfaces as `time.SyncController` errors that look like a clock
problem and send you debugging the wrong subsystem.

`homelab/talos/machineconfig-patches/305-homelab-lan-network.yaml` therefore puts
`ens18` on a static address and moves NTP to public servers (time.cloudflare.com /
time.google.com / pool.ntp.org), so the cluster depends on the gateway for neither. The
OPNsense Kea reservation is kept as defense in depth.

## DNS (Route53)

Hosted zone for `cynexia.net`: `Z3409TNW35PGSS`. The AWS CLI is authenticated on the
workstation, so records are managed directly:

```bash
aws route53 change-resource-record-sets --hosted-zone-id Z3409TNW35PGSS \
  --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"<host>.cynexia.net","Type":"A","TTL":60,"ResourceRecords":[{"Value":"10.100.0.100"}]}}]}'
```

TTL is 60s, so after a change browsers usually need a hard refresh (Cmd+Shift+R) to stop
using the cached target.

`cynexia.com` is a **different** zone on Cloudflare, used by the VPS cluster and the
health tunnel. It has nothing to do with Route53.

## Encryption at rest

Two independent layers, both live:

- **Kubernetes secretbox** — verified 2026-07-26 with a canary: the plaintext sentinel
  is absent from etcd and the `k8s:enc:secretbox:v1:` prefix is present.
- **Disk (LUKS2 + TPM2)** — since 2026-07-26 both pve3 NVMe partitions backing the Talos
  VM are LUKS2: the `vmdata` LVM (OS disk, etcd) and the user volume. They are
  auto-unlocked at host boot by the TPM via clevis (PCR 7, requires Secure Boot enabled).
  VM 100's disks point at `/dev/mapper/vmdata_crypt` LVs and `/dev/mapper/talos_ssd_crypt`.
  This covers etcd and every local-path PVC, including `health-dumps`, `influxdb-data`,
  `grafana-data` and `garmin-tokens` — which is why per-PVC encryption was recorded as
  superseded rather than skipped.

The jottacloud staging copy on the HDD pool is separately encrypted via rclone crypt
(`DEST_REMOTE` in the workload ConfigMap).

### Recovery: VMs don't autostart after a pve3 boot

That means the TPM refused to release the key — normally after a firmware or Secure Boot
change. On the Proxmox host:

1. `cryptsetup open` each volume using the recovery passphrases in 1Password
   (`op://Homelab/TPM/...`).
2. `vgchange -ay`
3. `systemctl start pve-guests`
4. Re-run `clevis luks bind` so the TPM can unlock unattended again.

## Migration notes (Phase 4)

Services were deployed fresh with empty PVCs; app-level backups were exported from the
old microk8s cluster through each service's own UI and imported into the new instance
the same way. No rsync-from-old-cluster data seeding was needed — simpler than the
original plan.

The old cluster's jottacloud-backup CronJob is suspended to avoid overlapping runs:

```bash
kubectl --context=microk8s -n jottacloud-backup patch cronjob jottacloud-backup-scheduled \
  -p '{"spec":{"suspend":true}}'
```

## Operational gotchas

Symptom-first; the rules these imply for *writing* manifests are in `AGENTS.md`.

**An NFS PV stays `Released` and the replacement PVC stays `Pending`.** NFS PVs use
reclaim policy `Retain` and keep their `claimRef` after the PVC is deleted, so they
won't auto-bind:

```bash
kubectl patch pv <name> --type=json -p='[{"op":"remove","path":"/spec/claimRef"}]'
```

**A pod is rejected with a PodSecurity violation.** The cluster enforces PSA `baseline`.
hostPath / hostNetwork workloads need their namespace elevated to `privileged` via
labels in `homelab/bootstrap/namespaces.yaml` (as `traefik` and `backup` already are).

**sabnzbd returns 403 "Access denied - Hostname verification failed".** Linuxserver's
sabnzbd image ships a `host_whitelist` that blocks unknown hostnames. Add the external
hostname to `/config/sabnzbd.ini` and restart the pod — edit in place rather than
spinning up a helper pod:

```bash
kubectl -n downloads exec deployment/sabnzbd -- \
  sh -c "sed -i 's/^host_whitelist =.*/host_whitelist = sab.cynexia.net/' /config/sabnzbd.ini"
kubectl -n downloads rollout restart deployment/sabnzbd
```

**Linuxserver (Alpine-based) containers can't resolve DNS.** Alpine's resolver
misbehaves under Kubernetes' default DNS policy, so every affected Deployment sets:

```yaml
dnsPolicy: None
dnsConfig:
  nameservers: ["8.8.8.8", "8.8.4.4"]
```

**An Ingress serves the wrong certificate, or none.** Traefik serves
`wildcard-cynexia-net-tls` as its default cert via the file-provider ConfigMap in
`homelab/bootstrap/traefik/traefik.yaml`. Ingresses therefore need **no** `tls:` block —
just the `host:` rule. Adding a per-Ingress `tls:` block reintroduces the
cross-namespace TLS secret replication problem this design avoids.

**keel silently downgraded a service to an ancient version.** The Deployment is missing
`keel.sh/match-tag: "true"`. Without it, keel's `force` policy watches the image name
across all tags, finds the newest digest, then rewrites the tag to whatever sits in the
image's `org.opencontainers.image.version` label — for linuxserver images that's the
upstream app version, which usually also exists as a years-old Docker Hub tag.

**`kubectl apply` reports `configured` for the same resources every run.** Expected and
benign — see [apply-workflow.md](apply-workflow.md#configured-is-not-drift).

**Cluster access problems** (`connection refused` on 127.0.0.1:8080, missing PGP key,
`node not found`) are in [omni-access.md](omni-access.md#troubleshooting).
