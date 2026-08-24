# Homelab cluster reference

Single-node Talos Linux VM on Proxmox (`pve3`), managed by Omni, serving the media /
downloads stack and the health-data pipeline. Not exposed to the public internet —
remote access is through Tailscale. Kubectl context: `cynexia-homelab`.

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
| `health` | Personal health data pipeline | influxdb, apple-health-ingester, garmin-grafana, grafana, influxdb-mcp (behind Cloudflare Access), cloudflared, backup + freshness CronJobs — see [homelab-health.md](homelab-health.md) |

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

Applications' own scheduled backups (sonarr, radarr, emby, sabnzbd) must write zips to
`/config/Backups/` so restic catches them. The sqlite-quiesce sidecar pattern from
earlier drafts of the plan is **not** used here — it's redundant when the app's own zip
backup already handles DB consistency. (Both restic jobs carry a backup verification
gate, but with different shapes: the VPS gate checks its quiesce sidecars' snapshots,
while the homelab gate checks mount identity, tree scale, an expected-artifact list and
the freshness of the influx dumps and the hermes zip — see
[monitoring.md](monitoring.md#the-backup-verification-gates).)

Until 2026-08 neither restic CronJob reported anywhere and neither had a runtime ceiling,
so a hung run would have blocked every following night silently and been discovered at
restore time. Both now ping healthchecks.io and carry `activeDeadlineSeconds` —
[monitoring.md](monitoring.md#scheduled-work).

## Node network

| Interface | IP | Purpose |
|---|---|---|
| `ens18` (LAN) | `10.100.0.100` | All `*.cynexia.net` A records point here |
| `ens19` (storage) | `10.10.10.10` | NFS traffic to `10.10.10.1`; Kubernetes misleadingly reports this as `InternalIP` |
| `tailscale0` | `100.85.18.48` | Remote access through the Tailscale mesh |

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
  auto-unlocked at host boot by the TPM through clevis (PCR 7, requires Secure Boot enabled).
  VM 100's disks point at `/dev/mapper/vmdata_crypt` LVs and `/dev/mapper/talos_ssd_crypt`.
  This covers etcd and every local-path PVC, including `health-dumps`, `influxdb-data`,
  `grafana-data` and `garmin-tokens` — which is why per-PVC encryption was recorded as
  superseded rather than skipped.

pve3 specifics that follow from that design:

- **Do not disable Secure Boot casually** — PCR 7 changes and both LUKS volumes refuse
  TPM unlock (designed anti-theft behaviour, proven in a live negative test). Routine
  BIOS updates survive because only PCR 7 is sealed against.
- **A new VM is encrypted iff its disks land on the `vmdata` storage** (the VG lives
  inside the LUKS mapper). Disks on `local` (the Proxmox root, ext4 on sda3) are NOT
  encrypted; the Proxmox root itself is deliberately unencrypted.
- Swap is `swap_crypt`, plain dm-crypt with a random `/dev/urandom` key per boot.
- **The hermes VM (103) has a nightly application-state backup.** The `hermes-pull`
  CronJob in the `backup` namespace SSHes to the VM at 02:00 UTC with a restricted
  forced-command key, runs `hermes backup`, and pulls the zip onto the `hermes-dumps`
  PVC; the 03:00 restic sweep carries it to B2. The zip covers `~/.hermes` only —
  config, credentials, `state.db`, sessions, profiles, skills, memories. Everything
  else on the VM (OS, systemd user units, tunnel config, `~/.local/bin` wrappers, the
  hermes-agent checkout) stays rebuild territory: rebuild the VM, reinstall hermes,
  then restore state with `hermes import`. The restore runbook, the wrapper script,
  and the key-rotation procedure are in [Hermes VM backup and
  restore](#hermes-vm-backup-and-restore); monitoring semantics are in
  [monitoring.md](monitoring.md#healthchecksio-checks).

The jottacloud staging copy on the HDD pool is separately encrypted with rclone crypt
(`DEST_REMOTE` in the workload ConfigMap). Its passphrase (`JOTTA_CRYPT_PASSWORD` in
1Password) is frozen by design: rclone crypt cannot rekey in place, so rotation is the
re-encrypt procedure documented in the `mnbf9rca/jottacloud-backup` image README.

### Recovery: VMs don't autostart after a pve3 boot

That means the TPM refused to release the key — normally after a firmware or Secure Boot
change. On the Proxmox host:

1. `cryptsetup open` each volume using the recovery passphrases in 1Password
   (`op://Homelab/TPM/...`).
2. `vgchange -ay`
3. `systemctl start pve-guests`
4. Re-run `clevis luks bind` so the TPM can unlock unattended again.

### Hermes VM backup and restore

The `hermes-pull` CronJob (`homelab/backup/hermes-pull.yaml`) pulls a full
`hermes backup` zip from the hermes VM every night at 02:00 UTC onto the `hermes-dumps`
PVC, where the 03:00 restic sweep replicates it to B2. The zip contains the VM's live
secrets in plaintext (`.env`, `auth.json`, MCP OAuth tokens), so its only permitted
resting places are that PVC and the restic repository. Delete any operator copy as soon
as a procedure finishes with it.

#### What `hermes backup` captures

Facts verified on the VM and against the upstream docs
(https://hermes-agent.nousresearch.com/docs/ — an `llms.txt` index and a full markdown
mirror exist; fetch those rather than searching):

- The zip covers the entire `~/.hermes` home: `config.yaml`, `.env`, `auth.json`,
  `mcp-tokens/`, `state.db`, sessions, memories, skills, plugins, cron, both profiles,
  and the bundled node runtime. It excludes the `hermes-agent/` checkout, `backups/`
  itself (which is why the wrapper stages there — the staged copy can never recurse
  into the next night's zip), `state-snapshots/`, checkpoints, and SQLite sidecar
  files. Measured 2026-08-24: 203 MB compressed from 572 MB, about 23 seconds.
- It is safe against live services: `state.db` is copied with SQLite's `backup()` API,
  so the four systemd user units keep running. Only **restore** requires stopping them.
- **Never pass `-q`/`--quick`**: it snapshots only `config.yaml`, `state.db`, `.env`,
  auth and cron — it drops profiles, skills and `mcp-tokens/`, which are exactly the
  irreplaceable set. The pull job's 100 MiB size floor exists to catch a quick-shaped
  zip if this ever regresses.
- `hermes profile export` is **not** a backup: it strips API keys by design, and
  session history stays in `state.db` regardless.
- Upstream provides no scheduling, retention, or encryption for these backups; the zip
  holds the VM's secrets in plaintext, which is why its only permitted resting places
  are the PVC and restic (both encrypted at rest).
- Concurrent backups fail fast, never block: the wrapper's own flock exits 75, and a
  second `hermes backup` against hermes's internal `.backup.lock` exits 2 with
  "another Hermes backup is already running". A hung pull therefore always means the
  network or the VM, not lock queueing.

Two pieces of state live on the VM itself and are not managed by this repo. This
section holds their canonical copies — if you change either on the VM, change it here
in the same sitting.

The forced-command wrapper, `/home/hermes/bin/hermes-pull-wrapper.sh`, mode 0755:

```sh
#!/bin/sh
# Forced-command wrapper for the k8s hermes-pull backup key.
# The key in authorized_keys can run ONLY this script. It supports two verbs,
# selected by the client's requested command (SSH_ORIGINAL_COMMAND):
#   backup  - run `hermes backup` into a staging file, then stream it on stdout
#   sum     - print the SHA-256 of the last staged zip
# Everything hermes prints (including the "1Password: applied 6 secrets"
# banner) is forced onto stderr so stdout carries zip bytes and nothing else.
set -eu

STAGE_DIR=/home/hermes/.hermes/backups
STAGE=$STAGE_DIR/k8s-pull.zip
PART=$STAGE_DIR/k8s-pull.partial.zip
LOCK=$STAGE_DIR/.k8s-pull.lock

verb=${SSH_ORIGINAL_COMMAND:-backup}
case "$verb" in
  backup)
    # One pull at a time. -n fails fast rather than queueing: the cluster
    # side has concurrencyPolicy Forbid, so a second concurrent call is
    # always a fault, never a schedule.
    exec 9> "$LOCK"
    flock -n 9 || { echo "hermes-pull: another pull is already running" >&2; exit 75; }
    rm -f "$PART"
    /home/hermes/.local/bin/hermes backup -o "$PART" 1>&2
    # CRC-test every member before publishing: catches a zip that hermes
    # itself produced corrupt. python3 is on the VM; unzip is not.
    python3 -m zipfile -t "$PART" 1>&2
    mv -f "$PART" "$STAGE"
    cat "$STAGE"
    ;;
  sum)
    sha256sum "$STAGE" | awk '{print $1}'
    ;;
  *)
    echo "hermes-pull: unknown verb: refusing" >&2
    exit 64
    ;;
esac
```

The restricted line in `/home/hermes/.ssh/authorized_keys` (the operator's own key line
is separate and unaffected):

```
restrict,command="/home/hermes/bin/hermes-pull-wrapper.sh" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICrRo/NPnEnUk3mUY9SmdnENyzXqlo1LFoq3OHoSWZ79 k8s-hermes-pull
```

The public key is `op://Homelab/hermes-ssh-key/public key` (fingerprint
`SHA256:YuIGosID1a0pwDM1IwBNuax5kHSSJRhwT3TQbp1xzEA`). `restrict` disables PTY
allocation, all forwarding, `~/.ssh/rc`, and every channel type OpenSSH adds in future
releases — fail closed, nothing re-enabled.

#### Restore

Restoring replaces `~/.hermes` application state on a working hermes install. Rebuild
the OS; restore the state.

1. Get the newest zip. Same-day, from the PVC (`kubectl cp` needs a running container,
   so the helper pod sleeps):

   ```
   kubectl -n backup run hermes-restore --restart=Never --image=alpine/k8s:1.36.0 \
     --overrides='{"spec":{"volumes":[{"name":"dumps","persistentVolumeClaim":{"claimName":"hermes-dumps"}}],"containers":[{"name":"hermes-restore","image":"alpine/k8s:1.36.0","command":["sleep","3600"],"volumeMounts":[{"name":"dumps","mountPath":"/dumps"}]}]}}'
   kubectl -n backup exec hermes-restore -- sh -c 'ls -1t /dumps/hermes-*.zip | head -n1'
   kubectl cp backup/hermes-restore:/dumps/<that file> ./hermes-restore.zip
   kubectl -n backup delete pod hermes-restore
   ```

   Older, from B2 (any machine with restic; run under `op run --env-file=.env.tpl --`):

   ```
   restic snapshots --tag nightly
   restic restore <snapshot-id> \
     --include '/data/pvc-*_backup_hermes-dumps/hermes-*.zip' \
     --target ./restore
   ```

2. Copy the zip to the VM: `scp ./hermes-restore.zip hermes@hermes.cynexia.net:`
3. On the VM, stop the services first (the hermes docs require it for import):

   ```
   systemctl --user stop hermes-dashboard hermes-gateway hermes-gateway-emh hermes-gateway-hal
   ```

4. Import and verify (`~/.local/bin` is not on the non-login PATH):

   ```
   ~/.local/bin/hermes import ~/hermes-restore.zip
   ~/.local/bin/hermes config check
   ```

5. Restart the four services and confirm the dashboard at hermes.cynexia.com.
6. On a fresh VM rebuild: install hermes first, then run steps 2–5, then
   `hermes setup`.
7. Delete every operator-side copy: `./hermes-restore.zip`, the `./restore/` tree if
   step 1 used B2, and `~/hermes-restore.zip` on the VM.

**Cross-version caveat:** upstream does not document restore behavior across hermes
versions. Restore onto the same or a newer version, never an older one. After any
version gap, run `hermes config check` and, if it complains, `hermes migrate` before
starting the services. If migration fails, install the version that took the backup
(git install, so any tag is reachable), import there, then upgrade in place.

#### Key rotation

- **Client key**: generate a new ed25519 keypair into `op://Homelab/hermes-ssh-key`,
  append the new public key as a second restricted `authorized_keys` line (same
  options), run `make create-hermes-ssh-secret`, confirm the next nightly run goes
  green, then delete the old line. Two lines during the overlap means rotation never
  risks a missed night.
- **Host key**: changes only when the VM's sshd is reinstalled or the VM is rebuilt.
  The pull then fails closed on the mismatch — desired. From a trusted network, run
  `ssh-keyscan -t ed25519 hermes.cynexia.net`, update the `hermes-known-hosts`
  ConfigMap in `homelab/backup/hermes-pull.yaml`, and `make apply-homelab`. Host
  public keys are tier-3 identifiers; committing them is fine.

## Migration notes (Phase 4)

Services were deployed fresh with empty PVCs; app-level backups were exported from the
old microk8s cluster through each service's own UI and imported into the new instance
the same way. No rsync-from-old-cluster data seeding was needed — simpler than the
original plan.

The old microk8s cluster was **decommissioned 2026-07-26** — no overlap concerns
remain, and any lingering `--context=microk8s` instructions are obsolete.

## Operational gotchas

Symptom-first; the rules these imply for *writing* manifests are in `AGENTS.md`.

**An NFS PV stays `Released` and the replacement PVC stays `Pending`.** NFS PVs use
reclaim policy `Retain` and keep their `claimRef` after the PVC is deleted, so they
won't auto-bind:

```bash
kubectl patch pv <name> --type=json -p='[{"op":"remove","path":"/spec/claimRef"}]'
```

**A pod is rejected with a PodSecurity violation.** The cluster enforces PSA `baseline`.
hostPath / hostNetwork workloads need their namespace elevated to `privileged` through
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
`wildcard-cynexia-net-tls` as its default cert through the file-provider ConfigMap in
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
