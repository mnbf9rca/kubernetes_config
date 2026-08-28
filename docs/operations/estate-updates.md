# Estate updates

How every part of this estate gets patched, and what the periodic update session does.
The session itself is a repo skill — `.claude/skills/update-estate/SKILL.md`, invoked as `/update-estate`.
This document is the reference material that skill reads; it does not repeat the runbook.

The strategy behind it lives in the local-only design note `docs/superpowers/specs/2026-08-26-estate-update-strategy.md`, which is gitignored.

## Two modes, and nothing else

| Mode | Surface | Cadence | Watched by |
|---|---|---|---|
| Continuous | keel on both clusters (floating tags), `unattended-upgrades` on the hermes VM | Its own timer | `homelab-keel-fresh` and `vps-keel-fresh`, **uptime-kuma push monitors** ([uptime-kuma.md](uptime-kuma.md#push-monitors)) |
| The session | Every open Renovate pull request, the hand-managed kustomize pins, Talos and Kubernetes through Omni, the Hermes VM update runbook | Every 4 to 6 weeks | `estate-update` (45d / 7d) and `hermes-update` (10d / 4d), **healthchecks.io** checks, both pinged by hand from the laptop ([monitoring.md](monitoring.md#healthchecksio-checks)) |

Which signal lives in healthchecks.io and which in uptime-kuma, and why, is in [monitoring.md](monitoring.md#healthchecksio-checks); the names were kept the same across the move, so look in whichever of the two holds the signal a step names.

The rule that keeps them apart: **a floating tag means keel; a pinned tag means Renovate; never both.**
A `match-tag: "true"` annotation on a semver pin refreshes the digest only, so a pinned image carrying keel annotations is frozen while looking covered.

## When to run a session out of band

An advisory in the FreshRSS `security` category that names a component this estate runs triggers a session **now**, not at the next calendar slot.
That is the whole of the estate's vulnerability signal: Renovate emits none for container images, and no scanner runs here.
Cloudflare Access is the primary boundary, which is what makes a cadence-based answer proportionate — but only if an advisory actually shortens the cadence.

Components to match an advisory against: Talos, Kubernetes, cert-manager, Traefik, cloudflared, keel, InfluxDB, Grafana, PostgreSQL, restic, and every image under `homelab/health/`, `homelab/hindsight/`, `homelab/ops/` and `*/backup/`.

## The version ledger

Recorded at the end of every session.
A session that changes neither version still updates the "confirmed" date, so a stale date means a session was skipped.
The control-plane count is part of the record because it decides whether an upgrade is an outage or a roll.

| Cluster | Talos | Kubernetes | Control-plane nodes | Confirmed |
|---|---|---|---|---|
| homelab | 1.13.9 | 1.36.4 | 1 (`talos-5yn-s9u`) | August 28, 2026 |
| vps | 1.13.9 | 1.36.4 | 3 (`ubuntu-16gb-fsn1-2`, `ubuntu-4gb-fsn1-2`, `ubuntu-4gb-nbg1-1`) | August 28, 2026 |

Read the live values with:

```bash
omnictl get clusters -o json | jq '{id:.metadata.id, talos:.spec.talosversion, k8s:.spec.kubernetesversion}'
kubectl --context cynexia-homelab get nodes -o wide    # OS-IMAGE names the booted Talos
kubectl --context cynexia-homelab get nodes -l node-role.kubernetes.io/control-plane
kubectl --context cynexia-vps get nodes -l node-role.kubernetes.io/control-plane
```

When Omni's recorded version and the booted version disagree, an upgrade did not finish.
Resolve that before starting any other upgrade.

**Read the control-plane count from the table above before you plan an upgrade, and re-measure it rather than trusting your memory of it.**
The count decides the shape of the operation:

- **A single-node control plane makes the upgrade a total outage** on that cluster for the length of one reboot.
  Draining, rebooting and rejoining the only node takes the API server, etcd and every workload with it.
  Tell the operator before you start one.
- **A three-node control plane rolls one machine at a time**, and Omni waits for etcd health between them.
  Verify quorum between machines with `talosctl -n <node> etcd members` — three members, all healthy — before letting the next one go.

## Upgrading Talos and Kubernetes through Omni

**Ask Omni what it will allow before you plan anything.**
Omni refuses unsupported paths and publishes the permitted targets:

```bash
omnictl get talosupgradestatus <cluster> -o yaml        # .spec.upgradeversions = allowed Talos targets
omnictl get kubernetesupgradestatus <cluster> -o yaml   # same for Kubernetes
omnictl cluster kubernetes upgrade-pre-checks <cluster> --to <version>
```

Talos policy, from Sidero's own documentation: configuration migration is tested only between adjacent minor releases, so **upgrade to the latest patch of every intermediate minor** rather than jumping.
Omni enforces this and will require an intermediate hop.
A Talos upgrade does **not** move Kubernetes; the two are separate operations.

Sidero states no Kubernetes skew policy of its own — it points at the Talos support matrix for which Kubernetes versions each Talos release supports.
Upstream Kubernetes skew rules apply, and Sidero does not restate them, so do not quote Sidero for them.

The documented mechanism is the Omni web UI: **Clusters → the cluster → Update Talos**, and **Update Kubernetes**.
A cluster-template path exists (`talos.version` and `kubernetes.version` on a `kind: Cluster` document, applied with `omnictl cluster template diff -f <file>` then `omnictl cluster template sync -f <file>`), but this repo keeps no template file — `homelab/talos/` and `vps/talos/` hold machine config patches only.
Export one with `omnictl cluster template export <cluster> -o <file>` if you want a reviewable diff; otherwise the UI is the working path.

**Neither cluster can use that path as things stand.** Both were created in the web interface rather than from a template, which the cluster resource shows by carrying no template annotation, so `omnictl cluster template sync` is unavailable until template management is adopted.
Adopting it is a design decision and not a step in an update session, because the exported template inlines the cluster's config patches by `idOverride` — including the five `homelab/talos/machineconfig-patches/` files that `make apply-talos` already owns.
That would give those patches two writers, last write winning, with no guard between the two tools: the concurrent-writer failure this repo has already paid for once.
Taking it means deciding in the same change which tool owns the patches, and deleting or guarding the other; until somebody does, the web UI is the only upgrade path and Step 4 of the session needs the operator.

**`homelab/talos/` and `vps/talos/` are a subset of the live patch set, not an inventory of it.**
Omni is the system of record for machine config patches, and these trees hold only the ones this repository authors.
`make apply-talos` is push-only: it applies each file under `homelab/talos/machineconfig-patches/`, never enumerates what Omni already holds, and never deletes.
It also covers the homelab alone — there is no VPS equivalent, so the two files under `vps/talos/machineconfig-patches/` reach Omni only through a hand-run `omnictl apply`.
A patch created in the web interface, or one whose file was deleted here, therefore stays applied and invisible.
Omni's own patches are recognisable by the `omni.sidero.dev/system-patch:` label — the `400-<cluster>-control-planes-untaint` pair and the per-machine `900-cm-<machine>-kubernetes-upgrade` patches written by `KubernetesUpgradeStatusController` — and they must never be copied into a file here, because a repo copy would collide with a resource Omni rewrites.
The hand-made ones carry no such label, and those are the ones that need a decision.
`200-homelab`, which sets the cluster's pod and service subnets, was typed into the cluster-creation form and lived only in Omni until it was codified from the live cluster on August 28, 2026.
One is still outstanding: `500-7a4333c7-df30-4205-a022-fd93154da992` is a fossil of the pre-SSD kubelet self-bind on `/var/mnt/local-path-provisioner`, whose codified successor was deleted from this repository at `ea0a75c` and never deleted from Omni.
Nothing mounts at that path today, so it is inert — but a future user volume landing there would reinstate the `EBUSY` upgrade failure that `homelab/bootstrap/local-path/kustomization.yaml` records as fixed.
Deleting it is the operator's call: `omnictl delete configpatch 500-7a4333c7-df30-4205-a022-fd93154da992` restarts the kubelet, so it belongs in a maintenance window rather than in the middle of an update session.
Run `omnictl get configpatches` at the start of the next session and reconcile the list against these two trees; anything unaccounted for is either Omni's by its label or a decision waiting to be made.

Editing the `Clusters.omni.sidero.dev` resource directly to change a version is **undocumented**.
It is mechanically possible and it is not a supported path.
Do not.

**`talosctl upgrade-k8s` does not work against these clusters.**
Omni's RBAC denies the Talos-side Kubernetes proxy, so even `--dry-run` fails with `rpc error: code = PermissionDenied desc = not authorized`.
Plain Talos API calls through the same talosconfig succeed, so this is authorization, not a broken config.

## Bootstrap manifests

Omni never applies Kubernetes bootstrap manifest changes on its own — CoreDNS, kube-proxy, the CNI plugin and the bootstrap tokens — because doing so would overwrite hand edits.
The changes accumulate as a backlog and wait for review.

```bash
omnictl get kubernetesupgrademanifeststatus -o yaml     # .spec.outofsync = pending objects
omnictl cluster kubernetes manifest-sync <cluster>      # --dry-run defaults to TRUE: prints what it would do
omnictl cluster kubernetes manifest-sync <cluster> --dry-run=false   # applies
```

The UI equivalent is **Bootstrap Manifests** in the left navigation, which Omni surfaces after a Kubernetes upgrade completes and before the changes are applied.
Read the dry run in full and apply only what suits this cluster.

**A backlog here is not cosmetic, and the first session to read one found out why.**
Both clusters carried `outofsync: 21` with an empty `lastfatalerror` on August 28, 2026, accumulated before that session.
Reading the dry run showed it was not drift in annotations: kube-proxy was running **v1.35.3** on homelab and **v1.35.2** on the VPS against a control plane that had just moved to **v1.36.4**.
CoreDNS was on v1.13.2 and Flannel on v0.27.4.
Earlier Kubernetes upgrades had moved the control plane and left these behind, because Omni holds them back by design and nothing had ever applied them.

One minor of skew between kube-proxy and the API server is the edge of what upstream supports, so the next upgrade would have taken it out of support.
Both clusters were synced on August 28, 2026 and now report `outofsync: 0`, with kube-proxy v1.36.4, CoreDNS v1.14.6 and Flannel 0.28.8.

**Read the distinct changed lines rather than paging through the objects**, which is what turned a count that looked like drift into a version gap:

```bash
omnictl cluster kubernetes manifest-sync <cluster> 2>&1 | grep -E '^[-+][^-+]' | sort -u
```

That collapses the whole backlog to the set of things that actually differ, which is where the image versions are.
Then read the dry run in full before applying, because the one-liner discards the object each line belongs to.

So read the count as a version gap, not a queue of formatting changes, and sync it in the session that creates it.
Applying restarts kube-proxy, the CNI and DNS, so verify service networking afterwards rather than assuming: on the single-node homelab this is a brief outage, and on the VPS it rolls.

Do not run `talosctl get manifests -o yaml` unfiltered to inspect the sources: the output embeds the cluster's bootstrap-token Secret.

## Recovering a bad upgrade

- **Assert an etcd backup before you start.**
  `omnictl get etcdbackupstatus <cluster> -o yaml` must show an empty `error` and a recent `lastbackuptime`.
  The backup is the primary recovery path.
- **Talos rolls back.**
  Talos boots the new image once and only makes the bootloader change permanent after it verifies itself and rejoins, so a node that fails to boot reverts on its own.
  `talosctl rollback` reverts a node that booted but broke your workloads.
  Whether Omni's RBAC permits that command here is untested.
- **Kubernetes does not roll back.**
  There is no A/B equivalent and no documented downgrade path, though Omni's `upgradeversions` list may offer a lower version.
- **A node showing `Rebooting` or `Installing` is still working.**
  Wait before intervening, then read `omnictl machine-logs <machine-id>`, then the serial console.
  Talos allows no SSH access by design.
- **Never** delete machines out of band at the infrastructure provider, add control-plane nodes to repair quorum, or `kubectl delete node` a control-plane node during a stalled upgrade.

## Advisory feeds

Subscribed in FreshRSS under the category `security`.
Every URL below was fetched and returned a valid feed on August 26, 2026.

| Component | Feed | What it actually is |
|---|---|---|
| Kubernetes | `https://kubernetes.io/docs/reference/issues-security/official-cve-feed/feed.xml` | A purpose-built vulnerability feed (RSS 2.0, title "Kubernetes Vulnerability Announcements - CVE Feed") |
| Talos Linux | `https://github.com/siderolabs/talos/releases.atom` | Release notes |
| cert-manager | `https://github.com/cert-manager/cert-manager/releases.atom` | Release notes |
| Traefik | `https://github.com/traefik/traefik/releases.atom` | Release notes |
| cloudflared | `https://github.com/cloudflare/cloudflared/releases.atom` | Release notes |

**Four of the five are release feeds, not advisory feeds, and that is the honest state of the world.**
GitHub publishes no per-repository security-advisory Atom feed: `/security/advisories.atom` answers 406 with an empty body, and so does the global `https://github.com/advisories.atom`.
These projects announce CVE fixes in their release notes, so the coverage is real — you will read every routine release alongside the security ones.

The `kubernetes-security-announce` Google Group has **no working feed**.
Every `groups.google.com/forum/feed/...` form answers 404, and so does the same form for unrelated public groups, so Google has retired the endpoint rather than restricted this group.
The Kubernetes CVE feed above is the substitute.
Do not spend a session rediscovering this.

Genuine advisories exist as JSON at `https://api.github.com/repos/<owner>/<repo>/security-advisories?state=published`.
FreshRSS can consume JSON sources, but each needs field mapping and an authenticated token to stay inside the 60-requests-per-hour anonymous limit.
Not done, deliberately.

## Hand-managed pins

Renovate's `kustomize` manager reads the VPS go-getter URL.
The remaining kustomize base pins repeat the version inside the URL path, sometimes twice, and a regex manager for them is exactly the fragile parsing this estate refuses.
The session bumps them by hand.

The inventory — which files, which upstream repository, and how many occurrences each bump touches — is the work list in `.claude/skills/update-estate/SKILL.md`, Step 3.
It lives there rather than here because it is consulted at exactly one step of one session, and one copy cannot go stale against the other.

## Omni etcd backups

Automatic etcd backups are configured per cluster and stored in S3.
Omni's backend choice (`local` or `s3`) is fixed at Omni initialization and cannot be changed from `omnictl`.

**Assert the age at the start of every session:**

```bash
omnictl get etcdbackupoverallstatus -o yaml     # configurationname, configurationerror, status
omnictl get etcdbackupstatus -o yaml            # per cluster: lastbackuptime, lastbackupattempt
```

`lastbackuptime.seconds` is raw Unix seconds, which no one can judge by eye.
Convert it:

```bash
omnictl get etcdbackupstatus -o json | jq -r '"\(.metadata.id) \(.spec.lastbackuptime.seconds | todate)"'
```

Do not pass `-n ephemeral`.
The documentation places these resources in `ephemeral`; this instance returns them in `metrics`.

**Never run `omnictl get etcdbackups3configs`.**
It prints the Backblaze B2 access key and secret in plaintext.

To list individual backups, the cluster selector is mandatory and must use the full label key:

```bash
omnictl get etcdbackup --selector omni.sidero.dev/cluster=homelab
```

A bare `omnictl get etcdbackups` fails with `cluster ID must be specified in query`, and `--selector cluster=homelab` fails with `unsupported label query term`.

**Where the interval lives.**
For a cluster managed by a cluster template, it is `features.backupConfiguration.interval` in the `Cluster` document — a Go duration string, where `0` disables automatic backups:

```yaml
kind: Cluster
name: homelab
kubernetes:
  version: v1.36.0
talos:
  version: v1.13.8
features:
  backupConfiguration:
    interval: 1h
```

Applied with `omnictl cluster template diff -f <file>` then `omnictl cluster template sync -f <file>`.

The verb comes **before** `-f`.
`-f/--file` is a flag on the subcommands (`diff`, `sync`, `render`, `validate`), not on the parent command, so putting the file first and the verb last fails with an unknown-shorthand error.

For a cluster managed as a raw resource, the equivalent is lowercase and structured, on the `Clusters.omni.sidero.dev` resource, applied with `omnictl apply -f <file>`:

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

The live resource carries `enabled: true` inside `backupconfiguration`, which appears in neither documented example.
Do not drop it when hand-editing.

Both clusters were confirmed enabled at a 1-hour interval on August 28, 2026.

**Omni never deletes objects from the backup bucket.**
The bucket grows without bound unless a lifecycle rule is set on the storage side.
That rule is not this repo's to apply, and nothing here checks it.

There is deliberately **no automated `omni-etcd-backup-age` check**.
`omnictl`'s only credential is a full-privilege operator identity, and a pod holding it would escalate any Secret read into lifecycle control of both clusters.
The session asserting the age at its start is the compensating control.

## The Hermes VM update step

There is no updater on the VM.
The step is to **follow `docs/operations/hermes-vm-updates.md` end to end** — preconditions, change analysis, the detached update, verification, the report ping, and rollback if verification fails.
The runbook is written for an agent or the operator to execute with the session open, because `hermes update` sometimes carries a step that needs judgement, and its preconditions are the only gate on this step: the skill adds none of its own.

The runbook's report step pings `hermes-update`, on success only.
Why that check is separate from `estate-update`, and what to read when it goes red, is in [monitoring.md](monitoring.md#healthchecksio-checks).

## What the session does not cover

- **No CVE scanning.**
  Compensated by keel cadence, session cadence and the advisory feeds above.
  A gap, documented rather than papered over.
- **No automated applies.**
  Nothing merges or applies unattended.
  An auto-applier would need a permanent kubeconfig and a 1Password token on an always-on runner, and it could not judge whether a diff line reverts another branch's deployed work.
- **Keel-managed SQLite applications get no pre-update dump.**
  The nightly restic sweep is the accepted floor.
- **Restore drills stay manual**, on the session's occasional checklist.
