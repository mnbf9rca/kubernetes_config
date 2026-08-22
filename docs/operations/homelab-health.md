# Health namespace

Personal health-data pipeline in the homelab cluster: Apple Health + Garmin →
InfluxDB → Grafana, plus a Claude MCP connector. Added in Phase 0/1 as its own `health`
namespace. Manifests live in `homelab/health/`.

The namespace also hosts one workload that is **not** health data: the Cloudflare
analytics ingest ([below](#cloudflare-analytics-ingest)). It shares this InfluxDB and
Grafana rather than standing up a second pair. That is a deliberate trade — infrastructure
telemetry living in the same database as personal health data — taken because a second
InfluxDB for ~3,500 rows a day is not worth the operational surface. It stays in its own
bucket and its own measurements.

Design docs are in the separate `~/Downloads/git/HealthRecords` repo —
`docs/superpowers/specs/2026-07-11-health-platform-vision.md` and
`docs/superpowers/specs/2026-07-11-health-records-ingestion-design.md`. Phase 2 (facade,
records store, multi-person registry) is scoped there, not here.

## Image policy

**No keel in this namespace.** This is a data pipeline; auto-upgrading it is not wanted.
Every image is version- or digest-pinned and Renovate proposes bumps instead. Renovate is
scoped to `homelab/health/**` only, with `pinDigests` (see `renovate.json`).

`namespaces.yaml` marks `health` as PSA `baseline` (nothing here needs
hostPath/hostNetwork), but every current workload already trips `restricted`-level PSA
warnings — a hardening pass to `restricted` is a queued follow-up.

### Pins that carry a reason

- **Pomerium `v0.33.0`** — HISTORICAL: removed 2026-08-22 (replaced by Cloudflare
  Access Managed OAuth). The pin reasoning is preserved in git history.
- **`garmin-grafana` is digest-pinned to a main-branch build**
  (`thisisarpanghosh/garmin-fetch-data@sha256:8b7955d3...`), not a tagged release.
  Release `v0.5.0` crashes with an `AttributeError` on `client.profile` when
  `TAG_MEASUREMENTS_WITH_USER_EMAIL` is set — fixed upstream post-release but not yet in
  a tagged build. Renovate is **disabled** for this image in `renovate.json`
  `packageRules`: a digest-tracking rule would eventually propose bumping straight to
  the broken `v0.5.0` build, and there's no way to encode "this specific release is bad".
  Exit path is manual — when upstream publishes a release newer than `v0.5.0`, re-enable
  the Renovate rule and re-pin the manifest as `tag@digest`.
- **`apple-health-ingester` memory limit is 1Gi**, not the original 256Mi: large Health
  Auto Export batch exports OOMKilled it at 256Mi.
- **`influx-backup` runs on `alpine/k8s:1.36.0`**, version-matched to the cluster's
  server minor — not `bitnami/kubectl`, which no longer publishes plain version tags on
  Docker Hub (moved to the frozen, unauthenticated `bitnamilegacy/*`, and that image
  lacks `wget`, which the healthchecks.io ping needs).

## Ingress

A dedicated `cloudflared` Deployment runs the **`cynexia-health`** tunnel — separate from
the VPS cluster's `cynexia-vps` tunnel. Credentials are in 1Password as the **DOCUMENT**
item `health-cloudflared`: use `op document get`, not `op read` (document items don't
expose a plain field).

Public `*.cynexia.com` hostnames on this tunnel:

| Hostname | Purpose |
|---|---|
| `hae.cynexia.com` | Health Auto Export ingest → `apple-health-ingester` |
| `mcp.cynexia.com` | Claude/Hermes MCP connector, via Cloudflare Access (Managed OAuth) |
| `hermes.cynexia.com` | Hermes agent dashboard on the hermes VM (`hermes.cynexia.net:9119`, off-cluster), via Cloudflare Access (karakeep-style email policy) |

`hermes.cynexia.com` is the one off-cluster origin on this tunnel: cloudflared
proxies to the hermes VM on the LAN, not to a cluster Service. The Access app
(`hermes`) attaches the same three reusable policies as karakeep — home/VPS IP +
service-token bypass, service-token allow, and `email_domain: cynexia.com` allow.
The dashboard runs its own mandatory login behind that (basic auth, forced by its
non-loopback bind), so Access is defence in depth, not the only gate — but the
Access gate still **fails open** like mcp's does, and the same post-rebuild rule
applies: verify the edge challenges an unauthenticated client before trusting the
hostname. Hermes Desktop's remote-attach cannot pass Access's browser login (no
custom-header support upstream); it uses the tailnet path
(`http://hermes.cynexia.net:9119` via the OPNsense subnet route) instead.

VM-side state that makes the tunnel work (on `hermes.cynexia.net`, login
`ssh hermes@…`; note `~/.local/bin` is not on the non-login PATH, so run
`hermes` via an interactive shell or full path): `dashboard.public_url:
https://hermes.cynexia.com` in `~/.hermes/config.yaml`, and
`Environment=FORWARDED_ALLOW_IPS=*` in the `hermes-dashboard` systemd user
unit — cloudflared runs off-host, so uvicorn must be told to trust
`X-Forwarded-*` or cookies lose their `Secure` flag. The wildcard means any
LAN client can spoof forwarded headers (they feed the login rate-limiter and
audit log); accepted for now — tighten to the cluster egress IP if it matters.
`.bak-hermes-tunnel` copies of both edited files sit beside the originals.
Hermes registers MCP OAuth clients with callbacks at
`https://hermes.cynexia.com/api/mcp/oauth/callback/<server>`, which is why
that wildcard sits in the DCR allowlist below.

Grafana is **not** on this tunnel — it is private, Traefik-fronted at
`grafana-health.cynexia.net` like every other homelab service (LAN/Tailscale only).

After changing hostnames in `homelab/health/cloudflared.yaml`, run `make
route-health-dns`. To recreate the credentials Secret, `make
create-health-cloudflared-secret`.

## MCP behind Cloudflare Access

Since 2026-08-22 the InfluxDB MCP server is a plain single-container Deployment +
Service (`influxdb-mcp`, port 3000) and **auth lives entirely at the Cloudflare
edge**: an Access app on `mcp.cynexia.com` (email policy, one-time-PIN IdP) with
Managed OAuth — RFC 8414/9728 metadata served by Access, dynamic client
registration enabled, 15m access tokens against a 336h (2-week) grant session.

Dynamic client registration is gated by a **redirect-URI allowlist**
(`oauth_configuration.dynamic_client_registration.allowed_uris` on the Access
app). A client whose callback is not listed gets
`400 invalid_client_metadata: "redirect_uri is not allowed by the account
configuration"` at registration — this is what blocked the Hermes agent until
2026-08-22. The list currently holds Claude's two callbacks
(`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`)
and `https://hermes.cynexia.com/api/mcp/oauth/callback/*` (a trailing `/*`
wildcards sub-paths); localhost and loopback clients are allow-any. **Every new
MCP client host needs its callback added** via GET-then-full-PUT of the app —
and like everything else about this app, the list is account-side state this
repo cannot restore: re-creating the Access app means re-entering it.
This replaced the Pomerium proxy (daily re-auth from its 14h session expiry;
DCR disabled, locking out non-allowlisted MCP clients). The retired Google
OAuth client is soaking until ~2026-08-29; delete it after that if nothing
has needed it.

The origin is authless in HTTP mode and does not validate the
`Cf-Access-Jwt-Assertion` header Access injects — accepted deliberately: Pomerium
fronted the same authless origin, which ignored its injected identity too. The
tunnel is the only path in from the internet, and Access gates the hostname.

**RESIDUAL RISK — the gate fails OPEN.** The old gate was committed here and
failed closed (no Pomerium → 502). The new gate is Access dashboard/API state
tracked nowhere in this repo: delete or disable the app and cloudflared serves
the authless origin raw to the internet, silently — and a rebuild from this repo
(`make apply-homelab` + `make route-health-dns`) republishes the hostname with no
guarantee the app still exists. After any rollback, rebuild or account-side
change, `curl -s -o /dev/null -D - https://mcp.cynexia.com/mcp` must return 401
before the hostname is trusted; the `health-mcp` uptime-kuma monitor is pinned to
exactly `["401"]` so a naked origin alarms ([uptime-kuma.md](uptime-kuma.md)).

In-cluster exposure is unchanged in kind from the 2026-08 sidecar era: flannel
does not enforce NetworkPolicy, and upstream
`ghcr.io/mnbf9rca/influxdb-mcp-server` (built multi-arch from source — there is
no official image) binds `0.0.0.0` with no `--bind` flag, so pod-IP:3000 was
reachable from any pod even as a sidecar; the restored Service only re-adds DNS
discoverability. Any in-cluster pod can query InfluxDB read-only through it.
Documented in `influxdb-mcp.yaml`. Queued: a bind-flag patch, and reinstating a
NetworkPolicy if the CNI is ever swapped to Cilium.

## InfluxDB bootstrap

`make health-influx-bootstrap` creates the buckets, the v1 DBRP mapping and the
v1-compat auth user (garmin-grafana needs InfluxDB 1.x-style auth), then prints two
scoped tokens — ingester write-only, MCP+Grafana read-only — for one-time manual paste
into 1Password. InfluxDB 2.9 hash-stores tokens server-side, so **the printed value is
the only copy, ever**.

Token extraction uses `--json | jq -r .token`, not `--hide-headers` + awk column
parsing: the multi-word `-d` description strings shift awk's column and it silently
captures a description fragment instead of the token. `jq` is therefore a hard
dependency and is asserted by `make check-tools`.

`make health-influx-cloudflare-bootstrap` does the same job for the Cloudflare analytics
bucket. It creates `cloudflare` with `-r 0` (infinite retention — expiring the copy would
defeat the entire point) and prints **two** tokens:

| Token | Paste into | Scope |
|---|---|---|
| Cloudflare ingest | `op://Homelab/health-influxdb/cloudflare-token` | read **and** write on `cloudflare` |
| Replacement read-only | `op://Homelab/health-influxdb/read-token` | read on all four buckets |

The ingest token needs read as well as write because the job's resume point is
`max(_time)` read back out of the bucket.

The read token has to be **replaced**, not amended: InfluxDB offers no way to add a bucket
to an existing auth, so Grafana and the MCP connector cannot see `cloudflare` until a new
token exists. Order matters — paste, `make apply-homelab`, restart `grafana` and
`influxdb-mcp`, and only **then** `influx auth delete` the superseded auth. Delete it first and
you lock Grafana and the connector out until the new Secret has actually rolled.

## Backups and restore

The `influx-backup` CronJob runs at 02:30 daily, ahead of the 03:00 restic sweep. It
writes both:

- a native `influx backup` (14 generations), and
- a per-bucket, 8-day-windowed line-protocol export (60 generations, gzip), over an
  **explicit** bucket list: `apple_metrics apple_workouts garmin cloudflare`

to the `health-dumps` PVC on `local-path`. Because that PVC lives on the node's SSD, the
existing hostPath restic→B2 CronJob picks it up for free — no separate off-cluster
wiring needed.

**Adding a bucket means adding it to that list**, or it is silently never exported — the
same class of bug as the VPS gate's expected-set assertion, and the reason the list is
explicit rather than a wildcard over `influx bucket list`. A named bucket that does not
exist is now a **named fatal error**: the pipeline `influx bucket list | awk` exits with
awk's status, so a failed lookup used to leave the bucket ID empty and sail straight past
`set -eu` into an opaque `export-lp` error. Consequence for ordering: run
`make health-influx-cloudflare-bootstrap` **before** the apply that adds `cloudflare` here,
or the next night's export fails.

**Restore drill:** `influx restore --full` self-defeats — it clobbers its own auth
mid-restore. Use scoped `influx restore --bucket <name>` instead. First drill passed
2026-07-26. Quarterly drills should also exercise the still-untested DR path: `--full`
onto a brand-new, never-`setup` instance.

## Cloudflare analytics ingest

`homelab/health/cloudflare-analytics.yaml`. Hourly CronJob at `37 * * * *` that copies
Cloudflare edge traffic data into the `cloudflare` bucket before Cloudflare deletes it.

Cloudflare's Free plan keeps **8 days** of per-hostname analytics and rejects any GraphQL
query wider than **1 day**. That window is enough to answer "what is happening right now"
and useless for "was this normal?" — which is what you actually want when a webshell sweep
shows up. This job is the retention fix; nothing about the Cloudflare configuration
changes, and the token is read-only.

### Shape

| | |
|---|---|
| Zones | `cynexia.com` and `making-tracks.app` |
| Dataset | `httpRequestsAdaptiveGroups`, grouped by `datetimeHour` |
| Bucket | `cloudflare`, **infinite** retention, raw hourly rows, no downsampling |
| Measurement | `http_requests`; tags `zone`, `host`, `path`, `status`, `country`; fields `count`, `sample_interval` |
| Monitoring | `homelab-cloudflare-analytics`, pinged `/start` and exit code |

Two bookkeeping measurements share the bucket: `ingest_status` (one point per committed
chunk) and `ingest_gap` (see below).

### Why the script is Python, and a file rather than an inline string

Every other scheduled job in this repo is inline POSIX `sh`. This one is not, for three
reasons that are all about the failure modes this repo has already been bitten by:

- **Cloudflare answers a failed query with HTTP 200 and an `errors` array in the body.**
  Telling that apart from "no traffic" by grepping JSON in `sh` is precisely the shape
  that made `ingest-freshness` report STALE for 25 days.
- Rows must be **aggregated in memory** before writing. Path truncation merges several
  source paths into one series and row-cap subdivision splits one hour across several
  responses; both need keyed summation.
- Tag values are **user-controlled URL paths** and need real line-protocol escaping.

Standard library only, so there is no `pip install` at run time and the job depends on
nothing but the two APIs it talks to.

### The resume rule

The watermark is `max(_time)` over the `cloudflare` bucket, read back from InfluxDB on
every run. There is no state file, no PVC and no ConfigMap cursor, because all three can
disagree with what was actually stored — after a restore, after a manual delete, after a
partial write. The data is its own watermark and cannot drift from itself.

Every run then rewinds **2 hours** behind that watermark, because the final hour of the
previous run was almost certainly still in progress when it was written. Re-ingestion is
free: same measurement, same tag set and same timestamp overwrite in InfluxDB.

Backfill runs in **23-hour chunks** (Cloudflare rejects anything wider than a day), at most
**8 chunks per run**. Chunks are committed oldest-first and only when *every* zone
succeeded for that chunk — commit a chunk in which one zone failed and the watermark jumps
past hours that zone never covered, which Cloudflare then deletes.

A successfully-queried chunk with **zero rows** still writes its `ingest_status` point.
Without that, eight genuinely quiet days would look identical to eight days of broken
ingestion and would trip the gap alarm below for no reason.

**A chunk's points are written oldest-first, and that ordering is part of the resume
rule.** A chunk over 5,000 series is sent to InfluxDB in several batches, so a later batch
can fail with earlier ones already durably stored. Because the watermark is `max(_time)`
over what *is* stored and the next run rewinds only 2 hours behind it, a surviving partial
write has to be a **prefix** of the chunk in time. Ordered any other way — the code once
sorted on the tag tuple, zone first — a surviving first batch could carry points from the
last hour of a 23-hour chunk while its first hours went unwritten; the watermark would
jump past them, the 2-hour rewind would fall short, and Cloudflare would delete them. The
`ingest_status` marker is appended after every data point for the same reason.

### Gaps are permanent, so they are loud

If the rewound start is older than Cloudflare's retention, those hours are gone and no
future run can recover them. The job then:

1. logs the exact missing range,
2. writes an `ingest_gap` point (fields `missing_hours`, `gap_end`) timestamped at the gap
   start, so the hole is visible in Grafana instead of reading as a quiet week, and
3. **exits non-zero**, so `homelab-cloudflare-analytics` goes red.

It still ingests everything that *is* still available in the same run. The alarm fires
once: the next run's watermark is current again, which is the intended behaviour — a
permanent hole should be recorded permanently, not re-alerted hourly.

`ingest_gap` deliberately uses a field named `missing_hours`, not `count`. The watermark
query filters on `_field == "count"`, so a gap marker can never advance the watermark and
claim the hole was filled.

To surface gaps in Grafana, add an **annotation** query on `ingest_gap` to the Cloudflare
dashboard. A panel over `http_requests` alone will not show them.

### Cardinality

Path is by far the highest-cardinality dimension — karakeep alone emits a distinct path per
asset UUID — so paths are truncated to their first two segments, with `/*` appended when
segments were dropped (`/api/v1` and `/api/v1/*` stay distinguishable). Hosts listed in the
CronJob's `FULL_PATH_HOSTS` env var keep their full path. It is empty by default; every
host added there trades series cardinality for path detail.

### Sampling

`sample_interval` is stored per point and **never applied**. Whether Cloudflare's `count`
is already extrapolated is a property of the dataset, not something this job should
silently assume, and a chart that quietly switches from real counts to estimates is exactly
the kind of lie this repo has a rule about. Observed values are 1.03–1.14, i.e. effectively
unsampled at current volume. **Confirm the relationship once against the Cloudflare
dashboard for a known hour before building any panel that multiplies by it.**

### Row-cap subdivision

`httpRequestsAdaptiveGroups` caps a response at 10,000 rows and this dataset offers no
cursor. A chunk returning exactly the cap is assumed truncated, halved, and re-queried —
recursively, down to a 1-minute floor. Aggregation makes the halves recombine into the
same per-hour totals. A run is capped at 180 GraphQL calls; exhausting that budget is a
loud failure, not a silent truncation, so the watermark stays put and the next run retries.
Cloudflare's user limit is 300 queries per 5 minutes and burning it would break the next
several hourly runs too.

### First-run setup

The job cannot run until four things exist. None of them are created by `make apply-homelab`.

1. **Cloudflare API token**, scoped **`Zone.Analytics: Read` only**, covering both zones.
   The job never writes to Cloudflare, so any edit scope is blast radius bought for
   nothing. Store as `op://Homelab/cloudflare/api-token`.
2. **Zone tags**, as one field `op://Homelab/cloudflare/zone-ids` holding
   `cynexia.com=<zoneid>,making-tracks.app=<zoneid>`. Zone IDs are not passwords, but they
   identify the account and this repo is public, so they are resolved at apply time like
   everything else. Mark it `[text]`, not concealed — it is an identifier, and a concealed
   value makes the vault harder to debug.
3. **healthchecks.io check** `homelab-cloudflare-analytics`, period 1h, grace 2h. UUID into
   `op://Homelab/health-healthchecks/cloudflare-uuid`.
4. **InfluxDB bucket and token**: `make health-influx-cloudflare-bootstrap`. See below.

Then `make apply-homelab`, and force the first run rather than waiting an hour:

```bash
kubectl -n health create job --from=cronjob/cloudflare-analytics cf-analytics-manual
kubectl -n health logs job/cf-analytics-manual
```

The first run seeds from the retention floor (~8 days back) and reports no gap, because
nothing was ever lost. Read the log: it names every chunk, the row count per zone, and the
GraphQL budget consumed.

**Smoke-test the query shape on that first run.** The `avg { sampleInterval }` selection is
taken from the GraphQL schema, not from a doc page that spells it out; if Cloudflare names
it differently the run fails loudly with the `errors` array in the log, and the fix is one
line in `homelab/health/scripts/cloudflare-analytics-ingest.py`. It cannot fail
silently.

## Garmin re-authentication (annual)

Tokens on the `garmin-tokens` PVC last roughly a year. When they expire:

1. **Scale `garmin-grafana` to 0 first.** A crashlooping pod with an expired token fires
   an MFA SMS at the operator on every restart.
2. Run the interactive login pod. It needs `enableServiceLinks: false` — the influxdb
   Service's injected `INFLUXDB_PORT=tcp://...` otherwise crashes the script's `int()`
   parse — plus the full InfluxDB v1 env block, because the script demo-writes to
   InfluxDB before it shows the login prompt.
3. Scale back to 1.

**Keep `replicas: 0` committed while paused** — `make apply-homelab` resurrects any
uncommitted scale-down.

## Why probes exist here (2026-08-18 Pomerium wedge)

> **Historical.** Pomerium was removed 2026-08-22 — this failure mode and its
> custom probe target no longer exist. The section stays because it is why every
> HTTP-serving workload in this namespace carries probes.

Do not strip the liveness/readiness probes on the health workloads as cargo cult — they
were added in response to a real, silent 18.5-hour outage.

On **2026-08-18 at 20:57Z** the Pomerium pod (all-in-one, v0.33.0) stopped serving HTTP
entirely while its container stayed `Running`/`Ready` with **0 restarts for 12 days**.
Its control-plane goroutines kept running — the identity-manager logged "updating user
info" every 10 minutes throughout — but every request, including Pomerium's own
`/.well-known/*` endpoints, timed out having returned zero bytes. This was verified from
inside the cluster against the Service, so it was not a tunnel or edge problem; the MCP
sidecar upstream was healthy the whole time.

Nothing noticed for 18.5 hours, because the `pomerium` container had no liveness or
readiness probe: Kubernetes considered a process that answered no requests to be
perfectly healthy, and the healthchecks.io checks in this namespace watch *data
freshness*, not the auth proxy.

A `kubectl rollout restart` restored service immediately — 401 in 0.86s afterwards
versus a 20s hang before. **Root cause of the wedge itself is not established**; treat it
as unknown rather than assuming a specific Pomerium bug. The lesson that *is* established
is the failure mode: process-alive is not service-alive, so anything in this namespace
that serves HTTP needs a probe that actually exercises an HTTP endpoint.

### The probe target is deliberately not the documented one

Pomerium's liveness probe here is **`/ping` on :80**, not the `/healthz` on :28080 that
Pomerium's docs and its Ingress Controller use. This is not an oversight, and "correcting"
it re-creates the blind spot:

- `:28080` is a plain Go listener with **Envoy nowhere in its path**. Its `envoy.server`
  field is a ≤30s-stale cache of the Envoy admin thread reporting lifecycle state LIVE —
  which it was for all 18.5 hours. The documented probe would have stayed green throughout.
- `:80/ping` traverses listener → worker → HCM → ext_authz → control-plane cluster: the
  exact path that returned zero bytes. It answered 200 in 6.7 ms unauthenticated when
  tested, on Envoy's catch-all vhost, so the probe needs no `host:` field (kubelet dials
  the pod IP).
- Readiness on `:28080/readyz` is kept as a *complement*, catching databroker/config-sync
  failures `/ping` cannot see. It required adding `health_check_addr: :28080` to the
  ConfigMap — the default `127.0.0.1:28080` is unreachable by kubelet, which also makes
  the upstream example probe broken as written.
- A startup probe on `/startupz` (5-minute budget) keeps the tight liveness
  (`periodSeconds: 15`, `failureThreshold: 4` ≈ 60 s to restart) from firing during
  databroker sync.

Full policy and the cross-cluster probe inventory: [monitoring.md](monitoring.md).

**Upstream status:** this failure matches no known issue across Pomerium v0.32–v0.34
(searched issues and merged PRs for unresponsive/stuck/hang/deadlock/goroutine-leak/MCP-hang,
plus every issue opened since 2026-01-01). It may be unreported. One **unconfirmed**
hypothesis worth attaching if it recurs: v0.33 added an ext_proc filter for MCP response
interception, enabled per-route on MCP routes, opening a gRPC stream per MCP request with
`MessageTimeout` = 10s and **no `failure_mode_allow`** — a stream leak there is a plausible
resource-exhaustion path on an MCP-only deployment. There is no evidence that is what
happened. Pre-restart logs and the pod description are in the 2026-08-18 session
scratchpad for comparison.

## Monitoring

Four healthchecks.io checks; UUIDs in 1Password item `health-healthchecks`:

| Check | Period / grace | Signals failure by |
|---|---|---|
| `health-apple-ingest` | 1d / 12h | silence |
| `health-garmin-ingest` | 1d / 12h | silence |
| `health-influx-backup` | 1d / 6h | silence |
| `homelab-cloudflare-analytics` | 1h / 2h | **`/start` + exit code** |

**The first three signal failure by silence.** Neither of those CronJobs sends a `/start`
ping or a failure ping — unlike the two restic jobs and the Cloudflare job, which do both:

- `influx-backup` runs under `set -eu` with the ping as its **last** statement, so it
  pings **only on success**. Any earlier failure aborts the script before the ping and the
  check goes red on grace expiry. Nothing distinguishes "failed at step 2" from "never
  scheduled"; the Job's own status is the only place that detail exists, which is why
  `ttlSecondsAfterFinished` is 48h here.
- `ingest-freshness` (every 6h) pings the apple/garmin checks **only when that source's
  InfluxDB data is actually less than 24h old** — so a real ingest gap surfaces as a
  healthchecks.io alert instead of being masked by an unrelated cron firing on schedule.
  It **always exits 0 on purpose**: the signal is the absent ping, not a failed Job. Do
  not "fix" it into a non-zero exit.

- `cloudflare-analytics` (hourly) follows the **restic** pattern instead: `/start` at the
  top and `hc-ping.com/<uuid>/<rc>` at the exit, so a failure is distinguishable from a
  never-scheduled run without waiting for grace expiry. Pings are best-effort and can
  never fail the job.

All three are bounded by `timeZone: "UTC"` and `activeDeadlineSeconds` (3600 for
`influx-backup`, 300 for `ingest-freshness`, 1200 for `cloudflare-analytics`) — with
`concurrencyPolicy: Forbid` and no deadline, one hung run blocks every subsequent run with
nothing alerting. `influx-backup` sets `startingDeadlineSeconds: 3600` and
`cloudflare-analytics` 1800; `ingest-freshness` deliberately does not, since it runs again
in six hours anyway.

This namespace's checks watch **data freshness**, not the edge — which is why the
2026-08-18 Pomerium wedge went unnoticed (that proxy has since been removed).
External availability of `mcp.cynexia.com` and the other tunnel hostnames is
layer 3, in [uptime-kuma.md](uptime-kuma.md#monitor-list).

## Secret rotation

Per `health-*` 1Password item: edit the item → `make apply-homelab` → restart the
consuming pod. **No `direnv reload` step**: secrets are resolved per command by `op run`
at apply time, so nothing is cached in your shell to refresh (reload only matters if
`OP_SERVICE_ACCOUNT_TOKEN` itself changed). See
[apply-workflow.md](apply-workflow.md#rotating-a-secret).

InfluxDB tokens specifically: mint the replacement via the `health-influx-bootstrap`
pattern, update 1Password, apply, then delete the old auth server-side.

If a real secret value is ever disclosed, log it in `secrets-to-rotate.md` at the repo
root — see the honesty-box rule in `AGENTS.md`.

## Known state

**Verified working 2026-07-26:**

- The Claude.ai connector — read queries succeed, and a write probe correctly 403s
  (the MCP read-token has no write scope; server-log-verified).
- The HAE ingest path:
  `https://hae.cynexia.com/api/healthautoexport/v1/influxdb/ingest?target=iphone-rob`,
  bearer token `op://Homelab/health-hae/auth-token`, JSON, Batch Requests ON for large
  exports. Hourly aggregates cover 2020–2025, raw data from 2026-01-01. Keep the same
  URL and tags on every export or you get duplicate series.

**Verified working 2026-08-22:** the Access Managed OAuth path — unauthenticated
`GET /mcp` 401s at the edge with a `resource_metadata` pointer, the advertised
discovery chain serves Access metadata with a `registration_endpoint`. The
claude.ai connector is reconnected through the one-time-PIN flow and verified
2026-08-22: reads return data. Still open: Hermes fails dynamic client
registration with a redirect URI that is not yet identified — a client-side
issue, under investigation.

**Tech debt / deferred:**

- Garmin points can't carry a `person` tag (upstream limitation of the v1-compat write
  path); Apple points get a hardcoded `person=rob` static tag instead of a real
  multi-person model. The Phase 2 facade / person-registry design is expected to absorb
  this.
- Cloudflare Access service-token in front of the tunnel hostnames (the bearer token plus
  the Access app's email policy suffices for now; also the path to true end-to-end
  `health-mcp` monitoring — see [uptime-kuma.md](uptime-kuma.md)).
- Grafana alert rules (Phase 3, pending data accumulation).
- PSA hardening from `baseline` to `restricted`.
