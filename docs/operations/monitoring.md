# Failure detection: probes, deadlines and monitors

How failures in these two clusters are *noticed*. Written after a service ran wedged for
18.5 hours with Kubernetes reporting it perfectly healthy (see
[the Pomerium wedge](homelab-health.md#why-probes-exist-here-2026-08-18-pomerium-wedge)).

Read [§ What this does NOT catch](#what-this-does-not-catch) before trusting any of it.
Every layer below has a shape of failure it structurally cannot see, and for several
services that blind spot is the *likelier* incident.

## The decision rule

**Monitor the artifact, not the process.** A process being alive is evidence of nothing.
Three workload shapes, three correct instruments:

| Shape | Instrument | Why |
|---|---|---|
| Serves requests | Probe the real request path | kubelet can fix it by restarting; the failure is local and instantaneous |
| Produces an artifact on a schedule | **Dead-man's-switch on freshness** | Restarting does not fix "the data never arrived"; the absence of an event is only observable from outside |
| Must not hang | `activeDeadlineSeconds` (Jobs), progress probe (Deployments) | A bounded runtime is a contract you can enforce declaratively |

Corollary, and the rule to apply when reviewing a proposed probe: **if restarting the
thing cannot plausibly fix the failure, a probe is cargo cult.** Probe failure means "kill
and retry". Dead-man's-switch failure means "wake a human". Choosing the wrong one
produces either false confidence or self-inflicted outages — see
[§ Explicitly rejected](#explicitly-rejected) for the concrete cases where this repo said no.

## The four layers

1. **In-pod probes** — detect wedged request paths. Blind to tunnels, schedulers and
   background work.
2. **Job deadlines + dead-man's-switches** — detect hung, failed, or never-scheduled
   scheduled work.
3. **External monitors (uptime-kuma)** — detect "pod is green but the service is
   unreachable from outside". Cannot report its own death, hence layer 4.
4. **healthchecks.io dead-man's-switch on the monitor itself.**

Each layer exists because the one below it has a blind spot the layer above covers. Do
not remove one on the grounds that another "already checks that".

## Probe policy

- **Readiness on every long-running container** where a meaningful check exists. Cheap and
  safe: worst case the pod leaves Service routing.
- **Liveness only where the failure is detectable by that probe AND restart is a safe
  remedy.** Every service in both clusters is single-replica; an over-eager liveness probe
  manufactures outages that would not otherwise have happened.
- **Startup probes for anything with migrations or a slow boot**, so liveness cannot fire
  during startup. Skipping this is the most common way a probe rollout causes an incident.
- **Always set `timeoutSeconds` explicitly.** The Kubernetes default is 1s, which produces
  false positives on a loaded node — and with the timeout unset, a hang is
  indistinguishable from slowness.
- **Liveness thresholds must be much laxer than readiness**, so a pod goes unready long
  before it is killed. Readiness sheds traffic; liveness destroys state.
- **The probe must exercise the data plane.** A control-plane health endpoint reports on
  the wrong process. The vendor-documented Pomerium probe would have stayed green through
  our entire outage — see [§ Pomerium](#pomerium-the-probe-target-is-deliberately-not-the-documented-one).
- **No probe of any kind on a sidecar whose job is backup.** *Any* failing probe on a
  sidecar takes the application offline: readiness directly, liveness by way of
  CrashLoopBackOff (a container that is not Running is not Ready, so the Pod is dropped
  from its EndpointSlice). A backup fault must never cost you the service — see
  [§ Why the sidecars have no probes](#why-the-sidecars-have-no-probes).

House defaults, used unless a service's own note says otherwise:

| Probe | period | timeout | failureThreshold |
|---|---|---|---|
| liveness | 30s | 10s | 6 |
| readiness | 15s | 5s | 3 |
| startup | 10s | 5s | sized to the boot budget (30 = 5 min, 18 = 3 min, 12 = 2 min) |

## Probe inventory

### VPS cluster (`vps/workloads/`)

| Container | Target | Why this target |
|---|---|---|
| n8n | liveness `/healthz`, readiness `/healthz/readiness` (:5678) | Deliberately split: readiness gates on the DB, liveness must not — a transient sqlite lock must never cause a crashloop |
| freshrss | `/api/` (:80) | Upstream's own `cli/health.php` target. The **trailing slash is load-bearing**: `index.php` 400s on a non-empty `PATH_INFO` |
| karakeep | `/api/health` (:3000) | Upstream Dockerfile `HEALTHCHECK` and the official Helm chart agree |
| meilisearch | `/health` (:7700) | The deepest check of the set — search queue, task DB and auth store. Liveness is right here: its `mustRestart` state literally asks to be recycled |
| changedetection | `/` (:5000) | No upstream `HEALTHCHECK` (open since 2022) and the image ships neither curl nor wget. A 302-on-auth counts as probe success, so this survives enabling a password |
| sockpuppetbrowser | `/stats` on **:8080** | Shares the asyncio event loop with the CDP server, so a wedged loop fails it. **Never httpGet :3000** — the websockets library rejects a plain GET with 426 and you get a permanent restart loop. The `stats` port is on the container only, not the Service: kubelet dials the pod IP |
| umami | `/api/heartbeat` (:3000) | Upstream compose target. Static `{ok:true}` — see [the caveat](#what-this-does-not-catch) |
| uptime-kuma | `/api/entry-page` (:3001) | Unauthenticated JSON that reads a setting from sqlite through a 60s cache, so it touches the DB at least once a minute. Also sets `enableServiceLinks: false` |
| postgres (umami) | readiness + startup `pg_isready`; liveness **only** as `sh -c 'pg_isready -q …; test $? -lt 2'` | Exit 1 means "rejecting connections, e.g. crash recovery" and must count as **alive**. A naive `pg_isready` liveness SIGKILLs the postmaster mid-recovery and never converges |
| quiesce sidecars (n8n, karakeep, uptime-kuma, freshrss) and umami's `pg-dump-sidecar` | **no probes at all** | A freshness liveness probe was added here and then **deliberately removed** — see [§ Why the sidecars have no probes](#why-the-sidecars-have-no-probes). Do not add one back |

### Homelab cluster

| Container | Target | Note |
|---|---|---|
| pomerium | liveness `/ping` (:80), readiness `/readyz` (:28080), startup `/startupz` (:28080) | See below — the liveness target is chosen against the vendor docs, on purpose |
| pomerium `mcp` sidecar | liveness + readiness `tcpSocket` on `mcp-http` | The MCP server exposes no health endpoint. TCP detects process death, **not** a wedged handler. Documented as partial coverage, not as a solution |
| cloudflared (health) | liveness + readiness `/ready` (:2000) | The connector's own readiness endpoint. Note this Deployment has **no Service** — readiness routes no traffic here; it gates the rolling update (the old connector is not torn down until the new one has registered with Cloudflare) and makes "connected" visible in pod status |
| influxdb | `/health` | — |
| grafana | `/api/health` | — |
| apple-health-ingester | `tcpSocket` | No HTTP health endpoint upstream |
| sonarr, radarr, sabnzbd, emby, hydra2 | `/` on the app port, startup + liveness + readiness | Readiness was missing until 2026-08; Traefik routed to them while they were still booting |
| jottacloud-backup | **no probe, by design** | Its old liveness probe was a verified no-op — see [§ Explicitly rejected](#explicitly-rejected). `activeDeadlineSeconds: 21600` is what actually bounds the run |
| garmin-grafana | **no probe, by design** | It serves nothing and produces an artifact on a schedule, so the correct instrument is the `health-garmin-ingest` dead-man's-switch. Both probe shapes considered for it were rejected outright — see [§ Explicitly rejected](#explicitly-rejected) |
| traefik | liveness + readiness `exec: traefik healthcheck --ping` | **Exec, not httpGet, on purpose:** `hostNetwork: true` means kubelet picks one of the host's interfaces as the pod IP (here the `10.10.10.10` storage NIC), and Traefik's ping endpoint is bound to its internal entrypoint, not that NIC. The CLI queries `/ping` over loopback and works regardless |
| keel (both clusters) | liveness + readiness `/healthz` | Liveness 15s × 6 (90 s to restart) is strictly laxer than readiness 10s × 3 (30 s to unready) |
| cloudflared (VPS) | liveness + readiness `/ready` (:2000) | Same shape and same no-Service caveat as the health tunnel's connector above. This is the *only* ingress path into the VPS cluster, so a wedged connector is a total outage |

### Why the sidecars have no probes

This reverses something an earlier round of this work — and the spec that drove it —
explicitly endorsed, so the reasoning is recorded rather than left to look like fatigue.

There are five backup sidecars: four sqlite quiesce loops (n8n, karakeep, uptime-kuma,
freshrss) and umami's `pg_dumpall` loop. The four sqlite ones briefly carried a **freshness
liveness probe** — `stat -c %Y` on the published snapshot, failing past 15h. It is gone,
and the `pg-dump-sidecar` never had one. Do not add either back.

**Why it was justified, and why that justification expired.** The original argument was
self-healing: a failed `apk add` left the container with no `sqlite3`, and a restart
genuinely repaired that. The loop now retries `apk add` itself on a 5-minute backoff, so
against a transient fault the probe only duplicated what the loop already does.

**Why it is actively harmful against a permanent fault** — a corrupt database, a full
disk, a path that moved under an app upgrade. The probe restarts a container that
restarting cannot repair, the restarts back off into `CrashLoopBackOff`, and then:

> a container that is not Running is not Ready → a Pod with a non-Ready container is
> dropped from its EndpointSlice → cloudflared 502s the app.

**A backup fault would have taken a working application offline.** That is the same outage
the spec refused sidecar *readiness* probes to avoid; liveness reaches it by a longer road.

**This is the spec's own rule biting the one probe the spec endorsed:** *if restarting the
thing cannot plausibly fix the failure, a probe is the wrong instrument.* Worth sitting
with — the rule was written down, agreed, and then violated in the one case that felt like
an exception. A reader who sees only the outcome will assume we got lazy; the opposite
happened.

**Detection did not disappear, it moved to the artifact.** The restic verification gate
asserts a per-app — and per-FreshRSS-user — snapshot exists and is fresh, and pings
healthchecks.io red otherwise. Detection latency goes from roughly 45 minutes to at worst
a day. That is the right scale for a backup fault, and it buys immunity from ever taking
an application down for one.

### What the sidecar loops do instead

`set -e` is **deliberately absent**. If the sidecar exits, kubelet restarts it, and a
persistent fault becomes the same CrashLoopBackOff → EndpointSlice → 502 chain described
above — strictly worse, because the snapshotter stops running too. Instead each loop:

- runs under `set -u` only, and **logs loudly to stderr** rather than exiting;
- backs off **300 s** on any failure (failed `apk add`, missing source DB, failed
  snapshot) and **43200 s** (12h) on success;
- publishes atomically — `.backup` to a `.tmp`, then `mv` — so a failed run leaves the
  previous good snapshot in place rather than a half-written one;
- **asserts the snapshot has content before publishing:**
  `sqlite3 <tmp> 'select count(*) from sqlite_master'`, refusing to publish anything with
  zero schema objects. This closes a real hole: a truncated or empty source DB makes
  `.backup` succeed and emit a structurally valid but **empty** ~4 KB database with a
  current mtime, which passed every freshness check — probe and gate alike — because mtime
  was the only property either one looked at.

So "fails loudly" here means **logs**, not exits. The alarm is raised by the gate, not by
the container's own status. If you are debugging a missing snapshot, read the sidecar's
stderr; its restart count will be zero and tells you nothing.

umami's `pg-dump-sidecar` now follows the same shape (`set -u`, retry-on-failure with a
300 s backoff, atomic publish, `rm -f` the partial tmp) rather than the old
`|| echo "pg_dumpall failed"` swallow. It has no content assertion — `pg_dumpall`'s own
exit code is the check there.

### Pomerium: the probe target is deliberately *not* the documented one

Verified against the live pod, not from documentation:

- `:80/ping` → **200 `OK` in 6.7 ms**, unauthenticated, served on Envoy's catch-all vhost
  (no `Host:` header needed, which is why the probe has no `host:` field — kubelet dials
  the pod IP).
- `:28080/readyz` on the pod IP → connection refused until `health_check_addr: :28080` is
  set in the Pomerium ConfigMap. The default is `127.0.0.1:28080`, which kubelet cannot
  reach. The upstream example probe is broken as written for that reason.

Pomerium's docs and its Ingress Controller both probe `/healthz` on :28080 — a plain Go
listener with **Envoy nowhere in its path**. Its `envoy.server` field is a ≤30s-stale
cache of the Envoy admin thread reporting lifecycle state LIVE, which it was for all 18.5
hours of our outage. So:

- **Liveness is `:80/ping`** — the only endpoint that traverses listener → worker → HCM →
  ext_authz → control-plane cluster, i.e. the exact path that returned zero bytes.
  `periodSeconds: 15`, `failureThreshold: 4` ≈ 60s to restart, versus 18.5 h observed, and
  deliberately tighter than upstream's ~10 minutes.
- **Readiness is `:28080/readyz`** — complementary, not redundant: it catches
  databroker/config-sync failures that `/ping` cannot see.
- **Startup is `:28080/startupz`** with a 5-minute budget, so the tight liveness cannot
  kill the pod during databroker sync.

If someone later "corrects" this to the documented `/healthz`, they have re-created the
outage's blind spot. That is why this paragraph exists.

## Scheduled work: deadlines and dead-man's-switches

Every CronJob in both clusters sets:

| Field | Value | Why |
|---|---|---|
| `timeZone: "UTC"` | all | Without it the schedule is interpreted in kube-controller-manager's local zone |
| `startingDeadlineSeconds` | 3600 (300 for jottacloud; unset for `ingest-freshness`, which runs every 6h anyway) | A missed window is retried for that long, then skipped — rather than silently lost |
| `activeDeadlineSeconds` | restic 14400 (4h), influx-backup 3600, ingest-freshness 300, jottacloud 21600 | With `concurrencyPolicy: Forbid`, one hung run blocks **every** subsequent night and nothing alerts |
| `ttlSecondsAfterFinished` | 259200 (3d) on the restic jobs | A Friday failure is still forensically available on Monday. 86400 deleted the evidence before anyone looked |
| `terminationGracePeriodSeconds` | **not set** — removed | It looked like it let restic release its repo lock on SIGTERM. It did not: busybox `ash` is PID 1 and neither forwards TERM to the running restic child nor acts on it while a foreground child runs (verified against `restic/restic:0.17.3` — a TERM trap in the child never fired), and `exec`ing restic isn't available because the wrapper must outlive it to send the exit-code ping. All it bought was two extra minutes on every teardown. Lock recovery is `restic unlock` at the head of the next run |

The 4h `activeDeadlineSeconds` on restic is an **opening guess, not a measurement**. Resize
it from the durations healthchecks.io records over the first weeks of running.

### The restic ping wrapper

Both restic CronJobs wrap the backup as:

```
ping_hc start  →  snapshots || true  →  unlock, backup, forget --prune, check  →  [VPS: verification gate]  →  ping_hc "$rc"
```

(`restic snapshots || true` is a deliberate no-fail step: it prints the current repository
contents into the job log for forensics without letting a listing hiccup fail the run.)

- The `/start` ping is what detects **started-but-never-finished** — the failure mode that
  `Forbid` + no deadline turns into an indefinite silent outage. It also records durations.
- The exit-code ping (`hc-ping.com/$UUID/$rc`) distinguishes success from failure.
- Pings themselves never fail the job (`|| true`), and are `wget -T 10` so a hung
  healthchecks.io cannot hang the backup.
- The steps are chained with explicit `&&` rather than `set -e` inside a group: `errexit`
  is ignored for any command in an AND-OR list, so `{ set -e; … } || rc=$?` would keep
  running past a failure and report the *last* command's status. Do not "simplify" this.
- `restic unlock` runs first; without `--remove-all` it removes only stale locks. This is
  also the **only** lock recovery there is, now that `terminationGracePeriodSeconds` is
  gone — a run killed by `activeDeadlineSeconds` leaves its lock behind and the next run
  clears it.

Homelab has no sqlite sidecars (the media apps write their own zip backups into
`/config/Backups/`), so it has no verification gate. The VPS job does.

### The VPS backup verification gate

The consumer-side check that closes the loop on the quiesce sidecars: a dead sidecar
becomes a *backup alert* instead of nothing, and a sidecar deleted from the manifest is
caught too. It is two complementary checks, not one.

**1. Expected-set assertion** — authoritative. A labelled `EXPECTED_SNAPSHOTS` list, each
entry asserted **present** and **fresh** (15h threshold, a 3h margin over the sidecars' 12h
period), with the label and the failing path named in the output (`MISSING`, `STALE` or
`UNREADABLE` are distinguished):

| Service | Expected snapshot |
|---|---|
| n8n | `/data/*_vps_n8n-data/database.sqlite.restic` |
| karakeep | `/data/*_vps_karakeep-data/db.db.restic` |
| uptime-kuma | `/data/*_vps_uptime-kuma-data/kuma.db.restic` |
| umami (postgres) | `/data/*_vps_umami-pg-data/dump.sql.restic` |
| freshrss | **iterates the source glob** `/data/*_vps_freshrss-data/users/*/db.sqlite` and asserts a sibling `.restic` per user. Zero user DBs is *correct* and passes with a note |

**2. Broad sweep — advisory only.** Any `*.restic` under `/data` past the threshold is
printed as a `WARNING` and **does not fail the job**. It used to fail it, which meant a
single orphaned PV directory left behind by a deleted workload would pin the gate
permanently red and train everyone to ignore it. The sweep still earns its place: it names
stale files nobody listed, including ones from a service that was removed. (The one thing
that *does* fail from the sweep is `find` itself erroring — an unreadable `/data` is a real
fault, not an advisory one.)

#### "Newest of a glob" is a dangerous shape for a freshness check

Worth recording as a worked example, because both the removed probe and the first version
of this gate got it wrong in the same way. FreshRSS keeps one database **per user**, and
both checks took the *newest* snapshot matching the glob. So if one user's DB went corrupt
and stopped being snapshotted, every other user's snapshot kept the newest-mtime fresh —
forever. That user's data was silently never quiesced, and both checks stayed green.

The fix in both places is to **iterate the source objects and assert a sibling artifact for
each**, rather than aggregating over the artifacts. Any check that reduces a set to its
maximum can only detect "all of them stopped"; the failure you actually care about is
usually "one of them stopped". The four single-DB services still resolve their glob to the
newest match, but that glob is a PVC directory that should match exactly one path — a
different situation from a per-user fan-out.

**Why the explicit list exists.** A bare staleness sweep cannot distinguish three states
that produce identical output — no stale files:

- no snapshots because the apps legitimately have no databases,
- no snapshots because `/data` is empty or unmounted,
- three of four present and one app's PVC blank.

All three passed green before, and restic will happily back up an empty tree and ping
success. Naming the files is the only way to tell a legitimately absent snapshot from a
missing one. **Verified behaviour:** an empty `/data` used to ping healthchecks.io with
success; it now pings failure, printing one named path per missing snapshot.

**Maintenance contract: adding a sqlite-backed service means adding its snapshot to
`EXPECTED_SNAPSHOTS`.** Forget, and that service's backups are unverified — silently. That
trade is deliberate: an explicit list somebody has to maintain beats a wildcard that
silently accepts nothing at all.

**Why the entries are globs**, which otherwise looks like sloppiness:
local-path-provisioner names each PVC directory `<pvName>_<namespace>_<pvcName>` and
`pvName` is a random UUID, so the leading component cannot be written literally. An
unmatched glob survives literally and fails the `-f` test — which is exactly the "missing"
verdict wanted.

**Why the gate runs last, after the backup.** Making it a precondition is the intuitive
move and it is wrong: one stale or missing sqlite snapshot would then skip the entire
night's backup of everything else, and the backup is worth more than the gate. So: protect
the data first, then fail the job so the fault still turns the healthchecks.io ping red.
The gate only promotes to failure if restic itself succeeded — a real restic failure keeps
its own, more specific exit code. Do not "fix" this by moving it back above the chain.

## healthchecks.io check inventory

| Check | 1Password reference | Period / grace | Pinged by |
|---|---|---|---|
| `homelab-restic` | `op://Homelab/b2-restic/healthcheck-uuid` | 1d / 2h | `homelab/backup/restic-cronjob.yaml` (`/start` + exit code) |
| `vps-restic` | `op://VPS/b2-restic/healthcheck-uuid` | 1d / 2h | `vps/backup/restic-cronjob.yaml` (`/start` + exit code) |
| `vps-uptime-kuma-alive` | `op://VPS/uptime-kuma/healthcheck-uuid` | 5m / 15m | An uptime-kuma monitor — see [the self-monitor](#the-self-monitor-layer-4) |
| `health-apple-ingest` | `op://Homelab/health-healthchecks/apple-uuid` | 1d / 12h | `ingest-freshness` CronJob, **only if** InfluxDB data is <24h old |
| `health-garmin-ingest` | `op://Homelab/health-healthchecks/garmin-uuid` | 1d / 12h | as above |
| `health-influx-backup` | `op://Homelab/health-healthchecks/backup-uuid` | 1d / 6h | `influx-backup` CronJob — **success only**: the script is `set -eu` with the ping as its last statement, so any failure aborts before the ping |
| jottacloud-backup | `op://Homelab/jottacloud-backup/HEALTHCHECK_UUID` | per its 6-hourly schedule | `jottacloud-backup` CronJob, from inside the image's own `backup.sh` — the ping semantics live upstream, not in this repo |

The read-only API key for the healthchecks.io account is
`op://Homelab/healthchecks.io/read-only-api-key`.

Use it to verify a check actually went green after applying, rather than inferring
success from a job exiting 0:
`curl -sS -H "X-Api-Key: $(op read 'op://Homelab/healthchecks.io/read-only-api-key')" \
https://healthchecks.io/api/v3/checks/ | jq -r '.checks[] | "\(.status)\t\(.name)\t\(.last_ping)"'`

**Only the two restic jobs ping `/start` and an exit code.** The other three ping on
success only, so for them a failure and a never-scheduled run are the same signal:
silence, then a grace-expiry alert. That is adequate for a dead-man's-switch but gives no
duration data and no "started but never finished" detection — the reason the restic jobs
were given the fuller treatment first. New CronJobs should follow the restic pattern.

`ingest-freshness` **always exits 0 on purpose**: the alerting signal is the *absent* ping,
not a failed Job. A stale data source must not also surface as a Job failure, or the two
signals fight. Do not "fix" that into a non-zero exit.

Adding a new check means **four** edits — `.env.tpl`, `ENVSUBST_VAR_NAMES`,
`REQUIRED_VARS`, and the `${VAR}` placeholder in the manifest. `check-vars-consistency`
catches a var missing from `REQUIRED_VARS`; **nothing** catches one missing from
`ENVSUBST_VAR_NAMES`, which ships the literal `${VAR}` as the ping UUID and silently
disables the check. See
[apply-workflow.md](apply-workflow.md#adding-a-secret-is-four-edits-not-three).

## uptime-kuma: manual runbook (layer 3)

**Monitors are created by hand in the UI and documented here.** There is no supported
programmatic path in uptime-kuma v2:

- Monitor CRUD is **Socket.IO only**. The sole API-key-protected HTTP route is `/metrics`.
- The tracking issue for a REST API (#118) has been open since 2021, and two
  community REST-bridge PRs were closed unmerged.
- The community Python wrapper claims support only up to v1.23.2.

The alternatives — coding against an unversioned Socket.IO contract, or writing into
`kuma.db` behind the running process's back — are not worth it for a handful of monitors.
The sqlite quiesce sidecar means hand-created config is backed up nightly, so a rebuild
restores the monitors rather than requiring them to be retyped.

Keep this section in sync by hand when you add or change a monitor.

### Settings for every HTTP monitor

| Field | Value | Why |
|---|---|---|
| Monitor type | HTTP(s) | — |
| Heartbeat interval | **120 s** | — |
| Retries | **3** | The default of 0 pages on a single blip |
| Heartbeat retry interval | **60 s** | — |
| Request timeout | **20 s** | Long enough to distinguish "slow" from "wedged" |
| **Max redirects** | **0** | See the Access trap below. This is the field that silently defeats the whole layer |
| Accepted status codes | per monitor, below | — |
| Certificate expiry / ignore TLS | defaults | TLS terminates at the Cloudflare edge |

Keyword monitors are the wrong instrument here: the keyword is only evaluated *after* the
status-code check passes, so it adds nothing a status check has not already caught.
uptime-kuma's `saveErrorResponse` captures Cloudflare's error body into the alert, so a
`1033` (tunnel down) is diagnosable without one.

### The Cloudflare Access trap

An Access-protected hostname answers an unauthenticated request with a **302 to the
Cloudflare login page**. With uptime-kuma's default `maxredirects: 10` the monitor follows
that redirect, receives a 200 from Cloudflare's own login app, and reports the service
**UP while the tunnel, the pod and the node could all be dead**. The monitor would then be
worse than no monitor, because it manufactures confidence.

Two independent mitigations, both applied:

1. **`maxredirects: 0`** on every monitor, so the 302 is not followed.
2. **Cloudflare Access service-token headers** on the Access-protected monitors, so the
   request is authorised and reaches the origin. The token exists and covers
   `analytics` / `rss` / `keep` / `watch` / `n8n`.

Add the headers in the monitor's **Headers** box as JSON:

```json
{
  "CF-Access-Client-Id": "<op://VPS/cloudflare/CF-Access-Client-Id>",
  "CF-Access-Client-Secret": "<op://VPS/cloudflare/CF-Access-Client-Secret>"
}
```

Read the real values with `op read` at the moment you paste them; never write them into a
file in this repo. If a service-token header is missing or wrong, the monitor sees the 302
and — with `maxredirects: 0` — goes DOWN with a 302. That is the correct, loud failure.

Bypassed paths are not a substitute: an Access bypass glob of `/foo/*` does **not** match
the bare path `/foo`, so a bypassed health path needs both destinations registered.

### Monitor list

VPS cluster, all Access-protected — set the two headers above on each. The paths mirror
each service's in-pod probe target (verified unauthenticated in-cluster), so a monitor
failing while the probe passes isolates the fault to the tunnel or the edge. Confirm each
URL's status once with the `curl -w '%{http_code}'` form below before trusting the monitor.
`uptime.cynexia.com` is deliberately **not** in this list: uptime-kuma checking its own
hostname proves nothing it could report, which is what layer 4 is for.

| Monitor | URL | Accepted status codes |
|---|---|---|
| `vps-analytics` | `https://analytics.cynexia.com/api/heartbeat` | `["200-299"]` |
| `vps-rss` | `https://rss.cynexia.com/api/` | `["200-299"]` |
| `vps-keep` | `https://keep.cynexia.com/api/health` | `["200-299"]` |
| `vps-watch` | `https://watch.cynexia.com/` | `["200-299"]` — add `302` if changedetection's own password is ever enabled, since `maxredirects: 0` will not follow its login redirect (kubelet's probe treats the same 302 as success, so the in-pod probe won't warn you) |
| `vps-n8n` | `https://n8n.cynexia.com/healthz` | `["200-299"]` |

Homelab health tunnel — **no Access in front**, so no headers:

| Monitor | URL | Accepted status codes |
|---|---|---|
| `health-mcp` | `https://mcp.cynexia.com/` | `["200-299", "401"]` |
| `health-hae` | `https://hae.cynexia.com/` | pin to the observed unauthenticated status (see below) |
| `health-authenticate` | `https://authenticate.cynexia.com/` | pin to the observed status |

`401` is accepted for `mcp.cynexia.com` on purpose: **a fast 401 is a true end-to-end
signal** — it proves the tunnel is up, cloudflared is connected, and the Pomerium pod is
serving, which is precisely what was false during the wedge. A timeout, a 5xx or a
Cloudflare `1033` all fall outside the accepted set and mark the monitor DOWN.

For the two hostnames whose unauthenticated response is not recorded here, observe it once
and pin the set to exactly what you see, rather than guessing:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://hae.cynexia.com/
```

Widening the accepted set to swallow whatever appears is how a monitor stops being a
monitor. If the number surprises you, find out why before adding it.

### The self-monitor (layer 4)

uptime-kuma shares the VPS tunnel, node and scheduler with almost everything it watches,
and **cannot report its own death**. A silently-stopped monitor is indistinguishable from
everything being fine — the most dangerous state in this document.

So: create one more HTTP monitor whose **URL is a healthchecks.io ping URL**.

| Field | Value |
|---|---|
| Name | `self → healthchecks.io` |
| URL | `https://hc-ping.com/<op://VPS/uptime-kuma/healthcheck-uuid>` |
| Interval | **300 s** (5 min) |
| Accepted status codes | `["200-299"]` |
| Max redirects | 0 |

The healthchecks.io check `vps-uptime-kuma-alive` is configured 5m period / 15m grace. If
the pod, the node or uptime-kuma's own scheduler dies, the pings stop and healthchecks.io
alerts — from outside both clusters. This is the highest-value single item in the external
layer.

Note the inversion: this is a monitor being *used as a pinger*. Its own UP/DOWN status is
irrelevant; the signal is at healthchecks.io.

## What this does NOT catch

Probes fix the *hung request path* class. They do not fix the *silently stopped background
work* class, and for several of these services the latter is the likelier incident. This
list is the reason the document exists — a reader who over-trusts the probe table above is
the failure mode being guarded against.

| Service | The probe is green while… |
|---|---|
| **umami** | `/api/heartbeat` is a static `{ok:true}` that never touches Prisma. It returns 200 through any database failure (upstream #3417, connection-pool exhaustion). This probe buys Node-wedge detection only, **not** DB-outage detection |
| **changedetection** | Upstream #4214: 134 watches went **23 days** unchecked while `/` returned 200 and `/worker-health` reported healthy — the *ticker* died, not the workers. Only `overdue_watches` from `/api/v1/systeminfo` sees it. Wire that as an external json-query alert, **never** as liveness: a restart does not fix a scheduling bug |
| **uptime-kuma** | The HTTP server and the monitor scheduler are independent (#4967). A monitoring tool that has silently stopped monitoring is the worst version of this bug, and no in-pod probe detects it. Hence the layer-4 dead-man's-switch |
| **karakeep** | `/api/health` is a hardcoded literal in the *web* process and cannot observe the worker at all. Stuck-queue reports (#1802, #2704) all leave it returning 200. The detector is the `karakeep_queue_jobs` metric (`pending > 0 && running == 0`), not a probe |
| **freshrss** | `/api/` never opens the database, and feed refresh runs from a separate `crond`. A dead cron serves the UI perfectly and silently stops fetching news |
| **garmin-grafana** | `write_points_to_influxdb()` catches InfluxDB errors, logs them and **returns normally**, after which the caller advances the watermark — so an InfluxDB outage causes permanent, unrecoverable data loss for that window with the process staying Running/Ready. Covered by `ingest-freshness`; no probe would improve on it |
| **pomerium `mcp` sidecar** | Its probes are `tcpSocket`. A wedged HTTP handler with a live listener passes them. The MCP server exposes no health endpoint; this is acknowledged partial coverage |
| **homelab services generally** | The external layer runs on the VPS, which has no route to `*.cynexia.net`. Only the three health-tunnel hostnames get layer-3 coverage; sonarr/radarr/sabnzbd/emby/hydra2/grafana have probes and nothing external |
| **the VPS backup verification gate** | It proves each expected snapshot **exists, is recent, and has at least one schema object** (the sidecars refuse to publish a schema-less snapshot). It does **not** prove the contents are complete or uncorrupted: a snapshot missing rows, or with a corrupt page below the `sqlite_master` read, passes everything here and surfaces at restore time — see below |

Queued, not yet configured: a changedetection `overdue_watches` json-query monitor and a
karakeep queue-depth alert. Both need an API credential in the monitor and are the correct
detectors for the changedetection and karakeep rows above.

**Snapshot integrity is only partly verified, deliberately.** The sidecars'
`select count(*) from sqlite_master` assertion closes the worst case — a fresh, valid,
**empty** snapshot published from a truncated source, which used to pass every check
because mtime was all anything looked at. It proves a schema exists. It does not prove the
data is there.

Closing the rest means `sqlite3 <file> 'pragma integrity_check'` (and a postgres-dump
equivalent) inside the gate, which means adding sqlite to the `restic/restic` image. Out
of scope for now, and recorded here because "backup verification gate" reads as covering
exactly this and does not. If backup integrity is ever in question — a suspected corrupt
restore, a sidecar behaving oddly — this is the next thing to build, and until then a
periodic manual restore drill is the only real proof.

## Explicitly rejected

Recorded so nobody re-adds them believing they were an oversight.

- **Any probe at all on `garmin-grafana`.** Two shapes were considered and both rejected;
  the service is covered by the `health-garmin-ingest` freshness check instead.
- **Token-mtime freshness probe for garmin.** The token file is written only on the
  interactive credentials-login path — never on resume or proactive refresh — so its mtime
  changes roughly **once a year**. The probe would detect nothing and false-positive on any
  sane threshold.
- **Staleness-based liveness for garmin.** Freshness depends on the user syncing a watch. A
  weekend away would restart the pod repeatedly, and each restart with a stale token
  **fires an MFA SMS at the operator**. Staleness → notify a human, never restart.
- **Generalising the jottacloud liveness probe.** It was a verified no-op that could not
  fail: `backup.sh` is PID 1 for the whole run and never `exec`s over itself, so
  `ps | grep backup.sh` always matched, and `/tmp/backup-completed` (the fallback branch)
  is created by no upstream script. It also measured presence, not progress — a stalled
  rclone looked perfectly alive. Deleted. `activeDeadlineSeconds: 21600` in the same file
  is what actually bounds that job, and *that* is the pattern to generalise.
- **Naive `pg_isready` liveness on postgres** (the Bitnami chart shape: exit-0-only, 60s
  tolerance). Same narrow detection as the `test $? -lt 2` form, plus a recovery loop that
  never converges on a single-replica local-path PVC.
- **Any probe on the backup sidecars — readiness *or* liveness.** Readiness takes the Pod
  out of its EndpointSlice directly; liveness gets there via CrashLoopBackOff. Either way a
  backup fault takes the application offline. The freshness liveness probe was added, then
  removed for exactly this reason — see
  [§ Why the sidecars have no probes](#why-the-sidecars-have-no-probes).
- **`tcpSocket` on sockpuppetbrowser :3000 presented as a hang fix.** The kernel completes
  handshakes from the accept backlog while the event loop is fully blocked, so it detects
  process death only. Listed here so nobody later mistakes it for coverage.
- **A NetworkPolicy in front of the MCP server.** Inert on this cluster's flannel CNI —
  see [homelab-health.md](homelab-health.md#mcp-is-a-sidecar-not-a-standalone-deployment).

## Rolling out a probe change

A probe rollout that restarts pods is a **failed** rollout, not a partial success.

1. Confirm the probe path's expected status **from inside the cluster** with a throwaway
   `alpine/k8s:1.36.0` pod before trusting it. Do not take it from vendor documentation —
   the Pomerium case is exactly what that produces.
2. `make diff-homelab` / `make diff-vps`, then apply.
3. `kubectl -n <ns> rollout status deploy/<name>` — completes without a restart loop.
4. `kubectl -n <ns> get pods` ten minutes later — **0 restarts**.
5. For CronJob changes: `kubectl create job --from=cronjob/<name> <name>-manual`, then
   confirm the healthchecks.io check goes green and records a duration.
6. After a few weeks, resize `activeDeadlineSeconds` from the recorded durations.
