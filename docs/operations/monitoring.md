# Failure detection: probes, deadlines and monitors

How failures in both clusters get noticed. The manifests carry per-probe rationale in
comments; this file carries the policy, the inventory, and the failures none of it catches.
Read [What this does not catch](#what-this-does-not-catch) before you trust a green signal.

## The decision rule

Monitor the artifact, not the process. A live process proves nothing.

| Workload shape | Instrument | Why |
|---|---|---|
| Serves requests | Probe the real request path | kubelet repairs it by restarting; the failure is local and immediate |
| Produces an artifact on a schedule | Dead-man's-switch on freshness | Restarting does not deliver data that never arrived. Absence of an event is only visible from outside |
| Must not hang | `activeDeadlineSeconds` (Jobs), progress probe (Deployments) | A bounded runtime is a contract you enforce declaratively |

**If restarting the thing cannot plausibly fix the failure, a probe is the wrong
instrument.** Probe failure means "kill and retry"; dead-man's-switch failure means "wake a
human". The wrong choice buys false confidence or a self-inflicted outage.

## The four layers

| Layer | Instrument | Blind to |
|---|---|---|
| 1 | In-pod probes | Tunnels, schedulers, background work |
| 2 | Job deadlines and dead-man's-switches | Request-path wedges |
| 3 | External monitors (uptime-kuma) | Its own death |
| 4 | healthchecks.io switch on the monitor itself | Anything it is not pinged by |

Each layer covers the one below's blind spot. Do not drop one because another "already
checks that".

## Probe policy

- Put a readiness probe on every long-running container that serves traffic. Worst case,
  the pod leaves Service routing.
- Add liveness only when that probe detects the failure **and** a restart repairs it.
  Every service here is single-replica, so an eager liveness probe manufactures outages.
- Add a startup probe to anything with migrations or a slow boot, so liveness cannot fire
  during startup.
- Set `timeoutSeconds` on every probe. The 1s default false-positives on a loaded node.
- Keep liveness thresholds strictly laxer than readiness. Readiness sheds traffic;
  liveness destroys state.
- Probe the data plane. A control-plane health endpoint reports on the wrong process.
- Put no probe of any kind on a backup sidecar. See
  [Why the sidecars have no probes](#why-the-sidecars-have-no-probes).

Defaults, unless a service's entry says otherwise:

| Probe | period | timeout | failureThreshold |
|---|---|---|---|
| liveness | 30s | 10s | 6 |
| readiness | 15s | 5s | 3 |
| startup | 10s | 5s | sized to the boot budget (30 = 5 min, 18 = 3 min, 12 = 2 min) |

## Probe inventory

### VPS cluster

| Container | Target | Note |
|---|---|---|
| n8n | liveness `/healthz`, readiness `/healthz/readiness` (:5678) | Split on purpose: readiness gates on the DB, liveness must not, so a sqlite lock cannot crashloop it |
| freshrss | `/api/` (:80) | The trailing slash is load-bearing; `index.php` returns 400 on a non-empty `PATH_INFO` |
| karakeep | `/api/health` (:3000) | Upstream Dockerfile and Helm chart agree |
| meilisearch | `/health` (:7700) | Checks search queue, task DB and auth store. Liveness fits: its `mustRestart` state asks to be recycled |
| changedetection | `/` (:5000) | No upstream healthcheck exists, and the image ships no curl or wget. A 302 on auth counts as success |
| sockpuppetbrowser | `/stats` (:8080) | Shares the asyncio loop with the CDP server. Never probe :3000 — websockets rejects a GET with 426 and the pod restart-loops. The port is on the container only |
| umami | `/api/heartbeat` (:3000) | Shallow — see [What this does not catch](#what-this-does-not-catch) |
| uptime-kuma | `/api/entry-page` (:3001) | Unauthenticated JSON that reads sqlite through a 60s cache. Also sets `enableServiceLinks: false` |
| postgres (umami) | readiness and startup `pg_isready`; liveness as `sh -c 'pg_isready -q …; test $? -lt 2'` | Exit 1 means "rejecting connections during recovery" and counts as alive. A plain `pg_isready` liveness kills the postmaster mid-recovery and never converges |
| backup sidecars (5) | none | Deliberate — see below |

### Homelab cluster

| Container | Target | Note |
|---|---|---|
| pomerium | liveness `/ping` (:80), readiness `/readyz` (:28080), startup `/startupz` (:28080) | Liveness targets the data plane, against the vendor documentation. Full reasoning: [homelab-health.md](homelab-health.md#the-probe-target-is-deliberately-not-the-documented-one) |
| pomerium `mcp` sidecar | liveness and readiness `tcpSocket` | The MCP server exposes no health endpoint. TCP detects process death, not a wedged handler |
| cloudflared (both clusters) | liveness and readiness `/ready` (:2000) | Neither Deployment has a Service, so readiness gates the rolling update and shows connector state. It routes nothing |
| influxdb | `/health` | — |
| grafana | `/api/health` | — |
| apple-health-ingester | `tcpSocket` | No HTTP health endpoint upstream |
| sonarr, radarr, sabnzbd, emby, hydra2 | `/` on the app port; startup, liveness, readiness | Readiness stops Traefik routing to them while they boot |
| traefik | exec `traefik healthcheck --ping` | `hostNetwork` makes the pod IP the storage NIC, where the ping endpoint is not bound. The CLI queries loopback |
| keel (both clusters) | `/healthz` | Liveness 15s × 6 is laxer than readiness 10s × 3 |
| jottacloud-backup | none | Its old liveness probe could not fail. `activeDeadlineSeconds: 21600` bounds the run |
| garmin-grafana | none | It serves nothing. The `health-garmin-ingest` switch is the correct instrument |

## Why the sidecars have no probes

The four sqlite quiesce sidecars once carried a freshness liveness probe on the published
snapshot. It is gone, and umami's `pg-dump-sidecar` never had one. Do not add either back.

The probe existed to self-heal a failed `apk add`, which left the container without
`sqlite3`. Each loop now retries `apk add` itself on a 5 minute backoff, so the probe only
duplicates the loop.

Against a permanent fault — a corrupt database, a full disk, a path moved by an app upgrade
— the probe restarts a container that a restart cannot repair, and the restarts back off
into `CrashLoopBackOff`:

> A container that is not Running is not Ready. A Pod with a non-Ready container leaves its
> EndpointSlice. cloudflared returns 502 for the app.

A backup fault takes a working application offline. That is the decision rule biting the
one probe the spec endorsed.

Detection moves to the artifact: the restic gate asserts a per-app, and per-FreshRSS-user,
snapshot exists and is fresh, then pings healthchecks.io red. Latency goes from roughly 45
minutes to at worst a day — the right scale for a backup fault, and it never costs you the
application.

### What the sidecar loops do instead

`set -e` is deliberately absent: if the sidecar exits, kubelet restarts it and a persistent
fault reaches the same CrashLoopBackOff chain. Each loop instead:

- runs under `set -u`, logs failures to stderr, and keeps running;
- sleeps 300s after a failure and 43200s after a success;
- publishes atomically, writing a `.tmp` then `mv`, so a failed run leaves the previous
  snapshot intact;
- runs `sqlite3 <tmp> 'select count(*) from sqlite_master'` before publishing and refuses
  a snapshot with zero schema objects. A truncated source makes `.backup` emit a valid but
  empty database with a current mtime, which passes any mtime-only check.

These sidecars report failure by logging, not by exiting, so their restart counts stay at
zero. The gate raises the alarm. To debug a missing snapshot, read the sidecar's stderr.
`pg-dump-sidecar` uses the same shape without the content assertion; `pg_dumpall`'s exit
code is the check there.

## Scheduled work

Every CronJob sets:

| Field | Value | Why |
|---|---|---|
| `timeZone: "UTC"` | all jobs | Otherwise the schedule follows kube-controller-manager's local zone |
| `startingDeadlineSeconds` | 3600; 300 for jottacloud; unset for `ingest-freshness` | A missed window retries for that long, then drops |
| `activeDeadlineSeconds` | restic 14400, influx-backup 3600, ingest-freshness 300, jottacloud 21600 | With `concurrencyPolicy: Forbid`, one hung run blocks every later run silently |
| `ttlSecondsAfterFinished` | 259200 on the restic jobs | A Friday failure survives until Monday |
| `terminationGracePeriodSeconds` | not set | busybox `ash` runs as PID 1 and never forwards SIGTERM to restic, so a grace period only slows teardown. `restic unlock` at the head of the next run recovers the lock |

### The restic ping wrapper

```
VPS:      ping_hc start → snapshots → unlock, backup, forget --prune, check → gate → ping_hc "$rc"
homelab:  ping_hc start → snapshots → unlock, backup → gate → forget --prune (only if the gate
                                                              passed) → check → ping_hc "$rc"
```

The gate sits in a different place on each cluster, deliberately. Both run it after the
backup — as a precondition it would skip a whole night of everything else over one stale
artifact. Homelab additionally makes `forget --prune` conditional on the gate, because
pruning is the step that destroys data: failing the job *after* pruning still alerts, but
the seven good daily snapshots are already being expired on schedule while the alert goes
unread. A false positive there costs a repository that grows in B2 until somebody looks;
the false negative costs every recovery point. `restic check` runs either way.

- The `/start` ping detects a run that starts and never finishes, and records durations.
  The exit-code ping (`hc-ping.com/$UUID/$rc`) separates success from failure. Pings never
  fail the job and use `wget -T 10`, so healthchecks.io cannot hang the backup.
- Steps chain with `&&`, not `set -e` inside a group: errexit is ignored inside an AND-OR
  list, so `{ set -e; … } || rc=$?` runs past a failure and reports the wrong status.
- `restic unlock` runs first and, without `--remove-all`, clears only stale locks.

Observed runtimes:

| Job | Schedule | Runtime |
|---|---|---|
| `homelab-restic` | 03:00Z | 26s |
| `vps-restic` | 04:00Z | 57s |

These fell from 88s and 117s once retention started pruning, so `restic check` walks 14
snapshots instead of 137. The 4h `activeDeadlineSeconds` is an opening guess — resize it
from recorded durations. Expect `homelab-restic` to rise: its gate adds a `du` walk of the
same 44,288 files restic just read, which should be cheap against a warm page cache but has
not been measured in place yet.

### Why both restic jobs need a gate at all

`restic` succeeds on an empty tree. It writes a valid snapshot, `restic check` passes, the
job exits 0 and healthchecks.io goes green. Nothing in the backup path has an opinion about
whether the thing it backed up was the data.

Both jobs mount their source as `hostPath` with `type: Directory`, which asserts only that
the directory *exists*. If the underlying volume fails to mount while its mountpoint
survives on the root filesystem, the backup captures nothing and reports success. Then
`forget --prune --keep-daily 7` expires the seven genuine daily recovery points over the
following week, and the repository ends up holding only snapshots of nothing. This is not
theoretical: snapshot `551bd209` in the homelab repository is 12 B and is currently retained
as a "monthly".

The gate is the only thing in either job that asks whether the backup was of anything.

### The VPS backup verification gate

The VPS job runs two checks after the backup completes.

**1. Expected-set assertion — authoritative.** Each entry must be present and under 15h
old, a 3h margin over the sidecars' 12h period. Output names the app and distinguishes
`MISSING`, `STALE` and `UNREADABLE`.

| Service | Expected snapshot |
|---|---|
| n8n | `/data/*_vps_n8n-data/database.sqlite.restic` |
| karakeep | `/data/*_vps_karakeep-data/db.db.restic` |
| uptime-kuma | `/data/*_vps_uptime-kuma-data/kuma.db.restic` |
| umami | `/data/*_vps_umami-pg-data/dump.sql.restic` |
| freshrss | iterates `/data/*_vps_freshrss-data/users/*/db.sqlite` and asserts a sibling `.restic` per user. Zero user DBs passes with a note |

Adding a sqlite-backed service means adding its snapshot here; miss it and that service's
backups go unverified. An explicit list beats a wildcard, which cannot tell "no databases
exist" from "the volume is unmounted" from "three of four present" — all three produce no
stale files. An empty `/data` now pings healthchecks.io red, naming each missing snapshot.

The entries are globs because local-path-provisioner names each PVC directory
`<pvName>_<namespace>_<pvcName>` with a random UUID. An unmatched glob survives literally
and fails the `-f` test, which is the MISSING verdict.

**2. Broad sweep — advisory.** Any `*.restic` under `/data` past the threshold prints a
warning and does not fail the job, because one orphaned PV directory would otherwise pin
the gate red forever. A `find` that errors does fail the job: an unreadable `/data` is a
real fault.

The gate runs after the backup. As a precondition, one stale snapshot would skip that
night's backup of everything else. It promotes to failure only when restic itself
succeeded, so a real restic failure keeps its own exit code.

### The homelab backup verification gate

Homelab has no quiesce sidecars, so there is no `*.restic` artifact to check. Every
local-path PVC is backed up as live application state. The assertions are therefore about
the **tree** rather than about snapshot files, in four authoritative checks plus one
forensic pass.

`homelab/backup/restic-cronjob.yaml` is the source of truth for every threshold and both
artifact tables below; they are reproduced here for the reasoning, and must be changed in
the same commit as the manifest.

**1. Mount identity — authoritative, and the only first-order check.** Talos puts the
kubelet pod directory on the EPHEMERAL partition (`/dev/sda6`) — the same filesystem that
`/var/mnt/ssd/local-path-provisioner` falls back to when the SSD user volume fails to
mount. `/etc/hosts` is bind-mounted from that pod directory into every non-hostNetwork
container, so its `st_dev` *is* the EPHEMERAL device and is readable from inside the
container with no host access. The gate compares it with `st_dev` of `/data`:

| | `/data` `st_dev` | `/etc/hosts` `st_dev` | Verdict |
|---|---|---|---|
| SSD mounted | SSD `/dev/sdb1` (2065) | EPHEMERAL `/dev/sda6` (2054) | differ → pass |
| SSD not mounted | EPHEMERAL | EPHEMERAL | match → **fail** |

Measured in-cluster 2026-08-20. A `stat` that fails on either path is a failure, not a
pass: without the reference the mount cannot be distinguished from its fallback.

**2. Tree scale — authoritative.** Floors, not targets: at least `MIN_PVC_DIRS=8` PVC
directories and `MIN_DATA_KIB=1048576` (1 GiB) in total. Measured 2026-08-20: 10
directories, 44,288 files, 4.418 GiB. Roughly 4x headroom, so log rotation or emby cache
eviction cannot trip it, while an empty or single-PVC tree cannot clear it.

**3. Expected set — authoritative.** Each entry must resolve, and must be at least its
floor in bytes. Floors are an order of magnitude under the observed sizes; they exist to
reject a zero-length or truncated file, not to track growth.

| Artifact | Path | Floor |
|---|---|---|
| emby-library | `/data/pvc-*_downloads_emby-config/data/library.db` | 1 MiB |
| hydra2-config | `/data/pvc-*_downloads_hydra2-config/nzbhydra.yml` | 4 KiB |
| radarr-db | `/data/pvc-*_downloads_radarr-config/radarr.db` | 1 MiB |
| sabnzbd-ini | `/data/pvc-*_downloads_sabnzbd-config/sabnzbd.ini` | 1 KiB |
| sonarr-db | `/data/pvc-*_downloads_sonarr-config/sonarr.db` | 1 MiB |
| grafana-db | `/data/pvc-*_health_grafana-data/grafana.db` | 256 KiB |
| influxdb-bolt | `/data/pvc-*_health_influxdb-data/influxd.bolt` | 32 KiB |
| garmin-tokens | `/data/pvc-*_health_garmin-tokens/garmin_tokens.json` | 256 B |

Same maintenance contract as VPS: a new local-path PVC holding anything you would miss must
be added here, or that application's backup goes unverified, silently. Same glob rationale
too — `local-path-provisioner` names each directory `<pvName>_<namespace>_<pvcName>` with a
random UUID, and the StorageClass is `reclaimPolicy: Retain`, so a recreated PVC leaves its
predecessor behind forever. Each glob resolves to its most recently modified match, so a
live artifact always beats its frozen predecessor — with one residual: if the live artifact
is absent *entirely*, because the PVC was recreated and nothing has written to it yet, the
orphan is the only match and the check passes on it. Telling bound from orphaned would need
the Kubernetes API from inside the job, which is not worth a ServiceAccount and RBAC on a
backup CronJob: the orphan is under `/data` and is backed up too, so this is the gate
reporting on the wrong file rather than a lost recovery point. The gate prints the path each
glob resolved to, so the substitution shows up in the job log instead of hiding behind
"8/8".

**4. Dump freshness — authoritative.** The influx dumps are the only homelab artifacts
produced on a schedule, so they are the only ones with a meaningful age. Both must be under
30h old:

| Artifact | Path |
|---|---|
| influx-native-dump | `/data/pvc-*_health_health-dumps/native/*` |
| influx-lp-export | `/data/pvc-*_health_health-dumps/lp/*.lp.gz` |

`influx-backup` writes these at 02:30Z, 30 minutes before this job. 30h therefore tolerates
one missed or delayed run — `health-influx-backup` is the check for *that* — and fails on
two consecutive misses or on the dumps vanishing. Nothing else in the tree is
freshness-checked: applying a deadline to live application state manufactures reds on any
file an app happens not to touch for a day.

**5. Per-PVC size table — forensic.** Every run prints one line per PVC directory. The night
a PVC empties, the diff is in the log. Two verdicts sit on top of it, with deliberately
different force:

- a PVC directory with **no entries at all** prints a `WARNING` and does not fail the job —
  a freshly provisioned PVC is legitimately empty until its app writes, and that must not
  pin the one channel that means "restore is broken" permanently red;
- a PVC directory that **cannot be opened** (`-r`/`-x`) *does* fail. "I could not look" must
  never be reported as "everything is fine".

`du`'s exit status is the one deliberate exception to that rule: busybox `du` returns
non-zero when a file vanishes mid-walk, which happens routinely here (sqlite WAL files,
rotating logs) and would manufacture a nightly red. Its status only warns. Its *output* is
still authoritative — an unparseable total fails — and unlike `find`, a `du` that could not
walk still emits a number, so the scale floors catch it. Its *stderr* is deliberately not
captured: folding it in with `2>&1` puts the diagnostic ahead of the total in the same
string, the numeric prefix comes out empty, and the advisory warning is promoted to the
fatal "unparseable total" branch. It goes to the pod log instead.

The gate announces its passes (`8/8 artifacts present`, `2/2 newer than 30h`). A gate that
prints nothing when it is happy is indistinguishable from a gate that never ran.

### "Newest of a glob" is a dangerous shape

Both the removed probe and the first gate took the newest snapshot matching the FreshRSS
glob. FreshRSS keeps one database per user, so when one user's database stopped being
snapshotted, the other users kept the newest mtime fresh and both checks stayed green
forever.

Iterate the source objects and assert an artifact for each. A check that reduces a set to
its maximum detects only "all of them stopped"; the failure you care about is "one of them
stopped". The four single-DB services still take the newest match, because their glob is
one PVC directory expected to match one path.

## healthchecks.io checks

| Check | 1Password reference | Period / grace | Pinged by |
|---|---|---|---|
| `homelab-restic` | `op://Homelab/b2-restic/healthcheck-uuid` | 1d / 2h | restic CronJob, `/start` and exit code |
| `vps-restic` | `op://VPS/b2-restic/healthcheck-uuid` | 1d / 2h | restic CronJob, `/start` and exit code |
| `vps-uptime-kuma-alive` | `op://VPS/uptime-kuma/healthcheck-uuid` | 5m / 15m | An uptime-kuma monitor — see [the self-monitor](#the-self-monitor-layer-4) |
| `health-apple-ingest` | `op://Homelab/health-healthchecks/apple-uuid` | 1d / 12h | `ingest-freshness`, only when InfluxDB data is under 24h old |
| `health-garmin-ingest` | `op://Homelab/health-healthchecks/garmin-uuid` | 1d / 12h | as above |
| `health-influx-backup` | `op://Homelab/health-healthchecks/backup-uuid` | 1d / 6h | `influx-backup`, success only: the script is `set -eu` with the ping last |
| jottacloud-backup | `op://Homelab/jottacloud-backup/HEALTHCHECK_UUID` | 6-hourly schedule | The image's own `backup.sh` |

Only the two restic jobs send `/start` and an exit code. The other three ping on success
only, so a failure and a never-scheduled run produce the same signal: silence, then a
grace-expiry alert. Follow the restic pattern for new jobs.

`ingest-freshness` always exits 0. The signal is the absent ping, not a failed Job. Do not
change it to a non-zero exit.

The read-only API key is `op://Homelab/healthchecks.io/read-only-api-key`. If that path
returns 404, the item is still spelled `healtchecks.io` — a rename is in progress.

Adding a check takes [four edits](apply-workflow.md#adding-a-secret-is-four-edits-not-three).
Nothing catches a missing `ENVSUBST_VAR_NAMES` entry, which ships the literal `${VAR}` as
the ping UUID and silently disables the check.

## uptime-kuma runbook (layer 3)

Create monitors by hand in the UI and record them here. uptime-kuma v2 offers no supported
programmatic path: monitor CRUD is Socket.IO only, the one API-key-protected HTTP route is
`/metrics`, the REST API issue (#118) has been open since 2021 with two bridge PRs closed
unmerged, and the community Python wrapper stops at v1.23.2.

To read the monitor inventory, query `kuma.db` read-only:

```bash
kubectl -n vps exec deploy/uptime-kuma -- \
  sqlite3 -readonly /app/data/kuma.db 'select name, url, type, active from monitor'
```

**`/metrics` omits monitors created after the process started, until it restarts.** Using
`/metrics` as an inventory produces a wrong answer; `kuma.db` is the reliable source. The
quiesce sidecar backs up `kuma.db` nightly, so a rebuild restores the monitors.

### Settings for every HTTP monitor

| Field | Value | Why |
|---|---|---|
| Monitor type | HTTP(s) | — |
| Heartbeat interval | 120s | — |
| Retries | 3 | The default of 0 alerts on a single blip |
| Heartbeat retry interval | 60s | — |
| Request timeout | 20s | Separates "slow" from "wedged" |
| Max redirects | **0** | Defeats the Access trap below |
| Accepted status codes | per monitor | — |
| Certificate expiry, ignore TLS | defaults | TLS terminates at the Cloudflare edge |

Skip keyword monitors: the keyword is evaluated only after the status check passes, so it
adds nothing, and `saveErrorResponse` already captures Cloudflare's error body into the
alert, which makes a `1033` diagnosable.

### The Cloudflare Access trap

An Access-protected hostname answers an unauthenticated request with a 302 to the
Cloudflare login page. At the default `maxredirects: 10`, the monitor follows it, gets 200
from Cloudflare's login app, and reports UP while the tunnel, pod and node are all dead.

Two mitigations, both applied: `maxredirects: 0` on every monitor, and Access service-token
headers on the Access-protected ones so the request reaches the origin. The token covers
`analytics`, `rss`, `keep`, `watch` and `n8n`. Paste the headers as JSON in the monitor's
**Headers** box:

```json
{
  "CF-Access-Client-Id": "<op://VPS/cloudflare/CF-Access-Client-Id>",
  "CF-Access-Client-Secret": "<op://VPS/cloudflare/CF-Access-Client-Secret>"
}
```

Read the values with `op read` as you paste them; never write them into this repo. A wrong
or missing header produces a 302, which fails the monitor — the correct, loud outcome. An
Access bypass path is no substitute: the glob `/foo/*` does not match bare `/foo`, so a
bypassed health path needs both destinations.

### Monitor list

Each path mirrors the service's in-pod probe target, so a monitor failing while the probe
passes isolates the fault to the tunnel or the edge. `uptime.cynexia.com` is absent on
purpose: uptime-kuma checking its own hostname reports nothing it can deliver.

VPS cluster, Access-protected — set both headers on each:

| Monitor | URL | Accepted status codes |
|---|---|---|
| `vps-analytics` | `https://analytics.cynexia.com/api/heartbeat` | `["200-299"]` |
| `vps-rss` | `https://rss.cynexia.com/api/` | `["200-299"]` |
| `vps-keep` | `https://keep.cynexia.com/api/health` | `["200-299"]` |
| `vps-watch` | `https://watch.cynexia.com/` | `["200-299"]`; add `302` if you enable changedetection's password |
| `vps-n8n` | `https://n8n.cynexia.com/healthz` | `["200-299"]` |

Homelab health tunnel, no Access, so no headers:

| Monitor | URL | Accepted status codes |
|---|---|---|
| `health-mcp` | `https://mcp.cynexia.com/` | `["200-299", "401"]` |
| `health-hae` | `https://hae.cynexia.com/` | `["200-299", "401"]` |
| `health-authenticate` | `https://authenticate.cynexia.com/` | pin to the observed status |

A fast 401 is a true end-to-end signal: it proves the tunnel, cloudflared and the origin
pod all serve, which is exactly what was false during the Pomerium wedge. A timeout, a 5xx
or a Cloudflare `1033` falls outside the set and marks the monitor DOWN. Both
`mcp.cynexia.com` and `hae.cynexia.com` accept 401 and are verified working.

Before adding a status code to any set, observe it once:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://hae.cynexia.com/
```

Widening a set to swallow whatever appears stops the monitor being a monitor.

### The self-monitor (layer 4)

uptime-kuma shares the VPS tunnel, node and scheduler with most of what it watches, so it
cannot report its own death. Add one monitor that GETs a healthchecks.io ping URL.

| Field | Value |
|---|---|
| Monitor type | **HTTP(s)** |
| Name | `self → healthchecks.io` |
| URL | `https://hc-ping.com/<op://VPS/uptime-kuma/healthcheck-uuid>` |
| Interval | 300s |
| Accepted status codes | `["200-299"]` |
| Max redirects | 0 |

**Do not use a Push monitor.** A Push monitor waits to receive a ping and is the opposite
of what this needs. This monitor must send one.

The check `vps-uptime-kuma-alive` runs 5m period, 15m grace. If the pod, node or scheduler
dies, the pings stop and healthchecks.io alerts from outside both clusters. The monitor's
own UP/DOWN state is irrelevant — the signal lives at healthchecks.io.

## What this does not catch

Probes fix hung request paths. They do not fix silently stopped background work, which for
several of these services is the likelier incident.

| Service | The probe stays green while… |
|---|---|
| **umami** | `/api/heartbeat` returns a static `{ok:true}` that never touches Prisma. It returns 200 through any database failure (upstream #3417, connection-pool exhaustion). This buys Node-wedge detection only, not DB-outage detection |
| **changedetection** | Upstream #4214: 134 watches went 23 days unchecked while `/` returned 200 and `/worker-health` reported healthy, because the ticker died, not the workers. Only `overdue_watches` from `/api/v1/systeminfo` sees it. Wire it as an external json-query alert, never as liveness: a restart does not fix a scheduling bug |
| **uptime-kuma** | The HTTP server and the monitor scheduler run independently (#4967). A monitoring tool that has silently stopped monitoring is the worst version of this bug, and no in-pod probe detects it. Hence layer 4 |
| **karakeep** | `/api/health` is a hardcoded literal in the web process and cannot observe the worker. Stuck-queue reports (#1802, #2704) all leave it returning 200. The detector is the `karakeep_queue_jobs` metric (`pending > 0 && running == 0`) |
| **freshrss** | `/api/` never opens the database, and feed refresh runs from a separate `crond`. A dead cron serves the UI perfectly and stops fetching news |
| **garmin-grafana** | `write_points_to_influxdb()` catches InfluxDB errors, logs them and returns normally, after which the caller advances the watermark. An InfluxDB outage causes permanent data loss for that window with the process Running and Ready. `ingest-freshness` covers it; no probe improves on that |
| **pomerium `mcp` sidecar** | Its probes are `tcpSocket`. A wedged HTTP handler with a live listener passes them. The MCP server exposes no health endpoint |
| **homelab services** | The external layer runs on the VPS, which has no route to `*.cynexia.net`. Only the three health-tunnel hostnames get layer-3 coverage. sonarr, radarr, sabnzbd, emby, hydra2 and grafana have probes and nothing external |
| **the VPS gate** | It proves each snapshot exists, is recent, and holds at least one schema object. It does not prove the contents are complete or uncorrupted. A snapshot missing rows, or with a corrupt page below the `sqlite_master` read, passes everything here and surfaces at restore time |
| **the homelab gate** | It proves the SSD is mounted and the tree is the right *shape* — right number of PVC directories, right order of magnitude, the listed files present and non-trivial. It says nothing about *content*. Every homelab PVC is copied live, with no quiesce step: a sqlite database mid-write is captured torn, `sonarr.db` at 14 MiB of corruption passes the size floor exactly as 14 MiB of working database does, and a PVC that stopped being written to weeks ago looks identical to one written a minute ago. Only the two influx dumps are age-checked. A retained orphan directory from a recreated PVC can satisfy an expected-set entry the live PVC no longer can — the resolved paths are printed so it is visible, but nothing fails on it. The rest surfaces at restore time |

Queued, not configured: a changedetection `overdue_watches` json-query monitor and a
karakeep queue-depth alert. Both need an API credential in the monitor.

Snapshot integrity stays partly verified by choice. On VPS the `sqlite_master` assertion
closes the worst case — a fresh, valid, empty snapshot from a truncated source — by proving
a schema exists, not that the data is there. Homelab has no equivalent, because it has no
quiesce sidecars to assert against: its gate stops at shape. Closing the rest on either
cluster means `sqlite3 <file> 'pragma integrity_check'` inside the gate, which means adding
sqlite to the `restic/restic` image. Until then, a periodic manual restore drill is the only
real proof, and it is the only thing that covers homelab's torn-copy exposure at all.

## Explicitly rejected

- **Any probe on `garmin-grafana`.** The `health-garmin-ingest` freshness check covers it.
- **Token-mtime freshness probe for garmin.** The token file is written only on the
  interactive login path, so its mtime changes about once a year. The probe detects nothing
  and false-positives on any sane threshold.
- **Staleness-based liveness for garmin.** Freshness depends on you syncing a watch. A
  weekend away restarts the pod repeatedly, and each restart with a stale token sends you
  an MFA SMS. Staleness notifies a human; it never restarts.
- **Generalising the jottacloud liveness probe.** It could not fail: `backup.sh` is PID 1
  for the whole run and never `exec`s over itself, so `ps | grep backup.sh` always matched,
  and no upstream script creates `/tmp/backup-completed`. It measured presence, not
  progress, so a stalled rclone looked alive. `activeDeadlineSeconds` is the pattern to
  generalise instead.
- **Naive `pg_isready` liveness on postgres**, the exit-0-only Bitnami shape. Same narrow
  detection as the `test $? -lt 2` form, plus a recovery loop that never converges on a
  single-replica local-path PVC.
- **Any probe on the backup sidecars.** Readiness drops the Pod from its EndpointSlice;
  liveness reaches the same place through CrashLoopBackOff.
- **`tcpSocket` on sockpuppetbrowser :3000 as a hang fix.** The kernel completes handshakes
  from the accept backlog while the event loop is blocked, so it detects process death only.
- **A NetworkPolicy in front of the MCP server.** Inert on flannel — see
  [homelab-health.md](homelab-health.md#mcp-is-a-sidecar-not-a-standalone-deployment).

## Rolling out a probe change

A probe rollout that restarts pods is a failed rollout.

1. Confirm the probe path's status from inside the cluster with a throwaway
   `alpine/k8s:1.36.0` pod. Do not take it from vendor documentation.
2. Run `make diff-homelab` or `make diff-vps`, then apply.
3. Run `kubectl -n <ns> rollout status deploy/<name>` and confirm it completes without a
   restart loop.
4. Ten minutes later, run `kubectl -n <ns> get pods` and confirm 0 restarts.
5. For a CronJob change, run `kubectl create job --from=cronjob/<name> <name>-manual`, then
   confirm the healthchecks.io check goes green and records a duration.
6. After a few weeks, resize `activeDeadlineSeconds` from the recorded durations.
