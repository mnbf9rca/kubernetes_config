# Failure detection: probes, deadlines and monitors

How failures in both clusters get noticed. The manifests carry per-probe rationale in
comments; this file carries the policy, the inventory, and the failures none of it catches.

Read [What this does not catch](#what-this-does-not-catch) before you trust a green signal.

## Start here: something is wrong

Find your signal in the left column, then read the section on the right.

| Signal | What it tells you | Where to look |
|---|---|---|
| `homelab-restic` or `vps-restic` red | Read `failed_step=` in the ping body first. It names the phase that set the exit code and separates a restic failure from a gate failure | [The restic ping wrapper](#the-restic-ping-wrapper) |
| A restic body says `prune=failed` or `prune=skipped` | `failed` means retention started and died, so snapshots may be partly expired — look tonight. `skipped` means the gate deferred retention on purpose | [Reading a restic failure body](#reading-a-restic-failure-body) |
| `mount_ok=no` in a homelab restic body | `/data` is the bare mountpoint, so the SSD did not mount and the backup captured nothing | [The homelab backup verification gate](#the-homelab-backup-verification-gate) |
| `health-apple-ingest` or `health-garmin-ingest` red | No fresh data reached InfluxDB for 24h. Check whether the operator synced a watch before you suspect the pipeline | [healthchecks.io checks](#healthchecksio-checks) |
| `homelab-cloudflare-analytics` red | Often not a malfunction. Read the job log for the hours that were lost | [healthchecks.io checks](#healthchecksio-checks) |
| `vps-uptime-kuma-alive` red | uptime-kuma, its node or the VPS scheduler is dead. Layer 3 is down, so treat every green uptime-kuma monitor as unknown | [The self-monitor](#the-self-monitor-layer-4) |
| A snapshot is missing but the sidecar shows `RESTARTS: 0` | Expected. The sidecars report failure by logging, never by exiting. Read the sidecar's stderr | [Why the sidecars have no probes](#why-the-sidecars-have-no-probes) |
| An uptime-kuma monitor is UP but the service is down | Suspect a redirect to the Cloudflare Access login page | [The Cloudflare Access trap](#the-cloudflare-access-trap) |
| A pod restart-loops after a probe change | Roll the probe back before you diagnose. Every workload here is single-replica | [Rolling out a probe change](#rolling-out-a-probe-change) |
| Everything is green and the data is still wrong | Expected. Several of these probes are shallow by design | [What this does not catch](#what-this-does-not-catch) |

## The decision rule

Monitor the artifact, not the process. A live process proves nothing.

| Workload shape | Instrument | Why |
|---|---|---|
| Serves requests | Probe the real request path | kubelet repairs it by restarting; the failure is local and immediate |
| Produces an artifact on a schedule | Dead-man's-switch on freshness | Restarting does not deliver data that never arrived. Absence of an event is only visible from outside |
| Must not hang | `activeDeadlineSeconds` (Jobs), progress probe (Deployments) | A bounded runtime is a contract you enforce declaratively |

If restarting the thing cannot plausibly fix the failure, a probe is the wrong instrument.
Probe failure means "kill and retry". Dead-man's-switch failure means "wake a human". The
wrong choice buys you either false confidence or a self-inflicted outage.

## The four layers

| Layer | Instrument | Blind to |
|---|---|---|
| 1 | In-pod probes | Tunnels, schedulers, background work |
| 2 | Job deadlines and dead-man's-switches | Request-path wedges |
| 3 | External monitors (uptime-kuma) | Its own death |
| 4 | healthchecks.io switch on the monitor itself | Anything it is not pinged by |

Each layer covers the blind spot of the layer below it. Do not drop one because another
"already checks that".

## Probe policy

- Put a readiness probe on every long-running container that serves traffic. The worst
  case is that the pod leaves Service routing.
- Add liveness only when that probe detects the failure **and** a restart repairs it.
  Every service in both clusters runs a single replica, so kubelet has nowhere to send
  traffic while the pod restarts. An eager liveness probe manufactures the outage it was
  added to catch.
- Add a startup probe to anything with migrations or a slow boot, so liveness cannot fire
  during startup.
- Set `timeoutSeconds` on every probe. The 1s default false-positives on a loaded node, which
  turns ordinary disk contention into a restart. Every probe in both clusters sets it today.
- Keep liveness thresholds strictly laxer than readiness. Readiness sheds traffic;
  liveness destroys state.
- Probe the data plane, not a vendor health endpoint. A control-plane endpoint reports on
  a different process from the one serving your users, and the vendor-documented probe
  stayed green throughout the 2026-08-18 Pomerium wedge. Full account:
  [homelab-health.md](homelab-health.md#the-probe-target-is-deliberately-not-the-documented-one).
- Put no probe of any kind on a backup or quiesce sidecar. See
  [Why the sidecars have no probes](#why-the-sidecars-have-no-probes).

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
| the 5 quiesce sidecars | none | Deliberate — see below |

### Homelab cluster

| Container | Target | Note |
|---|---|---|
| pomerium | liveness `/ping` (:80), readiness `/readyz` (:28080), startup `/startupz` (:28080) | Liveness targets the data plane, against the vendor documentation. Reasoning: [homelab-health.md](homelab-health.md#the-probe-target-is-deliberately-not-the-documented-one) |
| pomerium `mcp` sidecar | liveness and readiness `tcpSocket` | The MCP server exposes no health endpoint. TCP detects process death, not a wedged handler |
| cloudflared (both clusters) | liveness and readiness `/ready` (:2000) | Neither Deployment has a Service, so readiness gates the rolling update and shows connector state. It routes nothing |
| influxdb | `/health` | — |
| grafana | `/api/health` | — |
| apple-health-ingester | `tcpSocket` | No HTTP health endpoint upstream |
| sonarr, radarr, sabnzbd, emby, hydra2 | `/` on the app port; startup, liveness and readiness | Readiness stops Traefik routing to them while they boot |
| traefik | exec `traefik healthcheck --ping` | `hostNetwork` makes the pod IP the storage NIC, where the ping endpoint is not bound. The CLI queries loopback |
| keel (both clusters) | `/healthz` | Liveness 15s × 6 is laxer than readiness 10s × 3 |
| jottacloud-backup | none | Its old liveness probe could not fail. `activeDeadlineSeconds: 21600` bounds the run |
| garmin-grafana | none | It serves nothing. The `health-garmin-ingest` switch is the correct instrument |
| cloudflare-analytics | none | Scheduled work. `homelab-cloudflare-analytics` plus `activeDeadlineSeconds: 1200` is the instrument |

## Why the sidecars have no probes

**Put no probe — readiness, liveness or startup — on any of the five VPS quiesce sidecars:
`sqlite-snapshot` in n8n, freshrss, karakeep and uptime-kuma, and `pg-dump-sidecar` in
umami-postgres.** This has nearly been re-broken twice. The chain that turns a probe there
into an outage is short:

> A container that is not Running is not Ready. A Pod with a non-Ready container leaves its
> EndpointSlice. cloudflared then returns 502 for the application.

Readiness reaches that state directly. Liveness reaches it through `CrashLoopBackOff`.
Either way, a fault in last night's *backup* takes a working *application* offline — the
decision rule biting the one probe the original spec endorsed.

The one argument for such a probe was self-healing a failed `apk add` that left the container
without `sqlite3`. `ensure_sqlite3()` in `vps/workloads/scripts/sqlite-snapshot-lib.sh`
retries that install on a 5 minute backoff, so the probe only duplicated the loop. Against a
permanent fault — a corrupt database, a full disk, a path moved by an app upgrade — it
restarted a container that a restart cannot repair.

Detection lives at the artifact instead. The VPS restic gate asserts a fresh snapshot per
app, and per FreshRSS user, then turns healthchecks.io red. Latency goes from roughly 45
minutes to at worst a day, which is the right scale for a backup fault and never costs you
the application.

### What the sidecar loops do instead

`set -e` is deliberately absent from all five. If a sidecar exits, kubelet restarts it and a
persistent fault reaches the same `CrashLoopBackOff` chain. Each loop instead runs under
`set -u`, logs failures to stderr and keeps going; sleeps 300s after a failure and 43200s
after a success; publishes atomically as `.tmp` then `mv`, so a failed run leaves the
previous snapshot intact; and asserts *content* before publishing, not only an exit status.

That last one is the part to keep. `snapshot()` runs
`sqlite3 <tmp> 'select count(*) from sqlite_master'` and refuses a snapshot holding zero
schema objects, because a truncated source makes `.backup` emit a structurally valid but
empty database with a current mtime, which passes any mtime-only check.

`pg-dump-snapshot.sh` uses the same shape with `grep -c '^CREATE TABLE '`, and fewer than one
refuses to publish. `pg_dumpall`'s exit code alone is not enough: it exits 0 against a freshly
initialised postgres with no umami schema, because the image's entrypoint creates the empty
`umami` database either way. That yields a valid roles-only dump of roughly 100 lines with a
current mtime — a green backup that restores to an empty analytics database. Refusing it
leaves the previous dump ageing, which turns the check red.

Because these loops report failure by logging rather than by exiting, their restart counts
stay at zero. **To debug a missing snapshot, read the sidecar's stderr**, and read nothing
into `RESTARTS: 0`.

All five loops are real files under `vps/workloads/scripts/`, delivered by the
`sqlite-snapshot-scripts` `configMapGenerator` in `vps/workloads/kustomization.yaml` and
mounted at `/scripts`. Four of the five source `sqlite-snapshot-lib.sh`; n8n, karakeep and
uptime-kuma share `sqlite-snapshot.sh` outright and differ only in `$SNAPSHOT_DB`. Editing
one script rolls every Deployment that mounts it, and all five use `strategy: Recreate`, so
a script edit costs a brief hard-down window for each of them rather than a rolling update.
Generated scripts also pass through envsubst, so run `make check-script-substitution` and
read the note in `AGENTS.md` before you write a `$VAR` into one.

## Scheduled work

Every CronJob sets:

| Field | Value | Why |
|---|---|---|
| `timeZone: "UTC"` | all six jobs | Otherwise the schedule follows kube-controller-manager's local zone |
| `startingDeadlineSeconds` | 3600, except 1800 for cloudflare-analytics, 300 for jottacloud, and unset for `ingest-freshness` | A missed window retries for that long, then drops |
| `activeDeadlineSeconds` | restic 14400, influx-backup 3600, cloudflare-analytics 1200, ingest-freshness 300, jottacloud 21600 | With `concurrencyPolicy: Forbid`, one hung run silently blocks every later run |
| `ttlSecondsAfterFinished` | 259200 on both restic jobs and cloudflare-analytics; 172800 on influx-backup; 86400 on the rest | A Friday failure on the restic jobs survives until Monday |
| `terminationGracePeriodSeconds` | not set on any job | busybox `ash` runs as PID 1 and never forwards SIGTERM to restic, so a grace period only slows teardown. `restic unlock` at the head of the next run recovers the lock |

### The restic ping wrapper

The two clusters run the phases in a deliberately different order:

```
VPS:      ping_hc start → snapshots → unlock, backup, forget --prune, check → gate → ping_hc "$rc"
homelab:  ping_hc start → snapshots → unlock, backup → gate → forget --prune (only if the gate
                                                              passed) → check → ping_hc "$rc"
```

Both run the gate *after* the backup. As a precondition it would skip a whole night of
everything else over one stale artifact, and the backup is worth more than the gate.

Homelab additionally makes `forget --prune` conditional on the gate passing, because pruning
is the step that destroys data. Failing the job after pruning still alerts, but the seven
good daily snapshots are already being expired on schedule while the alert goes unread. A
false positive there costs a repository that grows in B2 until somebody looks; the false
negative costs every recovery point. `restic check` runs either way.

Three rules hold on both clusters:

- The `/start` ping detects a run that starts and never finishes, and records durations. The
  exit-code ping (`hc-ping.com/$UUID/$rc`) separates success from failure. Pings never fail
  the job and use `wget -T 10`, so healthchecks.io cannot hang the backup.
- Steps chain with `&&`, not `set -e` inside a group. errexit is ignored inside an AND-OR
  list, so `{ set -e; … } || rc=$?` runs past a failure and reports the wrong status.
- `restic unlock` runs first and, without `--remove-all`, clears only stale locks.

Observed runtimes:

| Job | Schedule | Runtime |
|---|---|---|
| `homelab-restic` | 03:00Z | 26s |
| `vps-restic` | 04:00Z | 57s |

These fell from 88s and 117s once retention started pruning, so `restic check` walks 14
snapshots instead of 137. The 4h `activeDeadlineSeconds` is an opening guess — resize it from
recorded durations. Expect `homelab-restic` to rise: its gate adds a `du` walk of the same
44,288 files restic just read, which is not yet measured in place.

### Why both restic jobs need a gate at all

`restic` succeeds on an empty tree. It writes a valid snapshot, `restic check` passes, the
job exits 0 and healthchecks.io goes green. Nothing in the backup path has an opinion about
whether the thing it backed up was the data.

Both jobs mount their source as `hostPath` with `type: Directory`, which asserts only that
the directory *exists*. If the volume fails to mount while its mountpoint survives on the
root filesystem, the backup captures nothing and reports success. `forget --prune
--keep-daily 7` then expires the seven genuine daily recovery points over the following week,
leaving a repository of snapshots of nothing. This is not theoretical: snapshot `551bd209` in
the homelab repository is 12 B and is retained as a "monthly".

The gate is the only thing in either job that asks whether the backup was of anything.

### The VPS backup verification gate

`vps/backup/scripts/restic-backup.sh` is the source of truth for the thresholds and the
table below. Change them in the same commit as this file. The gate runs two checks after the
backup completes.

**1. Expected-set assertion — authoritative, and it sets the exit code.** Each entry must be
present and under 15h old, a 3h margin over the sidecars' 12h period. Output names the app
and distinguishes `MISSING`, `STALE` and `UNREADABLE`.

| Service | Expected snapshot |
|---|---|
| n8n | `/data/*_vps_n8n-data/database.sqlite.restic` |
| karakeep | `/data/*_vps_karakeep-data/db.db.restic` |
| uptime-kuma | `/data/*_vps_uptime-kuma-data/kuma.db.restic` |
| umami | `/data/*_vps_umami-pg-data/dump.sql.restic` |
| freshrss | iterates `/data/*_vps_freshrss-data/users/*/db.sqlite` and asserts a sibling `.restic` per user. Zero user DBs passes with a note |

Adding a sqlite-backed service means adding its snapshot to `EXPECTED_SNAPSHOTS`. Miss it and
that service's backups go unverified, silently. An explicit list beats a wildcard, which
cannot tell "no databases exist" from "the volume is unmounted" from "three of four present"
— all three produce no stale files. An empty `/data` pings healthchecks.io red, naming each
missing snapshot. The entries are globs because local-path-provisioner names each PVC
directory `<pvName>_<namespace>_<pvcName>` with a random UUID; an unmatched glob survives
literally and fails the `-f` test, which is the `MISSING` verdict.

**2. Broad sweep — advisory.** Any `*.restic` under `/data` past the threshold prints a
warning and does not fail the job, because one orphaned PV directory would otherwise pin the
gate red forever. A `find` that *errors* does fail the job: an unreadable `/data` is a real
fault, and "I could not look" must never be reported as "everything is fine". The gate
promotes to failure only when restic itself succeeded, so a real restic failure keeps its
own, more specific exit code.

### The homelab backup verification gate

Homelab has no quiesce sidecars, so there is no `*.restic` artifact to check. Every
local-path PVC is backed up as live application state. The assertions are therefore about
the **tree**, in four authoritative checks plus one forensic pass.

`homelab/backup/restic-cronjob.yaml` is the source of truth for every threshold and both
tables below. Change them in the same commit as this file.

**1. Mount identity — authoritative, and the only first-order check.** Talos puts the kubelet
pod directory on the EPHEMERAL partition (`/dev/sda6`), the same filesystem
`/var/mnt/ssd/local-path-provisioner` falls back to when the SSD user volume fails to mount.
`/etc/hosts` is bind-mounted from that pod directory into every non-hostNetwork container, so
its `st_dev` *is* the EPHEMERAL device, readable from inside the container with no host
access. The gate compares it with `st_dev` of `/data`:

| | `/data` `st_dev` | `/etc/hosts` `st_dev` | Verdict |
|---|---|---|---|
| SSD mounted | SSD `/dev/sdb1` (2065) | EPHEMERAL `/dev/sda6` (2054) | differ → pass |
| SSD not mounted | EPHEMERAL | EPHEMERAL | match → **fail** |

Measured in-cluster 2026-08-20. A `stat` that fails on either path is a failure, not a pass:
without the reference, the mount cannot be distinguished from its fallback.

**2. Tree scale — authoritative.** Floors, not targets: at least `MIN_PVC_DIRS=8` PVC
directories and `MIN_DATA_KIB=1048576` (1 GiB) in total. Measured 2026-08-20: 10 directories,
44,288 files, 4.418 GiB. Roughly 4x headroom, so log rotation or emby cache eviction cannot
trip it, while an empty or single-PVC tree cannot clear it.

**3. Expected set — authoritative.** Each entry must resolve, and must be at least its floor
in bytes. Floors sit an order of magnitude under the observed sizes; they exist to reject a
zero-length or truncated file, not to track growth.

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

Same maintenance contract as VPS: add any new local-path PVC holding something you would
miss, or that application's backup goes unverified. Same glob rationale too, plus one
residual. The StorageClass is `reclaimPolicy: Retain`, so a recreated PVC leaves its
predecessor behind forever. Each glob takes its most recently modified match, so a live
artifact normally beats its frozen predecessor — but if the live artifact is absent entirely,
the orphan is the only match and the check passes on it. Telling bound from orphaned needs
the Kubernetes API from inside the job, which is not worth a ServiceAccount and RBAC on a
backup CronJob: the orphan is under `/data` and is backed up too, so this is the gate
reporting on the wrong file, not a lost recovery point. The gate prints the path each glob
resolved to, so the substitution shows up in the log instead of hiding behind "8/8".

**4. Dump freshness — authoritative.** The influx dumps are the only homelab artifacts
produced on a schedule, so they are the only ones with a meaningful age. Both must be under
30h old (`STALE_MINUTES=1800`):

| Artifact | Path |
|---|---|
| influx-native-dump | `/data/pvc-*_health_health-dumps/native/*` |
| influx-lp-export | `/data/pvc-*_health_health-dumps/lp/*.lp.gz` |

`influx-backup` writes these at 02:30Z, 30 minutes before this job. 30h therefore tolerates
one missed or delayed run — `health-influx-backup` is the check for *that* — and fails on two
consecutive misses or on the dumps vanishing. Nothing else in the tree is freshness-checked:
applying a deadline to live application state manufactures reds on any file an app happens
not to touch for a day.

**5. Per-PVC size table — forensic.** Every run prints one line per PVC directory. The night
a PVC empties, the diff is in the log. Two verdicts sit on top of it, with deliberately
different force:

- a PVC directory with **no entries at all** warns and does not fail. A freshly provisioned
  PVC is legitimately empty until its app writes, and that must not pin the one channel
  meaning "restore is broken" permanently red;
- a PVC directory that **cannot be opened** (`-r`/`-x`) *does* fail.

`du`'s exit status is the one deliberate exception to that second rule. busybox `du` returns
non-zero when a file vanishes mid-walk, which happens routinely here with sqlite WAL files
and rotating logs, so its status only warns. Its *output* stays authoritative — an
unparseable total fails — and unlike `find`, a `du` that could not walk still emits a number,
so the scale floors catch it. Do not capture its stderr: folding it in with `2>&1` puts the
diagnostic ahead of the total, empties the numeric prefix, and promotes the advisory warning
to the fatal "unparseable total" branch. It goes to the pod log instead.

The gate announces its passes (`8/8 artifacts present`, `2/2 newer than 30h`). A gate that
prints nothing when it is happy is indistinguishable from a gate that never ran.

### "Newest of a glob" is a dangerous shape

Both the removed sidecar probe and the first gate took the newest snapshot matching the
FreshRSS glob. FreshRSS keeps one database per user, so when one user's database stopped
being snapshotted, the other users kept the newest mtime fresh and both checks stayed green
forever.

Iterate the source objects and assert an artifact for each. A check that reduces a set to its
maximum detects only "all of them stopped"; the failure you care about is "one of them
stopped". The four single-DB services still take the newest match, because their glob is one
PVC directory expected to match one path.

## healthchecks.io checks

| Check | 1Password reference | Period / grace | Pinged by |
|---|---|---|---|
| `homelab-restic` | `op://Homelab/b2-restic/healthcheck-uuid` | 1d / 2h | restic CronJob, `/start` and exit code |
| `vps-restic` | `op://VPS/b2-restic/healthcheck-uuid` | 1d / 2h | restic CronJob, `/start` and exit code |
| `vps-uptime-kuma-alive` | `op://VPS/uptime-kuma/healthcheck-uuid` | 5m / 15m | An uptime-kuma monitor — see [the self-monitor](#the-self-monitor-layer-4) |
| `health-apple-ingest` | `op://Homelab/health-healthchecks/apple-uuid` | 1d / 12h | `ingest-freshness`, success only, and only when InfluxDB data is under 24h old |
| `health-garmin-ingest` | `op://Homelab/health-healthchecks/garmin-uuid` | 1d / 12h | as above |
| `health-influx-backup` | `op://Homelab/health-healthchecks/backup-uuid` | 1d / 6h | `influx-backup`, `/start` and exit code, from an EXIT trap |
| `homelab-cloudflare-analytics` | `op://Homelab/health-healthchecks/cloudflare-uuid` | 1h / 2h | `cloudflare-analytics` CronJob, `/start` and exit code |
| `jottacloud-backup` | `op://Homelab/jottacloud-backup/HEALTHCHECK_UUID` | 6-hourly schedule | The third-party image's own `backup.sh`, success only |

Four of the five jobs this repo pings send `/start` and an exit code: both restic jobs,
`cloudflare-analytics` and `influx-backup`. Follow that pattern for new jobs. The remaining
two exceptions each have a reason, below.

`influx-backup` runs under `set -eu -o pipefail` and pings from an EXIT trap. Keep both.
Under `set -e` alone, `xargs` swallows the prune step's `ls` failure and the ping fires
anyway. With the ping on the last line instead of in a trap, a failing prune, a missing
ConfigMap key or a dead influxdb pod produces *exactly nothing* until the 6h grace expires
roughly 30 hours later. As written, a non-zero exit goes red within a minute carrying
`failed_step=`, and a pod that is killed or never scheduled goes red at `last_start + 6h`.
The accepted cost is that a transient failure — an InfluxDB pod mid-restart when
`kubectl exec` lands, an API-server blip — pages immediately instead of self-healing into
silence. Thirty hours of silence on a hard failure is the larger defect.

**The two ingest checks stay success-only and must not be converted.** A `/fail` on a stale
bucket would flip the check DOWN on the first 6-hourly run that found nothing, trading a
36-hour tolerance for a 6-hour one — on a signal that depends on the operator syncing a
watch. They get an inert `/log` ping instead; see [Ping bodies](#ping-bodies).
`ingest-freshness` also always exits 0: the signal is the absent ping, not a failed Job. Do
not change it to a non-zero exit. `jottacloud-backup` is success-only for a different reason
— its ping comes from `backup.sh` inside a third-party image this repo does not control.

`homelab-cloudflare-analytics` goes red for one failure mode that is not a malfunction: **an
unrecoverable gap**. Cloudflare keeps 8 days, so if the job has been down longer, the missing
hours no longer exist anywhere. It logs the range, writes an `ingest_gap` marker into InfluxDB
so the hole reads as a hole rather than as a quiet week, ingests what survives, and exits
non-zero. The alarm fires **once**, because the next run's watermark is current again. Read a
red check here as "find out which hours were lost", not "the job is broken". Detail:
[homelab-health.md](homelab-health.md#gaps-are-permanent-so-they-are-loud).

Adding a check takes [four edits](apply-workflow.md#adding-a-secret-is-four-edits-not-three).
Nothing catches a missing `ENVSUBST_VAR_NAMES` entry, which ships the literal `${VAR}` as the
ping UUID and silently disables the check.

The read-only API key is `op://Homelab/healthchecks.io/read-only-api-key`. If that path
returns 404, the item is still spelled `healtchecks.io` — a rename is in progress.

### Ping bodies

Every ping this repo sends carries a short `key=value` body: one pair per line, printable
ASCII, first line always `summary=`. It makes the healthchecks.io Events log answer "what did
it see?" without a pod log that may already have aged out. The one bit is still the alerting
signal; the body is what makes a *green* check legible. It exists because
`health-apple-ingest` was green while Apple Health data had been stale for five days: nothing
had malfunctioned, but green could not distinguish "fresh" from "stale, window not expired".

**A ping body is a disclosure channel.** healthchecks.io is a third-party SaaS in the EU. The
body leaves the estate, is stored in their object storage, repeats on every run until somebody
fixes the script, and cannot be shortened short of deleting the check. **It also travels with
the alert**: upstream's email, webhook, Slack, Telegram, Matrix, GitHub and MS Teams transports
all read the last ping's body into the notification, and the alert email renders it verbatim.
A failure body therefore reaches the mail provider and every configured integration. Treat an
`emit` call exactly like a line in a committed file.

#### What must never enter a body

Two things are forbidden, because they grant somebody something:

- **Secret contents.** Anything read from a Kubernetes Secret or a 1Password item: tokens,
  passwords, private keys, API keys. Disclosure means rotation.
- **healthchecks.io ping UUIDs.** A ping UUID is the check's write credential. Anyone holding
  one can ping your check and mask a genuine failure.

#### What is left out because nothing reads it

Restic repository URIs, B2 and InfluxDB bucket names, PVC UUIDs, namespaces, and pod and node
names are **ordinary identifiers**. They grant nothing and enable nothing, they are fine in a
pod log, and no rule bans them from a body. They are absent only because they answer no
question an operator asks at 3am. Do not read a classification into that absence, and do not
open an honesty-box row if one appears somewhere; the three tiers are defined in `AGENTS.md`.
`make check-ping-bodies` does deny every name in `ENVSUBST_VAR_NAMES`, bucket names included,
but that list is a mechanical hazard list, not a secrecy classification.

#### Personal health data

A health *reading* is never emitted, on any check. `last_point` and `last_point_age` on the
two ingest checks are freshness *timestamps*, and they are transmitted by an explicit
operator decision recorded under [Named accepted residual](#named-accepted-residual) below.

#### Never build a body from a command's output

healthchecks.io's own documentation teaches the opposite. For this estate that pattern leaks:

- the two scripts `influx-backup.sh` execs into the influxdb pod pass the InfluxDB
  **operator** token on argv (`influx-native-backup.sh:21`, `influx-export-lp.sh:36`), so
  anything echoing that command's output or argv would ship a credential nightly;
- a failing `wget` or `curl` quotes the URL it was given, and for a ping the URL *is* the
  check's write credential.

The rule stays blanket rather than tiered because **a script cannot sort the tiers apart at
runtime.** It cannot tell a bucket name from an operator token inside a string it did not
construct, so command output is unclassifiable and stays out.

`emit` is therefore only ever called with a literal key and a value the script computed
itself: a count, an age, a byte size, a path built from a literal glob, or a verdict from a
fixed enum. `make check-ping-bodies` enforces this, including the one-intermediate-variable
evasion (`M=$(cmd); emit "error=$M"`), and it recognises a denied name in every
parameter-expansion form rather than only `$NAME` and `${NAME}` — `${HC_UUID:-}`,
`${HC_UUID#p}`, `${HC_UUID/a/b}` and `${#HC_UUID}` are all refused. A taint is cleared only by
an explicit `# check-ping-bodies: untaint <NAME> <reason>` line, which requires a written
reason.

#### Reading a restic failure body

`failed_step=` names the phase that set the exit code: `unlock|backup|forget|check` for
restic's own failures, `gate` for the verification gate. On homelab it is captured at the
point the chain aborts, *before* the gate and `restic check` run, because both of those run
unconditionally afterwards and would otherwise overwrite it. A restic failure is therefore
never reported as a gate failure, even when the gate also fails.

`prune=` has three states and they are not interchangeable:

| Value | Meaning | What to do |
|---|---|---|
| `ran` | `forget --prune` completed | Nothing |
| `skipped` | The gate deferred retention on purpose | Nothing urgent. The repository grows in B2 until the gate goes green |
| `failed` | `forget --prune` started and died | Look tonight. Snapshots may be partly expired |

Reporting `failed` as `skipped` is the reading that makes an operator not look, so the
script keeps them apart. `restic_check=` reads the same way: `ok`, `failed`, or
`not-reached` only when the run never got that far.

#### Named accepted residual

`last_point` and `last_point_age` on the two ingest checks are emitted every 6h. Over the
retained window they constitute a sync-and-absence timeline for an identified individual:
when the operator last wore and synced a watch, and by inference when they were away, asleep
or not wearing a device. They are emitted anyway: `last_point_age` *is* the finding these
bodies exist to deliver, and coarsening it to a bucket would throw it away. The data subject
is the operator, on the operator's own account, at a processor already chosen for this data.

**Open item — the recipient list is not known.** That justification names *one* processor, and
the transports listed above mean a failure body reaches more than one. When a check flips
DOWN, upstream reads the last ping's body regardless of its kind (`Transport.last_ping()`
filters on time, not on kind), so an ingest check's alert carries the most recent 6-hourly
`/log` body — the STALE one, with `last_point` and `last_point_age` in it.

Which channels that reaches is not established, because the key at
`op://Homelab/healthchecks.io/read-only-api-key` is read-only: `GET /api/v3/channels/` (and
`/api/v1/`) returns 401 with it, and a check fetched with a read-only key omits its `channels`
field. There is no full-access key in the vault. To close this, read the account's
Integrations page and record the configured channels here. If anything beyond email is
configured, decide explicitly whether those two fields may travel to it. The cheap mitigation
is to withhold them from the `/log` (stale) body only, where a flip can attach them to a
notification, and keep them on the success ping, where it cannot.

#### Bodies die with their ping-log entry

`Check.prune()` removes the objects and then the ping rows, so retention is plan-dependent:
100 entries per check on Hobbyist, 1000 on Business. For a daily check that is roughly three
months or roughly two and a half years; for a 6-hourly check, roughly 25 days or 250. For the
two ingest checks, that number is how long a third party holds the timeline above.

`ingest-freshness` uses `/log` for its stale and query-failure paths. A `log` ping sets no
`last_ping`, no `last_start` and no `status`, and `alert_after` is recomputed from unchanged
inputs, so it cannot postpone, suppress or trigger an alert. It has one cosmetic side effect:
`has_confirmation_link` is set from the body on every action, `log` included, which drives a
UI nag. No body here contains the substring `confirm`, and none should.

### Checks in the account that this repo does not ping

The Management API returns 14 checks; the table above lists the 8 this repo owns. The other
six are pinged from Proxmox hosts, Home Assistant and host cron, and are deliberately outside
this repo: `adsb.cynexia.net`, `pve3.cynexia.net`, `fs.cynexia.net`, `tailscale unattended
upgrades`, `Home Assistant`, `upsd.cynexia.net`. They are recorded so that "not in the table"
can be told from "does not exist".

**`upsd.cynexia.net` has `n_pings=0` and `status=new`.** It has never been pinged. It is a
check that monitors nothing, so either wire it up or delete it.

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

Skip keyword monitors. The keyword is evaluated only after the status check passes, so it adds
nothing, and `saveErrorResponse` already captures Cloudflare's error body into the alert,
which makes a `1033` diagnosable.

### The Cloudflare Access trap

An Access-protected hostname answers an unauthenticated request with a 302 to the Cloudflare
login page. At the default `maxredirects: 10`, the monitor follows it, gets 200 from
Cloudflare's login app, and reports UP while the tunnel, pod and node are all dead.

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

Read the values with `op read` as you paste them; never write them into this repo. A wrong or
missing header produces a 302, which fails the monitor — the correct, loud outcome. An Access
bypass path is no substitute: the glob `/foo/*` does not match bare `/foo`, so a bypassed
health path needs both destinations.

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

A fast 401 is a true end-to-end signal. It proves the tunnel, cloudflared and the origin pod
all serve, which is exactly what was false during the Pomerium wedge. A timeout, a 5xx or a
Cloudflare `1033` falls outside the set and marks the monitor DOWN. Both `mcp.cynexia.com` and
`hae.cynexia.com` accept 401 and are verified working.

Before you add a status code to any set, observe it once:

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

**Do not use a Push monitor.** A Push monitor waits to *receive* a ping, which is the opposite
of what this needs. This monitor must *send* one.

The check `vps-uptime-kuma-alive` runs a 5m period with a 15m grace. If the pod, node or
scheduler dies, the pings stop and healthchecks.io alerts from outside both clusters. The
monitor's own UP/DOWN state is irrelevant — the signal lives at healthchecks.io.

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
| **the VPS gate** | It proves each snapshot exists and is recent, and — through the sidecar's own refusal to publish a schema-less snapshot — that it holds at least one schema object. It does not prove the contents are complete or uncorrupted. A snapshot missing rows, or with a corrupt page below the `sqlite_master` read, passes everything here and surfaces at restore time |
| **the homelab gate** | It proves the SSD is mounted and the tree is the right *shape*: right number of PVC directories, right order of magnitude, the listed files present and non-trivial. It says nothing about *content*. Every homelab PVC is copied live, with no quiesce step: a sqlite database mid-write is captured torn, `sonarr.db` at 14 MiB of corruption passes the size floor exactly as 14 MiB of working database does, and a PVC that stopped being written to weeks ago looks identical to one written a minute ago. Only the two influx dumps are age-checked. A retained orphan directory from a recreated PVC can satisfy an expected-set entry the live PVC no longer can — the resolved paths are printed so it is visible, but nothing fails on it. The rest surfaces at restore time |
| **cloudflare-analytics** | It proves the hours it fetched were fetched. It cannot prove Cloudflare's own numbers are right, and it does not alert on *content* — a hostname that stops receiving traffic entirely, or a spike, produces a perfectly green check. That is Phase 3 (Grafana alert rules), deliberately deferred until a baseline exists |

Queued, not configured: a changedetection `overdue_watches` json-query monitor and a karakeep
queue-depth alert. Both need an API credential in the monitor.

Snapshot integrity stays partly verified by choice. On VPS the `sqlite_master` assertion
closes the worst case — a fresh, valid, empty snapshot from a truncated source — by proving a
schema exists, not that the data is there. Homelab has no equivalent, because it has no
quiesce sidecars to assert against: its gate stops at shape. Closing the rest on either
cluster means `sqlite3 <file> 'pragma integrity_check'` inside the gate, which means adding
sqlite to the `restic/restic` image. Until then, a periodic manual restore drill is the only
real proof, and it is the only thing that covers homelab's torn-copy exposure at all.

## Explicitly rejected

These are settled. Do not relitigate them without new evidence.

- **Any probe on `garmin-grafana`.** The `health-garmin-ingest` freshness check covers it.
- **Token-mtime freshness probe for garmin.** The token file is written only on the
  interactive login path, so its mtime changes about once a year. The probe detects nothing
  and false-positives on any sane threshold.
- **Staleness-based liveness for garmin.** Freshness depends on the operator syncing a watch.
  A weekend away restarts the pod repeatedly, and each restart with a stale token sends an MFA
  SMS. Staleness notifies a human; it never restarts.
- **Generalising the jottacloud liveness probe.** It could not fail: `backup.sh` is PID 1 for
  the whole run and never `exec`s over itself, so `ps | grep backup.sh` always matched, and no
  upstream script creates `/tmp/backup-completed`. It measured presence, not progress, so a
  stalled rclone looked alive. `activeDeadlineSeconds` is the pattern to generalise instead.
- **Naive `pg_isready` liveness on postgres**, the exit-0-only Bitnami shape. Same narrow
  detection as the `test $? -lt 2` form, plus a recovery loop that never converges on a
  single-replica local-path PVC.
- **Any probe on the backup sidecars.** Readiness drops the Pod from its EndpointSlice;
  liveness reaches the same place through `CrashLoopBackOff`.
- **`tcpSocket` on sockpuppetbrowser :3000 as a hang fix.** The kernel completes handshakes
  from the accept backlog while the event loop is blocked, so it detects process death only.
- **A NetworkPolicy in front of the MCP server.** Inert on flannel — see
  [homelab-health.md](homelab-health.md#mcp-is-a-sidecar-not-a-standalone-deployment).
- **Converting the two ingest checks to `/fail`.** It trades a 36-hour tolerance for a 6-hour
  one on a signal that depends on a human syncing a watch.

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
