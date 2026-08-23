# Failure detection: probes, deadlines and monitors

How failures in both clusters get noticed: the policy, the inventory, and the failures none of it
catches. Manifests carry per-probe rationale in comments. Read
[What this does not catch](#what-this-does-not-catch) before you trust a green signal.

## Start here: something is wrong

| Signal | Read |
|---|---|
| A restic check is red | `failed_step=` in the ping body names the phase, and `prune=` says whether retention ran — [Reading a restic failure body](#reading-a-restic-failure-body) |
| `mount_ok=no` on homelab restic | The SSD did not mount, so the backup captured nothing — [the gates](#the-backup-verification-gates) |
| An ingest check is red | Check whether the operator synced a watch before suspecting the pipeline — [healthchecks.io checks](#healthchecksio-checks) |
| A sidecar shows `RESTARTS: 0` but its snapshot is missing | Expected; they log rather than exit. Read the sidecar's stderr — [Why the sidecars have no probes](#why-the-sidecars-have-no-probes) |
| An uptime-kuma monitor is UP but the service is down | Suspect an Access redirect — [uptime-kuma.md](uptime-kuma.md#the-cloudflare-access-trap) |
| Everything is green and the data is still wrong | Expected; several probes are shallow by design — [What this does not catch](#what-this-does-not-catch) |

## The decision rule

Monitor the artifact, not the process — a live process proves nothing.

| Workload shape | Instrument | Why |
|---|---|---|
| Serves requests | Probe the real request path | kubelet repairs it by restarting; the failure is local and immediate |
| Produces an artifact on a schedule | Dead-man's-switch on freshness | Restarting does not deliver data that never arrived. Absence of an event is only visible from outside |
| Must not hang | `activeDeadlineSeconds` (Jobs), progress probe (Deployments) | A bounded runtime is a contract you enforce declaratively |

If restarting the thing cannot plausibly fix the failure, a probe is the wrong instrument: probe
failure means "kill and retry", dead-man's-switch failure means "wake a human", and the wrong
choice buys either false confidence or a self-inflicted outage.

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

- Put a readiness probe on every long-running container that serves traffic. The worst case is
  that the pod leaves Service routing.
- Add liveness only when that probe detects the failure **and** a restart repairs it. Every
  service in both clusters runs a single replica, so kubelet has nowhere to send traffic while
  the pod restarts: an eager liveness probe manufactures the outage it was added to catch.
- Add a startup probe to anything with migrations or a slow boot, so liveness cannot fire during
  startup, and keep liveness thresholds strictly laxer than readiness. Readiness sheds traffic;
  liveness destroys state.
- Set `timeoutSeconds` on every probe. The 1s default false-positives on a loaded node, turning
  ordinary disk contention into a restart. Every probe in both clusters sets it today.
- Probe the data plane, not a vendor health endpoint: a control-plane endpoint reports on a
  different process from the one serving your users, and the vendor-documented probe stayed green
  throughout the 2026-08-18 Pomerium wedge
  ([homelab-health.md](homelab-health.md#the-probe-target-is-deliberately-not-the-documented-one)).
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
| influxdb-mcp | liveness and readiness `tcpSocket` | The MCP server exposes no health endpoint. TCP detects process death, not a wedged handler |
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
umami-postgres.** This has nearly been re-broken twice, and the chain is short:

> A container that is not Running is not Ready. A Pod with a non-Ready container leaves its
> EndpointSlice. cloudflared then returns 502 for the application.

Readiness reaches that state directly; liveness reaches it through `CrashLoopBackOff`. Either
way, a fault in last night's *backup* takes a working *application* offline. The one argument for
such a probe was self-healing a failed `apk add` that left the container without `sqlite3`, and
`ensure_sqlite3()` in `vps/workloads/scripts/sqlite-snapshot-lib.sh` now retries that install on
a 5 minute backoff instead. Against a permanent fault — a corrupt database, a full disk, a path
moved by an app upgrade — a probe restarts a container a restart cannot repair.

Detection lives at the artifact instead: the VPS restic gate asserts a fresh snapshot per app and
per FreshRSS user, then turns healthchecks.io red. Latency goes from roughly 45 minutes to at worst
a day — the right scale for a backup fault, and it never costs you the application.

### What the sidecar loops do instead

`set -e` is deliberately absent from all five: if a sidecar exits, kubelet restarts it and a
persistent fault reaches the same `CrashLoopBackOff` chain. Each loop instead runs under `set -u`,
logs failures to stderr and keeps going; sleeps 300s after a failure and 43200s after a success;
publishes atomically as `.tmp` then `mv`, so a failed run leaves the previous snapshot intact;
and asserts *content* before publishing, not only an exit status.

That last one is the part to keep, because both content checks catch a failure an exit code does
not. `snapshot()` runs `sqlite3 <tmp> 'select count(*) from sqlite_master'` and refuses zero
schema objects, since a truncated source makes `.backup` emit a structurally valid but empty
database with a current mtime. `pg-dump-snapshot.sh` refuses fewer than one
`grep -c '^CREATE TABLE '`, since `pg_dumpall` exits 0 against a freshly initialised postgres with
no umami schema — the entrypoint creates the empty `umami` database either way, yielding a
roles-only dump that restores to an empty analytics database. Refusing to publish leaves the
previous artifact ageing, which turns the check red. And because these loops report failure by
logging rather than exiting, their restart counts stay at zero: **to debug a missing snapshot, read
the sidecar's stderr**, and read nothing into `RESTARTS: 0`.

All five loops are real files under `vps/workloads/scripts/`, delivered by the
`sqlite-snapshot-scripts` `configMapGenerator` in `vps/workloads/kustomization.yaml` and mounted
at `/scripts`. Four source `sqlite-snapshot-lib.sh`; n8n, karakeep and uptime-kuma share
`sqlite-snapshot.sh` outright and differ only in `$SNAPSHOT_DB`. Editing one rolls every
Deployment that mounts it, and all five use `strategy: Recreate`, so a script edit costs a brief
hard-down window for each rather than a rolling update. Generated scripts also pass through
envsubst, so run `make check-script-substitution` and read the note in `AGENTS.md` before you
write a `$VAR` into one.

## Scheduled work

| Field | Value | Why |
|---|---|---|
| `timeZone: "UTC"` | all six jobs | Otherwise the schedule follows kube-controller-manager's local zone |
| `startingDeadlineSeconds` | 3600, except 1800 for cloudflare-analytics, 300 for jottacloud, and unset for `ingest-freshness` | A missed window retries for that long, then drops |
| `activeDeadlineSeconds` | restic 14400, influx-backup 3600, cloudflare-analytics 1200, ingest-freshness 300, jottacloud 21600 | With `concurrencyPolicy: Forbid`, one hung run silently blocks every later run |
| `ttlSecondsAfterFinished` | 259200 on both restic jobs and cloudflare-analytics; 172800 on influx-backup; 86400 on the rest | A Friday failure on the restic jobs survives until Monday |
| `terminationGracePeriodSeconds` | not set on any job | busybox `ash` runs as PID 1 and never forwards SIGTERM to restic, so a grace period only slows teardown. `restic unlock` at the head of the next run recovers the lock |

### The restic ping wrapper

Both wrap the run as `ping_hc start` → `snapshots` → `unlock`, `backup`, `forget --prune`,
`check` → `ping_hc "$rc"`, with the gate placed differently on each cluster
([the gates](#the-backup-verification-gates)). Three rules hold on both:

- The `/start` ping detects a run that starts and never finishes, and records durations. The
  exit-code ping (`hc-ping.com/$UUID/$rc`) separates success from failure. Pings never fail the
  job and use `wget -T 10`, so healthchecks.io cannot hang the backup.
- Steps chain with `&&`, not `set -e` inside a group. errexit is ignored inside an AND-OR list,
  so `{ set -e; … } || rc=$?` runs past a failure and reports the wrong status.
- `restic unlock` runs first and, without `--remove-all`, clears only stale locks.

`homelab-restic` runs at 03:00Z in 26s, `vps-restic` at 04:00Z in 57s — down from 88s and 117s
once retention started pruning, so `restic check` walks 14 snapshots instead of 137. The 4h
`activeDeadlineSeconds` is an opening guess: resize it from recorded durations, and expect
`homelab-restic` to rise, since its gate adds a `du` walk of the files restic just read.

### The backup verification gates

`restic` succeeds on an empty tree: it writes a valid snapshot, `restic check` passes, and
healthchecks.io goes green. Both jobs mount their source as `hostPath` with `type: Directory`,
which asserts only that the directory *exists*, so a volume that fails to mount while its
mountpoint survives on the root filesystem produces a backup of nothing that reports success —
and `forget --prune --keep-daily 7` then expires the seven genuine recovery points over the
following week. Snapshot `551bd209` in the homelab repository is 12 B, retained as a "monthly".
**The gate is the only thing in either job that asks whether the backup was of anything.** Each
cluster's script is the source of truth for its thresholds; change them in the same commit here.

| | VPS (`vps/backup/scripts/restic-backup.sh`) | Homelab (`homelab/backup/restic-cronjob.yaml`) |
|---|---|---|
| What exists to check | A `*.restic` snapshot per app, published by the quiesce sidecars | No sidecars, so no artifact. Every PVC is backed up as live application state |
| Assertion shape | Snapshot files: present, fresh, readable | The **tree**: mounted, right scale, listed files present and non-trivial |
| Authoritative checks | Expected set, plus a `find` that must not error | Mount identity, tree scale, expected set, dump freshness |
| Freshness limit | 15h (`STALE_MINUTES=900`), a 3h margin over the sidecars' 12h period | 30h (`STALE_MINUTES=1800`), on the two influx dumps only |
| Advisory, never fatal | Any `*.restic` past the threshold, so one orphaned PV directory cannot pin the gate red forever | An empty PVC directory, legitimate on a freshly provisioned PVC; and `du`'s exit status |
| Runs | After `forget --prune` | **Before** `forget --prune`, which is skipped when the gate fails |
| Verdicts in the body | `MISSING`, `STALE`, `UNREADABLE`, per app | `mount_ok`, `artifacts=n/m`, `dumps_fresh=n/m`, `pvc_dirs` |

Both promote to failure only when restic itself succeeded, so a real restic failure keeps its own,
more specific exit code. Both announce their passes (`8/8 artifacts present`, `2/2 newer than
30h`): a gate that prints nothing when happy is indistinguishable from one that never ran. In
both, **"I could not look" must never be reported as "everything is fine"** — an unreadable
`/data` or an unopenable PVC directory fails the job.

**The one deliberate divergence is that homelab gates the prune**, because pruning is the step
that destroys data: failing the job afterwards still alerts, but the seven good daily snapshots
are already being expired on schedule while the alert goes unread. A false positive costs a
repository that grows in B2 until somebody looks; the false negative costs every recovery point.
`restic check` runs either way. Neither cluster makes the gate a *precondition of the backup* —
that would skip a whole night of everything else over one stale artifact.

**Add every new sqlite-backed service (VPS) or local-path PVC holding something you would miss
(homelab) to its expected set, or that application's backup goes unverified, silently.** An
explicit list beats a wildcard, which cannot tell "no databases exist" from "the volume is
unmounted" from "three of four present" — all three produce no stale files.

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
| homelab | influxdb-bolt | `/data/pvc-*_health_influxdb-data/influxd.bolt` | ≥32 KiB |
| homelab | garmin-tokens | `/data/pvc-*_health_garmin-tokens/garmin_tokens.json` | ≥256 B |
| homelab | influx-native-dump | `/data/pvc-*_health_health-dumps/native/*` | <30h |
| homelab | influx-lp-export | `/data/pvc-*_health_health-dumps/lp/*.lp.gz` | <30h |

Homelab byte floors sit an order of magnitude under observed sizes: they reject a zero-length or
truncated file, not slow growth. `influx-backup` writes the dumps at 02:30Z, 30 minutes before this
job, so 30h tolerates one missed run (`health-influx-backup` is the check for *that*) and fails on
two consecutive misses. Nothing else is freshness-checked: a deadline on live application state
manufactures reds on any file an app happens not to touch for a day.

Entries are globs because local-path-provisioner names each PVC directory
`<pvName>_<namespace>_<pvcName>` with a random UUID. On VPS an unmatched glob survives literally
and fails the `-f` test, which is the `MISSING` verdict. On homelab the StorageClass is
`reclaimPolicy: Retain`, so a recreated PVC leaves its predecessor behind forever; each glob
takes its newest match, so a live artifact normally beats its frozen predecessor — but if the
live artifact is absent entirely, the orphan is the only match and the check passes on it.
Telling bound from orphaned needs the Kubernetes API from inside the job, not worth a
ServiceAccount and RBAC on a backup CronJob: the orphan is under `/data` and is backed up too,
so this is the gate reporting on the wrong file, not a lost recovery point. The gate prints each
resolved path, so the substitution shows up in the log rather than hiding behind "8/8".

Two homelab checks have no VPS equivalent. **Mount identity** is the first-order one: Talos puts
the kubelet pod directory on the EPHEMERAL partition (`/dev/sda6`), the same filesystem
`/var/mnt/ssd/local-path-provisioner` falls back to when the SSD user volume fails to mount.
`/etc/hosts` is bind-mounted from that pod directory into every non-hostNetwork container, so its
`st_dev` *is* the EPHEMERAL device, readable with no host access. The gate compares it with
`st_dev` of `/data`: they differ when the SSD is mounted (2065 vs 2054, measured in-cluster
2026-08-20) and match when it is not, which fails. A `stat` that fails on either path is a
failure, not a pass — without the reference, the mount cannot be told from its fallback.

**Tree scale** sets floors, not targets: at least `MIN_PVC_DIRS=8` PVC directories and
`MIN_DATA_KIB=1048576` (1 GiB). Measured 2026-08-20 at 10 directories, 44,288 files and 4.418 GiB
— roughly 4x headroom, so log rotation or emby cache eviction cannot trip it, while an empty or
single-PVC tree cannot clear it.

A forensic pass then prints one line per PVC directory, so the night a PVC empties the diff is in
the log. `du`'s exit status is the one deliberate exception to the "could not look" rule: busybox
`du` returns non-zero when a file vanishes mid-walk, routine with sqlite WAL files and rotating
logs, so its status only warns while its *output* stays authoritative — an unparseable total
fails, and a `du` that could not walk still emits a number the scale floors catch. Do not capture
its stderr: `2>&1` puts the diagnostic ahead of the total, empties the numeric prefix, and
promotes the advisory warning to the fatal branch.

### "Newest of a glob" is a dangerous shape

FreshRSS keeps one database per user. An earlier check took the newest snapshot matching the
FreshRSS glob, so when one user's database stopped being snapshotted the other users kept the
newest mtime fresh and it stayed green forever. **Iterate the source objects and assert an
artifact for each**: reducing a set to its maximum detects only "all of them stopped", while the
failure you care about is "one of them stopped". The four single-DB services still take the
newest match, because their glob is one PVC directory expected to match one path.

## healthchecks.io checks

| Check | 1Password reference | Period / grace | Pinged by |
|---|---|---|---|
| `homelab-restic` | `op://Homelab/b2-restic/healthcheck-uuid` | 1d / 2h | restic CronJob, `/start` and exit code |
| `vps-restic` | `op://VPS/b2-restic/healthcheck-uuid` | 1d / 2h | restic CronJob, `/start` and exit code |
| `vps-uptime-kuma-alive` | `op://VPS/uptime-kuma/healthcheck-uuid` | 5m / 15m | An uptime-kuma monitor — [uptime-kuma.md](uptime-kuma.md#the-self-monitor-layer-4) |
| `health-apple-ingest` | `op://Homelab/health-healthchecks/apple-uuid` | 1d / 12h | `ingest-freshness`, success only, and only when InfluxDB data is under 24h old |
| `health-garmin-ingest` | `op://Homelab/health-healthchecks/garmin-uuid` | 1d / 12h | as above |
| `health-influx-backup` | `op://Homelab/health-healthchecks/backup-uuid` | 1d / 6h | `influx-backup`, `/start` and exit code, from an EXIT trap |
| `homelab-cloudflare-analytics` | `op://Homelab/health-healthchecks/cloudflare-uuid` | 1h / 2h | `cloudflare-analytics` CronJob, `/start` and exit code |
| `jottacloud-backup` | `op://Homelab/jottacloud-backup/HEALTHCHECK_UUID` | 6-hourly schedule | The third-party image's own `backup.sh`, success only |

**Four of the five jobs this repo pings send `/start` and an exit code** — both restic jobs,
`cloudflare-analytics` and `influx-backup`. Follow that pattern for new jobs. `influx-backup` also
needs `set -eu -o pipefail` and its ping in an EXIT trap: under `set -e` alone `xargs` swallows
the prune step's `ls` failure and the ping fires anyway, and with the ping on the last line a
failing prune, a missing ConfigMap key or a dead influxdb pod produces *exactly nothing* until the
6h grace expires ~30 hours later. The accepted cost — a transient failure now pages instead of
self-healing into silence — is the better trade.

**The two ingest checks stay success-only and must not be converted.** A `/fail` on a stale
bucket would flip the check DOWN on the first 6-hourly run that found nothing, trading a 36-hour
tolerance for a 6-hour one — on a signal that depends on the operator syncing a watch. They get
an inert `/log` ping instead, and `ingest-freshness` always exits 0: the signal is the absent
ping, not a failed Job. `jottacloud-backup` is success-only for a different reason — its ping
comes from `backup.sh` inside a third-party image this repo does not control.

`homelab-cloudflare-analytics` goes red for one failure mode that is not a malfunction: **an
unrecoverable gap**. Cloudflare keeps 8 days, so if the job has been down longer the missing hours
no longer exist anywhere. It logs the range, writes an `ingest_gap` marker into InfluxDB so the
hole reads as a hole rather than a quiet week, ingests what survives, and exits non-zero — once,
since the next run's watermark is current again. Read a red check here as "find out which hours
were lost" ([homelab-health.md](homelab-health.md#gaps-are-permanent-so-they-are-loud)).

Adding a check takes [four edits](apply-workflow.md#adding-a-secret-is-four-edits-not-three);
nothing catches a missing `ENVSUBST_VAR_NAMES` entry, which ships the literal `${VAR}` as the
ping UUID and silently disables the check. The read-only API key is
`op://Homelab/healthchecks.io/read-only-api-key`; if that path 404s, the item is still spelled
`healtchecks.io`.

### Ping bodies

Every ping carries a short `key=value` body: one pair per line, printable ASCII, first line
always `summary=`. The one bit is still the alerting signal; the body makes a *green* check
legible, so the Events log answers "what did it see?" once the pod log has aged out. Without one,
`health-apple-ingest` sat green through five days of stale Apple Health data — nothing had
malfunctioned, and green could not distinguish "fresh" from "stale, window not expired".

**A ping body is a disclosure channel.** healthchecks.io is a third-party SaaS in the EU: the body
leaves the estate, is stored in their object storage, repeats on every run until somebody fixes
the script, and cannot be shortened short of deleting the check. **It also travels with the
alert** — email, webhook, Slack, Telegram, Matrix, GitHub and MS Teams transports all read the
last ping's body into the notification, verbatim. Treat an `emit` call like a committed line.

**Forbidden, because they grant somebody something:** Secret contents — anything from a Kubernetes
Secret or a 1Password item — and healthchecks.io ping UUIDs, which are the check's write
credential, so anyone holding one can ping your check and mask a genuine failure. A health
*reading* is likewise never emitted; the two ingest checks carry freshness *timestamps* only, by
the [explicit decision recorded
below](#named-accepted-residual-the-ingest-checks-leak-a-presence-timeline).

**Omitted because nothing reads them, not because they are sensitive:** restic repository URIs,
B2 and InfluxDB bucket names, PVC UUIDs, namespaces, pod and node names. These are ordinary
identifiers — they grant nothing, they are fine in a pod log, and no rule bans them from a body;
they are absent only because they answer no question an operator asks at 3am. Do not read a
classification into that absence, and do not open an honesty-box row if one appears somewhere
(the three tiers are in `AGENTS.md`). `make check-ping-bodies` does deny every name in
`ENVSUBST_VAR_NAMES`, bucket names included, but that is a mechanical hazard list, not a secrecy
classification.

**Never build a body from a command's output.** healthchecks.io's own documentation teaches the
opposite, and here that pattern leaks: the two scripts `influx-backup.sh` execs into the influxdb
pod pass the InfluxDB **operator token** on argv (`influx-native-backup.sh:21`,
`influx-export-lp.sh:36`), and a failing `wget` or `curl` quotes the URL it was given, which for a
ping *is* the write credential. The rule is blanket rather than tiered because **a script cannot
sort the tiers apart at runtime**: it cannot tell a bucket name from an operator token in a string
it did not construct, so command output is unclassifiable and stays out.

`emit` is therefore only ever called with a literal key and a value the script computed itself: a
count, an age, a byte size, a path built from a literal glob, or a verdict from a fixed enum.
`make check-ping-bodies` enforces this, including the one-intermediate-variable evasion
(`M=$(cmd); emit "error=$M"`), and refuses a denied name in every parameter-expansion form —
`${HC_UUID:-}`, `${HC_UUID#p}`, `${HC_UUID/a/b}`, `${#HC_UUID}`. A taint clears only through an
explicit `# check-ping-bodies: untaint <NAME> <reason>` line.

Bodies die with their ping-log entry, `Check.prune()` removing the objects then the ping rows —
100 entries per check on Hobbyist, 1000 on Business, so a 6-hourly check's bodies persist 25 days
or 250. `ingest-freshness` uses `/log` for its stale and query-failure paths: a `log` ping sets no
`last_ping`, `last_start` or `status` and cannot postpone, suppress or trigger an alert. Its one
side effect is that `has_confirmation_link` is set from the body on every action, `log` included,
driving a UI nag — no body here contains the substring `confirm`, and none may.

#### Reading a restic failure body

`failed_step=` names the phase that set the exit code: `unlock|backup|forget|check` for restic's
own failures, `gate` for the verification gate. On homelab it is captured where the chain aborts,
*before* the gate and `restic check` run, because both run unconditionally afterwards and would
overwrite it — so a restic failure is never reported as a gate failure, even when the gate also
fails. `restic_check=` is `ok`, `failed`, or `not-reached` only when the run never got that far.
`prune=` has three states and they are not interchangeable:

| Value | Meaning | What to do |
|---|---|---|
| `ran` | `forget --prune` completed | Nothing |
| `skipped` | The gate deferred retention on purpose | Nothing urgent. The repository grows in B2 until the gate goes green |
| `failed` | `forget --prune` started and died | Look tonight. Snapshots may be partly expired |

Reporting `failed` as `skipped` is what makes an operator not look, so the script keeps them apart.

### Checks in the account that this repo does not ping

The Management API returns 14 checks; the table above lists the 8 this repo owns. The other six —
`adsb.cynexia.net`, `pve3.cynexia.net`, `fs.cynexia.net`, `tailscale unattended upgrades`, `Home
Assistant`, `upsd.cynexia.net` — are pinged from Proxmox hosts, Home Assistant and host cron, and
are deliberately outside this repo. They are recorded so that "not in the table" can be told from
"does not exist". **`upsd.cynexia.net` has `n_pings=0`**: never pinged, so wire it up or delete it.

## Layers 3 and 4: uptime-kuma

Layer 3 is a hand-maintained uptime-kuma instance on the VPS; layer 4 is one monitor inside it
that pings healthchecks.io, so uptime-kuma's own death is visible. Both are UI procedures, so they
live in **[uptime-kuma.md](uptime-kuma.md)** — monitor list, per-monitor HTTP settings, the
Cloudflare Access trap and the self-monitor. One consequence bites from this side: a monitor that
follows redirects reports UP off the Cloudflare login page while the origin is dead.

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
| **homelab services** | The external layer runs on the VPS, which has no route to `*.cynexia.net`. Only the three health-tunnel hostnames get layer-3 coverage. sonarr, radarr, sabnzbd, emby, hydra2 and grafana have probes and nothing external |
| **the VPS gate** | It proves each snapshot exists and is recent, and — through the sidecar's own refusal to publish a schema-less snapshot — that it holds at least one schema object. It does not prove the contents are complete or uncorrupted. A snapshot missing rows, or with a corrupt page below the `sqlite_master` read, passes everything here and surfaces at restore time |
| **agent mail (hermes VM)** | Nothing monitors it at all — no probe, no check, no canary. A Purelymail outage, expired credential, DNS drift or send-cap exhaustion surfaces only as tool errors inside agent sessions. Deliberate for now; the planned round-trip canary is in [agent-mail.md](agent-mail.md#monitoring-and-backup-none-deliberately-for-now) |
| **the homelab gate** | It proves the SSD is mounted and the tree is the right *shape*: right number of PVC directories, right order of magnitude, the listed files present and non-trivial. It says nothing about *content*. Every homelab PVC is copied live, with no quiesce step: a sqlite database mid-write is captured torn, `sonarr.db` at 14 MiB of corruption passes the size floor exactly as 14 MiB of working database does, and a PVC that stopped being written to weeks ago looks identical to one written a minute ago. Only the two influx dumps are age-checked. A retained orphan directory from a recreated PVC can satisfy an expected-set entry the live PVC no longer can — the resolved paths are printed so it is visible, but nothing fails on it. The rest surfaces at restore time |
| **cloudflare-analytics** | It proves the hours it fetched were fetched. It cannot prove Cloudflare's own numbers are right, and it does not alert on *content* — a hostname that stops receiving traffic entirely, or a spike, produces a perfectly green check. That is Phase 3 (Grafana alert rules), deliberately deferred until a baseline exists |

Queued, not configured: a changedetection `overdue_watches` json-query monitor and a karakeep
queue-depth alert; both need an API credential in the monitor.

Snapshot integrity stays partly verified by choice. On VPS the `sqlite_master` assertion closes the
worst case — a fresh, valid, empty snapshot from a truncated source — by proving a schema exists,
not that the data is there; homelab has no equivalent, having no quiesce sidecars to assert
against, so its gate stops at shape. Closing the rest means `sqlite3 <file> 'pragma
integrity_check'` inside the gate, hence sqlite in the `restic/restic` image. Until then a periodic
manual restore drill is the only real proof, and the only cover for homelab's torn-copy exposure.

### Named accepted residual: the ingest checks leak a presence timeline

`last_point` and `last_point_age` on `health-apple-ingest` and `health-garmin-ingest` are emitted
every 6h, and over the retained window they constitute a sync-and-absence timeline for an
identified individual: when the operator last wore and synced a watch, and by inference when they
were away, asleep or not wearing a device. They are emitted anyway — `last_point_age` *is* the
finding these bodies exist to deliver — and the data subject is the operator, on their own
account, at a processor already chosen for this data.

**The recipient list is not known, and that is an open item.** That justification names *one*
processor, but a failure body travels to every configured transport, and upstream reads the last
ping's body regardless of kind (`Transport.last_ping()` filters on time, not kind) — so an ingest
check's alert carries the most recent 6-hourly `/log` body, the STALE one, with both fields in
it. Which channels that reaches is unestablished: the key at
`op://Homelab/healthchecks.io/read-only-api-key` is read-only, `GET /api/v3/channels/` returns
401 with it, a check fetched with a read-only key omits its `channels` field, and the vault holds
no full-access key. To close this, read the account's Integrations page and record the channels
here. If anything beyond email is configured, decide whether those two fields may travel to it;
the cheap mitigation is to withhold them from the `/log` (stale) body only.

## Explicitly rejected

These are settled. Do not relitigate them without new evidence.

- **Any probe on `garmin-grafana`.** The `health-garmin-ingest` freshness check covers it.
- **Token-mtime freshness probe for garmin.** The token file is written only on the interactive
  login path, so its mtime changes about once a year: the probe detects nothing and
  false-positives on any sane threshold.
- **Staleness-based liveness for garmin.** Freshness depends on the operator syncing a watch, so
  a weekend away restarts the pod repeatedly, and each restart with a stale token sends an MFA
  SMS. Staleness notifies a human; it never restarts.
- **Generalising the jottacloud liveness probe.** It could not fail: `backup.sh` is PID 1 for the
  whole run and never `exec`s over itself, so `ps | grep backup.sh` always matched, and no
  upstream script creates `/tmp/backup-completed`. It measured presence, not progress, so a
  stalled rclone looked alive. `activeDeadlineSeconds` is the pattern to generalise instead.
- **Naive `pg_isready` liveness on postgres**, the exit-0-only Bitnami shape. Same narrow
  detection as the `test $? -lt 2` form, plus a recovery loop that never converges on a
  single-replica local-path PVC.
- **Any probe on the backup sidecars.** Readiness drops the Pod from its EndpointSlice; liveness
  reaches the same place through `CrashLoopBackOff`.
- **`tcpSocket` on sockpuppetbrowser :3000 as a hang fix.** The kernel completes handshakes from
  the accept backlog while the event loop is blocked, so it detects process death only.
- **A NetworkPolicy in front of the MCP server.** Inert on flannel — see
  [homelab-health.md](homelab-health.md#mcp-behind-cloudflare-access).
- **Converting the two ingest checks to `/fail`.** It trades a 36-hour tolerance for a 6-hour one
  on a signal that depends on a human syncing a watch.

## Rolling out a probe change

A probe rollout that restarts pods is a failed rollout.

1. Confirm the probe path's status from inside the cluster with a throwaway `alpine/k8s:1.36.0` pod
   rather than from vendor documentation.
2. Run `make diff-homelab` or `make diff-vps`, then apply.
3. Run `kubectl -n <ns> rollout status deploy/<name>` and confirm it completes without a restart
   loop. Ten minutes later, `kubectl -n <ns> get pods` must show 0 restarts.
4. For a CronJob change, run `kubectl create job --from=cronjob/<name> <name>-manual`, then
   confirm the healthchecks.io check goes green and records a duration.
5. After a few weeks, resize `activeDeadlineSeconds` from the recorded durations.
