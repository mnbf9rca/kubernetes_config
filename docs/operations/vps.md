# VPS cluster (Phase 2)

Public-internet-facing three-node Talos control plane on Hetzner for personal web services.
Kubectl context: `cynexia-vps`.
Manifests live in `vps/`.

## Shape

| Aspect | Detail |
|---|---|
| Host | Three Hetzner VMs, all control-plane, managed by the same Omni instance as homelab (cluster name `vps`): one CX43 in `fsn1` (`ubuntu-16gb-fsn1-2`, the storage node) plus two CX23s added August 28, 2026 — `ubuntu-4gb-fsn1-2` in `fsn1` and `ubuntu-4gb-nbg1-1` in `nbg1`. The Hetzner console shows all three in the same placement group. Shared Hetzner Private Network `10.0.0.0/24`; the nodes report internal IPs `10.0.0.2`, `10.0.0.3` and `10.0.0.4` |
| Storage | One 80 GB Hetzner Cloud Volume, attached to the CX43 only, as a Talos user volume at `/var/mnt/data`; local-path-provisioner points there. See "Storage is single-node on purpose" below |
| Scheduling | Workloads run on all three nodes. Omni's system patch `400-vps-control-planes-untaint` sets `cluster.allowSchedulingOnControlPlanes: true` and is labelled to the `vps-control-planes` machine set, so every control plane inherits it — there is no `NoSchedule` taint to remove when a node joins |
| Network | No public :80/:443 on any node; the Hetzner Cloud Firewall drops **inbound** public traffic and leaves egress open. Flannel's VXLAN rides the private network rather than the public interface — see "Flannel rides the private network" below |
| Ingress | `cloudflared` tunnel only (named tunnel `cynexia-vps`). No Traefik, no cert-manager, no MetalLB, no NFS CSI |
| TLS / auth | Terminated at the Cloudflare edge. Cloudflare Access with email-OTP in front of every hostname |
| Domain | `*.cynexia.com` (Cloudflare-hosted zone). Homelab's `cynexia.net` is separate and unrelated |
| Namespaces | `vps` for all workloads (PSA `baseline`), plus `backup` (PSA `privileged`, hostPath), `keel`, and `ops` (PSA `baseline`, one CronJob — see below) |
| Secrets | 1Password `VPS` vault, referenced via `VPS_*` / workload-specific vars in `.env.tpl` |
| Image updates | keel runs here (`vps/bootstrap/keel/`) and workloads carry the standard keel annotation set, except keel itself, which is digest-pinned and Renovate-bumped (see below) |
| Apply | `make apply-vps`, gated by `check-vps-context` |

The Talos user-volume patch (`vps/talos/machineconfig-patches/400-vps-user-volume-data.yaml`) selects the Cloud Volume by **size bracket** (70–100 GB), because the boot disk and the Cloud Volume both report `transport=virtio` and can't be distinguished by transport alone.
It is labelled `omni.sidero.dev/cluster-machine` to the one machine that has a Cloud Volume attached: cluster-scoped, it would push a disk selector at the two CX23s that matches nothing on their hardware.
Note there is no `make` target for VPS Talos patches — apply them with `omnictl apply -f <file>` directly.

Fresh Hetzner Cloud Volumes ship pre-formatted and Talos refuses to provision over them; wipe first with `talosctl wipe disk <dev> --method FAST`.
Boot disks arrive in the same state — see "Adding a control-plane node" at the end of this document.

### Image updates and keel

keel is digest-pinned and carries no keel annotations of its own.
A self-updating controller holding cluster-wide read **and write** across every workload kind — its ClusterRole grants `get, delete, watch, list, update` on Deployments, DaemonSets, StatefulSets, ReplicaSets, ReplicationControllers, Pods, Jobs and CronJobs — is the one component where an unattended upstream tag change is a security event rather than a convenience, so its bump belongs in a reviewed pull request rather than a six-hour poll.

Renovate has reached this cluster since 2026-08-26, when `renovate.json` gained a `/^vps/.+\.ya?ml$/` pattern alongside the homelab one, so keel's bump here arrives as a pull request like every other pinned image in `vps/`.
`check-renovate-scope-vps` runs in the `diff-vps`/`apply-vps` preflight and fails the apply if that scope is ever lost.
`vps/bootstrap/keel/**` sits on the `pinDigests: false` packageRule: the image is already pinned by tag and digest by hand, so there is nothing for Renovate to add.

Its RBAC was trimmed on August 26, 2026 (PR #68): no `secrets` rule, no `pods/portforward`.
Verify keel's permissions with a SelfSubjectAccessReview issued with keel's own ServiceAccount token from inside the cluster — `kubectl auth can-i --as=` is meaningless through the Omni proxy, which ignores impersonation and answers as the caller.

## Storage is single-node on purpose

Only the CX43 has a Hetzner Cloud Volume, so `/var/mnt/data` exists on one node and every byte the nightly restic job backs up lives there.
Three mechanisms keep it that way, and they are not interchangeable — read all three before changing any of them.

1. **Existing volumes pin themselves.**
   local-path-provisioner writes `spec.nodeAffinity.required` onto every PersistentVolume it creates, naming the node it provisioned on.
   The scheduler honours it.
   That is why `uptime-kuma`, `umami`'s postgres, `karakeep` and `meilisearch` cannot drift to another node, and why **none of them carries a `nodeSelector` or an affinity rule**.
   Adding one would be redundant and would hide the real mechanism.
   Verify with:

   ```bash
   kubectl --context cynexia-vps get pv \
     -o custom-columns='NAME:.metadata.name,CLAIM:.spec.claimRef.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values'
   ```

   Verified on the three-node cluster on August 28, 2026: all nine PVs then in existence read `[ubuntu-16gb-fsn1-2]`, and every stateful pod stayed on that node through a reboot and a drain of a different one.

2. **New volumes are refused elsewhere.**
   `vps/bootstrap/local-path/kustomization.yaml` names `ubuntu-16gb-fsn1-2` explicitly in `nodePathMap` and gives `DEFAULT_PATH_FOR_NON_LISTED_NODES` an empty `paths` list.
   That refusal rests on the **code**, not on upstream's README, which documents empty `paths` only for an explicitly listed node: in local-path-provisioner v0.0.31, `provisioner.go:236` falls back to the DEFAULT entry's paths for a non-listed node and `provisioner.go:243` returns `no local path available on node %v` when that list is empty, before any helper pod is created.
   Re-check both call sites on a provisioner upgrade.
   With the StorageClass's `WaitForFirstConsumer` binding mode, a PVC whose consumer lands on another node stays `Pending` with a provisioning error rather than binding to unbacked-up ephemeral disk.

3. **The backup job is pinned.**
   `vps/backup/restic-cronjob.yaml` carries `nodeSelector: {kubernetes.io/hostname: ubuntu-16gb-fsn1-2}`.
   Its `hostPath` mount already uses `type: Directory`, so an unpinned pod on the wrong node would fail to start rather than back up an empty tree — the selector removes the nightly failure, the hostPath type is what makes the failure loud.
   Keep both.

If the Cloud Volume ever moves to a different node, items 2 and 3 and the ConfigPatch scope all name that node by hand and must change together.

## Flannel rides the private network

The Hetzner Cloud Firewall drops **inbound** public traffic, UDP 4789 included, and every node's default route leaves through its public interface.
Flannel's default interface selection therefore built its VXLAN mesh between public addresses, where the firewall silently blackholed it: pods on one node could not reach a Service backed by a pod on another, and CoreDNS lookups from the two new nodes timed out with nothing logging an error.
`vps/talos/machineconfig-patches/300-vps-flannel-private-iface.yaml` fixes it by passing flannel `--iface-can-reach=10.0.0.1`, which resolves to whichever interface reaches the private network's gateway — a name-independent selector, because Hetzner does not name that interface consistently across server types.

**The patch alone did not move the running cluster.**
Omni pushed the setting into all three machine configs within seconds on August 28, 2026, but Talos does not re-render its bundled flannel manifest over an existing DaemonSet, so the live `kube-flannel` DaemonSet kept its old arguments and was hand-patched with the same single argument that day.
The ConfigPatch is what makes the setting survive a node rebuild or a cluster recreate; the hand-patch is what made it true on the running cluster.

**The two must agree, and nothing checks that they do.**
After any Talos upgrade or node rebuild, re-read the live DaemonSet's arguments and re-apply the argument if they have diverged — the exact re-patch command is in the header comment of the patch file, which is also where the full reasoning lives.

```bash
kubectl --context cynexia-vps -n kube-system get ds kube-flannel \
  -o jsonpath='{.spec.template.spec.containers[0].args}'
```

## Workloads

| Service | Hostname | DB |
|---|---|---|
| freshrss | `rss.cynexia.com` | sqlite |
| uptime-kuma | `uptime.cynexia.com` | sqlite |
| changedetection (+ sockpuppetbrowser) | `watch.cynexia.com` | sqlite / `/datastore` |
| umami | `analytics.cynexia.com` | dedicated postgres |
| n8n | `n8n.cynexia.com` | sqlite |
| karakeep (+ meilisearch) | `keep.cynexia.com` | sqlite |
| homelab-proxy | none — ClusterIP only | none |

Every container here carries readiness and — where a restart is a safe remedy — liveness probes.
Per-service targets and the reasoning behind each, including the deliberately shallow ones, are in [monitoring.md](monitoring.md#vps-cluster).

`make route-vps-dns` reads the hostname list out of `vps/bootstrap/cloudflared/cloudflared.yaml` (that ConfigMap is the single source of truth for hostname → Service routing) and upserts a CNAME per hostname onto the current tunnel UUID.
Run it after adding a hostname, and after any full cluster rebuild.

### The `ops` namespace

`vps/ops/` is the mirror of `homelab/ops/`, and holds one CronJob: **`keel-fresh`**, at 07:45Z daily.
It makes one request to keel's own `/metrics` — a single ClusterIP endpoint, `keel.keel.svc.cluster.local:9300`, reached across the namespace boundary from `ops` — and pushes the `vps-keel-fresh` uptime-kuma monitor.
It is the only thing that would notice this cluster's keel had stopped polling registries; keel's own probes hit `/healthz`, which stays green while the poll goroutine is dead.
Verdict enum, the image floor and why there is no `/start`: [monitoring.md](monitoring.md#the-keel-dead-mans-switch).

It has no hostname and no database, which is why it is not a row in the table above.
It holds no ServiceAccount and no RBAC; its only peers are that ClusterIP and `uptime.cynexia.com`.
Its two integers of state live on a 32Mi `local-path` PVC, `keel-fresh-state`, so its pod carries a `nodeSelector` for `ubuntu-16gb-fsn1-2` under "Storage is single-node on purpose" above.

It is a deliberate **copy** of the homelab tree rather than a shared one, script file included: a homelab pod holding a VPS kubeconfig would be a credential crossing a cluster boundary to save one file, and kustomize will not read a generator source outside its own root in any case.
The two image floors differ because the two estates do.
**Edit them together.**

### Cloudflare Access bypasses

Public endpoints serve callers that cannot authenticate: strangers' browsers running the umami beacon, third-party webhook senders, WebSub hubs, and — since August 26, 2026 — jobs inside either cluster driving an uptime-kuma push monitor.
They must be Access-bypassed or they break.
**Five** path-scoped Access apps carry the shared `bypass` policy, covering **ten** globs in total:

| Access app | Destinations | Added |
|---|---|---|
| `umami scripts` | `analytics.cynexia.com/script.js`, `/api/send`, `/api/send/*` | — |
| `n8n webhooks` | `n8n.cynexia.com/webhook/*`, `/webhook-test/*` | — |
| `freshrss api` | `rss.cynexia.com/api/*`, `/p/api/*` | — |
| `karakeep api` | `keep.cynexia.com/api/*` | — |
| `uptime-kuma push` | `uptime.cynexia.com/api/push/*`, `/api/push` | August 26, 2026 |

`bypass` is the only Access action that admits an unauthenticated request; an `Allow` policy with `Everyone` still serves a login page.
FreshRSS and karakeep enforce their own API credentials behind these globs.
The umami send and n8n webhook endpoints are open by design.

A bypass path glob of `/foo/*` does **not** match the bare path `/foo` — add both the exact and the wildcard destination.

The `uptime-kuma push` app is the only one serving this estate's own jobs rather than strangers.
A push monitor is driven from inside a cluster by a CronJob holding no Access credential, so without the bypass the edge answers 302, `curl -f` fails, and every push monitor sits permanently DOWN over healthy jobs — check this app first when debugging one.
Only the push path is opened: `/api/push/<token>` accepts a heartbeat and exposes no dashboard, no monitor list and no settings.
It reuses the shared bypass policy rather than carrying its own.
The verification performed at creation, and the proof commands to repeat after any Access change: [uptime-kuma.md](uptime-kuma.md#the-push-path-is-bypassed-at-the-edge).

These apps are also why the `rss.cynexia.com` and `Karakeep` uptime-kuma monitors need no service-token headers: their URLs resolve to the path-scoped app, not the root one ([uptime-kuma.md](uptime-kuma.md#monitor-list)).

### cloudflared runs two replicas

`cloudflared` is the cluster's only ingress, so a single replica meant every hostname went dark for the length of a node reboot.
Since August 28, 2026 it runs `replicas: 2` with `requiredDuringSchedulingIgnoredDuringExecution` pod anti-affinity on `kubernetes.io/hostname`, a surge-only rollout (`maxSurge: 1`, `maxUnavailable: 0`), and a `minAvailable: 1` PodDisruptionBudget.
Both pods share the one named tunnel and the one credentials file, which is Cloudflare's documented pattern for connector redundancy.

The anti-affinity is `required` rather than `preferred` deliberately: a `preferred` rule lets both replicas land on one node under scheduling pressure, reproducing the single point of failure while the Deployment still reports 2/2 ready.

Both halves of that were exercised on August 28, 2026 and every one of the 309 one-second probe samples returned 200.
A `talosctl reboot` of a replica's node makes no eviction call, so the PodDisruptionBudget is never consulted; what happened instead is that Talos stopped the pod gracefully, it went `Succeeded`, and the ReplicaSet placed a replacement on the third node within about ninety seconds — the 300-second `node.kubernetes.io/unreachable` toleration never came into it, because the node shut down cleanly rather than disappearing.
A `kubectl drain` of a different replica's node is the case that does exercise the budget: the eviction went through the API, the budget held the surviving replica serving, and required anti-affinity left exactly one node the replacement could land on.
Terminated pods from a reboot linger in `Completed` rather than being cleaned up, so a stale `0/1 Completed` cloudflared pod beside two `Running` ones is expected and is not a fault.

To verify the tunnel survives a node reboot, probe an **Access-bypassed** path — a protected hostname answers `302` from the Cloudflare login page whether the origin is alive or dead:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://analytics.cynexia.com/script.js
```

### FreshRSS WebSub push

FreshRSS builds its WebSub callback URL from `base_url` in `data/config.php`.
That file lives on the `freshrss-data` PVC rather than in this repo, and the image reads the `BASE_URL` environment variable only during first install, so setting it on the Deployment has no effect on an existing volume.
Change it with the CLI:

```bash
kubectl --context cynexia-vps -n vps exec deployment/freshrss -c freshrss -- \
  php /var/www/FreshRSS/cli/reconfigure.php --base-url=https://rss.cynexia.com
```

With an `http://` value, Cloudflare answers each hub callback with a 301 to the HTTPS URL.
Verification survives that: hubs follow the redirect for the `GET`, and Google's FeedFetcher did.
Whether *delivery* survives it is unknown — a hub is far less likely to replay a `POST` body across a redirect, and no push delivery has ever succeeded on this instance.
The value was corrected on August 20, 2026; issue #29 tracks whether that was the cause.

#### What `"error"` in `!hub.json` actually means

It is neither a failure counter nor a current-state signal.
`p/api/pshb.php` sets it to `true` when a subscription is verified, with the comment *"Do not assume that WebSub works until the first successful push"*, and clears it only after a delivery that updates at least one feed for at least one user.
So:

> `"error": true` means **no push has ever been successfully processed for this feed**.

Two things follow.
A quiet feed and a broken one look identical.
And `FreshRSS_Feed::pubSubHubbubPrepare()` re-subscribes any feed whose state has `error` set and whose `lease_start` is over 23 hours old — so a feed that has never received a push re-subscribes **daily, permanently**, regardless of how far its lease is from expiring.
That accounts for 3,040 subscribe requests across twelve feeds in six months here.
See issue #29.

Grepping for `pubSubHubbubError()` is misleading: that method is only ever called with `true`, which makes the flag look like a one-way latch.
The clearing path writes the array directly in `p/api/pshb.php` and does not go through the method.

**Subscription health is `lease_end` in the future.**
Run:

```bash
./scripts/freshrss-websub-status.py
```

It reports both columns per feed — whether the lease is live, and whether that feed has ever received a push — and exits non-zero if any subscription is not live, so it works as a check.
It reads the state inside the pod and prints only derived status: never print `!hub.json` yourself, because each file holds that feed's callback secret.

As of August 20, 2026 it reports 12 of 12 live and 0 of 12 ever pushed.

A second-order check is the outbound subscribe log, `data/users/_/log_pshb.txt`.
Tally the trailing HTTP status with `awk '{print $NF}' … | sort | uniq -c`: 2xx is success.
As of August 20, 2026 that log showed 2,959 successes against 69 transient 5xx over six months, and no redirects at all.

Neither check proves end-to-end delivery, only subscription.
Delivery is a `POST` to `/api/pshb.php` in the pod log, and it appears only when a subscribed feed publishes something.

The callback path `/api/pshb.php` falls under the `freshrss api` Access bypass, so it stays publicly reachable, and under the zone rate limiting rule of 50 requests per 10 seconds per IP, far above hub delivery volume.

### The claude.com/blog feed (HTML+XPath scrape)

`claude.com/blog` has no RSS/Atom feed or JSON API (Webflow static site), so it is a native FreshRSS **HTML+XPath** feed (feed id 132, kind 10, user `ruined0346`).
Config lives in the FreshRSS sqlite DB on the PVC (restic + sidecar backed up), not in git; it was imported with `cli/import-for-user.php` using `frss:`-namespace OPML attributes.

- **Egress quirk:** Cloudflare's "Just a moment…" challenge fires on the workstation IP/UA but **not** on the VPS egress, so FreshRSS's own fetch gets clean HTML.
  Verify any "is it blocked?" question from inside the pod, not from a laptop.
  RSSHub was therefore unnecessary (PikaPods fallback exists; key at `op://VPS/RSSHub/api_key`).
- Selectors: item `//div[contains(concat(' ',@class,' '),' marquee_cms_blog_list_item ')]`, title `descendant::h2`, uri `descendant::a[contains(@class,'clickable_link')]/@href`, timestamp `descendant::div[contains(@class,'u-text-style-caption')]`.
  Full-article content via `pathEntries` CSS `.blog_post_content_wrap`.
- **The time format MUST be `!F j, Y` (leading `!`).**
  Without the `!`, `DateTime::createFromFormat` fills omitted time fields with the *current* time, so every poll restamps each post, the `sha1:link_published_title` dedup key churns, and duplicates multiply (hit 560 once).
  Also keep `unicityCriteria: link` + `unicityCriteriaForced: true` in the feed attributes.
  Verify any dedup change by running `cli/actualize-user.php` twice — the second run must report 0 new.
  (BST dates render 23:00 the prior day; cosmetic.)
- Only the ~10 newest posts appear — older ones are JS-paginated and unreachable to a non-browser scraper.

### Browser backend for changedetection

Use `dgtlmoon/sockpuppetbrowser`.
`browserless/chrome:latest` is the deprecated v1 line and leaks CDP sessions under modern Playwright.

### Residential egress through the homelab

Several watched sites answer a fetch from Hetzner's address ranges with 403, so those watches leave from the operator's home connection instead.

The chain is: changedetection — or chrome inside `sockpuppetbrowser`, for a Playwright watch — dials the `homelab-proxy` Service on port 8888; that pod runs `cloudflared access tcp`, which presents a Cloudflare Access service token and reaches `proxy.cynexia.com` on the homelab's `cynexia-health` tunnel; the tunnel's connector dials tinyproxy in the homelab `proxy` namespace, which opens the outbound connection over the home connection's default route.
For an HTTPS watch the TLS session is end to end between changedetection and the target site, so no hop in the chain sees plaintext.

Assign the proxy per watch, in changedetection's own proxy settings, as an entry named `homelab`:

```
http://homelab-proxy.vps.svc.cluster.local:8888
```

Do **not** set `HTTP_PROXY`/`HTTPS_PROXY` on changedetection.
An environment proxy routes every fetch through the cross-cluster chain, including the fetches that already work directly, which is slower and has more ways to fail.

A watch with the proxy assigned **errors** when any pod in the chain is down; it does not fall back to direct egress.
Unproxied watches are unaffected, so proxied-only failures point at the chain rather than at the internet.
Recovery is `kubectl rollout restart` on the pod the error points at: `deploy/homelab-proxy` in `vps`, `deploy/cloudflared` in the homelab's `health` namespace, or `deploy/tinyproxy` in the homelab's `proxy` namespace.
Every proxied watch failing at once, with unproxied ones fine, is the Access service token instead — read the `homelab-proxy` pod log, which records the refusal on every dial.

The Access application is the whole gate, and it fails open: tinyproxy authenticates nobody, so a deleted or disabled application publishes an open HTTP proxy egressing from the home address rather than closing the path.
The `proxy.cynexia.com` uptime-kuma monitor exists to detect exactly that — see [uptime-kuma.md](uptime-kuma.md#monitor-list).

Stale proxy references hide in `/datastore`, inside both the watch entries and `changedetection.json`, and a URL-grep misses them because the compose-era proxy URL was a sibling container name rather than a hostname.

## Database shape

Per-service sqlite, except umami which needs postgres.
A shared postgres was researched and rejected: karakeep is sqlite-only (karakeep issue #1782), uptime-kuma v2 supports only sqlite/MariaDB (issue #5674), and the remaining consolidation saving did not justify the upgrade-coupling cost.

`N8N_ENCRYPTION_KEY` is load-bearing — it was extracted from the old n8n container during the rebuild and n8n credentials are unreadable without it.

## Backups

Separate B2 bucket and separate restic repo from homelab.
The restic CronJob runs at 04:00 UTC and backs up `/var/mnt/data/local-path-provisioner` by hostPath, with the same 7 daily / 4 weekly / 6 monthly retention as homelab, and `--group-by paths`.

That hostPath exists on the storage node only, so the job carries a `nodeSelector` naming `ubuntu-16gb-fsn1-2` — one of the three mechanisms in "Storage is single-node on purpose" above, and the reason a three-node cluster still backs up the right tree.

That flag is load-bearing: `restic forget` groups by host+paths by default, and every CronJob pod has a unique hostname, so each nightly snapshot formed a group of one and the policy kept all of them.
Verified on homelab 2026-08-20 — 137 snapshots in 137 groups across 131 hostnames, nothing ever pruned since the backup system was built.
The image is pinned to `restic/restic:0.17.3` (was `:latest` — an unpinned backup tool is a silent-change surface on the one job you cannot re-run).

Consistency sidecars run alongside the app containers: sqlite quiesce for n8n / freshrss / karakeep / uptime-kuma, and `pg_dumpall` for umami's dedicated postgres.
Each refreshes a `*.restic` snapshot every 12h.

**None of the five sidecars carries a probe**, and that is deliberate: any failing probe on a sidecar takes the *application* offline (readiness directly, liveness via CrashLoopBackOff → EndpointSlice), so a backup fault would cost you the service.
A freshness liveness probe existed here briefly and was removed; the full reasoning, which reverses what the original spec endorsed, is in [monitoring.md](monitoring.md#why-the-sidecars-have-no-probes).

Instead each loop runs under `set -u` (**not** `set -e` — exiting is the outage path), logs failures to stderr, backs off 300 s and retries, publishes atomically via a `.tmp` plus `mv`, and — for the four sqlite ones — refuses to publish a snapshot whose `sqlite_master` count is zero, so a truncated source can no longer publish a fresh, valid, empty database. umami's `pg-dump-sidecar` follows the same shape.
**These sidecars fail by logging, not by exiting**; their restart counts stay at zero and the alarm comes from the restic gate.

The restic job runs a **backup verification gate** after the backup.
The authoritative half is an expected-set assertion — each known snapshot present and <15h old, named app by app, and for FreshRSS a sibling snapshot per *user DB* rather than the newest of a glob.
A broad sweep for other stale `*.restic` files runs alongside it but is **advisory only**, so one orphaned PV directory cannot pin the gate permanently red.
Together that turns a silently dead sidecar — or one deleted from the manifest, or an empty/unmounted volume — into a backup alert rather than years of backing up a stale or absent copy.

**A passing gate prints nothing.**
Its counts go into the healthchecks.io ping body and its findings to stderr, so on a clean run the pod log simply ends at `==> backup verification gate` and the pass signal is the job's exit status, not a line in the log.
A hand-triggered run on August 28, 2026 confirmed that shape.

**Adding a sqlite-backed service means adding its snapshot to that list.**
The gate proves a snapshot exists, is fresh and has a schema; it does not prove the contents are complete.
That, and why the gate runs after rather than before the backup, are in [monitoring.md](monitoring.md#the-backup-verification-gates).

The job pings healthchecks.io on start and on exit code, and sets no `terminationGracePeriodSeconds` — busybox `ash` as PID 1 never forwards SIGTERM to restic, so the old 120 s grace delivered nothing and only slowed teardown; a lock left behind by a killed run is cleared by `restic unlock` at the head of the next one.
Details, and the reason the shell is chained with `&&` rather than `set -e`, are in [monitoring.md](monitoring.md#the-restic-ping-wrapper).

## External monitoring

uptime-kuma at `uptime.cynexia.com` is layer 3 of the detection stack.
Its monitors are **created by hand in the UI** — v2 has no supported programmatic path — and are documented monitor-by-monitor in [uptime-kuma.md](uptime-kuma.md), including the Cloudflare Access trap (a monitor that follows the Access 302 reports UP while the origin is dead) and the healthchecks.io dead-man's-switch that watches uptime-kuma itself.

Hand-created monitor config lives in `kuma.db`, which the quiesce sidecar snapshots and restic backs up nightly — so a cluster rebuild restores the monitors rather than requiring them to be retyped.

## Adding a control-plane node

Hetzner Cloud cannot mount a custom ISO, so a machine reaches Talos by having the Omni-generated raw image written over its boot disk from the Hetzner rescue system.
That image carries the SideroLink join configuration, so the machine registers itself with Omni on first boot and there is no join token to paste.

1. `omnictl download hcloud --arch amd64 --output /tmp/omni-media`.
   The argument matches the media's **profile** (`hcloud`) or its **name** (`Hetzner Cloud (amd64)`) — never its resource ID `hcloud-amd64.raw.xz`, which matches nothing.
   What lands in the output directory is named for the Omni instance and the Talos version, not for the profile: on August 28, 2026 it was `hcloud-amd64-omni-cynexia-1.13.8-88ace5.xz`.
   Glob for it rather than assuming a name — `IMG=$(echo /tmp/omni-media/hcloud-amd64-*.xz)` — and use that variable in step 3.
   `omnictl download` is deprecated in favour of `omni media download <preset-name>`; it still works today, so prefer the successor if the local CLI offers it.
   The `--talos-version` default tracks the Omni instance; check it matches `omnictl get clusters`.
   Add `--use-siderolink-grpc-tunnel` if the machine's network blocks UDP — that is an image-build option, so needing it later means re-imaging.
2. In the Hetzner Cloud Console, open the server, click the **Rescue** tab, then **Enable rescue & power cycle** with **Operating System** `linux64`.
   Copy the root password — it is shown once.
   Rescue is one-shot: the next reboot boots from disk.
3. In the rescue shell run `lsblk -o NAME,SIZE,TYPE,MODEL` first.
   **Stop unless the output shows exactly the one boot disk you expect and nothing else** — a CX23 shows a single `sda` of roughly 40 GB with no `sdb`.
   The commands below write to `/dev/sda` unconditionally, and if a Cloud Volume is attached, `dd` over the wrong device is unrecoverable data loss on that volume.
   Then `wipefs -a /dev/sda`, `sgdisk --zap-all /dev/sda`, `xz -d -c "$IMG" | dd of=/dev/sda bs=4M`, `sync`, `reboot`.
   **The two wipe commands are not optional.**
   Fresh Hetzner disks arrive pre-formatted, and the backup GPT header at the end of the disk survives a raw-image write — that stale header is what makes Talos refuse to provision.
   The rescue system runs from RAM, so streaming the image (`xz -d -c "$IMG" | ssh root@<ip> 'dd of=/dev/sda bs=4M'`) is preferable to `scp`-ing it into the rescue tmpfs first.
4. Watch `omnictl get machines` until the machine appears with `CONNECTED true`.
5. In the Omni UI: **Clusters** → **vps** → **Cluster Overview** → **Cluster Scaling**, tick the machine, click **ControlPlane**, click **Add Machines**.
6. Wait with `omnictl cluster status vps --wait 15m`.
7. Confirm pod-to-pod traffic actually crosses to the new node before trusting it — a Service backed by a pod on another node, or a CoreDNS lookup from a pod on the new one.
   A broken VXLAN mesh reports no error anywhere; see "Flannel rides the private network" above for the failure this cluster hit and the argument that fixes it.

**Take a snapshot before growing etcd.**
Omni's automatic backups for `vps` run hourly to S3; assert one is genuinely fresh with `omnictl get etcdbackupstatus -o yaml`, and take a local copy as well so recovery does not depend on Omni being healthy.
Never run `omnictl get etcdbackups3configs` — it prints the S3 credentials in plaintext (siderolabs/omni#3318).

```bash
omnictl get etcdbackupstatus -o yaml | grep -E 'id:|lastbackuptime|lastbackupstatus|error'
install -m 600 /dev/null /tmp/vps-etcd-pre-expansion.db
talosctl -n ubuntu-16gb-fsn1-2 etcd snapshot /tmp/vps-etcd-pre-expansion.db
```

That file contains every Secret in the cluster.
Keep it at mode 600, never print or copy it, and delete it once the new member is verified healthy.

**Add one machine at a time and check etcd between them.**
A two-member etcd has quorum 2 and tolerates zero failures, so the window between the first and second join is strictly less available than a single-node cluster.
Close it in the same session — and if you cannot, remove the machine you just added rather than leaving the cluster at two.

```bash
omnictl talosconfig --cluster vps --merge
talosctl config context cynexia-vps
talosctl -n ubuntu-16gb-fsn1-2 etcd members
talosctl -n ubuntu-16gb-fsn1-2 service etcd
talosctl -n ubuntu-16gb-fsn1-2 etcd alarm list
```

**Check what the new machine's rendered config contains while it is still installing**, before it reaches etcd — a cluster-scoped ConfigPatch reaches every machine the moment it joins, and a `UserVolumeConfig` whose disk selector matches nothing is the case to watch for here.
Read the **redacted** resource: `omnictl get clustermachineconfig` is `PermissionDenied` for every user role, including Admin, and `redactedclustermachineconfig` is the readable form — which is also the safer one, since a rendered machine config carries the cluster CA private key.

```bash
omnictl get redactedclustermachineconfig <machine-uuid> -o yaml | grep -c UserVolumeConfig
```

Expected `0` for a machine with no Cloud Volume.
A non-zero count means a patch is over-reaching: remove the machine from the cluster in **Cluster Scaling** and fix the patch scope first.

If Omni reports that Talos refused to install on a dirty disk, wipe it through the Omni-wide talosconfig (no `--cluster`, which also reaches machines belonging to no cluster) and reset the server from the Hetzner console so Talos retries.
**Note the addressing form:** machines in a cluster are addressed by node name, machines that belong to no cluster have no node name and are addressed by machine UUID.

```bash
omnictl talosconfig --merge
talosctl -n <machine-uuid> get disks
talosctl -n <machine-uuid> wipe disk sda --method FAST
```

Add `-i` to that last command if it is refused because the machine is still in maintenance mode.

**`talosctl health` cannot finish against this cluster, and that is not a fault.**
Every Talos-side check passes — etcd healthy, members consistent and all control plane, apid ready, no diagnostics, kubelet healthy, boot sequence finished — and then the run stops at `waiting for all k8s nodes to report` with `PermissionDenied`, followed by `DeadlineExceeded`.
Omni withholds that path from this identity regardless of role.
Read the Kubernetes half directly instead, which is stronger evidence anyway:

```bash
kubectl --context cynexia-vps get nodes -o custom-columns='NAME:.metadata.name,SCHEDULABLE:.spec.unschedulable,STATUS:.status.conditions[-1].type,TAINTS:.spec.taints'
kubectl --context cynexia-vps get --raw='/readyz?verbose' | tail -1
```
