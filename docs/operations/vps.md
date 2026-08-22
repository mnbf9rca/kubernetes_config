# VPS cluster (Phase 2)

Public-internet-facing single-node Talos cluster on Hetzner for personal web services.
Kubectl context: `cynexia-vps`. Manifests live in `vps/`.

## Shape

| Aspect | Detail |
|---|---|
| Host | Hetzner CX43 in `fsn1`, Talos single-node, managed by the same Omni instance as homelab (cluster name `vps`) |
| Storage | Hetzner Cloud Volume as a Talos user volume mounted at `/var/mnt/data`; local-path-provisioner points there |
| Network | Hetzner Private Network `10.0.0.0/24`. No public :80/:443 on the node; the Hetzner Cloud Firewall drops public inbound |
| Ingress | `cloudflared` tunnel only (named tunnel `cynexia-vps`). No Traefik, no cert-manager, no MetalLB, no NFS CSI |
| TLS / auth | Terminated at the Cloudflare edge. Cloudflare Access with email-OTP in front of every hostname |
| Domain | `*.cynexia.com` (Cloudflare-hosted zone). Homelab's `cynexia.net` is separate and unrelated |
| Namespaces | `vps` for all workloads (PSA `baseline`), plus `backup` (PSA `privileged`, hostPath) and `keel` |
| Secrets | 1Password `VPS` vault, referenced via `VPS_*` / workload-specific vars in `.env.tpl` |
| Image updates | keel runs here (`vps/bootstrap/keel/`) and workloads carry the standard keel annotation set |
| Apply | `make apply-vps`, gated by `check-vps-context` |

The Talos user-volume patch (`vps/talos/machineconfig-patches/400-vps-user-volume-data.yaml`)
selects the Cloud Volume by **size bracket** (70–100 GB), because the boot disk and the
Cloud Volume both report `transport=virtio` and can't be distinguished by transport
alone. Note there is no `make` target for VPS Talos patches — apply them with
`omnictl apply -f <file>` directly.

Fresh Hetzner Cloud Volumes ship pre-formatted and Talos refuses to provision over them;
wipe first with `talosctl wipe disk <dev> --method FAST`.

## Workloads

| Service | Hostname | DB |
|---|---|---|
| freshrss | `rss.cynexia.com` | sqlite |
| uptime-kuma | `uptime.cynexia.com` | sqlite |
| changedetection (+ sockpuppetbrowser) | `watch.cynexia.com` | sqlite / `/datastore` |
| umami | `analytics.cynexia.com` | dedicated postgres |
| n8n | `n8n.cynexia.com` | sqlite |
| karakeep (+ meilisearch) | `keep.cynexia.com` | sqlite |

Every container here carries readiness and (where a restart is a safe remedy) liveness
probes; the per-service targets and the reasoning behind each — including the ones that
are deliberately shallow — are in [monitoring.md](monitoring.md#vps-cluster).

`make route-vps-dns` reads the hostname list straight out of
`vps/bootstrap/cloudflared/cloudflared.yaml` (that ConfigMap is the single source of
truth for hostname → Service routing) and upserts a CNAME per hostname onto the current
tunnel UUID. Run it after adding a hostname, and after any full cluster rebuild.

### Cloudflare Access bypasses

Public ingestion endpoints must be Access-bypassed or they break:

- umami `/script.js` and `/api/send/*`
- n8n `/webhook/*`

A bypass path glob of `/foo/*` does **not** match the bare path `/foo` — add both the
exact and the wildcard destination.

### FreshRSS WebSub push

FreshRSS builds its WebSub callback URL from `base_url` in `data/config.php`. That file
lives on the `freshrss-data` PVC rather than in this repo, and the image reads the
`BASE_URL` environment variable only during first install, so setting it on the
Deployment has no effect on an existing volume. Change it with the CLI:

```bash
kubectl --context cynexia-vps -n vps exec deployment/freshrss -c freshrss -- \
  php /var/www/FreshRSS/cli/reconfigure.php --base-url=https://rss.cynexia.com
```

With an `http://` value, Cloudflare answers each hub callback with a 301 to the HTTPS
URL. Verification survives that: hubs follow the redirect for the `GET`, and Google's
FeedFetcher demonstrably did. Whether *delivery* survives it is a different question,
because a hub is far less likely to replay a `POST` body across a redirect — and no push
delivery has ever succeeded on this instance. The value was corrected on August 20, 2026;
issue #29 tracks whether that was the cause.

#### What `"error"` in `!hub.json` actually means

It is not a failure counter, and it is not a current-state signal. `p/api/pshb.php` sets
it to `true` when a subscription is verified, with the comment *"Do not assume that WebSub
works until the first successful push"*, and clears it only after a delivery that updates
at least one feed for at least one user. So:

> `"error": true` means **no push has ever been successfully processed for this feed**.

Two things follow. A quiet feed and a broken one look identical. And
`FreshRSS_Feed::pubSubHubbubPrepare()` re-subscribes any feed whose state has `error` set
and whose `lease_start` is over 23 hours old — so a feed that has never received a push
re-subscribes **daily, permanently**, regardless of how far its lease is from expiring.
That accounts for 3,040 subscribe requests across twelve feeds in six months here. See
issue #29.

Grepping for `pubSubHubbubError()` is misleading: that method is only ever called with
`true`, which makes the flag look like a one-way latch. The clearing path writes the
array directly in `p/api/pshb.php` and does not go through the method.

**Subscription health is `lease_end` in the future.** Run:

```bash
./scripts/freshrss-websub-status.py
```

It reports both columns per feed — whether the lease is live, and whether that feed has
ever received a push — and exits non-zero if any subscription is not live, so it works as
a check. It reads the state inside the pod and prints only derived status: never print
`!hub.json` yourself, because each file holds that feed's callback secret.

As of August 20, 2026 it reports 12 of 12 live and 0 of 12 ever pushed.

A second-order check is the outbound subscribe log, `data/users/_/log_pshb.txt`. Tally
the trailing HTTP status with `awk '{print $NF}' … | sort | uniq -c`: 2xx is success. As
of August 20, 2026 that log showed 2,959 successes against 69 transient 5xx over six
months, and no redirects at all.

Neither check proves end-to-end delivery, only subscription. Delivery is a `POST` to
`/api/pshb.php` in the pod log, and it only appears when a subscribed feed actually
publishes something.

The callback path `/api/pshb.php` falls under the `freshrss api` Access bypass, so it
stays publicly reachable, and under the zone rate limiting rule of 50 requests per 10
seconds per IP, far above hub delivery volume.

### The claude.com/blog feed (HTML+XPath scrape)

`claude.com/blog` has no RSS/Atom feed or JSON API (Webflow static site), so it is a
native FreshRSS **HTML+XPath** feed (feed id 132, kind 10, user `ruined0346`). Config
lives in the FreshRSS sqlite DB on the PVC (restic + sidecar backed up), not in git;
it was imported via `cli/import-for-user.php` with `frss:`-namespace OPML attributes.

- **Egress quirk:** Cloudflare's "Just a moment…" challenge fires on the workstation
  IP/UA but **not** on the VPS egress, so FreshRSS's own fetch gets clean HTML. Verify
  any "is it blocked?" question from inside the pod, not from a laptop. RSSHub was
  therefore unnecessary (PikaPods fallback exists; key at `op://VPS/RSSHub/api_key`).
- Selectors: item `//div[contains(concat(' ',@class,' '),' marquee_cms_blog_list_item ')]`,
  title `descendant::h2`, uri `descendant::a[contains(@class,'clickable_link')]/@href`,
  timestamp `descendant::div[contains(@class,'u-text-style-caption')]`. Full-article
  content via `pathEntries` CSS `.blog_post_content_wrap`.
- **The time format MUST be `!F j, Y` (leading `!`).** Without the `!`,
  `DateTime::createFromFormat` fills omitted time fields with the *current* time, so
  every poll restamps each post, the `sha1:link_published_title` dedup key churns, and
  duplicates multiply (hit 560 once). Also keep `unicityCriteria: link` +
  `unicityCriteriaForced: true` in the feed attributes. Verify any dedup change by
  running `cli/actualize-user.php` twice — the second run must report 0 new. (BST dates
  render 23:00 the prior day; cosmetic.)
- Only the ~10 newest posts appear — older ones are JS-paginated and unreachable to a
  non-browser scraper.

### Browser backend for changedetection

Use `dgtlmoon/sockpuppetbrowser`. `browserless/chrome:latest` is the deprecated v1 line
and leaks CDP sessions under modern Playwright.

Do **not** set `HTTP_PROXY`/`HTTPS_PROXY` on changedetection. The old setup routed
through a sibling `proxy-client` container chaining to homelab's tinyproxy over
Cloudflare Access TCP; homelab was rebuilt without tinyproxy, so that chain is dead and
the VPS now egresses directly. Migrated watches that referenced the named proxy
`homelab` had their `proxy` field cleared post-migration — stale proxy references hide
in `/datastore` inside both the watch entries and `changedetection.json`, and a
URL-grep misses them because the compose-era proxy URL was a sibling container name
rather than a hostname.

## Database shape

Per-service sqlite, except umami which needs postgres. A shared postgres was researched
and rejected: karakeep is sqlite-only (karakeep issue #1782), uptime-kuma v2 supports
only sqlite/MariaDB (issue #5674), and the remaining consolidation saving didn't justify
the upgrade-coupling cost.

`N8N_ENCRYPTION_KEY` is load-bearing — it was extracted from the old n8n container
during the rebuild and n8n credentials are unreadable without it.

## Backups

Separate B2 bucket and separate restic repo from homelab. The restic CronJob runs at
04:00 UTC and backs up `/var/mnt/data/local-path-provisioner` via hostPath, with the same
7 daily / 4 weekly / 6 monthly retention as homelab, with `--group-by paths`.

That flag is load-bearing: `restic forget` groups by host+paths by default, and every
CronJob pod has a unique hostname, so each nightly snapshot formed a group of one and the
policy kept all of them. Verified on homelab 2026-08-20 — 137 snapshots in 137 groups
across 131 hostnames, nothing ever pruned since the backup system was built. The image is pinned to
`restic/restic:0.17.3` (was `:latest` — an unpinned backup tool is a silent-change surface
on the one job you cannot re-run).

Consistency sidecars run alongside the app containers: sqlite quiesce for
n8n / freshrss / karakeep / uptime-kuma, and `pg_dumpall` for umami's dedicated postgres.
Each refreshes a `*.restic` snapshot every 12h.

**None of the five sidecars carries a probe**, and that is deliberate: any failing probe
on a sidecar takes the *application* offline (readiness directly, liveness via
CrashLoopBackOff → EndpointSlice), so a backup fault would cost you the service. A
freshness liveness probe existed here briefly and was removed; the full reasoning, which
reverses what the original spec endorsed, is in
[monitoring.md](monitoring.md#why-the-sidecars-have-no-probes).

Instead each loop runs under `set -u` (**not** `set -e` — exiting is the outage path),
logs failures to stderr, backs off 300 s and retries, publishes atomically via a `.tmp`
plus `mv`, and — for the four sqlite ones — refuses to publish a snapshot whose
`sqlite_master` count is zero, so a truncated source can no longer publish a fresh, valid,
empty database. umami's `pg-dump-sidecar` follows the same shape. **These sidecars fail by
logging, not by exiting**; their restart counts stay at zero and the alarm comes from the
restic gate.

The restic job runs a **backup verification gate** after the backup. The authoritative
half is an expected-set assertion — each known snapshot present and <15h old, named app by
app, and for FreshRSS a sibling snapshot per *user DB* rather than the newest of a glob. A
broad sweep for other stale `*.restic` files runs alongside it but is **advisory only**, so
one orphaned PV directory cannot pin the gate permanently red. Together that turns a
silently dead sidecar — or one deleted from the manifest, or an empty/unmounted volume —
into a backup alert rather than years of backing up a stale or absent copy.

**Adding a sqlite-backed service means adding its snapshot to that list.** The gate proves
a snapshot exists, is fresh and has a schema; it does not prove the contents are complete.
That, and why the gate runs after rather than before the backup, are in
[monitoring.md](monitoring.md#the-backup-verification-gates).

The job pings healthchecks.io on start and on exit code, and sets no
`terminationGracePeriodSeconds` — busybox `ash` as PID 1 never forwards SIGTERM to restic,
so the old 120 s grace delivered nothing and only slowed teardown; a lock left behind by a
killed run is cleared by `restic unlock` at the head of the next one. Details, and the
reason the shell is chained with `&&` rather than `set -e`, are in
[monitoring.md](monitoring.md#the-restic-ping-wrapper).

## External monitoring

uptime-kuma at `uptime.cynexia.com` is layer 3 of the detection stack. Its monitors are
**created by hand in the UI** — v2 has no supported programmatic path — and are documented
monitor-by-monitor in [uptime-kuma.md](uptime-kuma.md),
including the Cloudflare Access trap (a monitor that follows the Access 302 reports UP
while the origin is dead) and the healthchecks.io dead-man's-switch that watches
uptime-kuma itself.

Hand-created monitor config lives in `kuma.db`, which the quiesce sidecar snapshots and
restic backs up nightly — so a cluster rebuild restores the monitors rather than requiring
them to be retyped.
