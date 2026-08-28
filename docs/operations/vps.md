# VPS cluster (Phase 2)

Public-internet-facing single-node Talos cluster on Hetzner for personal web services.
Kubectl context: `cynexia-vps`.
Manifests live in `vps/`.

## Shape

| Aspect | Detail |
|---|---|
| Host | Hetzner CX43 in `fsn1`, Talos single-node, managed by the same Omni instance as homelab (cluster name `vps`) |
| Storage | One 80 GB Hetzner Cloud Volume as a Talos user volume mounted at `/var/mnt/data`; local-path-provisioner points there |
| Network | Hetzner Private Network `10.0.0.0/24`. No public :80/:443 on the node; the Hetzner Cloud Firewall drops public inbound |
| Ingress | `cloudflared` tunnel only (named tunnel `cynexia-vps`). No Traefik, no cert-manager, no MetalLB, no NFS CSI |
| TLS / auth | Terminated at the Cloudflare edge. Cloudflare Access with email-OTP in front of every hostname |
| Domain | `*.cynexia.com` (Cloudflare-hosted zone). Homelab's `cynexia.net` is separate and unrelated |
| Namespaces | `vps` for all workloads (PSA `baseline`), plus `backup` (PSA `privileged`, hostPath), `keel`, and `ops` (PSA `baseline`, one CronJob — see below) |
| Secrets | 1Password `VPS` vault, referenced via `VPS_*` / workload-specific vars in `.env.tpl` |
| Image updates | keel runs here (`vps/bootstrap/keel/`) and workloads carry the standard keel annotation set, except keel itself, which is digest-pinned and Renovate-bumped (see below) |
| Apply | `make apply-vps`, gated by `check-vps-context` |

The Talos user-volume patch (`vps/talos/machineconfig-patches/400-vps-user-volume-data.yaml`) selects the Cloud Volume by **size bracket** (70–100 GB): the boot disk and the Cloud Volume both report `transport=virtio`, so transport alone cannot tell them apart.
There is no `make` target for VPS Talos patches — apply them with `omnictl apply -f <file>`.

Fresh Hetzner Cloud Volumes ship pre-formatted and Talos refuses to provision over them; wipe first with `talosctl wipe disk <dev> --method FAST`.

### The local-path storage contract

**This cluster's `local-path` storage lives on one machine, and a PVC bound there is reachable from nowhere else.**
The storage node is `ubuntu-16gb-fsn1-2`, so far also the only node.
Every `local-path` PersistentVolume carries `nodeAffinity` pinning it to that hostname, and the StorageClass binds `WaitForFirstConsumer`, so a PVC has no node until a pod using it is scheduled, and is welded to that node from then on.
Verified 2026-08-26: all eight PVs then in existence read `[ubuntu-16gb-fsn1-2]`.

That is invisible while the cluster has one node and load-bearing the moment it does not.
**A pod with a `local-path` PVC needs a `nodeSelector` naming that hostname.**
Without one the scheduler may place it elsewhere, and the two outcomes are a loud one and a silent one:

- The PVC is **already bound** to the storage node.
  The pod cannot reach the volume, so it sits `Pending` until whatever deadline it carries.
  Loud, and easy to diagnose.
- The PVC is **still unbound**.
  This is the bad one.
  `vps/bootstrap/local-path/kustomization.yaml` patches in a `DEFAULT_PATH_FOR_NON_LISTED_NODES` catch-all, so local-path-provisioner does not refuse an unlisted node — it creates a fresh empty directory there and binds happily.
  The pod starts, reads and writes an empty volume that is not the Cloud Volume, and nothing errors anywhere.

**A `hostPath` mount of that same directory is subject to the identical rule.**
The nightly restic CronJob in `vps/backup/restic-cronjob.yaml` mounts `/var/mnt/data/local-path-provisioner` by `hostPath` and carries **no `nodeSelector`**, so on a multi-node cluster nothing keeps it on the storage node.

The mount declares `type: Directory`, which looks like it might catch a wrong-node run and mostly will not: the catch-all above provisions into **that same path**, so any second node that has ever provisioned a `local-path` volume already has the directory and the check passes.
You get the empty-source case instead — restic reading a tree holding, at most, that node's own stray volumes and none of the ones being backed up.

That is caught, but late and at a cost.
The job's expected-set verification gate names each snapshot by path, so the missing ones fail it by name.
The gate runs **after** `restic backup`, so a wrong-node run has already written a snapshot of nothing into the repository, where it counts against the 7-daily / 4-weekly / 6-monthly retention, and that night has no usable backup.
Neither the `Directory` check nor the gate has been exercised on a second node, because there has never been one — this is read off the manifest and the gate script, not observed.
Pinning that pod is outstanding work, carried with the multi-node expansion; it is named here because a storage contract that omitted the case would be worse than none.

The `keel-fresh` CronJob in the `ops` namespace is already pinned — see the comment beside its `nodeSelector`, which is where the reasoning lives in full.

### Image updates and keel

keel is digest-pinned and carries no keel annotations of its own.
A self-updating controller holding cluster-wide read **and write** across every workload kind — its ClusterRole grants `get, delete, watch, list, update` on Deployments, DaemonSets, StatefulSets, ReplicaSets, ReplicationControllers, Pods, Jobs and CronJobs — is the one component where an unattended upstream tag change is a security event rather than a convenience, so its bump belongs in a reviewed pull request rather than a six-hour poll.

Renovate has reached this cluster since 2026-08-26, when `renovate.json` gained a `/^vps/.+\.ya?ml$/` pattern alongside the homelab one, so keel's bump here arrives as a pull request like every other pinned image in `vps/`.
`check-renovate-scope-vps` runs in the `diff-vps`/`apply-vps` preflight and fails the apply if that scope is ever lost.
`vps/bootstrap/keel/**` sits on the `pinDigests: false` packageRule: the image is already pinned by tag and digest by hand, so there is nothing for Renovate to add.

Its RBAC was trimmed on August 26, 2026 (PR #68): no `secrets` rule, no `pods/portforward`.
Verify keel's permissions with a SelfSubjectAccessReview issued with keel's own ServiceAccount token from inside the cluster — `kubectl auth can-i --as=` is meaningless through the Omni proxy, which ignores impersonation and answers as the caller.

## Workloads

| Service | Hostname | DB |
|---|---|---|
| freshrss | `rss.cynexia.com` | sqlite |
| uptime-kuma | `uptime.cynexia.com` | sqlite |
| changedetection (+ sockpuppetbrowser) | `watch.cynexia.com` | sqlite / `/datastore` |
| umami | `analytics.cynexia.com` | dedicated postgres |
| n8n | `n8n.cynexia.com` | sqlite |
| karakeep (+ meilisearch) | `keep.cynexia.com` | sqlite |

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
Its two integers of state live on a 32Mi `local-path` PVC, `keel-fresh-state`, so its pod carries a `nodeSelector` for `ubuntu-16gb-fsn1-2` under the storage contract above.

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

Do **not** set `HTTP_PROXY`/`HTTPS_PROXY` on changedetection.
The old setup routed through a sibling `proxy-client` container chaining to homelab's tinyproxy over Cloudflare Access TCP; homelab was rebuilt without tinyproxy, so that chain is dead and the VPS now egresses directly.
Migrated watches that referenced the named proxy `homelab` had their `proxy` field cleared post-migration — stale proxy references hide in `/datastore` inside both the watch entries and `changedetection.json`, and a URL-grep misses them because the compose-era proxy URL was a sibling container name rather than a hostname.

## Database shape

Per-service sqlite, except umami which needs postgres.
A shared postgres was researched and rejected: karakeep is sqlite-only (karakeep issue #1782), uptime-kuma v2 supports only sqlite/MariaDB (issue #5674), and the remaining consolidation saving did not justify the upgrade-coupling cost.

`N8N_ENCRYPTION_KEY` is load-bearing — it was extracted from the old n8n container during the rebuild and n8n credentials are unreadable without it.

## Backups

Separate B2 bucket and separate restic repo from homelab.
The restic CronJob runs at 04:00 UTC and backs up `/var/mnt/data/local-path-provisioner` by hostPath, with the same 7 daily / 4 weekly / 6 monthly retention as homelab, and `--group-by paths`.

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

**Adding a sqlite-backed service means adding its snapshot to that list.**
The gate proves a snapshot exists, is fresh and has a schema; it does not prove the contents are complete.
That, and why the gate runs after rather than before the backup, are in [monitoring.md](monitoring.md#the-backup-verification-gates).

The job pings healthchecks.io on start and on exit code, and sets no `terminationGracePeriodSeconds` — busybox `ash` as PID 1 never forwards SIGTERM to restic, so the old 120 s grace delivered nothing and only slowed teardown; a lock left behind by a killed run is cleared by `restic unlock` at the head of the next one.
Details, and the reason the shell is chained with `&&` rather than `set -e`, are in [monitoring.md](monitoring.md#the-restic-ping-wrapper).

## External monitoring

uptime-kuma at `uptime.cynexia.com` is layer 3 of the detection stack.
Its monitors are **created by hand in the UI** — v2 has no supported programmatic path — and are documented monitor-by-monitor in [uptime-kuma.md](uptime-kuma.md), including the Cloudflare Access trap (a monitor that follows the Access 302 reports UP while the origin is dead) and the healthchecks.io dead-man's-switch that watches uptime-kuma itself.

Hand-created monitor config lives in `kuma.db`, which the quiesce sidecar snapshots and restic backs up nightly — so a cluster rebuild restores the monitors rather than requiring them to be retyped.
