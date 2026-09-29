# Estate updates

This document describes how the estate gets patched and what the periodic update session covers.
The session runs through `.claude/skills/update-estate/SKILL.md`, invoked as `/update-estate`.
This document supplies reference material without repeating that runbook.
The strategy lives in the gitignored design note `docs/superpowers/specs/2026-08-26-estate-update-strategy.md`.

## Update ownership

| Mode | Surface | Cadence | Watched by |
|---|---|---|---|
| Floating runtime images | Keel on both clusters; Jobs and CronJobs use `imagePullPolicy: Always` | Six-hour Keel polling or each new Job Pod | `homelab-keel-fresh`, `vps-keel-fresh`, and existing job monitors |
| Pinned dependencies | `alpine/k8s`, MCP build inputs, and the VPS local-path `?ref=` base | Renovate without a monthly window | `homelab-update-watch` reports lookup failures and a missing dashboard |
| Interactive infrastructure | Talos, Kubernetes and remote-base bundles through `/update-estate` | A few times a year, or sooner for an advisory | Existing `estate-update` check; its configured 45-day period is unchanged |
| Hermes application | The operator presses the WebUI's **Update Now** button | Operator choice | Existing daily `hermes-app-alive` monitor |
| Hermes operating system | `unattended-upgrades` | Existing timer | Existing daily `hermes-app-alive` monitor indirectly |

Everything on both clusters floats unless its image has no floating channel.
Floating Deployments, DaemonSets and StatefulSets carry the full Keel annotation set.
Jobs and CronJobs use floating tags with `imagePullPolicy: Always`, without Keel annotations.
PostgreSQL stays within `pg17`, `17-alpine`, or `16-alpine`; major upgrades remain explicit dump-and-restore operations.
Restic binaries float, while repository-format migrations remain explicit operations.
Nightly dumps are the accepted recovery path for application schema changes.
Meilisearch is rebuildable and follows `v1` with `MEILI_UPGRADE_DB=true` permanently enabled.
Renovate retains its three-day stability wait and the MCP build-input automerge.
The existing 45-day infrastructure heartbeat period does not match the few-times-yearly session cadence; its configuration remains unchanged.

**The repository builds one image, and its update needs no manifest apply.**
The repository's one workflow builds `ghcr.io/mnbf9rca/influxdb-mcp-server` from `homelab/health/mcp/`.
The Deployment follows its floating `stable` tag under Keel.
Renovate groups the `node` base image, pinned `influxdb-mcp-server` package and corresponding lockfile as **influxdb-mcp build inputs**.
The pull request build creates and signs the image; the merge promotes that digest to `stable`.
Keel delivers it on its six-hour poll.
Confirm that the pull request build passed.
Confirm that the merge's `promote` run passed.
This change needs no `make apply-homelab`, and its `make diff-homelab` is empty.
The `alpine/k8s` runtime in `homelab/health/` remains pinned and needs an ordinary apply.

The build-input group automerges after its three required checks and the three-day `minimumReleaseAge` wait pass.
An open Renovate pull request with a failed check therefore needs attention.
Read the failed check before taking further action.
Do not merge past a failed or pending check.

## When to run a session out of band

Run a session immediately when an advisory in FreshRSS's `security` category names a component this estate runs.
These feeds supply the estate's vulnerability signal; Renovate emits none for container images, and no scanner runs here.
Cloudflare Access is the primary boundary; advisory response remains immediate.

Components covered include Talos, Kubernetes, cert-manager, Traefik, cloudflared, Keel, InfluxDB, the Grafana PDC agent, PostgreSQL and restic.
The scope also includes every image under `homelab/health/`, `homelab/hindsight/`, `homelab/ops/` and `*/backup/`.

## The version ledger

Update this ledger at the end of every session.
Update the confirmed date even when neither version changes.
A stale date indicates a skipped session.
The control-plane count determines whether an upgrade causes an outage or rolls across nodes.

| Cluster | Talos | Kubernetes | Control-plane nodes | Confirmed |
|---|---|---|---|---|
| homelab | 1.14.2 | 1.37.1 | 1 (`talos-5yn-s9u`) | September 29, 2026 |
| vps | 1.14.2 | 1.37.1 | 3 (`ubuntu-16gb-fsn1-2`, `ubuntu-4gb-fsn1-2`, `ubuntu-4gb-nbg1-1`) | September 29, 2026 |

Read the live versions and control-plane counts:

```bash
omnictl get clusters -o json | jq '{id:.metadata.id, talos:.spec.talosversion, k8s:.spec.kubernetesversion}'
kubectl --context cynexia-homelab get nodes -o wide    # OS-IMAGE names the booted Talos
kubectl --context cynexia-vps get nodes -o wide
kubectl --context cynexia-homelab get nodes -l node-role.kubernetes.io/control-plane
kubectl --context cynexia-vps get nodes -l node-role.kubernetes.io/control-plane
```

A difference between Omni's recorded version and the booted version indicates an unfinished upgrade.
Resolve that difference before starting another upgrade.
Read the ledger before planning an upgrade.
Confirm the control-plane count from the live cluster.

- A single-node control plane loses the API server, etcd and every workload during its reboot.
  Tell the operator before starting this outage.
- A three-node control plane rolls one machine at a time, with Omni waiting for etcd health between machines.
  Check etcd membership with `talosctl -n <node> etcd members` between machines.
  Confirm that all three members are healthy before the next machine proceeds.

## Upgrading Talos and Kubernetes through Omni

Read Omni's permitted upgrade targets before planning an upgrade:

```bash
omnictl get talosupgradestatus <cluster> -o yaml        # .spec.upgradeversions = allowed Talos targets
omnictl get kubernetesupgradestatus <cluster> -o yaml   # same for Kubernetes
omnictl cluster kubernetes upgrade-pre-checks <cluster> --to <version>
```

[Sidero's Talos upgrade guide](https://docs.siderolabs.com/talos/v1.14/configure-your-talos-cluster/lifecycle-management/upgrading-talos) recommends upgrading the starting minor to its latest patch before moving to the next minor.
Configuration migration is tested only between adjacent minor releases.
Use the latest patch of each minor when following that recommended path.
Omni can permit a path that omits the recommended patch step.
On September 29, 2026, Omni allowed Talos 1.13.9 directly to 1.14.2 even though 1.13.10 was available.
Both clusters took that direct adjacent-minor path, deliberately omitting the recommended starting-minor patch step.
A Talos upgrade does not move Kubernetes; the two are separate operations.

The Talos support matrix determines which Kubernetes versions each Talos release supports.
Upstream Kubernetes version-skew rules also apply; Sidero does not define a separate Kubernetes skew policy.

Use **Clusters → the cluster → Update Talos** or **Update Kubernetes** in the Omni web UI.
Cluster templates also support upgrades through `talos.version` and `kubernetes.version` in a `kind: Cluster` document.
This repository has no cluster template; `homelab/talos/` and `vps/talos/` contain machine config patches only.
An export with `omnictl cluster template export <cluster> -o <file>` can support inspection.
Do not run `omnictl cluster template sync` during an update session.

Both clusters were created in the web interface and carry no template annotation.
Sidero documents export followed by template sync as adoption into template management.
Adoption needs a separate design decision because exported templates inline config patches through `idOverride`.
Those include the five homelab patch files already owned by `make apply-talos`.
Two tools would then write the same patches, with the last write winning and no guard between them.
Any adoption must assign one owner and remove or guard the other writer.
Until then, the web UI remains the upgrade path, and Step 3 of the session requires the operator.

**Omni is the system of record for machine config patches.**
The two repository trees contain only the patches authored here, not the entire live inventory.
`make apply-talos` applies each homelab patch file without enumerating or deleting live patches.
It covers homelab alone; the two VPS patch files require a manual `omnictl apply`.
A patch created in the UI or deleted from the repository can therefore remain active in Omni.

Omni's generated patches carry the `omni.sidero.dev/system-patch:` label.
Examples include `400-<cluster>-control-planes-untaint` and per-machine `900-cm-<machine>-kubernetes-upgrade` patches from `KubernetesUpgradeStatusController`.
Do not copy these generated patches into the repository.
A repository copy would compete with resources Omni rewrites.
Patches without that label need an ownership decision.
The subnet patch `200-homelab` existed only in Omni until it was codified on August 28, 2026.

Reconciliation also found obsolete patch `500-7a4333c7-df30-4205-a022-fd93154da992`.
It retained the pre-SSD kubelet self-bind on `/var/mnt/local-path-provisioner` after its successor was removed at `ea0a75c`.
It was inert because nothing mounted at that path.
The operator deleted it on August 28, 2026 with `omnictl delete configpatch <id>`.
Patch deletion can restart the kubelet.
Schedule such deletions in a maintenance window.
Run `omnictl get configpatches` at the start of each session.
Compare the list with both repository patch trees.
Identify each remaining patch by its system label or an explicit ownership decision.

Direct version edits to `Clusters.omni.sidero.dev` are undocumented and unsupported.
Do not change versions through that resource.

**`talosctl upgrade-k8s` cannot access these clusters.**
Omni's RBAC denies the Talos-side Kubernetes proxy, so even `--dry-run` fails with `rpc error: code = PermissionDenied desc = not authorized`.
Plain Talos API calls through the same talosconfig succeed; this is an authorization restriction.

## Bootstrap manifests

Omni holds bootstrap manifest changes for review instead of applying them automatically and overwriting manual edits.
The backlog includes CoreDNS, kube-proxy, the CNI plugin and bootstrap tokens.

```bash
omnictl get kubernetesupgrademanifeststatus -o yaml     # .spec.outofsync = pending objects
omnictl cluster kubernetes manifest-sync <cluster>    # dry run by default
omnictl cluster kubernetes manifest-sync <cluster> --dry-run=false   # applies
```

The UI exposes **Bootstrap Manifests** in the left navigation after a Kubernetes upgrade, before those changes are applied.
Read the dry run in full.
Apply only changes suitable for the cluster.

On August 28, 2026, both clusters had `outofsync: 21` and an empty `lastfatalerror`.
The dry run showed kube-proxy v1.35.3 on homelab and v1.35.2 on VPS against control planes at v1.36.4.
CoreDNS was v1.13.2 and Flannel was 0.27.4.
Earlier control-plane upgrades had left these components behind because no session had applied the held manifests.
The August 28 sync brought both clusters to kube-proxy v1.36.4, CoreDNS v1.14.6 and Flannel 0.28.8, with zero backlog.

[Upstream's version-skew policy](https://kubernetes.io/releases/version-skew-policy/) permits kube-proxy up to three minor versions older than kube-apiserver, but never newer.
During a mixed-version API-server rollout, kube-proxy must satisfy that limit against every API server it can contact.
The August 28 one-minor lag was within support, not at its boundary.
Sync bootstrap manifests during the session that creates the backlog to keep networking components aligned and avoid accumulating skew.

On September 29, 2026, Talos moved from 1.13.9 to 1.14.2 on both clusters.
Homelab took six minutes, from 21:29 to 21:35 UTC, for one reboot.
VPS took seven minutes, from 21:36 to 21:43 UTC, to roll three machines.
Each cluster then had four pending bootstrap objects.
The sync moved CoreDNS from v1.14.6 to v1.14.7 and both Flannel images from 0.28.8 to 0.28.9.
It enabled Flannel's nftables setting, added Linux amd64/arm64 affinity to CoreDNS and kube-proxy, and requested 100m CPU and 50Mi memory for kube-proxy.
Kube-proxy remained v1.36.4.
Both syncs completed with zero backlog and all active workloads Running and Ready.
VPS retained `--iface-can-reach=10.0.0.1`, and a pod resolved and connected to a Service with its endpoint on another node.

Current bootstrap state, September 29, 2026: the Kubernetes sync moved only kube-proxy from v1.36.4 to v1.37.1 on both clusters, retaining CoreDNS v1.14.7 and Flannel 0.28.9 with `outofsync: 0`; the Kubernetes upgrades took five minutes on homelab (22:20–22:25 UTC) and approximately eight minutes on VPS (22:25–about 22:33 UTC).

Read the distinct changed lines to identify what differs across the backlog:

```bash
omnictl cluster kubernetes manifest-sync <cluster> 2>&1 | grep -E '^[-+][^-+]' | sort -u
```

This view discards the object each line belongs to.
Map each changed line back to its object in the full dry run before applying.
Applying restarts kube-proxy, the CNI and DNS.
The single-node homelab has a brief outage; VPS rolls across nodes.
Verify service networking after each sync.
Confirm that the generated VPS Flannel manifest retains `--iface-can-reach=10.0.0.1` before applying.

```bash
talosctl --context cynexia-vps -n ubuntu-16gb-fsn1-2 get manifests 05-flannel -o json | jq '.spec[] | select(.kind=="DaemonSet") | .spec.template.spec.containers[].args'
```

Do not run `talosctl get manifests -o yaml` without filtering.
The output embeds the cluster's bootstrap-token Secret.

## Recovering a bad upgrade

- Check `omnictl get etcdbackupstatus <cluster> -o yaml` before starting.
  Confirm an empty `error` and a recent `lastbackuptime`.
  The backup is the primary recovery path.
- Talos boots a new image once before making the bootloader change permanent.
  A node that fails verification and rejoining reverts automatically.
  `talosctl rollback` can revert a node that booted but broke workloads; Omni RBAC permission for it remains untested here.
- Kubernetes has no equivalent rollback or documented downgrade path, even if Omni lists a lower upgrade target.
- Wait while a node reports `Rebooting` or `Installing`.
  Read `omnictl machine-logs <machine-id>` if it stalls.
  Inspect the serial console if those logs do not explain the failure.
  Talos provides no SSH access.
- Do not delete machines at the infrastructure provider during a stalled upgrade.
  Do not add control-plane nodes to repair quorum.
  Do not run `kubectl delete node` against a control-plane node during a stalled upgrade.

## Advisory feeds

FreshRSS subscribes to these feeds in the `security` category.
Each returned a valid feed on August 26, 2026.

| Component | Feed | Content |
|---|---|---|
| Kubernetes | `https://kubernetes.io/docs/reference/issues-security/official-cve-feed/feed.xml` | RSS 2.0 vulnerability announcements |
| Talos Linux | `https://github.com/siderolabs/talos/releases.atom` | Release notes |
| cert-manager | `https://github.com/cert-manager/cert-manager/releases.atom` | Release notes |
| Traefik | `https://github.com/traefik/traefik/releases.atom` | Release notes |
| cloudflared | `https://github.com/cloudflare/cloudflared/releases.atom` | Release notes |

Four feeds contain release notes, including CVE fixes, so routine releases appear alongside security updates.
On August 26, GitHub's per-repository `/security/advisories.atom` and global `https://github.com/advisories.atom` returned HTTP 406 with empty bodies.
The `kubernetes-security-announce` Google Group had no working feed.
Its `groups.google.com/forum/feed/...` URLs returned 404, as did equivalent URLs for unrelated public groups.
The Kubernetes CVE feed supplies that coverage instead.

Published advisories are also available as JSON at `https://api.github.com/repos/<owner>/<repo>/security-advisories?state=published`.
FreshRSS can consume JSON with field mapping, but these sources would need authentication to avoid the anonymous 60-requests-per-hour limit.
The estate deliberately does not configure them.

## Hand-managed pins

Renovate's `kustomize` manager reads the VPS go-getter URL.
Other remote-base pins repeat versions inside URL paths, sometimes twice.
The estate uses manual bundle updates to avoid fragile URL parsing in a regex manager.

Step 3 of `.claude/skills/update-estate/SKILL.md` lists the files, upstream repositories and occurrences for each remote-base bump.
That inventory stays beside its only consumer to avoid maintaining two copies.

## Omni etcd backups

Automatic etcd backups are configured per cluster and stored in S3.
Omni's backend choice, `local` or `s3`, is fixed at initialization and cannot be changed through `omnictl`.

Check backup age at the start of each session:

```bash
omnictl get etcdbackupoverallstatus -o yaml     # configurationname, configurationerror, status
omnictl get etcdbackupstatus -o yaml            # per cluster: lastbackuptime, lastbackupattempt
```

Convert `lastbackuptime.seconds` from Unix seconds to a readable timestamp:

```bash
omnictl get etcdbackupstatus -o json | jq -r '"\(.metadata.id) \(.spec.lastbackuptime.seconds | todate)"'
```

Do not pass `-n ephemeral`.
The documentation places these resources in `ephemeral`; this instance returns them in `metrics`.

**Never run `omnictl get etcdbackups3configs`.**
It prints the Backblaze B2 access key and secret in plaintext.

Use the full cluster label when listing individual backups:

```bash
omnictl get etcdbackup --selector omni.sidero.dev/cluster=homelab
```

A bare backup query fails because the cluster selector is mandatory.

For template-managed clusters, the interval is `features.backupConfiguration.interval` in the `Cluster` document.
It accepts a Go duration string; `0` disables automatic backups.

```yaml
kind: Cluster
name: homelab
kubernetes:
  version: v1.36.4
talos:
  version: v1.14.2
features:
  backupConfiguration:
    interval: 1h
```

Template-managed clusters use `omnictl cluster template diff -f <file>` followed by `omnictl cluster template sync -f <file>`.
This estate has not adopted template management, as explained above.
The `-f/--file` flag belongs to each subcommand, not the parent command.
Put the verb before `-f`.

For raw cluster resources, `omnictl apply -f <file>` uses a lowercase structured interval on `Clusters.omni.sidero.dev`:

```yaml
metadata:
  namespace: default
  type: Clusters.omni.sidero.dev
  id: homelab
spec:
  backupconfiguration:
    interval:
      seconds: 3600
      nanos: 0
    enabled: true
```

The live resource includes `backupconfiguration.enabled: true`, which neither documented example shows.
Preserve that field when editing.
Both clusters had backups enabled at a one-hour interval on August 28, 2026.

Omni never deletes backup objects.
The bucket grows without bound unless storage-side lifecycle rules limit retention.
This repository neither configures nor checks those rules.

There is no automated `omni-etcd-backup-age` check.
The only available `omnictl` credential is a full-privilege operator identity.
Giving it to a pod would turn a Secret read into lifecycle control of both clusters.
Each session checks backup age manually instead.

## What the session does not cover

- No CVE scanner runs here; Keel, periodic sessions and advisory feeds provide partial coverage.
- No unattended manifest apply runs here.
  The MCP build-input group automerges and reaches the cluster through Keel's image update.
  An automatic applier would need permanent Kubernetes and 1Password credentials on a runner.
  It could not judge whether a diff reverts another branch's deployed work.
- Floating applications have no mandatory dump before each update.
  Nightly native dumps and restic backups are the accepted recovery floor.
- Restore drills remain manual, on the session's occasional checklist.
