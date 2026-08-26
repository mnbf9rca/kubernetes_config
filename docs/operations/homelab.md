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
| keel | Image auto-updates from floating tags — **except** the `health`, `ops`, `hindsight` and `backup` namespaces, which forbid keel outright, and except keel itself, which is digest-pinned (see [keel](#keel) below) |
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

### keel

keel is digest-pinned and carries no keel annotations of its own. A self-updating
controller holding cluster-wide read **and write** across every workload kind — its
ClusterRole grants `get, delete, watch, list, update` on Deployments, DaemonSets,
StatefulSets, ReplicaSets, ReplicationControllers, Pods, Jobs and CronJobs — is the one
component where an unattended upstream tag change is a security event rather than a
convenience, so its bump belongs in a reviewed pull request rather than a six-hour poll.

Renovate does not watch `homelab/bootstrap/**` yet: `renovate.json` is scoped to
`homelab/health/**`, `homelab/ops/**` and `homelab/hindsight/**`, and
`scripts/check-renovate-scope.py` exempts `homelab/bootstrap` by name. So the pin is
unwatched today and is bumped **by hand**; widening that scope is what makes the pull
request arrive on its own.

Its RBAC was trimmed on August 26, 2026 (PR #68): no `secrets` rule, no
`pods/portforward`. Verify keel's permissions with a SelfSubjectAccessReview issued
with keel's own ServiceAccount token from inside the cluster — `kubectl auth can-i
--as=` is meaningless through the Omni proxy, which ignores impersonation and answers
as the caller.

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
| `ops` | Cluster-wide operational jobs | `update-watch` and `keel-fresh` CronJobs — see below |

Ingress hostnames are `*.cynexia.net` (Route53), Traefik-fronted, LAN/Tailscale only:
`sonarr`, `radarr`, `sab`, `hydra`, `emby`, `grafana-health`.

Retired in the rebuild: immich, ollama, open-webui, komga, jellyfin, mylar3,
lazylibrarian, caddy, postgresql, **tinyproxy**. cloudflared was retired from the
downloads-era stack but is not retired homelab-wide — the `health` namespace runs its own
dedicated `cynexia-health` tunnel, separate from the VPS cluster's `cynexia-vps` tunnel.

### The `ops` namespace

`homelab/ops/` holds work that belongs to the estate rather than to any one application. Today
that is two CronJobs, both dead-man's-switches over the update path itself:

- **`update-watch`**, at 06:45Z daily, makes a single unauthenticated GitHub call, counts the
  open Renovate pull requests on this repo, and drives the `homelab-update-watch`
  healthchecks.io check so a waiting update is visible instead of silent. Full behaviour, every
  cause of red, and the deliberate absence of a `/start` ping:
  [monitoring.md](monitoring.md#the-update-watcher).
- **`keel-fresh`**, at 07:15Z daily, makes one request to keel's own `/metrics` — a single
  ClusterIP endpoint, `keel.keel.svc.cluster.local:9300`, reached across the namespace boundary
  from `ops`; it scrapes nothing else and holds no cluster-wide read — and pushes the
  `homelab-keel-fresh` uptime-kuma monitor. It is the only thing that would notice keel's
  registry poll loop had wedged — keel's own probes hit `/healthz`, which stays green while the
  poll goroutine is dead. Verdict enum, the image floor and why there is no `/start`:
  [monitoring.md](monitoring.md#the-keel-dead-mans-switch).

The half-hour gap is deliberate: the two update-path checks should not alert in the same minute.
`keel-fresh` keeps two integers of state on a 32Mi `local-path` PVC, `keel-fresh-state` — the
previous run's process start time and poll counter — which is the only way to assert a counter
is *increasing*. It still needs no ServiceAccount and no RBAC; its only peers are a ClusterIP in
the `keel` namespace and `uptime.cynexia.com`.

Three things about this namespace are deliberate and should survive a refactor:

- **It is not `health` and not `backup`.** Its scope is the whole repo, so a health-namespace
  object alerting about other namespaces would misstate ownership; and `backup` runs at PSA
  privileged for restic's hostPaths, which an outbound-HTTPS poller has no business inheriting.
  `ops` is PSA baseline, the cluster default, and needs no ServiceAccount and no RBAC.
- **No keel here.** Every image is version-pinned and Renovate watches this tree, so neither
  job's own pin is the estate's one unwatched image. `make check-renovate-scope` fails the
  build if that scope is ever lost.
- **Removal is one commit,** but it is a longer list than it was with one job. Drop `- ops` from
  `homelab/kustomization.yaml`, `rm -r homelab/ops/`, remove the namespace block, remove **both**
  `OPS_HC_UPDATE_UUID` and `OPS_KUMA_KEEL_TOKEN` from `.env.tpl` and from both Makefile lists,
  and from `scripts/check-ping-bodies.py` remove **both** `REQUIRED_TARGETS` entries
  (`update-watch.py` and `keel-fresh.sh`), the eight `PY_VALUE_ALLOWLIST` names, and `PUSH_URL`
  from `DENY_VARS`. Then apply, `kubectl delete namespace ops` — which takes the
  `keel-fresh-state` PVC with it — and delete the `keel` **Service** in the `keel` namespace,
  which exists only to serve `keel-fresh` and is not removed by deleting the `ops` namespace.
  Finally retire both instruments: the `homelab-update-watch` healthchecks.io check *and* the
  `homelab-keel-fresh` uptime-kuma push monitor, then delete both 1Password items.

  Removing only `keel-fresh` and keeping `update-watch` is the same list minus the
  `OPS_HC_UPDATE_UUID`, `update-watch.py` and `PY_VALUE_ALLOWLIST` items, and without deleting
  the namespace.

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
  hermes-agent and hermes-webui checkouts, `~/workspace`) stays rebuild territory:
  rebuild the VM, reinstall hermes, then restore state with `hermes import`. The
  restore runbook, the wrapper script, and the key-rotation procedure are in [Hermes VM
  backup and restore](#hermes-vm-backup-and-restore); monitoring semantics are in
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

### Hermes VM configuration layout and secrets

The rules that hold for **every** Hermes plugin and profile on VM 103, learned the
hard way while wiring hindsight (2026-08-24) and binding on any future integration:

- **Plugin config resolves per `HERMES_HOME`, which makes it per PROFILE.** A profile is
  a separate Hermes home directory, so profile `<name>` reads
  `~/.hermes/profiles/<name>/<plugin>/config.json`. The path `~/.hermes/<plugin>/config.json`
  is not shared: it belongs to the *default* profile, whose home is `~/.hermes` itself.
  Each plugin loads the first source that exists — the profile's file, then a legacy
  shared path, then environment defaults — and **the whole file wins with no merging**,
  so malformed JSON presents as a config that reverted to defaults. Some keys accept an
  environment variable as a per-key fallback, but only when the key is absent from the
  file: **the file value wins over the environment.** Onboarding a profile therefore
  means copying the config file, not relying on a shared one.
- **Secrets are per profile and MUST go through hermes's 1Password integration** —
  `hermes -p <profile> secrets onepassword set VAR "op://Vault/item/field"` — never a
  plain value in `.env` or `config.yaml`. hermes stores its own `op://` reference in
  the profile's `config.yaml` and resolves it at start ("1Password: applied N
  secrets"), so no secret value rests on disk, and the nightly backup zip carries
  references rather than values for these.
- **Do not trust the dashboard GUI as the writer of record — it fails in both
  directions.** It silently drops some writes: at least one field (the hindsight API
  server URL) reports saved and is not. It also silently persists too much, writing an
  API key into the plugin's `config.json` as **cleartext**, which defeats the 1Password
  integration and puts a live credential in the nightly backup zip. After any GUI
  session, read the config file back and delete any secret value you find there. Because
  the file wins over the environment, a stored key shadows the 1Password-backed variable
  until it is removed. To change a value reliably, edit the file and restart the affected
  gateway (`systemctl --user restart hermes-gateway-<profile>`).
- **Never disable a toolset that shares its name with a provider plugin's feature.**
  `hermes tools disable memory` is the live case: `memory` names both a built-in tool and
  a toolset, and the toolset gates every memory provider's tools as well. Disabling it
  removes the provider's tools with no warning. Suppress the built-in tool through the
  profile's `config.yaml` instead (`memory: {memory_enabled: false,
  user_profile_enabled: false}`) and leave the toolset enabled per platform —
  `--platform` defaults to `cli`, and gateway platforms are separate keys, so check
  `hermes -p <profile> tools --summary`.
- **A tool listing is not evidence that a plugin's tools are absent.** `/tools` and
  `hermes tools list` render from the static registry, which knows nothing about provider
  plugins; provider tools are appended to the agent afterwards and never appear in either
  listing. Verify a plugin tool by asking the agent to call it, never by listing.

Service-specific wiring lives in that service's runbook (for example
`docs/operations/hindsight.md`); this section is the layout contract they share.

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

Three pieces of state live on the VM itself and are not managed by this repo. This
section holds their canonical copies — if you change any of them on the VM, change it
here in the same sitting.

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

The third piece is the WebUI service unit,
`/home/hermes/.config/systemd/user/hermes-webui.service`. Its rationale is in [Hermes
WebUI on the VM](#hermes-webui-on-the-vm) below; the file itself:

```ini
[Unit]
Description=Hermes WebUI - browser/mobile client for the Hermes agent
Documentation=https://github.com/nesquena/hermes-webui
After=network-online.target
Wants=network-online.target

# Deliberately NOT StartLimitIntervalSec=0 (which the hermes-gateway units set).
# A gateway should retry through long upstream outages; this service's
# dependencies are all local, so a start that keeps failing is a bug, not
# weather. The window is widened from the 10s default because RestartSec=5
# spaces retries 5s apart - with a 10s window only two starts ever land inside
# it and the limiter would never trip, leaving the unit looping in 'activating'
# forever where nothing can see it. 5 starts / 60s trips after ~25s and parks
# the unit in 'failed', which 'systemctl --user is-failed' reports.
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple

# Refuse to start on a blank password. server.py:597-603 only PRINTS a warning
# when a non-loopback bind has no password, then serves every path with no auth
# at all (api/auth.py:423 falls back to settings.json; api/auth.py:1078
# short-circuits check_auth when auth is disabled). EnvironmentFile without a
# leading '-' catches only a MISSING file; this catches the likelier failures -
# an empty value, a whitespace value, or a misspelt key. '$$' stops systemd
# expanding the value, so the password never reaches sh's argv.
ExecStartPre=/bin/sh -c 'case "$${HERMES_WEBUI_PASSWORD}" in *[![:space:]]*) exit 0 ;; *) echo "hermes-webui: HERMES_WEBUI_PASSWORD is empty, blank or misspelt in /home/hermes/.hermes/webui.env - refusing to start unauthenticated" >&2 ; exit 1 ;; esac'

ExecStart=/home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/hermes-webui/server.py
WorkingDirectory=/home/hermes/hermes-webui

Environment="PATH=/home/hermes/.hermes/hermes-agent/venv/bin:/home/hermes/.hermes/hermes-agent/node_modules/.bin:/home/hermes/.hermes/node/bin:/home/hermes/.hermes/node:/home/hermes/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="VIRTUAL_ENV=/home/hermes/.hermes/hermes-agent/venv"
Environment="HERMES_HOME=/home/hermes/.hermes"
Environment="HERMES_WEBUI_AGENT_DIR=/home/hermes/.hermes/hermes-agent"
Environment="HERMES_WEBUI_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python"
Environment="HERMES_WEBUI_HOST=0.0.0.0"
Environment="HERMES_WEBUI_PORT=8787"
Environment="HERMES_WEBUI_SECURE=1"
Environment="HERMES_WEBUI_ALLOWED_ORIGINS=https://hermes-app.cynexia.com"
# 30 days (upstream's default), by operator decision 2026-08-25: every request
# to this hostname must carry the Access service token regardless, so the webui
# session is the second wall, and a daily password re-prompt on a phone buys
# nothing. If the service-token gate is ever weakened, shorten this again.
Environment="HERMES_WEBUI_SESSION_TTL=2592000"
# Pinned explicitly: the default would CREATE ~/workspace at first start. This
# path is OUTSIDE ~/.hermes, so agent-authored files here are rebuild territory
# and are NOT in the nightly backup zip. Deliberate - see docs/operations/homelab.md.
Environment="HERMES_WEBUI_DEFAULT_WORKSPACE=/home/hermes/workspace"

# No leading '-': the unit must refuse to start when the file is absent.
EnvironmentFile=/home/hermes/.hermes/webui.env

Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Its `EnvironmentFile`, `/home/hermes/.hermes/webui.env`, holds one key and is mode
`0600`. Only the key name is recorded here:

```
HERMES_WEBUI_PASSWORD=<the WebUI password>
```

That file is inside `~/.hermes`, so unlike the unit it **is** in the nightly zip and
survives a rebuild the same way `.env` and `auth.json` do.

### Hermes WebUI on the VM

`hermes-webui` ([github.com/nesquena/hermes-webui](https://github.com/nesquena/hermes-webui))
runs on port 8787 as the `hermes-webui` systemd user unit, published at
`https://hermes-app.cynexia.com` through the `cynexia-health` tunnel
([homelab-health.md](homelab-health.md#ingress)).

**One instance serves every profile.** It reads `~/.hermes/profiles/<name>` off disk and
switches per client on a `hermes_profile` cookie, so `emh`, `hal` and the default profile
share one process and clients get an in-app switcher. There is no second unit and no
second port. It is a third, distinct service alongside the dashboard on 9119 and the
gateways on 8642 — none of the three replaces another.

**Who uses it.** The Hermex iOS app
([github.com/uzairansaruzi/hermex](https://github.com/uzairansaruzi/hermex)). The app
forces https, appends no port and no path, and probes `GET /health` then
`GET /api/auth/status` before login, which is why the hostname has to serve the WebUI at
the root on 443. It streams over SSE, never websockets.

**It runs out of the agent venv.** `server.py` needs only `pyyaml` and `cryptography`, but
everything it calls into (`openai`, `httpx`, the agent itself) lives in
`/home/hermes/.hermes/hermes-agent/venv`, which system python does not have. The unit runs
`server.py` directly under an explicit environment rather than `bootstrap.py` or
`start.sh`: both of those load `REPO_ROOT/.env` and auto-discover paths, which would make
what the service runs depend on files systemd cannot see.

**Do not give it a venv of its own.** The cohabitation looks like an accident worth
tidying up, and it is not. `api/agent_runtime.py` imports `run_agent.AIAgent` into the
server process, and `api/streaming.py` instantiates that class for every chat turn, so the
WebUI needs hermes-agent's whole dependency tree rather than the two packages its own
`requirements.txt` names. Splitting the venvs fails, and it fails silently. Measured on
2026-08-25: a `/home/hermes/hermes-webui/venv` built from system python 3.13 with
`requirements.txt` installed returns `None` from `get_ai_agent_class()`, because
`run_agent` imports `dotenv`, `httpx` and `openai` and that venv holds none of them. The
unit still reaches `active`. `/health` still returns `status: ok` and `/api/auth/status`
still reports `password_auth_enabled: true`. Every check in this document passes while the
iOS app answers every message with `AIAgent not available`. Upstream reaches the same
conclusion in `bootstrap.py`, where `ensure_python_has_webui_deps` prefers the agent venv
and creates a local `.venv` only when no agent venv can run both.

#### Update — tracks upstream, not pinned

Run alongside the weekly hermes-agent update. This is a manual runbook; there is no timer
(see [monitoring.md](monitoring.md#what-this-does-not-catch)).

```sh
git -C /home/hermes/hermes-webui pull --ff-only
# Install under a constraint of what the venv already has. This venv is what
# hermes-gateway, hermes-gateway-emh, hermes-gateway-hal and hermes-dashboard all
# execute from; without -c, an upstream requirements.txt that raises a floor past one
# of hermes-agent's pyproject pins would silently mutate their runtime and pip would
# report success. With it, pip fails loudly and a human decides.
V=/home/hermes/.hermes/hermes-agent/venv/bin
"$V/pip" freeze --local | grep -E '^[A-Za-z0-9._-]+==' > /tmp/webui-constraints.txt
"$V/pip" install -q -r /home/hermes/hermes-webui/requirements.txt -c /tmp/webui-constraints.txt
rm -f /tmp/webui-constraints.txt
systemctl --user restart hermes-webui
sleep 5
curl -fsS http://127.0.0.1:8787/health
# The venv is shared: prove the agent still imports and its four units still run.
"$V/python" -c 'import hermes_cli.main'
systemctl --user is-active hermes-gateway hermes-gateway-emh hermes-gateway-hal hermes-dashboard
# Only once all of the above passed: record this revision as the local known-good.
git -C /home/hermes/hermes-webui rev-parse HEAD > /home/hermes/.hermes/webui.last-good
```

The `pip` line is not optional and the constraint file is not decoration. `pyyaml` and
`cryptography` are already hermes-agent dependencies (`pyproject.toml` pins `pyyaml==6.0.3`
and `cryptography==50.0.0`), so today the install is a no-op — the risk is not that the
deps go missing, it is that an unpinned weekly install eventually moves one of them under
four production services.

#### Rollback when an update breaks the app

Roll back to the revision that last worked **here**, which the update runbook records:

```sh
SHA=$(cat /home/hermes/.hermes/webui.last-good)
git -C /home/hermes/hermes-webui checkout "$SHA"   # detached HEAD, expected
systemctl --user restart hermes-webui
```

Second resort, if `webui.last-good` is missing or is itself the broken revision: the
Hermex repo publishes the upstream commit the app was last validated against as
`UPSTREAM_TESTED_SHA`. Note the branch is `master`, not `main`, and validate the value
before it reaches `git` — a 404 returns an HTML error page, and `git checkout ""` on an
empty variable fails with a confusing pathspec error:

```sh
SHA=$(curl -fsS https://raw.githubusercontent.com/uzairansaruzi/hermex/master/UPSTREAM_TESTED_SHA | tr -d '[:space:]')
case "$SHA" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
  *) echo "refusing: UPSTREAM_TESTED_SHA did not look like a SHA" >&2; exit 1 ;;
esac
git -C /home/hermes/hermes-webui fetch origin
git -C /home/hermes/hermes-webui checkout "$SHA"
```

Prefer `webui.last-good`. `UPSTREAM_TESTED_SHA` advances only when the app's own contract
tests are re-run, so it drifts: on 2026-08-24 it pointed at a commit from 2026-05-17 while
upstream `master` was three months further on. Rolling back that far means running May
code against an August `~/.hermes/webui` state directory and an agent that has moved
underneath it, and upstream documents no backwards state compatibility.

Return to tracking with `git checkout master && git pull --ff-only`. A rollback is a
signal to open a Hermex issue, not a new steady state.

#### What is and is not backed up

- **Backed up** (inside `~/.hermes`, so in the nightly zip): `~/.hermes/webui` — sessions,
  `settings.json`, the session DB — and `~/.hermes/webui.env`, the password.
- **Not backed up, deliberately**: the checkout at `/home/hermes/hermes-webui` and the
  workspace at `/home/hermes/workspace`. The checkout is rebuild territory for the same
  reason `hermes-agent` is; it would otherwise add about 70 MB to a zip that already
  measures 203 MB compressed and is pulled over SSH nightly.
  `HERMES_WEBUI_DEFAULT_WORKSPACE` is pinned in the unit precisely so this is a decision
  rather than a default — left unset, the server **creates** `~/workspace` at first start
  and quietly accumulates every file an app session writes, outside the backup and with
  nothing bounding its size. **Treat `/home/hermes/workspace` as expendable**: anything
  worth keeping goes into a git remote or into the profile.

#### Rebuild step

A VM rebuild does not restore this service. After `hermes import` (step 6 of the restore
runbook), re-clone and re-create the unit:

```sh
git clone https://github.com/nesquena/hermes-webui.git /home/hermes/hermes-webui
V=/home/hermes/.hermes/hermes-agent/venv/bin
"$V/pip" freeze --local | grep -E '^[A-Za-z0-9._-]+==' > /tmp/webui-constraints.txt
"$V/pip" install -q -r /home/hermes/hermes-webui/requirements.txt -c /tmp/webui-constraints.txt
# unit file: copy from this document
systemctl --user daemon-reload
systemctl --user enable --now hermes-webui
```

`webui.env` comes back with the restored `~/.hermes`, so no password step is needed on a
restore — only on a first install.

#### Security posture — read before "fixing" any of it

- **`HERMES_WEBUI_SECURE=1` is a cookie flag, not an access control.** It forces `Secure`
  on the session cookie because cloudflared speaks plain HTTP to the origin and the cookie
  would otherwise lose the flag. The alternative,
  `HERMES_WEBUI_TRUST_FORWARDED_PROTO=1`, means trusting a header any LAN client can forge
  against a `0.0.0.0` bind; forcing the flag needs no trust at all.
  `HERMES_WEBUI_TRUST_FORWARDED_HOST` and `_FOR` stay off for the same reason. The visible
  cost is that a browser cannot log in over plain `http://hermes.cynexia.net:8787` — use
  the tunnel hostname. Do not "fix" this.
- **The LAN is a trusted zone here, and the WebUI password is the only control on it.**
  `Secure` is honoured by browsers only: any script can `POST` to
  `http://hermes.cynexia.net:8787/api/auth/login` over plain HTTP, take the `Set-Cookie`
  value, and replay it, entirely outside Cloudflare Access. The VM has no host firewall
  (`ufw` is not installed) and the dashboard and gateways already bind `0.0.0.0`, so this
  is the VM's established posture rather than something this service introduced — but it
  is the reason the blank-password guard in the unit exists, and the reason no IP-bypass
  policy was carried onto the Access app. Firewalling 8787, 9119 and 8642 down to the
  cluster egress address is the obvious hardening, and is a change of its own.
- **The WebUI password is readable by the agent it protects.** It arrives as a plaintext
  environment variable, and the WebUI builds child processes with an unfiltered
  `os.environ.copy()` (`api/routes.py:1308`, `api/gateway_restart.py:99`,
  `api/workspace_git.py:113`) with no scrubbing. Any session — including one steered by
  prompt injection through fetched content — can read `$HERMES_WEBUI_PASSWORD`. **Treat
  the Cloudflare Access service token as the only gate an agent cannot forge.** The
  hardening path is to set the password through the WebUI's own Settings page, which
  writes a `password_hash` into `settings.json`, and then drop the env var; the env var is
  what makes the *first* start safe on a `0.0.0.0` bind, so it cannot simply be omitted.
- **Not every path is behind the password.** `/share`, `/share/*`, `/api/share/*`,
  `/static/*`, the manifests and the auth endpoints are in the WebUI's public set, and
  `GET /api/share/<token>` returns a full shared conversation on token possession alone.
  Under a Cloudflare Access *bypass* policy those paths would have no gate whatsoever,
  which is why the Access app authenticates every request instead.
- **The Access service token is shared with `hermes.cynexia.com`.** The same token
  (`468dc6d0-d0d6-4b98-8c07-4d002fda2df1`, value in 1Password) sits on the phone and opens
  the agent dashboard as well as the WebUI. A lost or compromised device therefore yields
  both hostnames, and the response — rotating the token — also breaks the uptime-kuma
  monitors that carry it. Plan on rotating the token and updating those monitors in the
  same sitting.

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
   systemctl --user stop hermes-dashboard hermes-gateway hermes-gateway-emh \
     hermes-gateway-hal hermes-webui
   ```

4. Import and verify (`~/.local/bin` is not on the non-login PATH):

   ```
   ~/.local/bin/hermes import ~/hermes-restore.zip
   ~/.local/bin/hermes config check
   ```

5. Restart the five services and confirm the dashboard at hermes.cynexia.com.
6. On a fresh VM rebuild: install hermes first, then run steps 2–5, then
   `hermes setup`. `hermes-webui` is a separate install that the zip does not carry —
   re-clone it and re-create its unit from [Hermes WebUI on the
   VM](#rebuild-step) before step 5 restarts it.
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
