# Homelab cluster reference

Single-node Talos Linux VM on Proxmox (`pve3`), managed by Omni, serving the media / downloads stack and the health-data pipeline.
Private `*.cynexia.net` apps use Tailscale; selected `*.cynexia.com` origins use the shared Cloudflare tunnel.
Kubectl context: `cynexia-homelab`.

## Platform stack

| Component | Notes |
|---|---|
| Talos Linux | Single-node VM on Proxmox, managed by Omni (cluster name `homelab`) |
| Tailscale | Talos system extension on the host: subnet router + remote `talosctl`/`kubectl` |
| Traefik | hostNetwork DaemonSet on :80/:443. No MetalLB. |
| cert-manager | Let's Encrypt, Route53 DNS-01 solver, single wildcard `*.cynexia.net` cert |
| local-path-provisioner | Backed by the node's SSD user volume (`/var/mnt/ssd`) |
| NFS CSI driver | Static PV/PVCs against the Proxmox host's ZFS pool |
| keel | Floating image updates, including keel itself; see [keel](#keel) below |
| restic | Nightly CronJob (03:00 UTC) → Backblaze B2 `b2:homelab-restic-d5e15f22`, 7 daily / 4 weekly / 6 monthly. Pings healthchecks.io on start and exit code — see [monitoring.md](monitoring.md#the-restic-ping-wrapper) |
| jottacloud-backup | Own namespace; rclone Jottacloud → NFS, then kopia → B2 `cloud-files-backup`; reports to the `jottacloud-backup` uptime-kuma push monitor |

Workloads run on the single node because Omni's own system patch `400-homelab-control-planes-untaint` sets `cluster.allowSchedulingOnControlPlanes: true` — the mirror of the VPS twin described in [vps.md](vps.md), labelled `omni.sidero.dev/system-patch:`, so Omni owns it and it must never be copied into a file under `homelab/talos/`.

PSA: the cluster enforces `baseline` by default.
`traefik` (hostNetwork/hostPort), `backup` (hostPath), and `netalertx` (NET_ADMIN) are elevated to `privileged` by labels in `homelab/bootstrap/namespaces.yaml`; check that file for the current list.
Any new workload requiring capabilities beyond PSA baseline needs the same treatment.

`cert-manager` and `local-path-storage` namespaces are created by their upstream manifests, so they are deliberately absent from `namespaces.yaml` (kustomize rejects duplicates).
**Those three upstream bases stay unwatched, and the 2026-08-26 Renovate widening did not change that.**
`cert-manager`, `nfs-csi` and `local-path` are pulled in as raw GitHub URLs with the version in the *path*, which the `kustomize` manager does not parse; it reads `?ref=` and `images:` transformers, and this tree has neither.
That manager's only footprint in the repo is the VPS's `vps/bootstrap/local-path/kustomization.yaml`, pinned `?ref=v0.0.37`.
Bumping a homelab base is still a hand edit of the URL.
Update these three bundles during the Kubernetes step of `/update-estate`.
`keel`'s namespace **is** declared there, because upstream keel moved to Helm-only distribution and `homelab/bootstrap/keel/keel.yaml` is hand-written.

### keel

The manifests select `ghcr.io/keel-hq/keel:latest` for self-updates and `traefik:v3` for Traefik.
Keel can rewrite `traefik:v3` as `library/traefik:v3`; a later apply restores the equivalent name and rolls Traefik, so this diff alone does not prove missing deployed branch content.
Both controllers carry the full keel annotation set: `force`, `match-tag: "true"`, `poll`, and `@every 6h`.
Every container moved to a floating image has `imagePullPolicy: Always`.
Jobs and CronJobs pull their floating tags at each run without keel annotations.

The accepted recovery point for application data is the nightly dump or application snapshot captured by restic.
An automatic update can require restoration and lose changes since that recovery point.
The existing restore procedures remain the recovery path.

`scripts/check-renovate-scope.py` checks floating controller annotations and Renovate coverage for remaining pins.
The `alpine/k8s` backup runtime stays pinned because it has no suitable floating tag.
Renovate excludes floating runtime images from digest pinning.

Its RBAC was trimmed on August 26, 2026 (PR #68): no `secrets` rule, no `pods/portforward`.
Verify keel's permissions with a SelfSubjectAccessReview issued with keel's own ServiceAccount token from inside the cluster — `kubectl auth can-i --as=` is meaningless through the Omni proxy, which ignores impersonation and answers as the caller.

## Namespaces and workloads

| Namespace | Purpose | Services |
|---|---|---|
| `downloads` | Media management | sonarr, radarr, sabnzbd, hydra2, emby |
| `jottacloud-backup` | Cloud backup | jottacloud-backup CronJob (own namespace) |
| `cert-manager` | TLS | cert-manager controller |
| `traefik` | Ingress | Traefik DaemonSet (PSA privileged — hostNetwork) |
| `keel` | Auto-updates | keel controller |
| `backup` | Backup | restic init Job + nightly CronJob (PSA privileged — hostPath) |
| `health` | Personal health data pipeline | influxdb, apple-health-ingester, garmin-grafana, influxdb-mcp (behind Cloudflare Access), pdc-agent (the Grafana Cloud Private Datasource Connect tunnel), backup + freshness CronJobs — see [homelab-health.md](homelab-health.md) |
| `hindsight` | Memory backend for the Hermes profiles | hindsight API, its PostgreSQL, the nightly `pg_dump` and the 15-minute canary — see [hindsight.md](hindsight.md) |
| `printing` | Core One+ printer services | Spoolman inventory, FilaBridge filament accounting, PrintGuard failure detection — see below |
| `cloudflared` | Shared homelab Cloudflare tunnel | Connector for health, Hermes, proxy and Homepage routes — `homelab/bootstrap/cloudflared/` |
| `homepage` | Service directory | Homepage at `home.cynexia.com` behind Cloudflare Access; current card count: `/api/services` |
| `netalertx` | Private device inventory | Merged OPNsense ARP and Netdisco access-port import, ICMP, NBTSCAN, INTRNT and daily NMAP; see [NetAlertX](#netalertx) |
| `ops` | Cluster-wide operational jobs | `update-watch` and `keel-fresh` CronJobs — see below |
| `proxy` | Residential egress for changedetection on the VPS | tinyproxy — see [vps.md](vps.md#residential-egress-through-the-homelab) |

Ingress hostnames are `*.cynexia.net` (Route53), Traefik-fronted, LAN/Tailscale only: `sonarr`, `radarr`, `sab`, `hydra`, `emby`, `hindsight`, `hindsight-ui`, `spoolman`, `filabridge`, `printguard`, `netalertx`, `netdisco`.
`grafana-health.cynexia.net` was one of them until September 6, 2026, when the self-hosted Grafana was removed.

Retired in the rebuild: immich, ollama, open-webui, komga, jellyfin, mylar3, lazylibrarian, caddy, postgresql.
tinyproxy was on that list until September 2, 2026, when it returned in its own `proxy` namespace to serve changedetection on the VPS — see [vps.md](vps.md#residential-egress-through-the-homelab).

### Shared Cloudflare tunnel

cloudflared was retired from the downloads-era stack but remains in `homelab/bootstrap/cloudflared/` for the whole homelab.
The connector runs in the `cloudflared` namespace with tunnel UUID `1a4245a3-5264-420c-9893-b45ff25a0214`, separate from the VPS `cynexia-vps` tunnel.
Its Cloudflare display name is `cynexia-homelab`; the 2026-10-09 name change kept the UUID and credentials.
The legacy 1Password **DOCUMENT** item `health-cloudflared` is retrieved by ID through `make create-homelab-cloudflared-secret` (`op document get`, not `op read`).
The tunnel serves origins in `health`, `proxy`, `homepage` and the Hermes VM.
After editing its ConfigMap routes, restart `deploy/cloudflared` in the `cloudflared` namespace because the config uses a `subPath` mount.

Public `*.cynexia.com` hostnames on this tunnel:

| Hostname | Purpose |
|---|---|
| `hae.cynexia.com` | Health Auto Export ingest → `apple-health-ingester` |
| `mcp.cynexia.com` | Claude/Hermes MCP connector, via Cloudflare Access (Managed OAuth) |
| `hermes.cynexia.com` | Hermes agent dashboard on the hermes VM (`hermes.cynexia.net:9119`, off-cluster), via Cloudflare Access (karakeep-style email policy) |
| `hermes-app.cynexia.com` | `hermes-webui` on the same VM (`hermes.cynexia.net:8787`, off-cluster) — the server the Hermex iOS app talks to, via Cloudflare Access (Service Auth + the same email policy) |
| `proxy.cynexia.com` | Residential egress proxy for changedetection on the VPS — see [vps.md](vps.md#residential-egress-through-the-homelab) |
| `home.cynexia.com` | Static service directory in the `homepage` namespace; Access app uses only `allow_cynexia_com` |

After changing hostnames in `homelab/bootstrap/cloudflared/cloudflared.yaml`, every hostname needs a proxied CNAME to `1a4245a3-5264-420c-9893-b45ff25a0214.cfargotunnel.com`.
`make route-homelab-dns` mints them all, but it shells out to `cloudflared tunnel route dns`, which needs an **origin certificate** at `~/.cloudflared/cert.pem`.
On a machine that has never run `cloudflared tunnel login` that file does not exist, the target aborts under `set -euo pipefail` on the *first* hostname, and a newly added one is never reached — `cloudflared` is not in `make check-tools`, so nothing warns first.
Either run `cloudflared tunnel login` once, or create the single record through the Cloudflare API (zone `2bf4553c3f994e36202b5f574577d2e5`), which is also the only way to set the record comment this zone uses as its provenance note.
`hermes-app.cynexia.com` was created that way.

To recreate the credentials Secret, `make create-homelab-cloudflared-secret`.

`homelab/workloads/homepage.yaml` serves a grouped directory at `home.cynexia.com`, including local device interfaces and cloud consoles. Read its live `/api/services` response for the current card count.
Its origin has no login, so the Cloudflare Access application for that hostname, with only the reusable `allow_cynexia_com` policy, is the public gate.
Deleting that application exposes the page.
The Buddy3D camera card opens `rtsp://192.168.17.103/live` in a registered RTSP player; browsers do not render that stream themselves.
Local IP links need LAN or tailnet reachability from the browser.
Later edits to the ConfigMap need `kubectl -n homepage rollout restart deploy/homepage` because its files use `subPath` mounts.

### NetAlertX

`https://netalertx.cynexia.net` is a private Traefik UI.
Its Route53 `A` record in zone `Z3409TNW35PGSS` points to `10.100.0.100` with TTL 60 (see [DNS](#dns-route53)).
Keep it off the public Cloudflare tunnel.
The operator chose no UI password. Anyone on the private LAN or tailnet who can reach the UI can change its settings, so do not expose the Ingress publicly.
Manual device labels, locks, groups and network-tree assignments live on `netalertx-data` and survive pod restarts.
Device types use exactly ten values.
Gateway covers the OPNsense interfaces and the ISP router, and Switch and AP are as named.
Server covers hypervisors, VMs, the Talos node, Raspberry Pis running a service, iLO and JetKVM.
Computer is a desktop or laptop, and Mobile covers phones, tablets and watches.
Media covers speakers, receivers, consoles and streamers, and Camera covers cameras and doorbells.
Printer is as named, and IoT covers sensors, plugs, hubs, meters, appliances, the UPS and the scales.
The type field is free text and the filter dropdown lists every value in use, so a new device typed with any other word lengthens the dropdown.
New devices arrive with a blank type, so type them by hand.
The network tree accepts only parents whose type is in `NETWORK_DEVICE_TYPES`, and that list does not include Server.
To hang VMs under their host, add Server to `NETWORK_DEVICE_TYPES` through the override.

`homelab/workloads/scripts/netalertx-bootstrap.sh` owns one no-auth REST Import from the in-cluster Netdisco exporter, five-minute REST/ICMP schedules, three scan subnets, 90-day device history, `LOG_LEVEL=minimal`, the device-table columns and filters (`UI_device_columns`, `UI_columns_filters`), the default view (`UI_MY_DEVICES`) and the ignored pod network (`NEWDEV_ignored_IPs`) through `APP_CONF_OVERRIDE`; `LOADED_PLUGINS` includes REST Import, ICMP, NBTSCAN, INTRNT and NMAP.
Do not edit those settings in the UI: the next start reapplies them.
The override explicitly schedules NBTSCAN, INTRNT and daily 02:00 NMAP in NetAlertX's configured timezone (Europe/London on 2026-10-09), and disables ARPSCAN, AVAHISCAN, DIGSCAN and NSLOOKUP; `LOADED_PLUGINS` alone does not stop an upstream plugin whose `*_RUN` default is active.
NBTSCAN looks for NetBIOS names, while INTRNT tracks WAN reachability; neither supplies a LAN MAC inventory.
NMAP uses the image default ports 1–10,000; the gateway will log this traffic and IoT devices may react badly, so exclude a sensitive device by appending `--exclude <device IP>` to `NMAP_ARGS` in Settings (keep the default `-p -10000`).
The first 96-device pass took 24 minutes; NetAlertX runs plugins serially, so REST and ICMP cycles wait during NMAP.
The exporter fetches OPNsense's resolved ARP snapshot (`mac`, `ip`, `hostname`, `manufacturer`) through `search_arp?resolve=yes` and merges Netdisco's active access-port MACs into one JSON array.
Every gateway row remains separate, even when the same MAC appears on several interfaces; matching rows all gain the Netdisco switch MAC and port without replacing their gateway IP or interface name.
A Netdisco-only MAC is imported with its latest active Netdisco IP, or no IP if none is known, plus its switch and port.
It also maps the row's `intf_description` (the OPNsense interface name, such as LAN, iot, servers, WAN or LAN3backupport) to the device VLAN field.
The exporter's raw `netdisco_vlan` stays in the JSON for inspection; it is not mapped to NetAlertX's VLAN field.
Filter the device table by network name with the VLAN filter dropdown (`UI_columns_filters`) or the search box; the VLAN column only sorts.
The override shows these device-table columns through `UI_device_columns`: icon, name, type, MAC vendor, first seen, last seen, last IP, VLAN, status, full MAC, source plugin and custom properties.
The default device view excludes archived devices; the Archived tile still shows them.
A device seen only by ICMP, with no ARP row, has no VLAN.
The gateway's single MAC appears on several VLANs, so NetAlertX may show any one of those interface names for that device.
`NEWDEV_ignored_IPs` holds the SQL `LIKE` pattern `10.244.%` so the pod's own address in the Kubernetes pod network creates no device row after each restart.
Dnsmasq serves DHCPv4 names, and Unbound forwards `lan.cynexia.net` and reverse lookups to it, so active ARP rows with PTR records carry hostnames across all three VLANs.
The ARP-only OPNsense key and secret still come from `op://Homelab/netalertx/opnsense-key` and `/opnsense-secret`; they now enter the `netdisco` Secret and exporter container, not NetAlertX's configuration.
Its OPNsense user has only `Diagnostics: ARP Table`.
Keep the Netdisco Secret and pod environment private; older `netalertx-data` backups can still contain the previous import's encoded credentials.

ICMP sweeps Home (`192.168.17.0/24`), Servers (`10.100.0.0/24`) and IoT (`10.0.2.0/24`) every five minutes.
The three `/24` sweeps exceeded the image's 10-second default timeout, so the override bounds each ICMP run at 120 seconds, below the five-minute schedule.
The pod uses routed pings, not layer-2 discovery on each VLAN.
The exporter uses the gateway ARP snapshot, which is cached for 30 seconds, and Netdisco's access-port view, which refreshes through five-minute `macwalk` runs.
If the Netdisco query fails, the import still returns gateway rows without parent enrichment; if the gateway call fails, it returns 503 with no partial inventory.
During a Netdisco pod restart the whole REST feed is unavailable until the pod returns. ICMP continues checking known IPs, but MAC-only devices may briefly show Down or Disconnected.
The gateway retains an ARP entry for about 20 minutes after contact (`net.link.ether.inet.max_age=1200` on 2026-10-09), so a departed device can linger in the import for that long.
Dnsmasq leases last 4000 seconds, so a departed DHCP client may retain a DNS name for up to 66 minutes 40 seconds after its last renewal.
An access-port row asserts presence until Netdisco marks it inactive on a later macsuck, so a departed device's Down alert can lag switch MAC aging plus a five-minute collection interval and the five-minute import interval.
ICMP-silent devices may be slow to change presence state.
The gateway mDNS repeater is enabled on all three VLANs, but the image's Avahi lookup resolved zero names from a normal pod; `AVAHISCAN` stays off.
Add or lock names manually for static-address devices.
MAC is the identity key: randomized MACs create new records, and several IPs sharing one MAC can collapse into one device.
The network tree receives parent switch and port from Netdisco for active access-port MACs; device types and other parent assignments remain manual.

The existing nightly restic job backs up the entire `netalertx-data` local-path PVC and checks for `/data/db/app.db`.
To restore, stop the Deployment, restore the whole PVC directory including any `app.db-wal` and `app.db-shm`, restart, and check the UI and database.
A live-file restic copy can capture inconsistent SQLite state despite a passing file-size gate; use a different snapshot if SQLite will not open.
No separate backup Job or freshness check exists for this mostly idle database.

### Netdisco

`https://netdisco.cynexia.net` is a private Traefik route to the login-protected Netdisco UI. Its Homepage card is under Network. Search by device name, IP or MAC for the learned switch port; movement history appears after a later natural change. Netdisco polls six switches (`192.168.17.10`–`.14`, `.72`) and the OPNsense Servers address `10.100.0.1` only.

The two read-only SNMP communities are scoped per address in the Secret-backed `deployment.yml`; there is no write community. A separate ARP-only OPNsense API key and secret in the same Kubernetes Secret feed the exporter on port 5001, reached through the in-cluster Service. The Ingress still points at the UI on port 5000. The first admin was created interactively; its login is at `op://Homelab/netdisco/admin-username` and `/admin-password`.

After a `deployment.yml` Secret change, restart the Deployment with `kubectl --context cynexia-homelab -n netdisco rollout restart deploy/netdisco`: its `subPath` mount does not update in place.

`schedule.macwalk.when` in that Secret is `*/5 * * * *`, replacing the packaged default of minute 20 each hour. A macwalk queues collection of access-port locations; a device that leaves can remain present until the switch ages its MAC and the next collection and NetAlertX import run. This interval is an inventory cadence, not an instantaneous reachability signal.

OPNsense has `os-net-snmp` enabled on `10.100.0.1:161` with `l3visibility=1` (`sysServices=76`). It is not listening on Home `192.168.17.1`; the Servers policy already allows the Talos node through. The gateway's ARP table supplies IP-to-MAC mapping across Home, Servers and IoT. Dnsmasq/Unbound PTR records supply names when present. An ARP entry, name or move can lag the live link until the next Netdisco collection; do not use it as an instantaneous reachability test. To roll gateway SNMP back, disable Net-SNMP in Services or remove `os-net-snmp`; retain the saved gateway config `config-1791562068.9104.xml`.

Four manual links are recorded in Netdisco: gateway `ix0`/`ix1` to CRS309 `opnsense (5)`/`opnsense (6)`, CRS309 `Study` to Study `Uplink`, and CRS309 `loft` to Loft `SFP+2`. The CRS309 `POE` port leads to the unpolled Instant On access point at `192.168.17.209`; the polled lounge GS1900 hangs off one of the AP's built-in LAN ports. Do not invent a direct POE-to-lounge link or pseudo-device. Marantz is located at lounge `GigabitEthernet2` and Prusa at Study `Port4`; APC and iLO are on CRS326 `Port18`/`Port24`, and fox-watch is on garage GS1900 `GigabitEthernet6`. The Secret-backed `deployment.yml` sets `macsuck_no_deviceports` for only CRS309 `POE`, so its upstream MAC rows cannot displace the polled edge locations after discovery. At publication, 60 stale POE node rows were archived with `active=false`; none were deleted. Devices wired directly to the AP's LAN ports and Wi-Fi clients associated with that AP have no switch-port location while this exclusion is active; devices on the polled lounge GS1900 still locate at its own ports. Do not use Netdisco `expire-nodes -d 192.168.17.11 -p POE` for this cleanup: it also archives valid edge rows sharing those MACs. Devices behind an unpolled access point elsewhere show at its nearest polled uplink. The garage LAG is established as a logical link, but its individual member-port pairing is unverified; confirm it at the switches before adding member links. SwOS does not expose LLDP or map the observed LAG bridge-port IDs to ifIndex, so bonded-host ports may be absent. The `is_uplink` database flag on CRS309 POE is reset by `discover`; the persistent MAC exclusion, rather than that flag, prevents false endpoint placement.

PostgreSQL 18 stores data under `/var/lib/postgresql/18/docker` on `netdisco-postgres-data`. The `netdisco-pg-dump` CronJob writes an atomic gzip SQL dump to `netdisco-dumps` at 02:25Z, keeps seven files, and pushes to the `netdisco-pg-dump` uptime-kuma monitor. The 03:00 restic sweep copies that PVC; its expected/fresh gate requires ≥360,000 B and <30h. To restore, recover one `netdisco-*.sql.gz` from a restic snapshot to the workstation. In a shell with `set -o pipefail`, run `gzip -dc <dump> | kubectl --context cynexia-homelab -n netdisco exec -i deployment/netdisco -c postgres -- psql -v ON_ERROR_STOP=1 -U netdisco -d netdisco`. The in-pod socket authenticates locally without printing a password. Then `kubectl --context cynexia-homelab -n netdisco rollout restart deployment/netdisco` and verify the login and device count. The dump uses `--clean --if-exists`, so it replaces database objects.

### The `ops` namespace

`homelab/ops/` holds work that belongs to the estate rather than to any one application: two CronJobs, both dead-man's-switches over the update path itself.

- **`update-watch`**, at 06:45Z daily, reads the Renovate dependency dashboard through GitHub's unauthenticated API.
  It pushes DOWN for dependency lookup failures or a missing dashboard, and UP when neither condition exists.
  It does not track pull-request age or dashboard age.
  It pushes **nothing at all** on a run that could not read GitHub, which stops "I could not look" reading as "everything is fine".
  Full behaviour and every cause of DOWN: [monitoring.md](monitoring.md#the-update-watcher).
- **`keel-fresh`**, at 07:15Z daily, makes one request to keel's own `/metrics` — a single ClusterIP endpoint, `keel.keel.svc.cluster.local:9300`, reached across the namespace boundary from `ops`; it scrapes nothing else and holds no cluster-wide read — and pushes the `homelab-keel-fresh` uptime-kuma monitor.
  It is the only thing that would notice keel's registry poll loop had wedged: keel's own probes hit `/healthz`, which stays green while the poll goroutine is dead.
  The configured `IMAGE_FLOOR` is 25, derived from the rendered floating controller images.
  Verdict enum and why there is no `/start`: [monitoring.md](monitoring.md#the-keel-dead-mans-switch).

The half-hour gap is deliberate: the two update-path checks should not alert in the same minute.
`keel-fresh` keeps two integers of state on a 32Mi `local-path` PVC, `keel-fresh-state` — the previous run's process start time and poll counter — which is the only way to assert a counter is *increasing*.
Its only peers are a ClusterIP in the `keel` namespace and `uptime.cynexia.com`.

Three things about this namespace are deliberate and should survive a refactor:

- **It is not `health` and not `backup`.**
  Its scope is the whole repo, so a health-namespace object alerting about other namespaces would misstate ownership; and `backup` runs at PSA privileged for restic's hostPaths, which an outbound-HTTPS poller has no business inheriting.
  `ops` is PSA baseline, the cluster default, and needs no ServiceAccount and no RBAC.
- **Each scheduled run pulls its runtime image.**
  The manifests select `python:3-alpine` for `update-watch` and `curlimages/curl:latest` for `keel-fresh`.
  Both use `imagePullPolicy: Always` and no keel annotations.
- **Removal is one commit,** with a long list in it.
  Drop `- ops` from `homelab/kustomization.yaml`, `rm -r homelab/ops/`, remove the namespace block, remove every `OPS_*` variable from `.env.tpl` and from both Makefile lists — `OPS_KUMA_UPDATE_TOKEN` and `OPS_KUMA_KEEL_TOKEN`, plus `OPS_HC_UPDATE_UUID` for as long as that retired line is still wired; grep rather than working from a list here — and from `scripts/check-ping-bodies.py` remove **both** `REQUIRED_TARGETS` entries (`update-watch.py` and `keel-fresh.sh`), **every name in that file's `update-watch.py` block of `PY_VALUE_ALLOWLIST`** — no count is written here, because it read "eight" while the block held nine, and a stale count leaves names on a security allowlist for code that no longer exists — and `PUSH_URL` from `DENY_VARS`.
  Then apply, `kubectl delete namespace ops` — which takes the `keel-fresh-state` PVC with it — and delete the `keel` **Service** in the `keel` namespace, which exists only to serve `keel-fresh` and survives deletion of the `ops` namespace.
  Finally retire both instruments: the `homelab-update-watch` *and* `homelab-keel-fresh` uptime-kuma push monitors — both push monitors since August 26, 2026 — then delete both 1Password items.

  Removing only `keel-fresh` and keeping `update-watch` is the same list minus the `OPS_KUMA_UPDATE_TOKEN`, `update-watch.py` and `PY_VALUE_ALLOWLIST` items, and without deleting the namespace.

### Printing

The three private UIs are `https://spoolman.cynexia.net`, `https://filabridge.cynexia.net`, and `https://printguard.cynexia.net`. They have no login. Treat LAN/tailnet access as full control. Do not publish them to the internet. PrintGuard must be running and watching the print to detect or report a failure. Check its dashboard before relying on it.

The Core One+ PrusaLink address is `192.168.17.97`. The separate Buddy3D camera has Local Stream Mode enabled at `rtsp://192.168.17.103/live`. FilaBridge reaches Spoolman at `http://spoolman.printing.svc.cluster.local:8000`. The Telegram bot token is `op://Homelab/printguard/telegram_token`; the chat ID is its `chat_id` text field at `op://Homelab/printguard/chat_id`. Find the PrusaLink API key on the printer at **Settings > Network > PrusaLink**. It is the displayed password. Keep these values out of Git and terminal output.

First-run setup belongs to the operator:

1. Open Spoolman. Add the filament and the spool in the printer. Note its starting weight.
2. Open FilaBridge. Set the printer address to `192.168.17.97`. Read the PrusaLink API key from the printer screen and enter it. Set Spoolman to `http://spoolman.printing.svc.cluster.local:8000`. Map the Core One+ toolhead to the loaded spool.
3. Open PrintGuard. Add the printer at `192.168.17.97` with user `maker` and the API key as its password. Add the RTSP camera. Link a monitor to the printer and camera.
4. Open the monitor tile and find **On sustained defect** under **Monitoring**. Set it to **Pause the print**. Turn **Push notifications** on. Keep the initial threshold at 0.75 and three consecutive detections. Crop the print area into the square detection region.
5. Enter the Telegram bot token and chat ID from 1Password. Send a test alert. Confirm its snapshot arrives.

For acceptance, start a small print through Prusa Connect. After it finishes, compare FilaBridge's recorded use with the decrease in Spoolman's mapped spool weight. During a separate safe Prusa Connect print, stage a visible failure while present at the printer. Confirm the printer pauses after a sustained detection. Confirm Telegram receives a snapshot with the pause result. Stop the test if needed.

Spoolman stores `spoolman.db` on `spoolman-data`, FilaBridge stores `filabridge.db` on `filabridge-data`, and PrintGuard stores `state.json` on `printguard-data`. The existing nightly homelab restic job copies these local-path PVCs. If state is lost, restore the whole matching PVC directory from restic, then restart that single Deployment. For FilaBridge, include `filabridge.db-wal` and `filabridge.db-shm`; do not restore `filabridge.db` alone. Restic copies live SQLite files, so a snapshot taken during a write can be inconsistent even when its file-size gate passes. After a restore, read the PrusaLink API key from the printer screen again. If PrintGuard needs its Telegram settings, re-enter them from 1Password.

## Storage and NFS

`10.10.10.1` and `fs.cynexia.net` are the **same physical host** — one `tank` zpool, folders exported over NFS and consumed by the cluster as static PV/PVCs.
Two hostnames for one box (storage-NIC IP vs. general LAN name), not two servers.

| Address | Typical paths |
|---|---|
| `10.10.10.1` | `/tank/video/` (emby + *arr media), `/tank/largeappdata/jottacloud` (rclone sync target) |
| `fs.cynexia.net` | `/tank/appdata/*`, `/tank/largeappdata/*` |

All the data that matters lives on that zpool.
The Talos node's own SSD partitions (vmdata LVM, user volume) are for VM/system lifecycle — etcd and local-path PVCs — not for NFS-backed data.
The homelab restic/B2 job backs up **only** that SSD (`/var/mnt/ssd/local-path-provisioner`); it is the only backup destination this repo manages and it does **not** cover the NFS zpool, which has its own backup story on the NAS side, outside this repo.

The restic backup CronJob and init Job select `restic/restic:latest` with `imagePullPolicy: Always`.
The Hermes pull CronJob selects `kroniak/ssh-client:latest` with the same pull policy.

Applications' own scheduled backups (sonarr, radarr, emby, sabnzbd) must write zips to `/config/Backups/` so restic catches them.
The sqlite-quiesce sidecar pattern from earlier drafts of the plan is **not** used here — it is redundant when the app's own zip backup already handles DB consistency.
(Both restic jobs carry a backup verification gate, in different shapes: the VPS gate checks its quiesce sidecars' snapshots, while the homelab gate checks mount identity, tree scale, an expected-artifact list and the freshness of the influx dumps and the hermes zip — see [monitoring.md](monitoring.md#the-backup-verification-gates).)

Until 2026-08 neither restic CronJob reported anywhere and neither had a runtime ceiling, so a hung run would have blocked every following night silently and been discovered at restore time.
Both now ping healthchecks.io — after the 2026-08-26 migration they are the only jobs in either cluster that still do — and carry `activeDeadlineSeconds` — [monitoring.md](monitoring.md#scheduled-work).

## Node network

| Interface | IP | Purpose |
|---|---|---|
| `ens18` (LAN) | `10.100.0.100` | All `*.cynexia.net` A records point here |
| `ens19` (storage) | `10.10.10.10` | NFS traffic to `10.10.10.1`; Kubernetes misleadingly reports this as `InternalIP` |
| `tailscale0` | `100.85.18.48` | Remote access through the Tailscale mesh |

**Never use `10.10.10.10` as a DNS target** — the storage NIC is not reachable from the home LAN.

### Why the LAN NIC is static

Talos v1.12's controller-runtime DHCP4 client can NAK-loop on renewal if the boot lease didn't land on the reserved IP: Kea OFFERs the reservation cleanly, but the client REQUEST in the same transaction asks for the cached dynamic-pool address, so the loop never converges.
The resulting retry storm also trips RFC 5905 KoD rate-limiting on the gateway's NTP, which surfaces as `time.SyncController` errors that look like a clock problem and send you debugging the wrong subsystem.

`homelab/talos/machineconfig-patches/305-homelab-lan-network.yaml` therefore puts `ens18` on a static address and moves NTP to public servers (time.cloudflare.com, time.google.com, pool.ntp.org), so the cluster depends on the gateway for neither.
The OPNsense Dnsmasq reservation is kept as defense in depth.

### Gateway DHCP and local names

OPNsense Dnsmasq serves DHCPv4 on Home, Servers and IoT with 4000-second leases.
Unbound forwards `lan.cynexia.net` and the three reverse /24 zones to Dnsmasq on `127.0.0.1:53053`.
Kea DHCPv4 and its Control Agent are disabled; the Kea subnets and 30 reservations remain for rollback.

A Dnsmasq reconfigure restarts the daemon and empties `/var/etc/dnsmasq-leases`, its supplemental DNS `addn-hosts` file.
The actual DHCP lease database is `/var/db/dnsmasq.leases` and survived the 2026-10-09 reconfigure.
After any reconfigure, check a live lease on each VLAN and forward/PTR answers through Unbound.

## DNS (Route53)

Hosted zone for `cynexia.net`: `Z3409TNW35PGSS`.
The AWS CLI is authenticated on the workstation, so records are managed directly:

```bash
aws route53 change-resource-record-sets --hosted-zone-id Z3409TNW35PGSS \
  --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"<host>.cynexia.net","Type":"A","TTL":60,"ResourceRecords":[{"Value":"10.100.0.100"}]}}]}'
```

TTL is 60s, so after a change browsers need a hard refresh (Cmd+Shift+R) to stop using the cached target.

`cynexia.com` is a **different** zone on Cloudflare, used by the VPS cluster and by the homelab cloudflared tunnel (tunnel UUID `1a4245a3-5264-420c-9893-b45ff25a0214`, run from the `cloudflared` namespace).
It has nothing to do with Route53.

## Encryption at rest

Two independent layers, both live:

- **Kubernetes secretbox** — verified 2026-07-26 with a canary: the plaintext sentinel is absent from etcd and the `k8s:enc:secretbox:v1:` prefix is present.
- **Disk (LUKS2 + TPM2)** — since 2026-07-26 both pve3 NVMe partitions backing the Talos VM are LUKS2: the `vmdata` LVM (OS disk, etcd) and the user volume.
  They are auto-unlocked at host boot by the TPM through clevis (PCR 7, requires Secure Boot enabled).
  VM 100's disks point at `/dev/mapper/vmdata_crypt` LVs and `/dev/mapper/talos_ssd_crypt`.
  This covers etcd and every local-path PVC, including `health-dumps`, `influxdb-data` and `garmin-tokens` — which is why per-PVC encryption was recorded as superseded rather than skipped.

pve3 specifics that follow from that design:

- **Do not disable Secure Boot casually** — PCR 7 changes and both LUKS volumes refuse TPM unlock (designed anti-theft behaviour, proven in a live negative test).
  Routine BIOS updates survive because only PCR 7 is sealed against.
- **A new VM is encrypted iff its disks land on the `vmdata` storage** (the VG lives inside the LUKS mapper).
  Disks on `local` (the Proxmox root, ext4 on sda3) are NOT encrypted; the Proxmox root itself is deliberately unencrypted.
- Swap is `swap_crypt`, plain dm-crypt with a random `/dev/urandom` key per boot.
- **The hermes VM (103) has a nightly application-state backup.**
  The `hermes-pull` CronJob in the `backup` namespace SSHes to the VM at 02:00 UTC with a restricted forced-command key, runs `hermes backup`, and pulls the zip onto the `hermes-dumps` PVC; the 03:00 restic sweep carries it to B2.
  The zip covers `~/.hermes` only — config, credentials, `state.db`, sessions, profiles, skills, memories.
  Everything else on the VM (OS, systemd user units, tunnel config, `~/.local/bin` wrappers, the hermes-agent and hermes-webui checkouts, `~/workspace`) stays rebuild territory: rebuild the VM, reinstall hermes, then restore state with `hermes import`.
  The restore runbook, the wrapper script, and the key-rotation procedure are in [Hermes VM backup and restore](#hermes-vm-backup-and-restore); monitoring semantics are in [monitoring.md](monitoring.md#healthchecksio-checks).

The jottacloud staging copy on the HDD pool is separately encrypted with rclone crypt (`DEST_REMOTE` in the workload ConfigMap).
Its passphrase (`JOTTA_CRYPT_PASSWORD` in 1Password) is frozen by design: rclone crypt cannot rekey in place, so rotation is the re-encrypt procedure documented in the `mnbf9rca/jottacloud-backup` image README.

### Recovery: VMs don't autostart after a pve3 boot

That means the TPM refused to release the key — normally after a firmware or Secure Boot change.
On the Proxmox host:

1. `cryptsetup open` each volume using the recovery passphrases in 1Password (`op://Homelab/TPM/...`).
2. `vgchange -ay`
3. `systemctl start pve-guests`
4. Re-run `clevis luks bind` so the TPM can unlock unattended again.

### Hermes VM configuration layout and secrets

These rules hold for **every** Hermes plugin and profile on VM 103.
They were learned while wiring hindsight (2026-08-24), and they bind any future integration:

- **Plugin config resolves per `HERMES_HOME`, which makes it per PROFILE.**
  A profile is a separate Hermes home directory, so profile `<name>` reads `~/.hermes/profiles/<name>/<plugin>/config.json`.
  The path `~/.hermes/<plugin>/config.json` is not shared: it belongs to the *default* profile, whose home is `~/.hermes` itself.
  Each plugin loads the first source that exists — the profile's file, then a legacy shared path, then environment defaults — and **the whole file wins with no merging**, so malformed JSON presents as a config that reverted to defaults.
  Some keys accept an environment variable as a per-key fallback, but only when the key is absent from the file: **the file value wins over the environment.**
  Onboarding a profile therefore means copying the config file, not relying on a shared one.
- **Secrets are per profile and MUST go through hermes's 1Password integration** — `hermes -p <profile> secrets onepassword set VAR "op://Vault/item/field"` — never a plain value in `.env` or `config.yaml`. hermes stores its own `op://` reference in the profile's `config.yaml` and resolves it at start ("1Password: applied N secrets"), so no secret value rests on disk, and the nightly backup zip carries references rather than values for these.
  **`secrets op` and `secrets onepassword` are aliases**, not a contradiction: `hermes secrets` accepts `bitwarden`, `bw`, `onepassword`, `op` and `1password`, and its own help line reads `onepassword (op, 1password)`.
  Every spelling committed across `docs/` is correct as it stands.
  This was flagged once as a live contradiction and was a false alarm; do not "fix" it.
  **The VM's 1Password service account can read only the `hermes` vault.**
  That is the rule any new integration has to design around: **a reference the VM itself resolves must live in `hermes`; a reference the operator's laptop resolves and pipes over ssh may live anywhere.**
  An `op://Homelab/...` reference handed to the VM resolves to nothing, and hermes reports that as a config value that quietly stayed unresolved.
- **Do not trust the dashboard GUI as the writer of record — it fails in both directions.**
  It silently drops some writes: at least one field (the hindsight API server URL) reports saved and is not.
  It also persists too much, writing an API key into the plugin's `config.json` as **cleartext**, which defeats the 1Password integration and puts a live credential in the nightly backup zip.
  After any GUI session, read the config file back and delete any secret value you find there.
  Because the file wins over the environment, a stored key shadows the 1Password-backed variable until it is removed.
  To change a value reliably, edit the file and restart the affected gateway (`systemctl --user restart hermes-gateway`).
- **Never disable a toolset that shares its name with a provider plugin's feature.**
  `hermes tools disable memory` is the live case: `memory` names both a built-in tool and a toolset, and the toolset gates every memory provider's tools as well.
  Disabling it removes the provider's tools with no warning.
  Suppress the built-in tool through the profile's `config.yaml` instead (`memory: {memory_enabled: false, user_profile_enabled: false}`) and leave the toolset enabled per platform — `--platform` defaults to `cli`, and gateway platforms are separate keys, so check `hermes -p <profile> tools --summary`.
- **A tool listing is not evidence that a plugin's tools are absent.**
  `/tools` and `hermes tools list` render from the static registry, which knows nothing about provider plugins; provider tools are appended to the agent afterwards and never appear in either listing.
  Verify a plugin tool by asking the agent to call it, never by listing.

This section is the single copy of that contract.
A service runbook carries only its own wiring and points back here for the shared rules rather than restating them — `docs/operations/hindsight.md` is the worked example.

### Hermes VM backup and restore

The `hermes-pull` CronJob (`homelab/backup/hermes-pull.yaml`) pulls a full `hermes backup` zip from the hermes VM every night at 02:00 UTC onto the `hermes-dumps` PVC, where the 03:00 restic sweep replicates it to B2.
The zip contains the VM's live secrets in plaintext (`.env`, `auth.json`, MCP OAuth tokens), so its only permitted resting places are that PVC and the restic repository.
Delete any operator copy as soon as a procedure finishes with it.

#### What `hermes backup` captures

Facts verified on the VM and against the upstream docs (https://hermes-agent.nousresearch.com/docs/ — an `llms.txt` index and a full markdown mirror exist; fetch those rather than searching):

- The zip covers the entire `~/.hermes` home: `config.yaml`, `.env`, `auth.json`, `mcp-tokens/`, `state.db`, sessions, memories, skills, plugins, cron, both profiles, and the bundled node runtime.
  It excludes the `hermes-agent/` checkout, `backups/` itself (which is why the wrapper stages there — the staged copy can never recurse into the next night's zip), `state-snapshots/`, checkpoints, and SQLite sidecar files.
  Measured 2026-08-24: 203 MB compressed from 572 MB, in about 23 seconds.
- It is safe against live services: `state.db` is copied with SQLite's `backup()` API, so the four systemd user units keep running.
  Only **restore** requires stopping them.
- **Never pass `-q`/`--quick`**: it snapshots only `config.yaml`, `state.db`, `.env`, auth and cron — it drops profiles, skills and `mcp-tokens/`, which are exactly the irreplaceable set.
  The pull job's 100 MiB size floor exists to catch a quick-shaped zip if this ever regresses.
- `hermes profile export` is **not** a backup: it strips API keys by design, and session history stays in `state.db` regardless.
- Upstream provides no scheduling, retention, or encryption for these backups; the zip holds the VM's secrets in plaintext, which is why its only permitted resting places are the PVC and restic (both encrypted at rest).
- Concurrent backups fail fast, never block: the wrapper's own flock exits 75, and a second `hermes backup` against hermes's internal `.backup.lock` exits 2 with "another Hermes backup is already running".
  A hung pull therefore always means the network or the VM, not lock queueing.

Three pieces of state live on the VM itself and are not managed by this repo.
This section holds their canonical copies — if you change any of them on the VM, change it here in the same sitting.

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

The restricted line in `/home/hermes/.ssh/authorized_keys` (the operator's own key line is separate and unaffected):

```
restrict,command="/home/hermes/bin/hermes-pull-wrapper.sh" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICrRo/NPnEnUk3mUY9SmdnENyzXqlo1LFoq3OHoSWZ79 k8s-hermes-pull
```

The public key is `op://Homelab/hermes-ssh-key/public key` (fingerprint `SHA256:YuIGosID1a0pwDM1IwBNuax5kHSSJRhwT3TQbp1xzEA`).
`restrict` disables PTY allocation, all forwarding, `~/.ssh/rc`, and every channel type OpenSSH adds in future releases — fail closed, nothing re-enabled.

The third piece is the WebUI service unit, `/home/hermes/.config/systemd/user/hermes-webui.service`.
Its rationale is in [Hermes WebUI on the VM](#hermes-webui-on-the-vm) below; the file itself:

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

ExecStart=/usr/bin/python3 /home/hermes/hermes-webui/server.py
WorkingDirectory=/home/hermes/hermes-webui

Environment="PATH=/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="HERMES_HOME=/home/hermes/.hermes"
Environment="HERMES_WEBUI_AGENT_DIR=/home/hermes/.hermes/hermes-agent"
Environment="HERMES_SUPERVISED_CHILD=1"
UnsetEnvironment=VIRTUAL_ENV HERMES_WEBUI_PYTHON
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

Its `EnvironmentFile`, `/home/hermes/.hermes/webui.env`, holds one key and is mode `0600`.
Only the key name is recorded here:

```
HERMES_WEBUI_PASSWORD=<the WebUI password>
```

That file is inside `~/.hermes`, so unlike the unit it **is** in the nightly zip and survives a rebuild the same way `.env` and `auth.json` do.

### Hermes WebUI on the VM

`hermes-webui` ([github.com/nesquena/hermes-webui](https://github.com/nesquena/hermes-webui)) runs on port 8787 as the `hermes-webui` systemd user unit, published at `https://hermes-app.cynexia.com` through the homelab tunnel (see [the shared tunnel](#shared-cloudflare-tunnel)).

**One instance serves every profile.**
It reads `~/.hermes/profiles/<name>` off disk and switches per client on a `hermes_profile` cookie, so every profile under that directory shares one process and clients get an in-app switcher.
There is no second unit and no second port.
It is a third, distinct service alongside the dashboard on 9119 and the gateways on 8642 — none of the three replaces another.

**Who uses it.**
The Hermex iOS app ([github.com/uzairansaruzi/hermex](https://github.com/uzairansaruzi/hermex)).
The app forces https, appends no port and no path, and probes `GET /health` then `GET /api/auth/status` before login, which is why the hostname has to serve the WebUI at the root on 443.
It streams over SSE, never websockets.

**Stable WebUI activates the agent's PM runtime itself.**
The unit starts `server.py` directly through system Python; stable v0.52.458’s native startup re-enters the selected PM Python 3.14 environment before API imports.
Its YAML compatibility module uses PM's existing ruamel fallback, so this source deployment needs no PyYAML supplement and no WebUI venv.
Do not persist a PM generation path in `HERMES_WEBUI_PYTHON`; the actual serving interpreter and a read-only WebUI gateway-status action are the checks.

**Do not give it a venv of its own.**
The WebUI imports `run_agent` and needs the complete agent dependency surface. HTTP health alone can remain green while chat imports fail.
The daily alive check therefore deep-imports WebUI YAML/profile APIs and `run_agent` through the guarded PM helper; it isolates import-time settings writes.

#### Updates

Update core, its bundled dashboard and external WebUI together. Core uses upstream `hermes update` with implicit stable/forward-only behavior; external WebUI uses stable and its WebUI-only Update Now action.
Follow [the application update runbook](hermes-vm.md#application-updates), including managed terminal policy mirroring and the accepted reader limitation.

#### Rollback when an update breaks the app

Restore matching source, environments, units and stores into **emptied destinations** using [Rollback](hermes-vm.md#rollback).
Keep moved-aside newer trees, the private recovery archive and the reboot hold; forward-only schema/pruning changes cannot be undone by a Git reset.

#### What is and is not backed up

Only paths under `~/.hermes` are covered by the nightly Hermes zip.
The WebUI unit pins `HERMES_WEBUI_DEFAULT_WORKSPACE=/home/hermes/workspace`, which is **outside that backup**.
Treat files there as expendable; put durable work in a Git remote or a profile workspace under `~/.hermes`.
The private full recovery archive taken for maintenance is separate from the nightly zip.

| Path | In the nightly zip? |
|---|---|
| `~/.hermes/webui/` sessions/settings and `~/.hermes/webui.env` | Yes |
| Profile state, env/config, workspaces and durable sandbox homes under `~/.hermes` | Yes |
| `~/.hermes/scripts/` and profile cron stores | Yes |
| `/home/hermes/workspace` | **No; expendable WebUI default workspace** |
| `/home/hermes/hermes-webui` | No; restore the reviewed source checkout separately |
| `~/.config/systemd/user/` units and drop-ins | No |
| `/etc/hermes`, apt configuration and apt timer drop-ins | No; reinstall canonical files |
| `/var/lib/docker` images and container layers | No; durable host mounts live under `~/.hermes` |

#### Rebuild step

A VM rebuild does not restore this service.
After `hermes import` (step 6 of the restore runbook), re-clone and re-create the unit:

```sh
git clone https://github.com/nesquena/hermes-webui.git /home/hermes/hermes-webui
# Select the reviewed stable WebUI release and prove native PM imports first.
# unit file: copy from this document
loginctl enable-linger hermes
systemctl --user daemon-reload
systemctl --user enable --now hermes-webui
```

**`loginctl enable-linger hermes` is not optional and is easy to miss.**
Without it the `hermes` user manager stops when the last session ends, so all three user units die at the next reboot, which is now automatic and can happen any night at 04:45 UTC.
Part of the failure hides itself: the daily liveness check runs as a cron job inside `hermes-gateway`, so the thing that would push a `down` dies with everything else. uptime-kuma sees silence and that monitor stays green until its heartbeat lapses about 30 hours later.
The existing `hermes API` HTTP monitor does catch it sooner, because the dashboard it probes is one of the units that died.
Confirm lingering with `loginctl show-user hermes -p Linger`, which must print `Linger=yes`.

`webui.env` comes back with the restored `~/.hermes`, so no password step is needed on a restore — only on a first install.

#### Security posture — read before "fixing" any of it

- **`HERMES_WEBUI_SECURE=1` is a cookie flag, not an access control.**
  It forces `Secure` on the session cookie because cloudflared speaks plain HTTP to the origin and the cookie would otherwise lose the flag.
  The alternative, `HERMES_WEBUI_TRUST_FORWARDED_PROTO=1`, means trusting a header any LAN client can forge against a `0.0.0.0` bind; forcing the flag needs no trust at all.
  `HERMES_WEBUI_TRUST_FORWARDED_HOST` and `_FOR` stay off for the same reason.
  The visible cost is that a browser cannot log in over plain `http://hermes.cynexia.net:8787` — use the tunnel hostname.
  Do not "fix" this.
- **The LAN is a trusted zone here, and the WebUI password is the only control on it.**
  `Secure` is honoured by browsers only: any script can `POST` to `http://hermes.cynexia.net:8787/api/auth/login` over plain HTTP, take the `Set-Cookie` value, and replay it, entirely outside Cloudflare Access.
  The VM has no host firewall (`ufw` is not installed) and the dashboard and gateways already bind `0.0.0.0`, so this is the VM's established posture rather than something this service introduced — but it is the reason the blank-password guard in the unit exists, and the reason no IP-bypass policy was carried onto the Access app.
  Firewalling 8787, 9119 and 8642 down to the cluster egress address is the obvious hardening, and is a change of its own.
- **The WebUI password is readable by the agent it protects.**
  It arrives as a plaintext environment variable, and the WebUI builds child processes with an unfiltered `os.environ.copy()` (`api/routes.py:1308`, `api/gateway_restart.py:99`, `api/workspace_git.py:113`) with no scrubbing.
  Any session — including one steered by prompt injection through fetched content — can read `$HERMES_WEBUI_PASSWORD`.
  **Treat the Cloudflare Access service token as the only gate an agent cannot forge.**
  The hardening path is to set the password through the WebUI's own Settings page, which writes a `password_hash` into `settings.json`, and then drop the env var; the env var is what makes the *first* start safe on a `0.0.0.0` bind, so it cannot be omitted.
- **Not every path is behind the password.**
  `/share`, `/share/*`, `/api/share/*`, `/static/*`, the manifests and the auth endpoints are in the WebUI's public set, and `GET /api/share/<token>` returns a full shared conversation on token possession alone.
  Under a Cloudflare Access *bypass* policy those paths would have no gate whatsoever, which is why the Access app authenticates every request instead.
- **The Access service token is shared with `hermes.cynexia.com`.**
  The same token (`468dc6d0-d0d6-4b98-8c07-4d002fda2df1`, value in 1Password) sits on the phone and opens the agent dashboard as well as the WebUI.
  A lost or compromised device therefore yields both hostnames, and the response — rotating the token — also breaks the uptime-kuma monitors that carry it.
  Plan on rotating the token and updating those monitors in the same sitting.

#### Restore

Restoring replaces `~/.hermes` application state on a working hermes install.
Rebuild the OS; restore the state.

1. Get the newest zip.
   Same-day, from the PVC (`kubectl cp` needs a running container, so the helper pod sleeps):

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
   systemctl --user stop hermes-dashboard hermes-gateway hermes-webui
   ```

4. Import and verify (`~/.local/bin` is not on the non-login PATH):

   ```
   ~/.local/bin/hermes import ~/hermes-restore.zip
   ~/.local/bin/hermes config check
   ```

5. Restart the six services and confirm the dashboard at hermes.cynexia.com.
6. On a fresh VM rebuild: install hermes first, then run steps 2–5, then `hermes setup`.
   Run `loginctl enable-linger hermes` before enabling any user unit, or every one of them dies at the next reboot.
   `hermes-webui` is a separate install that the zip does not carry — re-clone it and re-create its unit from [Hermes WebUI on the VM](#rebuild-step) before step 5 restarts it.
   The daily liveness check and `unattended-upgrades` are a separate install as well — [hermes-vm.md](hermes-vm.md#installing-or-reinstalling).
7. Delete every operator-side copy: `./hermes-restore.zip`, the `./restore/` tree if step 1 used B2, and `~/hermes-restore.zip` on the VM.

**Cross-version caveat:** upstream does not document restore behaviour across hermes versions.
Restore onto the same or a newer version, never an older one.
After any version gap, run `hermes config check` and, if it complains, `hermes migrate` before starting the services.
If migration fails, install the version that took the backup (git install, so any tag is reachable), import there, then upgrade in place.

#### Key rotation

- **Client key**: generate a new ed25519 keypair into `op://Homelab/hermes-ssh-key`, append the new public key as a second restricted `authorized_keys` line (same options), run `make create-hermes-ssh-secret`, confirm the next nightly run goes green, then delete the old line.
  Two lines during the overlap means rotation never risks a missed night.
- **Host key**: changes only when the VM's sshd is reinstalled or the VM is rebuilt.
  The pull then fails closed on the mismatch — desired.
  From a trusted network, run `ssh-keyscan -t ed25519 hermes.cynexia.net`, update the `hermes-known-hosts` ConfigMap in `homelab/backup/hermes-pull.yaml`, and `make apply-homelab`.
  Host public keys are tier-3 identifiers; committing them is fine.

## Migration notes (Phase 4)

Services were deployed fresh with empty PVCs; app-level backups were exported from the old microk8s cluster through each service's own UI and imported into the new instance the same way.
No rsync-from-old-cluster data seeding was needed.

The old microk8s cluster was **decommissioned 2026-07-26** — no overlap concerns remain, and any lingering `--context=microk8s` instructions are obsolete.

## Operational gotchas

Symptom-first; the rules these imply for *writing* manifests are in `AGENTS.md`.

**An NFS PV stays `Released` and the replacement PVC stays `Pending`.**
NFS PVs use reclaim policy `Retain` and keep their `claimRef` after the PVC is deleted, so they won't auto-bind:

```bash
kubectl patch pv <name> --type=json -p='[{"op":"remove","path":"/spec/claimRef"}]'
```

**A pod is rejected with a PodSecurity violation.**
The cluster enforces PSA `baseline`. hostPath / hostNetwork workloads need their namespace elevated to `privileged` through labels in `homelab/bootstrap/namespaces.yaml` (as `traefik` and `backup` already are).

**sabnzbd returns 403 "Access denied - Hostname verification failed".**
Linuxserver's sabnzbd image ships a `host_whitelist` that blocks unknown hostnames.
Add the external hostname to `/config/sabnzbd.ini` and restart the pod — edit in place rather than spinning up a helper pod:

```bash
kubectl -n downloads exec deployment/sabnzbd -- \
  sh -c "sed -i 's/^host_whitelist =.*/host_whitelist = sab.cynexia.net/' /config/sabnzbd.ini"
kubectl -n downloads rollout restart deployment/sabnzbd
```

**Linuxserver (Alpine-based) containers can't resolve DNS.**
Alpine's resolver misbehaves under Kubernetes' default DNS policy, so every affected Deployment sets:

```yaml
dnsPolicy: None
dnsConfig:
  nameservers: ["8.8.8.8", "8.8.4.4"]
```

**An Ingress serves the wrong certificate, or none.**
Traefik serves `wildcard-cynexia-net-tls` as its default cert through the file-provider ConfigMap in `homelab/bootstrap/traefik/traefik.yaml`.
Ingresses therefore need **no** `tls:` block — just the `host:` rule.
Adding a per-Ingress `tls:` block reintroduces the cross-namespace TLS secret replication problem this design avoids.

**keel silently downgraded a service to an ancient version.**
The Deployment is missing `keel.sh/match-tag: "true"`.
Without it, keel's `force` policy watches the image name across all tags, finds the newest digest, then rewrites the tag to whatever sits in the image's `org.opencontainers.image.version` label — for linuxserver images that's the upstream app version, which usually also exists as a years-old Docker Hub tag.

**`kubectl apply` reports `configured` for the same resources every run.**
Expected and benign — see [apply-workflow.md](apply-workflow.md#configured-is-not-drift).

**Cluster access problems** (`connection refused` on 127.0.0.1:8080, missing PGP key, `node not found`) are in [omni-access.md](omni-access.md#troubleshooting).
