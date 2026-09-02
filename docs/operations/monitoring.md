# Failure detection: probes, deadlines and monitors

How failures in both clusters get noticed: the policy, the inventory, and the failures none of it catches.
Manifests carry per-probe rationale in comments.
Read [What this does not catch](#what-this-does-not-catch) before you trust a green signal.

## Start here: something is wrong

| Signal | Read |
|---|---|
| A restic check is red | `failed_step=` in the ping body names the phase, and `prune=` says whether retention ran — [Reading a restic failure body](#reading-a-restic-failure-body) |
| `mount_ok=no` on homelab restic | The SSD did not mount, so the backup captured nothing — [the gates](#the-backup-verification-gates) |
| `health-ingest` is DOWN | Check whether the operator synced a watch before suspecting the pipeline. The last heartbeat's `apple_age_h=`/`garmin_age_h=` names which path was ageing — [the push monitors](uptime-kuma.md#push-monitors) |
| `homelab-update-watch` is DOWN | In a fresh heartbeat, `verdict=` names the cause and `next=` names the command to run; a stale `run_epoch=` means the watcher itself went quiet — [The update watcher](#the-update-watcher) |
| A sidecar shows `RESTARTS: 0` but its snapshot is missing | Expected; they log rather than exit. Read the sidecar's stderr — [Why the sidecars have no probes](#why-the-sidecars-have-no-probes) |
| `hindsight-canary` is DOWN | Read `verdict=`: `retain-failed` is the API, the database or the tenant key; `recall-miss` is the retrieval side. An agent is losing memories right now — [hindsight.md](hindsight.md) |
| `hermes-update` is red | Nobody pinged it, the only way it goes red: the runbook reports on success and sends nothing on failure. Either no update session ran inside the period, or one ran and stopped before its report step. **Read the mtime of `~/.hermes/hermes-update.pre-run` before its contents** — the runbook's preconditions truncate that file at the start of every session, so a record older than the alarm period is the last *successful* session's and describes that run, not a stalled one. Only in a record written since the alarm do the keys say how far the session got: no `target_sha` means it stopped in change analysis, a `webui_target_sha` means the update itself ran — [the runbook](hermes-vm-updates.md#report) |
| `hermes-app-alive` is DOWN | Read `verdict=`: `units-down` is the user manager or lingering, `import-failed` is the shared venv, `webui-unreachable` is the WebUI itself. No beat at all means the VM, the timer or the Access bypass — [Reading a DOWN `hermes-app-alive`](hermes-vm.md#reading-a-down-hermes-app-alive) |
| Proxied changedetection watches error while unproxied ones are fine | The residential egress chain, not the internet. One pod down, or the Access service token gone — the failure table and the per-pod recovery are in [vps.md](vps.md#residential-egress-through-the-homelab) |
| `disk_pct` is climbing on homelab restic | `local-path` has no quota, so this is the node SSD every workload shares — [the gates](#the-backup-verification-gates) |
| An uptime-kuma monitor is UP but the service is down | Suspect an Access redirect — [uptime-kuma.md](uptime-kuma.md#the-cloudflare-access-trap) |
| Everything is green and the data is still wrong | Expected; several probes are shallow by design — [What this does not catch](#what-this-does-not-catch) |

## The decision rule

Monitor the artifact, not the process — a live process proves nothing.

| Workload shape | Instrument | Why |
|---|---|---|
| Serves requests | Probe the real request path | kubelet repairs it by restarting; the failure is local and immediate |
| Produces an artifact on a schedule | Dead-man's-switch on freshness | Restarting does not deliver data that never arrived. Absence of an event is only visible from outside |
| Must not hang | `activeDeadlineSeconds` (Jobs), progress probe (Deployments) | A bounded runtime is a contract you enforce declaratively |

If restarting the thing cannot plausibly fix the failure, a probe is the wrong instrument: probe failure means "kill and retry", dead-man's-switch failure means "wake a human", and the wrong choice buys either false confidence or a self-inflicted outage.

## The four layers

| Layer | Instrument | Blind to |
|---|---|---|
| 1 | In-pod probes | Tunnels, schedulers, background work |
| 2 | Job deadlines and dead-man's-switches | Request-path wedges |
| 3 | External monitors and in-cluster push monitors (uptime-kuma) | Its own death |
| 4 | healthchecks.io switch on the monitor itself | Anything it is not pinged by |

Each layer covers the blind spot of the layer below it.
Do not drop one because another "already checks that".

## Probe policy

- Put a readiness probe on every long-running container that serves traffic.
  The worst case is that the pod leaves Service routing.
- Add liveness only when that probe detects the failure **and** a restart repairs it.
  Every service in both clusters runs a single replica, so kubelet has nowhere to send traffic while the pod restarts: an eager liveness probe manufactures the outage it was added to catch.
- Add a startup probe to anything with migrations or a slow boot, so liveness cannot fire during startup, and keep liveness thresholds strictly laxer than readiness.
  Readiness sheds traffic; liveness destroys state.
- Set `timeoutSeconds` on every probe.
  The 1s default false-positives on a loaded node, turning ordinary disk contention into a restart.
  Every probe in both clusters sets it.
- Probe the data plane, not a vendor health endpoint: a control-plane endpoint reports on a different process from the one serving your users, and the vendor-documented probe stayed green throughout the 2026-08-18 Pomerium wedge ([homelab-health.md](homelab-health.md#the-probe-target-is-deliberately-not-the-documented-one)).
- Put no probe of any kind on a backup or quiesce sidecar.
  See [Why the sidecars have no probes](#why-the-sidecars-have-no-probes).

Defaults, unless a service's entry below says otherwise:

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
| postgres (umami) | readiness plain `pg_isready`; liveness and startup as `sh -c 'pg_isready -q …; test $? -lt 2'` | Exit 1 means "rejecting connections during recovery". Liveness and startup count that as alive so recovery can finish; readiness does not, so traffic waits. A plain `pg_isready` liveness kills the postmaster mid-recovery and never converges |
| keel-fresh | none | Scheduled work. The `vps-keel-fresh` kuma push monitor plus `activeDeadlineSeconds: 300` is the instrument |
| the 5 quiesce sidecars | none | Deliberate — see below |
| homelab-proxy (cloudflared) | readiness `tcpSocket` (:8888) | Readiness-only. `access tcp` binds its listener at start and dials Cloudflare only when a client connects, so this proves the process is up and nothing about the path to the homelab. It still keeps a starting pod out of the Service. No liveness: a restart does not repair a broken Access token or a dead tunnel |

### Homelab cluster

| Container | Target | Note |
|---|---|---|
| influxdb-mcp | liveness and readiness `tcpSocket` | The MCP server exposes no health endpoint. TCP detects process death, not a wedged handler |
| cloudflared tunnel connectors (both clusters) | liveness and readiness `/ready` (:2000) | Neither Deployment has a Service, so readiness gates the rolling update and shows connector state. It routes nothing |
| influxdb | `/health` | — |
| grafana | `/api/health` | — |
| apple-health-ingester | `tcpSocket` | No HTTP health endpoint upstream |
| sonarr, radarr, sabnzbd, emby, hydra2 | `/` on the app port; startup, liveness and readiness | Readiness stops Traefik routing to them while they boot |
| traefik | exec `traefik healthcheck --ping` | `hostNetwork` makes the pod IP the storage NIC, where the ping endpoint is not bound. The CLI queries loopback |
| keel (both clusters) | `/healthz` | Liveness 15s × 6 is laxer than readiness 10s × 3 |
| jottacloud-backup | none | Its old liveness probe could not fail. `activeDeadlineSeconds: 21600` bounds the run |
| garmin-grafana | none | It serves nothing. The `health-ingest` push monitor is the correct instrument |
| cloudflare-analytics | none | Scheduled work. `homelab-cloudflare-analytics` plus `activeDeadlineSeconds: 1200` is the instrument |
| withings-ingest | none | Scheduled work. `withings-ingest` plus `activeDeadlineSeconds: 900` is the instrument |
| update-watch | none | Scheduled work. `homelab-update-watch` plus `activeDeadlineSeconds: 300` is the instrument |
| keel-fresh | none | Scheduled work. The `homelab-keel-fresh` kuma push monitor plus `activeDeadlineSeconds: 300` is the instrument |
| hindsight api | liveness `/health/live`, readiness and startup `/health` (:8888) | Split on purpose, the same way n8n's is. `/health` is database-gated, so a broken postgres drains traffic; `/health/live` is in-process and never touches the database, so a slow or recovering postgres cannot crashloop the single replica. `/health/live` needs image ≥ 0.9.1 — keep the pin at or above it |
| hindsight control-plane | readiness `/` (:9999) | No liveness: a wedged admin UI is an inconvenience, not an outage, and restarting a single-replica pod over it buys risk for nothing |
| postgres (hindsight) | readiness plain `pg_isready`; liveness and startup as `sh -c 'pg_isready -q …; test $? -lt 2'` | Copied verbatim from umami-postgres above, and for the same reasons |
| hindsight-pg-dump, hindsight-canary | none | Scheduled work. `hindsight-pg-dump` and `hindsight-canary` plus their `activeDeadlineSeconds` are the instruments |
| tinyproxy | readiness `tcpSocket` (:8888) | Readiness-only. A tcpSocket proves the listener bound, which is all it can prove. No liveness: the only fault it detects is a dead process, and a dead process exits the container, which the kubelet restarts anyway |

## Why the sidecars have no probes

**Put no probe — readiness, liveness or startup — on any of the five VPS quiesce sidecars: `sqlite-snapshot` in n8n, freshrss, karakeep and uptime-kuma, and `pg-dump-sidecar` in umami-postgres.**
This has nearly been re-broken twice, and the chain is short:

> A container that is not Running is not Ready.
> A Pod with a non-Ready container leaves its EndpointSlice. cloudflared then returns 502 for the application.

Readiness reaches that state directly; liveness reaches it through `CrashLoopBackOff`.
Either way, a fault in last night's *backup* takes a working *application* offline.
The one argument for such a probe was self-healing a failed `apk add` that left the container without `sqlite3`, and `ensure_sqlite3()` in `vps/workloads/scripts/sqlite-snapshot-lib.sh` now retries that install on a 5 minute backoff instead.
Against a permanent fault — a corrupt database, a full disk, a path moved by an app upgrade — a probe restarts a container a restart cannot repair.

Detection lives at the artifact instead: the VPS restic gate asserts a fresh snapshot per app and per FreshRSS user, then turns `vps-restic` red at healthchecks.io.
Latency goes from roughly 45 minutes to at worst a day — the right scale for a backup fault, and it never costs the application.

### What the sidecar loops do instead

`set -e` is deliberately absent from all five: if a sidecar exits, kubelet restarts it and a persistent fault reaches the same `CrashLoopBackOff` chain.
Each loop instead runs under `set -u`, logs failures to stderr and keeps going; sleeps 300s after a failure and 43200s after a success; publishes atomically as `.tmp` then `mv`, so a failed run leaves the previous snapshot intact; and asserts *content* before publishing, not only an exit status.

That last one is the part to keep, because both content checks catch a failure an exit code does not.
`snapshot()` runs `sqlite3 <tmp> 'select count(*) from sqlite_master'` and refuses zero schema objects, since a truncated source makes `.backup` emit a structurally valid but empty database with a current mtime.
`pg-dump-snapshot.sh` refuses fewer than one `grep -c '^CREATE TABLE '`, since `pg_dumpall` exits 0 against a freshly initialised postgres with no umami schema — the entrypoint creates the empty `umami` database either way, yielding a roles-only dump that restores to an empty analytics database.
Refusing to publish leaves the previous artifact ageing, which turns the check red.
And because these loops report failure by logging rather than exiting, their restart counts stay at zero: **to debug a missing snapshot, read the sidecar's stderr**, and read nothing into `RESTARTS: 0`.

All five loops are real files under `vps/workloads/scripts/`, delivered by the `sqlite-snapshot-scripts` `configMapGenerator` in `vps/workloads/kustomization.yaml` and mounted at `/scripts`.
Four source `sqlite-snapshot-lib.sh`; n8n, karakeep and uptime-kuma share `sqlite-snapshot.sh` outright and differ only in `$SNAPSHOT_DB`.
Editing one rolls every Deployment that mounts it, and all five use `strategy: Recreate`, so a script edit costs a brief hard-down window for each rather than a rolling update.
Generated scripts also pass through envsubst, so run `make check-script-substitution` and read the note in `AGENTS.md` before you write a `$VAR` into one.

## Scheduled work

| Field | Value | Why |
|---|---|---|
| `timeZone: "UTC"` | every job | Otherwise the schedule follows kube-controller-manager's local zone |
| `startingDeadlineSeconds` | 3600 (update-watch and both clusters' keel-fresh included), except 1800 for cloudflare-analytics and withings-ingest, 600 for hindsight-canary, 300 for jottacloud, and unset for `ingest-freshness` | A missed window retries for that long, then drops. `update-watch` takes the 3600 default deliberately: a silently skipped run is the failure it exists to prevent |
| `activeDeadlineSeconds` | restic 14400, influx-backup 3600, hindsight-pg-dump 3600, hermes-pull 1800, cloudflare-analytics 1200, withings-ingest 900, ingest-freshness 300, update-watch 300, keel-fresh 300 on both clusters, hindsight-canary 300, jottacloud 21600 | With `concurrencyPolicy: Forbid`, one hung run silently blocks every later run |
| `ttlSecondsAfterFinished` | 259200 on both restic jobs, hermes-pull, cloudflare-analytics, withings-ingest, update-watch and both clusters' keel-fresh; 172800 on influx-backup and hindsight-pg-dump; 3600 on hindsight-canary, which runs hourly; 86400 on the rest | A Friday failure on the restic jobs survives until Monday |
| `terminationGracePeriodSeconds` | not set on any job | busybox `ash` runs as PID 1 and never forwards SIGTERM to restic, so a grace period only slows teardown. `restic unlock` at the head of the next run recovers the lock |

Two of those are the hindsight jobs.
`hindsight-pg-dump` runs at 02:15Z — after `hermes-pull` at 02:00, before `influx-backup` at 02:30 and the 03:00 sweep — on `influx-backup`'s figures exactly.
`hindsight-canary` runs hourly, and its `activeDeadlineSeconds: 300` is deliberately far shorter than its own schedule, so a hung run can never block the next one under `concurrencyPolicy: Forbid`; its `ttlSecondsAfterFinished: 3600` reaps a completed run before the next one lands.

**Neither hindsight CronJob carries a probe of any kind**, like every other scheduled job here.

### The restic ping wrapper

Both wrap the run as `ping_hc start` → `snapshots` → `unlock`, `backup`, `forget --prune`, `check` → `ping_hc "$rc"`, with the gate placed differently on each cluster ([the gates](#the-backup-verification-gates)).
Three rules hold on both:

- The `/start` ping detects a run that starts and never finishes, and records durations.
  The exit-code ping (`hc-ping.com/$UUID/$rc`) separates success from failure.
  Pings never fail the job and use `wget -T 10`, so healthchecks.io cannot hang the backup.
- Steps chain with `&&`, not `set -e` inside a group. errexit is ignored inside an AND-OR list, so `{ set -e; … } || rc=$?` runs past a failure and reports the wrong status.
- `restic unlock` runs first and, without `--remove-all`, clears only stale locks.

`homelab-restic` runs at 03:00Z in 26s, `vps-restic` at 04:00Z in 57s — down from 88s and 117s once retention started pruning, so `restic check` walks 14 snapshots instead of 137.
The 4h `activeDeadlineSeconds` is an opening guess: resize it from recorded durations, and expect `homelab-restic` to rise, since its gate adds a `du` walk of the files restic just read.

### The backup verification gates

`restic` succeeds on an empty tree: it writes a valid snapshot, `restic check` passes, and healthchecks.io goes green.
Both jobs mount their source as `hostPath` with `type: Directory`, which asserts only that the directory *exists*.
So a volume that fails to mount, while its mountpoint survives on the root filesystem, produces a backup of nothing that reports success — and `forget --prune --keep-daily 7` then expires the seven genuine recovery points over the following week.
Snapshot `551bd209` in the homelab repository is 12 B, retained as a "monthly".
**The gate is the only thing in either job that asks whether the backup was of anything.**
Each cluster's script is the source of truth for its thresholds; change them in the same commit here.

| | VPS (`vps/backup/scripts/restic-backup.sh`) | Homelab (`homelab/backup/restic-cronjob.yaml`) |
|---|---|---|
| What exists to check | A `*.restic` snapshot per app, published by the quiesce sidecars | No sidecars, so no artifact. Every PVC is backed up as live application state |
| Assertion shape | Snapshot files: present, fresh, readable | The **tree**: mounted, right scale, listed files present and non-trivial |
| Authoritative checks | Expected set, plus a `find` that must not error | Mount identity, tree scale, disk usage, expected set, dump freshness |
| Freshness limit | 15h (`STALE_MINUTES=900`), a 3h margin over the sidecars' 12h period | 30h (`STALE_MINUTES=1800`), on the two influx dumps, the Grafana dump, the hermes zip and the hindsight dump only |
| Advisory, never fatal | Any `*.restic` past the threshold, so one orphaned PV directory cannot pin the gate red forever | An empty PVC directory, legitimate on a freshly provisioned PVC; `/data` above 80% full; and `du`'s exit status |
| Runs | After `forget --prune` | **Before** `forget --prune`, which is skipped when the gate fails |
| Verdicts in the body | `MISSING`, `STALE`, `UNREADABLE`, per app | `mount_ok`, `artifacts=n/m`, `dumps_fresh=n/m`, `pvc_dirs`, `disk_pct` |

**The homelab gate also watches free space, and it is the only thing that does.**
`local-path` enforces no quota — a PVC's `storage:` figure is a request and nothing more — so "a PVC filled up" always means "the node SSD every workload shares filled up".
The gate runs `df -Pk /data` and puts the result in the ping body as `disk_pct=` on every run, green or red, so the trend is readable before anything is wrong.
Above 80% it prints a loud advisory and stays green, because a backup that runs is worth more than an alert about headroom; above 90% it fails the gate, which also defers `forget --prune` — correct, since pruning is the wrong thing to be doing while the node is about to wedge.
An unparseable `df` reading **fails**, on the same "I could not look" rule as the rest of the gate.
The residual is honest and small: this samples once a night, so a fill faster than a day still lands between runs.

Both promote to failure only when restic itself succeeded, so a real restic failure keeps its own, more specific exit code.
Both announce their passes (`12/12 artifacts present`, `5/5 newer than 30h`): a gate that prints nothing when happy is indistinguishable from one that never ran.
In both, **"I could not look" must never be reported as "everything is fine"** — an unreadable `/data` or an unopenable PVC directory fails the job.

**The one deliberate divergence is that homelab gates the prune**, because pruning is the step that destroys data: failing the job afterwards still alerts, but the seven good daily snapshots are already being expired on schedule while the alert goes unread.
A false positive costs a repository that grows in B2 until somebody looks; the false negative costs every recovery point.
`restic check` runs either way.
Neither cluster makes the gate a *precondition of the backup* — that would skip a whole night of everything else over one stale artifact.

**Add every new artifact to its cluster's expected set, or that application's backup goes unverified, silently.**
On VPS that means every new sqlite-backed service; on homelab, every local-path PVC holding something you would miss.
An explicit list beats a wildcard, which cannot tell "no databases exist" from "the volume is unmounted" from "three of four present" — all three produce no stale files.

| Cluster | Artifact | Path | Assertion |
|---|---|---|---|
| VPS | n8n | `/data/*_vps_n8n-data/database.sqlite.restic` | present, <15h |
| VPS | karakeep | `/data/*_vps_karakeep-data/db.db.restic` | present, <15h |
| VPS | uptime-kuma | `/data/*_vps_uptime-kuma-data/kuma.db.restic` | present, <15h |
| VPS | umami | `/data/*_vps_umami-pg-data/dump.sql.restic` | present, <15h |
| VPS | freshrss | iterates `/data/*_vps_freshrss-data/users/*/db.sqlite` | a sibling `.restic` **per user**; zero user DBs passes with a note |
| homelab | emby-library | `/data/pvc-*_downloads_emby-config/data/library.db` | ≥1 MiB |
| homelab | hydra2-config | `/data/pvc-*_downloads_hydra2-config/nzbhydra.yml` | ≥4 KiB |
| homelab | radarr-db | `/data/pvc-*_downloads_radarr-config/radarr.db` | ≥1 MiB |
| homelab | sabnzbd-ini | `/data/pvc-*_downloads_sabnzbd-config/sabnzbd.ini` | ≥1 KiB |
| homelab | sonarr-db | `/data/pvc-*_downloads_sonarr-config/sonarr.db` | ≥1 MiB |
| homelab | grafana-db | `/data/pvc-*_health_grafana-data/grafana.db` | ≥256 KiB |
| homelab | grafana-dump | `/data/pvc-*_health_health-dumps/grafana/*-grafana.db` | ≥200 KiB **and** <30h |
| homelab | influxdb-bolt | `/data/pvc-*_health_influxdb-data/influxd.bolt` | ≥32 KiB |
| homelab | garmin-tokens | `/data/pvc-*_health_garmin-tokens/garmin_tokens.json` | ≥256 B |
| homelab | withings-tokens | `/data/pvc-*_health_withings-tokens/withings_tokens.json` | ≥64 B |
| homelab | influx-native-dump | `/data/pvc-*_health_health-dumps/native/*` | <30h |
| homelab | influx-lp-export | `/data/pvc-*_health_health-dumps/lp/*.lp.gz` | <30h |
| homelab | hermes-zip | `/data/pvc-*_backup_hermes-dumps/hermes-*.zip` | ≥16 MiB **and** <30h |
| homelab | hindsight-dump | `/data/pvc-*_hindsight_hindsight-dumps/hindsight-*.sql.gz` | ≥4 KiB **and** <30h |

Homelab byte floors sit an order of magnitude under observed sizes: they reject a zero-length or truncated file, not slow growth.
The two newest rows follow the same derivation from a measured seed run: `grafana-dump` at 200 KiB, from 2,039,808 B / 273 schema objects on 2026-08-24, and `hindsight-dump` at 4 KiB, from 48,829 B / 23 tables at rollout step 5 the same day.
Each has a twin `MIN_BYTES` in the script that writes it — `homelab/health/scripts/grafana-sqlite-backup.py` and `homelab/hindsight/scripts/hindsight-pg-dump.sh` — and the pair must be raised together.
Live sizes are reported as `grafana_kib=` and `dump_kib=` in their heartbeat messages.

`withings-tokens` sits at 64 B rather than the 256 B its `garmin-tokens` neighbour carries, because that file holds two strings and measured 83 bytes on September 2, 2026.
That size tracks the length of Withings' own tokens, so a provider change that shortened them would fail this row on a perfectly healthy file.
It is also the one row whose artifact cannot usefully be restored: the refresh token rotates every six hours, so the gate detects that the PVC stopped being captured rather than promising a recovery.

`influx-backup` writes the influx dumps *and* the Grafana dump at 02:30Z, 30 minutes before this job, so 30h tolerates one missed run (`health-influx-backup` is the monitor for *that*) and fails on two consecutive misses; `hermes-pull` writes its zip at 02:00Z on the same terms, with `homelab-hermes-pull` as its own first-line check.
Nothing else is freshness-checked: a deadline on live application state manufactures reds on any file an app happens not to touch for a day.

Entries are globs because local-path-provisioner names each PVC directory `<pvName>_<namespace>_<pvcName>` with a random UUID.
On VPS an unmatched glob survives literally and fails the `-f` test, which is the `MISSING` verdict.
On homelab the StorageClass is `reclaimPolicy: Retain`, so a recreated PVC leaves its predecessor behind forever; each glob takes its newest match, so a live artifact normally beats its frozen predecessor — but if the live artifact is absent entirely, the orphan is the only match and the check passes on it.
Telling bound from orphaned needs the Kubernetes API from inside the job, not worth a ServiceAccount and RBAC on a backup CronJob: the orphan is under `/data` and is backed up too, so this is the gate reporting on the wrong file, not a lost recovery point.
The gate prints each resolved path, so the substitution shows up in the log rather than hiding behind "8/8".

Two homelab checks have no VPS equivalent.
**Mount identity** is the first-order one: Talos puts the kubelet pod directory on the EPHEMERAL partition (`/dev/sda6`), the same filesystem `/var/mnt/ssd/local-path-provisioner` falls back to when the SSD user volume fails to mount.
`/etc/hosts` is bind-mounted from that pod directory into every non-hostNetwork container, so its `st_dev` *is* the EPHEMERAL device, readable with no host access.
The gate compares it with `st_dev` of `/data`: they differ when the SSD is mounted (2065 vs 2054, measured in-cluster 2026-08-20) and match when it is not, which fails.
A `stat` that fails on either path is a failure, not a pass — without the reference, the mount cannot be told from its fallback.

**Tree scale** sets floors, not targets: at least `MIN_PVC_DIRS=8` PVC directories and `MIN_DATA_KIB=1048576` (1 GiB).
Measured 2026-08-20 at 10 directories, 44,288 files and 4.418 GiB — roughly 4x headroom, so log rotation or emby cache eviction cannot trip it, while an empty or single-PVC tree cannot clear it.

A forensic pass then prints one line per PVC directory, so the night a PVC empties the diff is in the log.
`du`'s exit status is the one deliberate exception to the "could not look" rule.
Busybox `du` returns non-zero when a file vanishes mid-walk, which is routine with sqlite WAL files and rotating logs, so its status only warns while its *output* stays authoritative: an unparseable total fails, and a `du` that could not walk still emits a number the scale floors catch.
Do not capture its stderr — `2>&1` puts the diagnostic ahead of the total, empties the numeric prefix, and promotes the advisory warning to the fatal branch.

### "Newest of a glob" is a dangerous shape

FreshRSS keeps one database per user.
An earlier check took the newest snapshot matching the FreshRSS glob, so when one user's database stopped being snapshotted the other users kept the newest mtime fresh and it stayed green forever.
**Iterate the source objects and assert an artifact for each**: reducing a set to its maximum detects only "all of them stopped", while the failure you care about is "one of them stopped".
The four single-DB services still take the newest match, because their glob is one PVC directory expected to match one path.

## healthchecks.io checks

**Five checks live here, and everything else pushes to uptime-kuma.**
The account is capped at 20 checks and six of them are pinged from outside this repo, so a check on healthchecks.io has to earn its slot.
These five do: the two restic checks because their multi-line ping bodies *are* the triage runbook and a one-line push message cannot carry them; `vps-uptime-kuma-alive` because it watches kuma and cannot live inside it; `estate-update`, because it is pinged by hand from a laptop at the close of a session, with no job and no cluster behind it; and `hermes-update`, for the same reason and from the same laptop, on a longer period.
Every other job in this estate drives a **push monitor** — inventory in [uptime-kuma.md](uptime-kuma.md#push-monitors).
Migrated August 26, 2026.

| Check | 1Password reference | Period / grace | Pinged by |
|---|---|---|---|
| `homelab-restic` | `op://Homelab/b2-restic/healthcheck-uuid` | 1d / 2h | restic CronJob, `/start` and exit code |
| `vps-restic` | `op://VPS/b2-restic/healthcheck-uuid` | 1d / 2h | restic CronJob, `/start` and exit code |
| `vps-uptime-kuma-alive` | `op://VPS/uptime-kuma/healthcheck-uuid` | 5m / 15m | An uptime-kuma monitor — [uptime-kuma.md](uptime-kuma.md#the-self-monitor-layer-4) |
| `estate-update` | `op://Homelab/estate-update/healthcheck-uuid` | 45d / 7d | Pinged by hand at the end of each update session. No job pings it |
| `hermes-update` | `op://Homelab/hermes-update/healthcheck-uuid` | 10d / 4d | Pinged by hand from the operator's laptop, on success only, at the end of an update session. No job pings it, and there is no `/start` — [the runbook's report step](hermes-vm-updates.md#report) |

**`hermes-update` and `estate-update` are both laptop-pinged, and `hermes-update` earns its separate slot on one distinct signal:** it catches the hermes step being skipped or failing inside a session that still pinged `estate-update`.
One check over both would go green on a session that updated the clusters and never touched the VM.

The 10-day period against a roughly weekly cadence, with 4 days of grace, allows one skipped week before it alarms.
It is pinged from the laptop rather than the VM because the UUID lives at `op://Homelab/hermes-update/healthcheck-uuid`, and the VM's 1Password service account can see only the `hermes` vault — the same constraint that puts `estate-update` on the laptop.

`hermes-update` is also the only row here that no manifest in this repository creates: the check and its 1Password field are made by hand, as step 8 of [the VM install](hermes-vm.md#installing-or-reinstalling).

**Two of the jobs this repo pings send `/start` and an exit code** — the two restic CronJobs, and since the migration they are the only ones that do.
Nothing replaced that pattern for everyone else, because the kuma push API has nothing to replace it with: a push is a heartbeat carrying a status, so there is no start signal to send.
For a push monitor the hang bound is the CronJob's own `activeDeadlineSeconds` and the silence bound is the monitor's heartbeat interval plus its retry.
A run that starts and wedges is killed by the deadline and then shows up as a missing heartbeat — the same alarm, one step later.

What did **not** change is the shape of the job around the signal, and it is still load-bearing.
`influx-backup` and `hermes-pull` need `set -eu -o pipefail` and their reporting call in an EXIT trap.
Under `set -e` alone, `xargs` swallows the prune step's `ls` failure and the job reports success anyway.
With the call on the last line instead of in a trap, a failing prune, a missing ConfigMap key or a dead influxdb pod produces *exactly nothing* until the silence bound expires about 30 hours later.
The accepted cost — a transient failure now alerts instead of self-healing into silence — is the better trade.
Both hindsight scripts follow the same EXIT-trap shape, for the same reason.
`cloudflare-analytics` reaches the same place by a different route: it is Python, with no trap at all, and gets its guarantee from a module-level `try`/`except` that catches `SystemExit`, `QueryFailed` and every other exception, sets a verdict for each, and pushes on the way out.

`hindsight-canary` is the only signal here that watches a *request path* rather than an artifact, and it exists because nothing else could.
Hermes fails open at four layers: with the memory server down, a turn proceeds with no memories injected and a retain is dropped with a `logger.warning`, so the client-side symptom of a dead memory backend is an agent that has forgotten things — indistinguishable from an agent that was never told them.
Worse, `/health` checks database connectivity and not auth validity, so a rotated or mistyped tenant key leaves every server-side signal green while every write is discarded.
The canary authenticates with the real tenant key and performs a real retain followed by a real recall against a dedicated `canary` bank, hourly, so both failures surface within roughly 90 minutes.
An uptime-kuma **HTTP** monitor could not have done this: kuma runs on the VPS, which has no route to any `*.cynexia.net` address (see [What this does not catch](#what-this-does-not-catch)), and an unauthenticated probe cannot see a broken write path in any case.
A **push** monitor reverses the direction — the canary pod calls out to `uptime.cynexia.com` — which is why the reporting side moved to kuma on August 26, 2026 while the probing side could not.
**Rotating the tenant key is not finished until the VM-side smoke test in [hindsight.md](hindsight.md) has been re-run** — the canary proves the server accepts writes, not that Hermes still sends them.

**The two ingest signals are now one monitor, `health-ingest`, and it stays success-only.**
One `ingest-freshness` CronJob checks both buckets in one process, so two monitors would be one signal counted twice.
It pushes `up` only when both buckets are fresh and pushes **nothing** on every other path — a stale bucket, a failed query, a dead InfluxDB.
A `down` push on a stale bucket would flip the monitor at the first 6-hourly run that found nothing, trading a 36-hour tolerance for a 6-hour one, on a signal that depends on the operator syncing a watch.
The absent heartbeat is the alarm, and `ingest-freshness` still always exits 0: a stale source must not also show up as a failed Job.

**What the merge costs is per-path resolution at the moment of the alarm.**
Two checks told you *which* ingest path went stale; one monitor tells you that one of them did.
The recovery is the `msg` on the **last** heartbeat before the silence, which carries both ages — a monitor that goes DOWN with a last message of `apple_age_h=3 garmin_age_h=22` names garmin without ambiguity — plus the pod log, which carries the full per-bucket verdict and keeps "stale" apart from "query failed".
Both ages are written on every `up` push for exactly this reason; do not trim one.

`jottacloud-backup` is success-only for a different reason: its request comes from `backup.sh` inside `ghcr.io/mnbf9rca/jottacloud-backup`, an image this repo does not build.
It is the one migrated job whose request shape this repo does not control, so the shape was measured rather than assumed (August 26, 2026): kuma routes the success POST, reading `status` and `msg` from the query string and discarding the body, and routes neither the `/fail` POST nor the every-run `/log` POST.
That is the contract this repo wants — **a failed backup pushes nothing and the monitor goes DOWN by silence** — at the cost of one cosmetic `WARNING: Failed to send…` line per unrouted request.
The full measurements, and what to re-check after an image bump, are in [uptime-kuma.md](uptime-kuma.md#the-one-monitor-whose-request-this-repo-does-not-control).

**`hermes-pull`** backs up the off-cluster hermes VM: it SSHes in with a forced-command key, runs `hermes backup`, and pulls the zip onto the `hermes-dumps` PVC ([homelab.md](homelab.md#hermes-vm-backup-and-restore)).
Its heartbeat reports a verdict, the pulled zip's size in KiB and a fixed `sha256_match=yes|no` — never the checksum values themselves, which are command output — and the pod log's `detail:` line carries the local-copy and prune counts as well.
The zip is verified four ways before publishing: a CRC test of every member on the VM, then a size floor, zip magic, and a remote-versus-local SHA-256 in the cluster.
A DOWN `homelab-hermes-pull` means no new zip landed that night; the restic gate's `hermes-zip` entries then turn `homelab-restic` red if the newest zip on the PVC goes stale past 30 hours or vanishes.
A VM rebuild turns the monitor DOWN by design: the pinned host key in the `hermes-known-hosts` ConfigMap no longer matches, and the fix is `ssh-keyscan` from a trusted network into that ConfigMap, never `StrictHostKeyChecking=no`.

`homelab-cloudflare-analytics` goes DOWN for one failure mode that is not a malfunction: **an unrecoverable gap**.
Cloudflare keeps 8 days, so if the job has been down longer the missing hours no longer exist anywhere.
It logs the range, writes an `ingest_gap` marker into InfluxDB so the hole reads as a hole rather than a quiet week, ingests what survives, and exits non-zero — once, since the next run's watermark is current again.
The heartbeat carries `verdict=gap` with `gap_hours=`, `gap_start=` and `gap_end=`.
Read a DOWN monitor here as "find out which hours were lost" ([homelab-health.md](homelab-health.md#gaps-are-permanent-so-they-are-loud)).

**`homelab-update-watch`** is the only signal here that watches the *repository* rather than the cluster.
It is covered in full below: [The update watcher](#the-update-watcher).

Adding either kind of signal takes [four edits](apply-workflow.md#adding-a-secret-is-four-edits-not-three) — the `op://` line, both Makefile lists and the manifest placeholder.
A missing `ENVSUBST_VAR_NAMES` entry, which would ship the literal `${VAR}` as the ping UUID or the push token, is caught by the Makefile's `PLACEHOLDER_SCAN` inside `make apply-homelab`, after the render and before kubectl, so nothing is applied and no dead signal is created.
Two things that scan does not do: it does not run in `diff-*`, so a clean diff is not proof the apply will proceed; and it cannot see a *well-formed but wrong* UUID or token, which reports to something that does not exist and is therefore dead from birth while looking configured.
Only a forced run settles that — and for a push monitor the run itself settles it, because kuma answers an unknown token with a 404 that `curl -f` and busybox `wget` both treat as a failure, so a silent runner is proof the heartbeat landed.
The read-only healthchecks.io API key is `op://Homelab/healthchecks.io/read-only-api-key`; if that path 404s, the item is still spelled `healtchecks.io`.

**The account-level nag is set to Daily, and it now covers four checks rather than thirteen.** healthchecks.io notifies on status *flips*, so a check that is already down raises nothing further; the supported fix is the account-wide report (Profile → reports, Hourly or Daily).
**Decision, taken August 26, 2026: Daily**, recorded on the operator's confirmation, since the Management API does not expose the report frequency and nothing here can check it.
It was deferred while `homelab-update-watch` went red on any open pull request, because a nag over a permanently red check trains the operator to ignore it.
That reason is gone twice over: the 45-day threshold removed the permanent red, and `homelab-update-watch` is no longer on this account at all.
What the nag reaches now is the two restic checks, `vps-uptime-kuma-alive`, `estate-update` and `hermes-update` — none of which has a normally-red steady state, so a daily reminder is signal.
**It does not reach the push monitors**: kuma's own notifications are configured per monitor inside kuma, and it has no equivalent digest.
A push monitor that goes DOWN alerts once on the flip and then stays quiet.

**One trap is Python-only and it is silent.**
Cloudflare answers urllib's default `Python-urllib/3.x` User-Agent with HTTP 403 and `error code: 1010` before the request reaches kuma, so both Python jobs set an explicit `User-Agent` on the push and both suites assert it.
A push failure is swallowed by design, so the only symptom is a monitor that never goes UP.
Shell runners use curl or wget and are unaffected.
Measurement and detail: [uptime-kuma.md](uptime-kuma.md#push-monitors).

### The update watcher

`update-watch` (namespace `ops`, 06:45Z daily) makes one unauthenticated GitHub call — `GET /repos/mnbf9rca/kubernetes_config/issues?state=open&per_page=100` — and drives the `homelab-update-watch` uptime-kuma push monitor.
It exists because the `health` and `ops` namespaces forbid keel: their images are pinned, Renovate proposes the bumps, and until this watcher existed nothing pointed at the waiting pull requests.
Detection for `health` came free on day one.

**DOWN means one of nine things.**
Five arrive as a `down` push and name themselves in the heartbeat's `verdict=` field.
The other four turn the monitor DOWN through **silence** past its interval plus retry: the job did not run, it ran and hung, its push token is well-formed but wrong, or GitHub has for a day been either unreachable or answering with something this script refuses to parse — every `nothing` verdict in the table below, sustained.

**Every verdict the watcher can emit, and what each one pushes.**
`up` is green, `down` is red, and an indeterminate verdict pushes **nothing at all**, which records no state change and cannot postpone, suppress or trigger an alert.
That third column replaced healthchecks.io's `/log` ping on August 26, 2026 and is behaviourally the same thing minus the event line in the history:

| `verdict=` | Pushes | Means |
|---|---|---|
| `ok` | `up` | The Dependency Dashboard is fresh and no Renovate pull request is open |
| `updates-waiting` | `up` | Open Renovate pull requests, none older than `pr_age_red_days=`. **Green on purpose** — see below |
| `updates-pending` | `down` | A Renovate pull request has been open longer than `pr_age_red_days=`, so an update session was plainly skipped |
| `renovate-stale` | `down` | The Dependency Dashboard has not moved in `renovate_alive_max_days=`. Renovate itself has gone quiet |
| `dashboard-missing` | `down` | Renovate's Dependency Dashboard is gone or closed, so Renovate is probably uninstalled |
| `renovate-config-error` | `down` | Renovate opened a configuration-error issue and has stopped proposing pull requests |
| `renovate-lookup-failed` | `down` | The Dependency Dashboard reports a failed package lookup, so `lookup_failures=` dependencies get no pull request at all, freezing every pinned image that references them — see below |
| `rate-limited` | nothing | GitHub's unauthenticated quota is exhausted for this IP |
| `secondary-limit` | nothing | A GitHub secondary rate limit |
| `repo-unreachable` | nothing | HTTP 404 — the repo was renamed, deleted or made private |
| `api-error` | nothing | Anything else unreadable: a 5xx, a timeout, a paginated response, an HTTP 200 carrying a JSON object, or a Dependency Dashboard whose timestamp did not parse |

`push_status()` and `ping_suffix()` in `update-watch.py` are the same decision in two vocabularies, and the suite asserts they agree; if they ever disagree the signal's meaning has quietly forked.

**A silence-triggered alert carries the previous run's message**, because kuma reports the monitor's last heartbeat alongside the DOWN event.
That is why every message carries `run_epoch=`, an integer Unix timestamp: a `run_epoch` a day old means "this message is not about this alert; the watcher has gone quiet".
Telling the four silence causes apart means reading the pod log.
The monitor has one bit; DOWN means "go and look".

**`next=` is the third token of every message and names what to do about that verdict** — the `gh pr list` command for `updates-pending`, the Mend job log and a `managerFilePatterns` check for `renovate-stale`, the `gh issue list` command for `renovate-config-error`, the dashboard's repository problems and the Mend run log for `renovate-lookup-failed`, the app-installations page for `dashboard-missing`, the pod-log command for the indeterminate verdicts, and `none` for both green verdicts (`updates-waiting`'s line says so explicitly, so a green message is not mistaken for one whose `next=` went missing).
Each string is a fixed literal in the script's `NEXT_ACTIONS` map, keyed by verdict, so the alert is self-contained and nothing derived at run time is formatted into it.
It sits third — behind only `verdict=` and `run_epoch=` — rather than last, because kuma stores one line and cuts it at 200 characters: under a multi-line body last was where the eye landed, but under a one-line message last is the first thing lost.
Read `next=` together with `run_epoch=`: a stale message's advice is about the last run that completed, not about the silence that raised the alert.
The counters that follow it — and everything the cut drops — are printed to the pod log in full on every run.

**There is no start signal, and one must not be invented.**
The push API has no such concept, and this job never wanted one.
Under healthchecks.io, a watcher that sent `/start` and then hit a single transient GitHub 503 would have sent `/start` then `/log` and gone **down one grace period later** — turning every unreadable run into a false alarm and destroying the property the indeterminate branch exists for.
A start signal would have bought only a duration graph for a job already bounded by `activeDeadlineSeconds: 300`, and silence past the monitor's interval already covers "did not run".

**Indeterminate runs push nothing and change nothing.**
A rate limit, a 404, a 5xx, a timeout, a paginated response, an HTTP 200 carrying a JSON *object*, and a Dependency Dashboard whose `updated_at` will not parse are all "I could not look", never "zero pull requests" and never "Renovate is alive".
Pushing `up` would report a successful read that did not happen and pushing `down` would turn every transient GitHub 503 into an alert, so the run sends nothing and logs `indeterminate verdict <v>: pushing nothing`.
If the outage persists no `up` push arrives either and the monitor goes DOWN on its own once its interval plus retry expires.
Verified live on August 26, 2026 by pointing a one-off Job at a repository that does not exist: `verdict=repo-unreachable`, nothing pushed, monitor unchanged.

**Why a `down` here when `ingest-freshness` was refused one.**
That refusal rested on tolerance: a stale bucket self-heals and was routinely, legitimately stale.
Neither half transfers.
An update never self-heals — it waits until a human merges and applies — so there is no tolerance to trade away once it has waited past `pr_age_red_days=`, and red on *that* condition is the whole requirement.
The tolerance an available update does have is the session cadence itself, and that is exactly what the 45-day threshold spends before going red.

**One alert, then silence.**
Notifications come from status *flips*, so a repeated `down` push against an already-DOWN monitor sends nothing further: one notification on the day an update appears, then quiet until it is merged and the next run flips the monitor UP.
Manufacturing a re-flip by pushing up-then-down stays rejected outright: it writes a false "everything is fine" event into the monitor's own history.

**An open pull request is UP.**
The estate updates in a session every 4 to 6 weeks, so a pending Renovate pull request is this design working, not a fault.
`verdict=updates-waiting` is the green form and `verdict=updates-pending` is the red one; the boundary is `pr_age_red_days=` in the heartbeat, 45 days at the time of writing — a session and a half.
Read the value the heartbeat carries, not this number.
The rule this replaced went red on any open pull request, which under session cadence made red the steady state, and an alarm that is normally red is not an alarm.

**Renovate's own liveness is a `down` verdict on this same monitor**, `verdict=renovate-stale`.
It fires when the Dependency Dashboard's `updated_at` (a stable API field; this signal reads none of the body) exceeds `renovate_alive_max_days=` in the heartbeat, and it is evaluated **above** the pull-request rules, so a dead Renovate with a young pull request still open cannot read as the green `updates-waiting`.
A missing dashboard and a configuration error keep their own, more specific verdicts — both are already red and both name their own `next=`.
A dashboard whose timestamp does not parse is `api-error`, which pushes **nothing at all** and so changes nothing: an unreadable field is never evidence that Renovate is alive.

**A failed package lookup is its own `down` verdict**, `verdict=renovate-lookup-failed`, added August 28, 2026.
Renovate records a failed datasource lookup on the Dependency Dashboard in two places.
The `## Repository Problems` section at the top carries a one-line `⚠️ WARN: Package lookup failures` summary and a link to the run log, and names nothing.
The dependency-lookup warning block further down — `> [!WARNING] Renovate failed to look up the following dependencies:` — names the packages and the files affected.
The job matches either, because neither is unconditional.
The case that prompted this sat there unread for weeks and was still open when this shipped: `Failed to look up docker package ghcr.io/keel-hq/keel: no-result`, against two digest-pinned images that only Renovate can move.
An image Renovate cannot look up gets no pull request, so a failed lookup freezes it while every guard stays green — `make check-renovate-scope` proves the file is in scope, never that the lookup succeeded.
**What to do when it fires:** open the Dependency Dashboard issue and read the dependency-lookup warning block, which names the packages and the files affected, then fix the lookup.
**The one case seen here was diagnosed on August 28, 2026 from two Mend run logs**, and three plausible causes are ruled out.
Not repo configuration; not registry authentication, because sibling `ghcr.io` packages token-fetch and pull manifests normally seconds later in the same runs that fail on `ghcr.io/keel-hq/keel`; and not runner memory, because the second run completed at 1.8GB of the runner's 3.0GB cap and failed identically.
What both logs show is that the keel lookup fails **before the `ghcr.io` host queue is created** and issues **no HTTP request at all**.
A `no-result` returned without a network request is being served from Mend's shared package cache at the datasource layer.
That is a known, acknowledged upstream bug: Renovate's docker `getTags()` caches a `null` result unconditionally under a key with no tenant isolation (`registryHost:repository`), so one tenant's bad credential poisons the entry for every tenant, and reads short-circuit before any HTTP — renovatebot discussion 45249, fix pending in pull requests 45348 and 45409.
**The workable remedy is repeated reruns from the Dependency Dashboard's own checkbox**, spread over hours: the poisoned entry's TTL is 30 minutes, and a run landing in an expired window looks the package up anonymously, succeeds, and writes the good tag list back for everyone — until the next poisoner.
There is no repo-side cache-bust (the key derives from registry plus package name, so nothing this repo can edit reaches it — a `hostRules` entry in `renovate.json` least of all) and Mend documents no cache-eviction procedure.
A new instance of this verdict still starts at the run log.
This diagnosis belongs to one package, and the same one-line dashboard warning covers failures with nothing in common.
The heartbeat carries `lookup_failures=`, a count and nothing else: a package name is remote text and rule 4 keeps it out of the message.
The pod log carries the full lines on every run, and that is where the names are.
The count heads the counter group rather than trailing it, because this verdict's `next=` is 103 characters, which with `verdict=` and `run_epoch=` leaves 40 characters — past the second counter it would be cut from the one message it exists for.
`lookup_failures=0` means the section is there but its item lines did not parse — what a Renovate reword looks like — and the verdict fires anyway, because the section itself is the evidence; a zero also arises from a body carrying only the `Package lookup failures` summary bullet and no per-package lines, which is the likelier route when `suppressNotifications` hides the warning blockquote.
It is evaluated above the pull-request rules and below staleness: a dependency that cannot be looked up proposes nothing, so the pull-request count is an undercount by exactly the frozen images, while a Renovate that has stopped running at all is the larger fact and its dashboard is as stale as the rest of it.
A body this job could not read is not a lookup failure and never becomes one — it reads the body only when the issue list came back, so nothing here weakens the indeterminate contract above.

**Both threshold fields are the first things the 200-character cut takes, by design.**
`pr_age_red_days=` and `renovate_alive_max_days=` are emitted last because they are literals in `update-watch.py` and identical between runs, so an alert that loses them costs the reader one look at the source.
What must never be cut is `run_epoch=`, which is emitted first for that reason.
Both halves are asserted, not merely intended: `test_run_epoch_and_next_survive_the_cut_for_every_verdict` holds the protected end, and `test_the_thresholds_are_the_tokens_the_cut_takes_first` holds the sacrificed one — it proves the thresholds really are emitted last and that what survives is a whole-token *prefix*, so nothing is ever dropped from the middle.

**The liveness threshold is the unarmed floor, 14 days, and it is due a re-arming.**
The rule is twice the maximum `dash_age_days` this job has emitted across its last 30 heartbeats, floored at
14. Read August 26, 2026: the job had shipped two days earlier and had logged six pings in total, fewer than the 14 the rule needs, so there was no observed maximum to double.
    Those six were healthchecks.io pings whose bodies could not be read — the only API key in the vault is read-only and `/api/v3/checks/<uuid>/pings/` refuses it.
    The history now accumulates in the `homelab-update-watch` kuma monitor, where each heartbeat's message is readable in the UI.
    Re-read it after a month of data and re-arm `RENOVATE_ALIVE_MAX_DAYS` in `homelab/ops/scripts/update-watch.py`: a threshold tighter than the quiet periods is DOWN every fortnight, and one looser than a month lets Renovate die unnoticed.

**Why this is not a second signal.**
It was one, in the design that preceded this.
The argument was that an alerting backend notifies on status *flips*, so a liveness signal folded into a permanently-red one could never fire — and under the old rule, red on any open pull request, `homelab-update-watch` was permanently red.
The 45-day threshold removes the permanent red, so the one monitor flips on a Renovate death exactly as a second one would have.
Decided August 26, 2026, when the constraint was a healthchecks.io account capped at 20 checks; the argument survives the move to a push monitor unchanged.
The residual: while the monitor is already DOWN for `updates-pending`, a Renovate death changes `verdict=` in the heartbeat but raises no second alert.
Read `verdict=` before assuming you know why a DOWN monitor is DOWN.

**The watcher counts OPEN pull requests, so anything held on the Dependency Dashboard is invisible to it.**
Renovate lists an update as a dashboard checkbox rather than opening a pull request whenever a rule says to.
**The list is `renovate.json`, not this page:** every rule there setting `dependencyDashboardApproval` holds its matches on the dashboard, which today means all majors, `kroniak/ssh-client` digest bumps and `thisisarpanghosh/garmin-fetch-data`.
Read the config rather than trusting an enumeration here, which has drifted once already.
Such an update waits for a human tick indefinitely while this watcher reports `ok` with zero open pull requests, and `renovate-stale` does not catch it either: Renovate is alive and touching the dashboard the whole time.
**Read the Dependency Dashboard, not just the pull-request list, at the start of every update session.**
Counting dashboard-held items instead would mean taking an *inventory* out of the dashboard's markdown, which `update-watch.py` still refuses — the issue's `updated_at` is a stable API field, its body is not, and a reworded body would undercount silently while the count was reported as authoritative.
The repository-problems marker the watcher does read is the opposite shape and is the whole of the exception: it can only fail to *fire*, so a reword loses one red verdict and can turn nothing green, because no other verdict consults the body at all.

**`estate-update` is the session's own dead-man's-switch**, at roughly 45 days with a 7-day grace, pinged by hand at the end of each session.
It exists because a pull request's age is not a reliable proxy for a skipped session.
`renovate.json` sets `recreateWhen: "always"`, which is *meant* to recreate a closed pull request — **never verified live here** (see below), so treat what follows as what the config says, not as observed fact.
If it does recreate, the replacement is a new pull request with a new `created_at`, so closing one **without merging it** restarts its clock at zero.
(Merging closes a pull request too, but there is no recreation and no clock left to restart — the update is done.)
An untouched pull request does keep its age, so `updates-pending` still fires on a genuinely stalled one — but a close, or a stream of churning updates, hides a skipped session from any age-based threshold.
This check cannot be fooled that way, because nothing but a human ever pings it.
Ping it with:

    curl -fsS -m 15 -o /dev/null --data-binary 'summary=estate-update session complete' \
      "https://hc-ping.com/$(op read 'op://Homelab/estate-update/healthcheck-uuid')"

The check and its UUID both exist, created by hand on August 26, 2026 — neither could be scripted: the only healthchecks.io API key in the vault is read-only, and creating a check needs a read-write one.
The UUID is typed `[text]` in 1Password, not concealed, because a ping UUID grants no access and only lets a stranger mask a failure; that is why it stays out of this public repository but is legible in the vault.

**How the account got to 20 and back down to 10.**
`estate-update` took the twentieth and last slot on August 26, 2026, and the arithmetic is worth writing down because it did not add up on first inspection: this repository drove 12 checks and the operator holds 6 more outside it, which is 18.
The missing one was an orphaned `homelab-keel-fresh` healthchecks.io check — created against the original spec, which gave that job a healthchecks.io check before the check-budget ruling moved `keel-fresh` to an uptime-kuma push monitor instead.
Nothing here could ever have pinged it: the CronJob is handed a kuma push URL and its runner contains no `hc-ping` reference at all, so it sat `new` forever, costing a slot and alarming on nothing. 12 plus 6 plus that orphan is 19, and `estate-update` made 20.

The orphan has been deleted, and no `vps-keel-fresh` healthchecks.io check was ever created — the VPS copy of that job drives a kuma push monitor of the same name, which costs no slot here.
Later the same day nine more routine heartbeats moved to push monitors, leaving this repository with **four** checks.
The nine retired checks are **not deleted yet**: they are left in place and un-pinged so that a red check at healthchecks.io beside a green monitor in kuma is the migration visibly working, and the operator deletes each one after seeing that pair.
Until they go the account holds 19 of 20; once they go it holds **10**, and **11** once `hermes-update` is created during the hermes VM install.
New scheduled work takes a push monitor, per the policy in [Layers 3 and 4](#layers-3-and-4-uptime-kuma) — that is what keeps this from happening again.

**The quarterly liveness drill, narrowed.**
`renovate-stale` now covers the *idle-Renovate* case this drill was invented for, so what is left of it is the residual: a Renovate that keeps updating its dashboard while proposing nothing — `managerFilePatterns` that stopped matching, a registry lookup failing silently.
Once a quarter, bump a pin backwards and confirm a pull request appears within a cycle.
`make check-renovate-scope` closes the commonest variant, a pinned image no file in scope names.
If the drill ever fails for a reason that guard cannot see, the first thing to build is a registry canary.

**Never close a Renovate pull request** — the operating rule from the design spec, and it still stands.
`renovate.json` sets `recreateWhen: "always"`, so a closed pull request is *supposed* to be recreated rather than left on the dashboard's Closed/Ignored list where Renovate's default would strand it.
**That recreation has never been verified live against a real closed pull request**, and the verification is a rollout step nobody has done.
Until it passes, a close risks the worst outcome this page has: if `recreateWhen` is not doing what the config says, the update goes to Closed/Ignored and never comes back — a green check over an update that is silently gone.
That risk, not tidiness, is why the prohibition exists.
Merging is not closing in this sense and is unaffected.

**Once that verification passes, closing becomes a snooze of up to 45 days.**
The mechanism: recreation produces a *new* pull request with a new `created_at`, and the watcher judges age on `created_at`, so closing an aged pull request without merging it restarts that clock at zero and can drop the verdict from `updates-pending` back to the green `updates-waiting` for up to another `pr_age_red_days=`.

**"Can", not "will": `oldest_pr_days` is the maximum over every open pull request.**
Closing one aged pull request while another aged one is still open leaves the monitor DOWN on the survivor.
Do not read a still-DOWN monitor as "the close did not take" and close more — read `oldest_pr` in the pod log, which names the pull request the verdict is now about.
It is emitted after the counters the 200-character heartbeat message is guaranteed to carry, so on a long `next=` it may not be in the alert.

**And nothing on this monitor will tell you a close happened at all.**
An operator who closes a pull request to tidy up silences the watcher for a month and a half without meaning to.
That is precisely why `estate-update` exists on its own 45-day period, pinged only by a human at the end of a session: no amount of pull-request churn can reset it, so it is what still catches the skipped session underneath a silenced watcher.

### Ping bodies and heartbeat messages

There are two destinations now and one rule set.
A healthchecks.io **body** is multi-line and effectively unbounded; a kuma **heartbeat message** is one `msg` string, cut at 200 characters.
The disclosure rules below apply to **both**, and `make check-ping-bodies` enforces them across both — it recognises a sink by function name (`emit`, `say_err`, `fatal` in shell; `hc_emit`, `hc_summary` in Python) and never by the destination host, which is why the sinks kept their names through the migration.
What differs between the two is only how much fits and who stores it.

Every message carries a short `key=value` summary: printable ASCII, and a verdict first.
The one bit is still the alerting signal; the summary makes a *green* signal legible, so the history answers "what did it see?" once the pod log has aged out.
Without one, `health-apple-ingest` sat green through five days of stale Apple Health data — nothing had malfunctioned, and green could not distinguish "fresh" from "stale, window not expired".

**Both are disclosure channels, for different reasons.** healthchecks.io is a third-party SaaS in the EU: a body leaves the estate, is stored in their object storage, repeats on every run until somebody fixes the script, and cannot be shortened short of deleting the check. uptime-kuma is the operator's own VPS, so a heartbeat message stays inside the estate — but it is still written to a database, still repeats, and **still travels with the alert** to whatever notification transports that monitor has configured.
Treat an `emit` call like a committed line either way.

**Forbidden, because they grant somebody something:** Secret contents — anything from a Kubernetes Secret or a 1Password item — and the reporting credential itself.
For healthchecks.io that is the ping UUID; for kuma it is the push **token**, which is the last path segment of `PUSH_URL`, so emitting the URL emits the token.
Either lets a stranger report a heartbeat and mask a genuine failure.
`check-ping-bodies` names `HC_UUID`, `HC_APPLE`, `HC_GARMIN` and `PUSH_URL` in `DENY_VARS` for that reason.
A health *reading* is likewise never emitted; the ingest signal carries freshness *ages* only, by the [explicit decision recorded below](#named-accepted-residual-the-ingest-signal-leaks-a-presence-timeline).

**Omitted because nothing reads them, not because they are sensitive:** restic repository URIs, B2 and InfluxDB bucket names, PVC UUIDs, namespaces, pod and node names.
These are ordinary identifiers — they grant nothing, they are fine in a pod log, and no rule bans them; they are absent only because they answer no question an operator asks at 3am.
Do not read a classification into that absence, and do not open an honesty-box row if one appears somewhere (the three tiers are in `AGENTS.md`).
`make check-ping-bodies` does deny every name in `ENVSUBST_VAR_NAMES`, bucket names included, but that is a mechanical hazard list, not a secrecy classification.

**Never build a message from a command's output.** healthchecks.io's own documentation teaches the opposite, and here that pattern leaks: the two scripts `influx-backup.sh` execs into the influxdb pod pass the InfluxDB **operator token** on argv (`influx-native-backup.sh:21`, `influx-export-lp.sh:36`), and a failing `wget` or `curl` quotes the URL it was given, which is the reporting credential whichever backend it points at.
The rule is blanket rather than tiered because **a script cannot sort the tiers apart at runtime**: it cannot tell a bucket name from an operator token in a string it did not construct, so command output is unclassifiable and stays out.

`emit` — and `hc_emit`/`hc_summary` in the Python jobs — is therefore only ever called with a literal key and a value the script computed itself: a count, an age, a byte size, a path built from a literal glob, or a verdict from a fixed enum.
A verdict may also *select* a fixed literal rather than being emitted raw, which is how `update-watch` builds its `next=` line: the enum decides which sentence, and the sentences are written into the source.
Nothing derived at run time may be formatted into one.

**What the 200-character cut changes, and what it does not.**
It does not relax any rule above; a truncated secret is still a disclosed secret.
What it changes is *ordering*, and the rule is **sacrifice the token that carries least, which is not the same token in every runner**.
All of them emit the verdict first, then the values an operator acts on.
After that they split three ways:

- **`influx-backup` and `hermes-pull`** each have one variable-length token, `error=`, holding a `fatal` message.
  It goes last, so a long one costs the counters nothing.
- **`update-watch`** has one too, `next=`, at 89 to 111 characters — but it is the *action*, so it is deliberately protected in third place and the two fixed-width threshold literals are what the cut takes instead.
  They are constants in the source and identical between runs, so losing them costs one look at `update-watch.py`; losing the action would cost the alert its point.
- **`ingest-freshness`, both hindsight jobs, `cloudflare-analytics` and both `keel-fresh` jobs** emit nothing of variable length at all, so the question does not arise for them.

Read that as one rule with three outcomes rather than three rules.

Everything that used to be a body line is now printed to the pod log as well — the shell runners as a `detail:` line from the exit trap, the Python jobs as a `heartbeat message (full):` block — so nothing was lost, it moved.
**Read the pod log first**; the heartbeat history is the fallback, not the record.
The pod log's window is `ttlSecondsAfterFinished` on the Job, and that varies more than it looks: **3 days** for `cloudflare-analytics`, `withings-ingest`, `hermes-pull`, `update-watch` and both `keel-fresh` jobs; **2 days** for `influx-backup` and `hindsight-pg-dump`; **1 day** for `ingest-freshness` and `jottacloud-backup`; and **1 hour** for `hindsight-canary`, which runs hourly.
So it is an hour on the canary and no more than three days on anything.

`make check-ping-bodies` enforces all of this.
It catches the one-intermediate-variable evasion (`M=$(cmd); emit "error=$M"`), and refuses a denied name in every parameter-expansion form — `${PUSH_URL:-}`, `${PUSH_URL#p}`, `${PUSH_URL/a/b}`, `${#PUSH_URL}`.
A taint clears only through an explicit `# check-ping-bodies: untaint <NAME> <reason>` line.
In Python it works from the other end, refusing any name a sink argument references unless that name is on `PY_VALUE_ALLOWLIST` in `scripts/check-ping-bodies.py`.
Adding a name there is a deliberate review act: it asserts the value is an int, a timestamp or a fixed literal, and the reason belongs in the comment beside it.
Its `OK:` line reports a file count and a **sink-call count**, and the right way to read that count is per file.
The aggregate moves on its own: it was 153 across the homelab tree before the 2026-08-26 migration and 124 immediately after, because multi-line bodies collapsed into short messages, and it has since risen to 129 as fixes added sink calls back.
Do not treat any of those as a target — re-run the guard for today's figure.
What must never happen is a *file* losing its last sink call, or dropping out of the scan — that is "I could not look" reported as "I looked and everything is fine", and `REQUIRED_TARGETS` in the guard exists to catch the second half of it.

Healthchecks.io bodies die with their ping-log entry, `Check.prune()` removing the objects then the ping rows — 100 entries per check on Hobbyist, 1000 on Business.
With only the five checks in the table above left there, that window is long. kuma keeps heartbeats per monitor on its own retention setting, in the VPS database that the nightly restic sweep backs up.

One healthchecks.io quirk survives and still constrains what may be written: `has_confirmation_link` is set from the body on every action, driving a UI nag, so no body may contain the substring `confirm`.
That now applies only to the restic bodies and the hand-written `estate-update` and `hermes-update` pings, but the estate keeps one spelling — say "check" instead — and `update-watch`'s test suite still asserts it.

#### Reading a restic failure body

`failed_step=` names the phase that set the exit code: `unlock|backup|forget|check` for restic's own failures, `gate` for the verification gate.
On homelab it is captured where the chain aborts, *before* the gate and `restic check` run, because both run unconditionally afterwards and would overwrite it — so a restic failure is never reported as a gate failure, even when the gate also fails.
`restic_check=` is `ok` or `failed`, and `not-reached` when the run died before that step.
`prune=` has three states and they are not interchangeable:

| Value | Meaning | What to do |
|---|---|---|
| `ran` | `forget --prune` completed | Nothing |
| `skipped` | The gate deferred retention on purpose | Nothing urgent. The repository grows in B2 until the gate goes green |
| `failed` | `forget --prune` started and died | Look tonight. Snapshots may be partly expired |

Reporting `failed` as `skipped` is what makes an operator not look, so the script keeps them apart.

**`failed_step=backup` with rc=3 can be a transient, self-healing race on live SQLite files.**
Restic exits 3 for "at least one source file could not be read"; the snapshot is still saved.
Seen 2026-08-22 on VPS: a freshrss `db.sqlite-journal` file vanished between restic's directory enumeration and its xattr read, the first attempt pinged red, and the Job's `backoffLimit: 1` retry pinged green a minute later (the Job shows `Complete` with roughly double the usual duration — two attempts).
Before treating an rc=3 as an incident: read the failed pod's log for the named file, and check whether it is a `-journal`/`-wal`/`-shm` sidecar of a database whose quiesced `.restic` snapshot the gate already verified fresh — those transient files are useless in a file-level restore, and the quiesced copy is the real artifact.
Recurring rc=3 on such files can be silenced with `--exclude='*.sqlite-journal'` and WAL siblings in the backup command; a one-off needs nothing.

### Checks in the account that this repo does not ping

The Management API returned 19 checks when it was last counted, on 2026-08-26.
Four of those are in the table above (`hermes-update`, its fifth row, was created later, at VM install, and did not exist at that count).
Nine are the retired checks this repo no longer pings, which stay until the operator deletes them by hand and are named in the migration paragraph above, and six belong to the operator — `adsb.cynexia.net`, `pve3.cynexia.net`, `fs.cynexia.net`, `tailscale unattended upgrades`, `Home Assistant`, `upsd.cynexia.net` — pinged from Proxmox hosts, Home Assistant and host cron, and deliberately outside this repo.
Once the nine go the count is 10, and 11 with `hermes-update`.
Re-count with the command below rather than trusting the arithmetic here; the point of this section is that "not in the table" can still be told from "does not exist".

```bash
curl -sS -H "X-Api-Key: $(op read 'op://Homelab/healthchecks.io/read-only-api-key')" \
  https://healthchecks.io/api/v3/checks/ | grep -o '"name": *"[^"]*"'
```

**`upsd.cynexia.net` has `n_pings=0`**: never pinged, so wire it up or delete it.

A seventh non-repo check existed until 2026-08-26: the orphaned `homelab-keel-fresh` described in the migration paragraph above.
It is worth remembering as a shape.
**A superseded design can leave a check behind that no repository grep will find**, which is what this census is for: reconcile the account against the table above whenever a job changes which instrument it drives.

## Layers 3 and 4: uptime-kuma

Layer 3 is a hand-maintained uptime-kuma instance on the VPS; layer 4 is one monitor inside it that pings healthchecks.io, so uptime-kuma's own death is visible.
Both are UI procedures, so they live in **[uptime-kuma.md](uptime-kuma.md)** — monitor list, per-monitor HTTP settings, the Cloudflare Access trap and the self-monitor.
One consequence bites from this side: a monitor that follows redirects reports UP off the Cloudflare login page while the origin is dead.

Layer 3 is no longer only outbound HTTP checks.
It also holds **push monitors**, driven by jobs that send a heartbeat rather than answering a request — the dead-man's-switch shape that used to mean a healthchecks.io check.
Ten were created on August 26, 2026: `homelab-keel-fresh` and `vps-keel-fresh`, and the eight that took over the routine heartbeats — `health-influx-backup`, `health-ingest`, `homelab-cloudflare-analytics`, `homelab-hermes-pull`, `hindsight-pg-dump`, `hindsight-canary`, `homelab-update-watch` and `jottacloud-backup`.
An eleventh, **`hermes-app-alive`**, is the one driven from **outside** both clusters: a `no_agent` cron job inside the hermes agent on the off-cluster VM at 05:45 UTC, `up` on exit 0 and `down` otherwise ([hermes-vm.md](hermes-vm.md#reading-a-down-hermes-app-alive)).
That widens the `/api/push/*` Access bypass's blast radius past "the clusters".
A twelfth, **`withings-ingest`**, was added on September 2, 2026 for the `withings-ingest` CronJob in `health`.
Because the healthchecks.io account is capped at 20 checks, new scheduled work takes a push monitor instead.
Both the roster and the Cloudflare Access bypass that lets a job reach the push endpoint are in **[uptime-kuma.md](uptime-kuma.md#push-monitors)**.

**The monitors deliberately kept the retired checks' names**, so the estate reads as one inventory across the change: `health-influx-backup` names the same job it always did, in a different place.
The one exception is the merge — `health-apple-ingest` and `health-garmin-ingest` became the single `health-ingest`, because one CronJob checked both in one process.

What that costs: a push carries one short `msg` line rather than a multi-line body, and there is no `/start`.
The verdict and one or two counters travel with the alert; the full diagnostic stays in the pod log until `ttlSecondsAfterFinished` reaps it, so read the pod log first and the kuma heartbeat history second.
Two of the migrated jobs also push **nothing** on some runs — `ingest-freshness` on any non-fresh path, `update-watch` on an indeterminate one — so a monitor that has not moved recently is not necessarily broken, and its silence bound is the interval plus retry rather than any per-run signal.

### The keel dead-man's-switch

keel's own probes hit `/healthz`, which answers from its HTTP server while the registry poll goroutine is dead.
A wedged poll loop therefore leaves keel Running, Ready and green while every floating-tag workload in the estate silently stops receiving updates.
The `keel-fresh` CronJob reads two numbers from keel's own `/metrics` once a day — never from log text — and pushes the `homelab-keel-fresh` uptime-kuma monitor DOWN on either ([uptime-kuma.md](uptime-kuma.md#push-monitors)).

Both numbers come from one endpoint, and that is worth knowing before debugging it.
The job was designed to read the image count from keel's `/v1/tracked` REST listing; that endpoint does not exist here. keel registers its entire `/v1/*` admin API only when authentication is configured, and neither cluster's keel configures any, so every admin path answers 404 — verified against 0.22.1 on August 26, 2026.
The replacement, `poll_trigger_tracked_images`, is a gauge keel sets to the tracked-image count on every reconcile, so it is the same number with no credential and no second failure mode.

The VPS cluster runs its own copy in its own `ops` namespace, at 07:45Z, with its own script file, its own image floor and its own `vps-keel-fresh` push monitor.
It is a deliberate copy rather than a shared file: a homelab pod holding a VPS kubeconfig would be a credential crossing a cluster boundary to save one file, and kustomize will not read a generator source outside its own root anyway.
**Edit the two together** — a fix applied to one cluster and not the other is a check that has quietly stopped checking on the cluster nobody looked at.

`vps-keel-fresh` is pushed from the same cluster uptime-kuma runs on, so a VPS-wide outage takes the job and its watcher together.
That is layer 4's job, not this monitor's: `vps-uptime-kuma-alive` is at healthchecks.io precisely so something outside the VPS notices.
The same reasoning now covers twelve push monitors rather than two, and it is the reason `vps-uptime-kuma-alive` may never move: if kuma dies, every heartbeat in the estate stops arriving and only something outside it can say so.

| `verdict=` | Means |
|---|---|
| `ok` | The registry poll counter moved since yesterday and keel tracks at least the floor number of images |
| `polls-stalled` | The counter has not moved in a day, or has never moved at all. The poll loop is dead; restart keel and read its log |
| `too-few-images` | keel is polling but tracks fewer images than the floor. Its Deployment watch has fallen over, or a workload lost its annotations |
| `metric-missing` | `/metrics` answered but did not carry all three expected metrics. An upstream rename; re-verify the names and update `keel-fresh.sh` |
| `metrics-unreachable` | keel did not answer on 9300. It is down, or the `keel` Service lost its selector |
| `first-run` | No stored state, so nothing to compare. Green, because reaching this verdict already proves all three metrics are present, the counter is non-zero and the image floor is met |
| `restarted` | keel restarted, so the counter legitimately reset. Green for the same reason, and it is deliberately checked before the zero-counter test so a keel that started seconds ago — counter still zero, first scan not yet run — reads as `restarted` rather than a red `polls-stalled`. **Accepted residual: a crashlooping keel is permanently green here.** If keel restarts between every daily run the start epoch differs every time, so the job returns `restarted` at exit 0 forever and never reaches the delta comparison. It is not wholly vacuous — the metric-presence assertion and the image floor still carry every run — but note what does **not**: the at-least-one-poll check sits *below* the restart branch, so it is skipped on exactly this path, and the delta comparison is never reached either. Nothing in the estate alerts on restart counts, so this compound failure has no detector anywhere. Repeated `restarted` verdicts on consecutive days are the only signal; treat them as a fault to investigate, not as noise |

There is no `tracked-unreachable`: with one endpoint there is no second request that could fail on its own, so `metrics-unreachable` covers it.

**There is no `/start` here, and the reason is the API rather than a judgement** — as for every push monitor in the estate.
The hang bound is `activeDeadlineSeconds: 300` on the CronJob and the silence bound is the monitor's interval plus its one retry.
Every branch in *this* script is determinate — its only peer is a ClusterIP, so an unanswered request *is* the failure it exists to catch — which is why it can push `down` on failure rather than going quiet and waiting for the interval.
`ingest-freshness` and `update-watch` are the two that cannot say that of every branch, and they stay silent on the branches they cannot.

**The message is short, deliberately.** kuma stores one line per heartbeat, so the alert carries `verdict=`, `polls_delta=` and `images=n/floor` and nothing else.
The rest — the metric names, the stored state, the resolved endpoint — is in the pod log.

**The image floor is a literal and it does not track reality on its own.**
It was set at rollout to the steady-state tracked-image count with margin: 5 against the 6 homelab's own script records, one container clear, and 7 against the 9 measured on the VPS, two clear.
The homelab floor read 4 against 5 until September 2, 2026, when tinyproxy added a sixth tracked image and the floor was re-derived.
The margins differ because the VPS floor was fixed before its count was measured and left alone once the measurement came in higher than expected — a floor with more headroom than the rule asks for is not worth moving.
Reconciling either number against a list of keel-annotated workloads is off by however many distinct sidecar images those workloads carry: the gauge counts **images**, and keel tracks every container in an annotated workload, which is why the VPS reads 9 over 8 Deployments.
A workload on an image the gauge already tracks does not raise either floor; a workload that adds a new image does, and the floor is re-derived at that point.
Removing several without taking that estate below its floor does not lower it.
Revisit them whenever the keel-managed set changes materially — a floor that has drifted below reality is a check that has stopped checking.

**A day-apart comparison needs a day.**
Two runs minutes apart legitimately produce `polls-stalled`, because keel polls every six hours and the counter genuinely has not moved.
That is the check working.
When forcing runs by hand, expect it.

## What this does not catch

Probes fix hung request paths, not silently stopped background work — often the likelier incident.

| Service | The probe stays green while… |
|---|---|
| **umami** | `/api/heartbeat` returns a static `{ok:true}` that never touches Prisma. It returns 200 through any database failure (upstream #3417, connection-pool exhaustion). This buys Node-wedge detection only, not DB-outage detection |
| **changedetection** | Upstream #4214: 134 watches went 23 days unchecked while `/` returned 200 and `/worker-health` reported healthy, because the ticker died, not the workers. Only `overdue_watches` from `/api/v1/systeminfo` sees it. Wire it as an external json-query alert, never as liveness: a restart does not fix a scheduling bug |
| **uptime-kuma** | The HTTP server and the monitor scheduler run independently (#4967). A monitoring tool that has silently stopped monitoring is the worst version of this bug, and no in-pod probe detects it. Hence layer 4 |
| **karakeep** | `/api/health` is a hardcoded literal in the web process and cannot observe the worker. Stuck-queue reports (#1802, #2704) all leave it returning 200. The detector is the `karakeep_queue_jobs` metric (`pending > 0 && running == 0`) |
| **freshrss** | `/api/` never opens the database, and feed refresh runs from a separate `crond`. A dead cron serves the UI perfectly and stops fetching news |
| **garmin-grafana** | `write_points_to_influxdb()` catches InfluxDB errors, logs them and returns normally, after which the caller advances the watermark. An InfluxDB outage causes permanent data loss for that window with the process Running and Ready. `ingest-freshness` covers it; no probe improves on that |
| **influxdb-mcp** | Its probes are `tcpSocket`. A wedged HTTP handler with a live listener passes them. The MCP server exposes no health endpoint |
| **homelab services** | The external layer runs on the VPS, which has no route to `*.cynexia.net`, so no kuma **HTTP** monitor can reach them. Only the four hostnames on the homelab cloudflared tunnel get that coverage. sonarr, radarr, sabnzbd, emby, hydra2 and grafana have probes and nothing external. `hindsight` is the one exception, and it got there by giving up on an inbound prober entirely: its noticer is an **in-cluster** authenticated canary CronJob that pushes *outward* to a kuma push monitor, which needs no route in and no public exposure. Every migrated homelab job now reports the same way — outbound, through the Access bypass — which is what makes a private cluster visible to a monitor it cannot be reached from |
| **the VPS gate** | It proves each snapshot exists and is recent, and — through the sidecar's own refusal to publish a schema-less snapshot — that it holds at least one schema object. It does not prove the contents are complete or uncorrupted. A snapshot missing rows, or with a corrupt page below the `sqlite_master` read, passes everything here and surfaces at restore time |
| **VPS etcd member count** | Nothing checks it. After the August 28, 2026 expansion the cluster runs three control planes, and a node that dies and stays dead silently returns it to the fault tolerance it had before — two members, then one. Every workload keeps serving and no check flips. Noticed only by a human running `talosctl -n ubuntu-16gb-fsn1-2 etcd members` |
| **cloudflared node spread** | The Deployment reports `2/2` whether the two replicas sit on two nodes or on one. Required anti-affinity prevents the scheduler from co-locating them, so the realistic path is a node staying down long enough that only one replica is effectively serving — which reads as fully healthy from outside. Nothing asserts the spread |
| **a stalled cloudflared rollout** | keel runs `policy: force` on `cloudflare/cloudflared:latest` here. With `maxUnavailable: 0` and required anti-affinity, a keel-triggered rollout while any node is unavailable has nowhere to place the surge pod and stalls with a `Pending` pod past `progressDeadlineSeconds`. The old pods keep serving, so nothing is externally visible and the tunnel silently stops taking updates |
| **hermes-webui (hermes VM)** | `hermes-app-alive` checks it, but **once a day at 05:45 UTC**, from inside the VM — as a `no_agent` cron job inside `hermes-gateway`, so a beat arriving at all is also proof that the **default gateway executes**, which nothing used to check. That check curls the WebUI's own `/health` on `127.0.0.1:8787` and deep-imports `run_agent` from the shared venv — the only cheap assertion that catches the documented silent failure, where a venv missing `dotenv`, `httpx` or `openai` leaves the unit `active` and `/health` answering `status: ok` while every chat turn returns `AIAgent not available`. What that costs: **detection latency is up to about a day**, plus the monitor's 24-hour heartbeat and 6-hour retry before a missing beat alarms. **Accepted by the operator on August 26, 2026 — "homelab not NASA."** The 15-minute external chat-turn monitor the spec called for was deleted as overengineering, along with the published hostname, the Access app, the dedicated service token and the probe profile it needed. So **no chat turn is monitored at all**: the daily check makes none by design, and the only one anybody makes is the update runbook's verification step, which runs when a person runs it — so a fault that lets the WebUI import, serve `/health` and keep its units up while failing every chat turn surfaces at the next update session, roughly a week later. A fault confined to the `emh`, `hal` or `default` profile *state* — as opposed to the shared venv — stays invisible. Two older consequences still hold. The unit sets `StartLimitIntervalSec=60`/`StartLimitBurst=5` rather than the gateways' `StartLimitIntervalSec=0`, so a start that keeps failing parks in `failed` where `systemctl --user is-failed` reports it, instead of looping invisibly every 5s. And a future **HTTP** monitor on `hermes-app.cynexia.com` must carry the service-token headers and `maxredirects: 0` — see the Cloudflare Access trap in [uptime-kuma.md](uptime-kuma.md). **Nothing rolls back automatically, and that is the design:** the update path is a runbook a person follows with the session open ([hermes-vm-updates.md](hermes-vm-updates.md)), and its rollback is a judged decision rather than a trigger. What detects a bad update is therefore the person running it, not a check |
| **the hermes VM's terminal sandboxes** | All four profiles run their terminal tool in docker containers on a tag-pinned image (2026-08-29; `safer_web_reader` joined on 2026-08-31 through the managed scope). The weekly `hermes-sandbox-refresh` cron job pulls the tag and replaces idle stale containers, but **nothing watches the job itself** — it pushes to no monitor, by design. Its failure mode is silence: the containers just keep running whatever image they were created from. Detection is the update runbook's precondition on the job's own run record ([Preconditions](hermes-vm-updates.md#preconditions)), so latency is the update cadence against a 14-day threshold, same shape as the apt-stamp row below. Accepted: a stale sandbox userland is neither lockout nor data loss — the containment boundary is the host kernel, which `unattended-upgrades` patches nightly — [hermes-vm.md](hermes-vm.md#the-docker-terminal-sandboxes) |
| **the hermes VM's OS updates** | `unattended-upgrades` runs on a schedule and nothing watches it directly. The cover is indirect and deliberate: the update runbook's preconditions stop the session when `/var/lib/apt/periodic/unattended-upgrades-stamp` is missing or over 14 days old ([Preconditions](hermes-vm-updates.md#preconditions)). That check fires only when somebody runs it, so at a roughly weekly cadence a dead apt timer surfaces within about a week of crossing the threshold — the detection latency is the cadence, and it is accepted against a 14-day threshold. The stamp is weaker still than it looks: `unattended-upgrade` writes it on a run that found nothing to do just as readily as on one that installed everything, so it proves the timer fired and not that anything was patched. Accepted: the 04:45 reboot window and the daily `hermes-app-alive` check bound the damage, the latter by proving the VM came back — [hermes-vm.md](hermes-vm.md#unattended-upgrades) |
| **hindsight extraction** | Retain hands the content to an external LLM for extraction. A provider outage or a revoked key fails the retain task — the server retries three times and then logs, and nothing else notices. Recall is unaffected, because the full image runs embeddings and reranking locally, so a dead LLM account degrades to read-only memory rather than no memory. The canary proves the retain *pipeline* accepts writes; it does not judge whether what was extracted is any good |
| **hindsight memory content** | Poisoning cannot be prevented — writing memories is the product. What limits it is that only Hermes holds the tenant key and only the operator holds the control-plane access key; what recovers from it is seven days of nightly dumps plus the control plane's per-memory delete |
| **the hindsight dump** | The gate proves the dump exists, is fresh, is above a size floor and contains at least one `CREATE TABLE`. It does not prove the dump *restores*. The periodic restore drill in [hindsight.md](hindsight.md) is the only thing that does |
| **the homelab gate** | It proves the SSD is mounted and the tree is the right *shape*: right number of PVC directories, right order of magnitude, the listed files present and non-trivial. It says nothing about *content*. Every homelab PVC is copied live, with no quiesce step: a sqlite database mid-write is captured torn, `sonarr.db` at 14 MiB of corruption passes the size floor exactly as 14 MiB of working database does, and a PVC that stopped being written to weeks ago looks identical to one written a minute ago. Grafana is the one exception, and only in its dump: `grafana-dump` is taken with SQLite's online backup API and read back before it is published, so that artifact is consistent and verified even though the live `grafana.db` beside it is not. The hindsight dump is verified the same way, at the shape level. Only the two influx dumps, that Grafana dump, the hindsight dump and the hermes zip are age-checked. A retained orphan directory from a recreated PVC can satisfy an expected-set entry the live PVC no longer can — the resolved paths are printed so it is visible, but nothing fails on it. The rest surfaces at restore time |
| **cloudflare-analytics** | It proves the hours it fetched were fetched. It cannot prove Cloudflare's own numbers are right, and it does not alert on *content* — a hostname that stops receiving traffic entirely, or a spike, produces a perfectly green heartbeat. That is Phase 3 (Grafana alert rules), deliberately deferred until a baseline exists |
| **update-watch (Renovate silence)** | Renovate is installed, has opened no error issue, and is proposing nothing. Since August 26, 2026 the *idle* form is a determinate verdict on this same monitor: a Dependency Dashboard that has not moved in `renovate_alive_max_days=` is `verdict=renovate-stale`, pushed `down`. What is left uncovered is a Renovate that keeps touching its dashboard while proposing nothing — a `managerFilePatterns` that stopped matching. A registry lookup failing silently was on that list until August 28, 2026 and no longer is: `verdict=renovate-lookup-failed` reads the dashboard's repository-problems block and pushes `down` on it. What that leaves is a lookup that fails without Renovate saying so, and a reworded problems block, which loses the verdict and nothing else. `make check-renovate-scope` closes the commonest remaining variant, a pinned image no in-scope file names; the rest is the narrowed quarterly drill above |
| **update-watch (held on the dashboard)** | The watcher counts OPEN pull requests, and an update held by any `dependencyDashboardApproval` rule in `renovate.json` — today all majors, `kroniak/ssh-client` digests and `thisisarpanghosh/garmin-fetch-data`, but read the config, not this cell — is a dashboard checkbox, not a pull request. It can wait for a human tick indefinitely while the monitor reads `ok`, and `renovate-stale` will not fire because Renovate is alive and updating the dashboard throughout. The cover is procedural: read the Dependency Dashboard at the start of every update session |
| **update-watch (merged but not applied)** | It watches the **repository**, not the cluster. Merging a Renovate pull request closes it, so the next run reports zero and the monitor goes UP while the cluster still runs the old image. Merge and apply are one runbook operation for that reason; the independent noticer is drift in `make diff-homelab` |
| **the residential egress chain** | Nothing detects a broken chain — tinyproxy in the homelab `proxy` namespace, the `cynexia-health` tunnel, the Cloudflare Access application, or the `homelab-proxy` client on the VPS — before a proxied changedetection watch errors. The spec accepts that: the watch error is the detector, and recovery is the rollout restart named in [vps.md](vps.md#residential-egress-through-the-homelab). The `proxy.cynexia.com` monitor asserts the Access challenge only, so it stays green through every fault below it |

The three VPS expansion gaps above stay accepted gaps rather than becoming checks.
The natural home for an etcd-member-count and a node-spread assertion is the `vps-keel-fresh` CronJob, but both need cluster-read credentials that job does not have, and the estate's standing rule is that a new noticer must not introduce a new credential — the same reason the Omni-side check was refused.
Note also that the external prober runs **on the VPS storage node**, so it cannot observe a VPS-wide control-plane problem at all.

Queued, not configured: a changedetection `overdue_watches` json-query monitor and a karakeep queue-depth alert; both need an API credential in the monitor.

**Nothing detects a scale that stops syncing to Withings.**
The `withings-ingest` job runs, fetches zero groups, reports `verdict=ok groups=0` and stays green, because an empty window is a legitimate success — Withings reports a failed call with a non-zero `status`, not with an empty result.
The `groups=` counter in the heartbeat history is the only signal, and reading it is a human act.
A freshness check on the bucket would be the detector, and it is deliberately not built until the weighing habit is regular enough for a threshold to mean something.
Adding `withings` to `ingest-freshness` is the wrong fix: that check pushes `up` only when every bucket it watches is fresh, so a day without a weigh-in would silence the Apple and Garmin signal too.

Snapshot integrity stays partly verified by choice.
On VPS the `sqlite_master` assertion closes the worst case — a fresh, valid, empty snapshot from a truncated source — by proving a schema exists, not that the data is there; homelab has no equivalent, having no quiesce sidecars to assert against, so its gate stops at shape.
Closing the rest means `sqlite3 <file> 'pragma integrity_check'` inside the gate, hence sqlite in the `restic/restic` image.
Until then a periodic manual restore drill is the only real proof, and the only cover for homelab's torn-copy exposure.

### Frozen while looking covered: the update-mode rule

**Floating tag means keel; pinned tag means Renovate; never both.**
`keel.sh/match-tag: "true"` on a pinned tag only refreshes the digest, so a semver pin carrying keel annotations is frozen while looking covered.
`traefik:v3.3` and `meilisearch:v1.41.0` were both in that state until August 26, 2026.
`make check-renovate-scope` now refuses the combination outright, on both clusters, one container at a time, and both per-cluster halves run in their cluster's `diff-*` and `apply-*` preflight.

The guard also settles the scope question that used to sit under it.
Renovate watches `homelab/**` and `vps/**` as of the same date, so a pinned, keel-free container has to be named by a file in its own cluster's tree that `kubernetes.managerFilePatterns` matches — and the guard fails the apply when it is not.
What the guard does *not* claim to cover is an image it never sees: one from a remote base, which it reports as advisory because nothing here can edit it, and one embedded inside another resource, such as local-path-provisioner's helper Pod inside a ConfigMap.

`jottacloud-backup` is the one written exemption on that guard's `FLOATING_EXEMPT` list, and not because it is keel-managed — it carries no keel annotations at all.
It is a CronJob, so every scheduled run starts a fresh pod that pulls `:latest`, which already delivers the auto-pull behaviour keel would provide.
Correct anywhere the estate's own text says otherwise.

### Named accepted residual: the ingest signal leaks a presence timeline

`apple_age_h=` and `garmin_age_h=` are written on every successful `health-ingest` heartbeat, up to four times a day, and over the retained window they constitute a sync-and-absence timeline for an identified individual: when the operator last wore and synced a watch, and by inference when they were away, asleep or not wearing a device.
They are written anyway — the ages *are* the finding this signal exists to deliver, and after the merge they are also the only thing that says which of the two paths went stale.

**The move to a push monitor changed the premise of this residual, and it is worth restating rather than renaming.**
The old justification was that the data subject is the operator, on their own account, at a **third-party processor already chosen for this data**.
That processor is gone from this path: uptime-kuma runs on the operator's own VPS, in a database only they hold, backed up by their own restic job into their own B2 bucket.
So the storage half of the concern is now the same as any other self-hosted log line, and the "which processor" question no longer arises.

Two things did not improve, and one got slightly narrower.

- **Resolution.**
  The old bodies carried `last_point=`, a full RFC3339 timestamp, on both the fresh and the stale paths.
  The heartbeat carries whole hours, on the fresh path only.
  That is a coarser timeline, not no timeline.
- **The recipient list is still not known, and it is still the open item.**
  A message travels to whatever notification transports the monitor has configured, and nobody has enumerated them — the same gap as before, moved from healthchecks.io's Integrations page to kuma's Notifications page.
  It is now *readable*, which it was not: the healthchecks.io key in the vault is read-only, `GET /api/v3/channels/` returns 401 with it, and a check fetched with it omits its `channels` field, whereas kuma's notification list is visible in a UI the operator can already log into.
  **To close this, open kuma → Settings → Notifications, record the channels here, and decide whether the two ages may travel to each.**
- **What narrowed:** the stale path now sends nothing at all, where the old `/log` ping still posted a body carrying both fields.
  So the ages reach a transport only on a run that found both buckets fresh, and the cheap mitigation the old text proposed — withholding them from the stale body — has been applied by construction rather than by choice.

## Explicitly rejected

These are settled.
Do not relitigate them without new evidence.

- **Any probe on `garmin-grafana`.**
  The `health-ingest` freshness monitor covers it.
- **Token-mtime freshness probe for garmin.**
  The token file is written only on the interactive login path, so its mtime changes about once a year: the probe detects nothing and false-positives on any sane threshold.
- **Staleness-based liveness for garmin.**
  Freshness depends on the operator syncing a watch, so a weekend away restarts the pod repeatedly, and each restart with a stale token sends an MFA SMS.
  Staleness notifies a human; it never restarts.
- **Generalising the jottacloud liveness probe.**
  It could not fail: `backup.sh` is PID 1 for the whole run and never `exec`s over itself, so `ps | grep backup.sh` always matched, and no upstream script creates `/tmp/backup-completed`.
  It measured presence, not progress, so a stalled rclone looked alive.
  `activeDeadlineSeconds` is the pattern to generalise instead.
- **Naive `pg_isready` liveness on postgres**, the exit-0-only Bitnami shape.
  Same narrow detection as the `test $? -lt 2` form, plus a recovery loop that never converges on a single-replica local-path PVC.
- **Any probe on the backup sidecars.**
  Readiness drops the Pod from its EndpointSlice; liveness reaches the same place through `CrashLoopBackOff`.
- **`tcpSocket` on sockpuppetbrowser :3000 as a hang fix.**
  The kernel completes handshakes from the accept backlog while the event loop is blocked, so it detects process death only.
- **A NetworkPolicy in front of the MCP server.**
  Inert on flannel — see [homelab-health.md](homelab-health.md#mcp-behind-cloudflare-access).
- **Giving the ingest signal a failure state.**
  Refused as `/fail` at healthchecks.io and refused again as a `down` push in kuma, for the same reason both times: it trades a 36-hour tolerance for a 6-hour one on a signal that depends on a human syncing a watch.

## Rolling out a probe change

A probe rollout that restarts pods is a failed rollout.

1. Confirm the probe path's status from inside the cluster with a throwaway `alpine/k8s:1.36.0` pod rather than from vendor documentation.
2. Run `make diff-homelab` or `make diff-vps`, then apply.
3. Run `kubectl -n <ns> rollout status deploy/<name>` and confirm it completes without a restart loop.
   Ten minutes later, `kubectl -n <ns> get pods` must show 0 restarts.
4. For a CronJob change, run `kubectl create job --from=cronjob/<name> <name>-manual`, then read the pod log and check the signal it drives.
   For a push monitor a silent runner is already proof the heartbeat landed, but confirm the monitor went UP in the kuma UI as well.
5. After a few weeks, resize `activeDeadlineSeconds` from the recorded durations.
