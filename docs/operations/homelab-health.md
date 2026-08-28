# Health namespace

Personal health-data pipeline in the homelab cluster: Apple Health + Garmin → InfluxDB → Grafana, plus a Claude MCP connector.
It was added in Phase 0/1 as its own `health` namespace.
Manifests live in `homelab/health/`.

The namespace also hosts one workload that is **not** health data: the Cloudflare analytics ingest ([below](#cloudflare-analytics-ingest)).
It shares this InfluxDB and Grafana rather than standing up a second pair — a deliberate trade of infrastructure telemetry living in the same database as personal health data, taken because a second InfluxDB for ~3,500 rows a day is not worth the operational surface.
It stays in its own bucket and its own measurements.

Design docs are in the separate `~/Downloads/git/HealthRecords` repo — `docs/superpowers/specs/2026-07-11-health-platform-vision.md` and `docs/superpowers/specs/2026-07-11-health-records-ingestion-design.md`.
Phase 2 (facade, records store, multi-person registry) is scoped there, not here.

## Image policy

**No keel in this namespace.**
This is a data pipeline; auto-upgrading it is not wanted.
Every image is version- or digest-pinned and Renovate proposes bumps instead.
Renovate is scoped to `homelab/**` and `vps/**`, with `pinDigests` on at the top level (see `renovate.json`); a `homelab/health/**` packageRule groups this namespace's bumps as `health stack` and keeps them off automerge.

`namespaces.yaml` marks `health` as Pod Security Admission (PSA) `baseline` (nothing here needs hostPath/hostNetwork), but every workload already trips `restricted`-level PSA warnings — a hardening pass to `restricted` is a queued follow-up.

### Pins that carry a reason

- **`garmin-grafana` is digest-pinned to a main-branch build** (`thisisarpanghosh/garmin-fetch-data@sha256:8b7955d3...`), not a tagged release.
  Release `v0.5.0` crashes with an `AttributeError` on `client.profile` when `TAG_MEASUREMENTS_WITH_USER_EMAIL` is set — fixed upstream post-release but not in a tagged build.
  A `renovate.json` `packageRule` puts this image on **`dependencyDashboardApproval`**: there is no way to encode "this specific release is bad", so an unapproved rule would propose bumping straight to the broken `v0.5.0` build.
  Dashboard-only rather than disabled, so an upstream fix still surfaces.
  The exit path is manual — when upstream publishes a release newer than `v0.5.0`, approve the bump from the dashboard and re-pin the manifest as `tag@digest`.
- **`apple-health-ingester` memory limit is 1Gi**, not the original 256Mi: large Health Auto Export batch exports OOMKilled it at 256Mi.
- **`influx-backup` runs on `alpine/k8s:1.36.0`**, version-matched to the cluster's server minor — not `bitnami/kubectl`, which no longer publishes plain version tags on Docker Hub (moved to the frozen, unauthenticated `bitnamilegacy/*`, and that image has neither `wget` nor `curl`, one of which the heartbeat push needs).

## Ingress

A dedicated `cloudflared` Deployment runs the **`cynexia-health`** tunnel — separate from the VPS cluster's `cynexia-vps` tunnel.
Credentials are in 1Password as the **DOCUMENT** item `health-cloudflared`: use `op document get`, not `op read` (document items don't expose a plain field).

Public `*.cynexia.com` hostnames on this tunnel:

| Hostname | Purpose |
|---|---|
| `hae.cynexia.com` | Health Auto Export ingest → `apple-health-ingester` |
| `mcp.cynexia.com` | Claude/Hermes MCP connector, via Cloudflare Access (Managed OAuth) |
| `hermes.cynexia.com` | Hermes agent dashboard on the hermes VM (`hermes.cynexia.net:9119`, off-cluster), via Cloudflare Access (karakeep-style email policy) |
| `hermes-app.cynexia.com` | `hermes-webui` on the same VM (`hermes.cynexia.net:8787`, off-cluster) — the server the Hermex iOS app talks to, via Cloudflare Access (Service Auth + the same email policy) |

`hermes.cynexia.com` and `hermes-app.cynexia.com` are the two off-cluster origins on this tunnel: cloudflared proxies both to the hermes VM on the LAN, not to a cluster Service.
The Access app (`hermes`) attaches two reusable policies: `service-auth-monitoring`, the `non_identity` policy holding the `Uptime` service token that lets the uptime-kuma monitor through, and `allow_cynexia_com`, which requires `email_domain: cynexia.com`.
It carried IP bypass policies until August 25, 2026; see [MCP behind Cloudflare Access](#mcp-behind-cloudflare-access).
The `hermes-app` Access app is separate: a Service Auth policy holding the Hermex token, plus the same `allow_cynexia_com` policy for browsers.
The dashboard runs its own mandatory login behind that (basic auth, forced by its non-loopback bind), so Access is defense in depth, not the only gate — but the Access gate still **fails open** like mcp's does, and the same post-rebuild rule applies.
Hermes Desktop's remote-attach cannot pass Access's browser login (no custom-header support upstream); it uses the tailnet path (`http://hermes.cynexia.net:9119` via the OPNsense subnet route) instead.

`hermes-app.cynexia.com` fronts a **different service on the same VM** — `hermes-webui` on port 8787, which the Hermex iOS app talks to.
Its own Access app, `hermes-app`, deliberately does **not** reuse the `hermes` app's policy set:

- **Service Auth (`non_identity`), not `bypass`, for the token.**
  A bypass policy keyed on a service token means "no authentication for anyone matching this rule" — Access forwards the request without validating the token and without an identity in the audit log.
  Service Auth verifies the `CF-Access-Client-Id`/`CF-Access-Client-Secret` pair, mints a `CF_Authorization` JWT and logs the token as the actor.
  That buys revocation that actually works, an attributable audit trail, and a clean 401 instead of an SSO redirect a native client cannot complete.
  The cost is that the token headers must be on **every** request including the first `GET /health`; Hermex is built for exactly that, taking custom headers on the connect screen before the first probe.
- **No IP bypass, neither the home address nor the VPS.**
  The VPS one is wrong outright — nothing there calls the WebUI.
  The home one is the tempting mistake: Hermex sends the token on every network, so a home bypass buys the app nothing, while creating a silently divergent path where it works on home WiFi with a broken or revoked token and fails the moment the phone moves onto cellular.
  That is the worst failure shape for a mobile client — a misconfiguration that only appears away from where it can be debugged.
- `options_preflight_bypass` is on, because an unadorned CORS preflight carries no token headers and Service Auth would reject it at the edge.

That gate is load-bearing in a way the dashboard's is not: hermes-webui serves `/share`, `/share/*`, `/api/share/*` and `/static/*` with no authentication of its own, so "the WebUI's password is the second gate" holds for the app but not for every path.
The VM-side half — the unit, the update and rollback runbooks, and the security posture — is in [homelab.md](homelab.md#hermes-webui-on-the-vm).

Both hostnames' Access apps **fail open** if the app is deleted; after any Cloudflare rebuild, verify the edge challenges an unauthenticated client before trusting either.

Two pieces of VM-side state make the dashboard work behind the tunnel (on `hermes.cynexia.net`, login `ssh hermes@…`; `~/.local/bin` is not on the non-login PATH, so run `hermes` through an interactive shell or by full path):

- `dashboard.public_url: https://hermes.cynexia.com` in `~/.hermes/config.yaml`.
- `Environment=FORWARDED_ALLOW_IPS=*` in the `hermes-dashboard` systemd user unit. cloudflared runs off-host, so uvicorn must be told to trust `X-Forwarded-*` headers or cookies lose their `Secure` flag.
  The wildcard means any LAN client can spoof forwarded headers (they feed the login rate-limiter and audit log); accepted — tighten to the cluster egress IP if it matters.

`.bak-hermes-tunnel` copies of both edited files sit beside the originals.
Hermes registers MCP OAuth clients with callbacks at `https://hermes.cynexia.com/api/mcp/oauth/callback/<server>`, which is why that wildcard sits in the dynamic client registration (DCR) allowlist below.

**Hermes profiles are fully isolated homes.**
Each profile (for example `~/.hermes/profiles/emh/`) has its own `mcp-tokens/` directory, so MCP auth is per-profile: a new profile inherits nothing from the main profile's `~/.hermes/mcp-tokens/` and re-runs dynamic client registration from scratch.
To re-authenticate a profile's MCP servers, use the **web dashboard** (`https://hermes.cynexia.com` with the profile selected) — never the Desktop app.
The desktop/TUI gateway flow binds an ephemeral loopback callback listener **on the VM**, which a browser on a remote machine can never deliver a callback to; the dashboard flow uses the public callback `https://hermes.cynexia.com/api/mcp/oauth/callback/<Server>`, which the Managed OAuth DCR allowlist already covers.
A failed or abandoned dashboard OAuth attempt blocks retries with HTTP 409 `MCP OAuth for '<name>' is already in progress`.
That stale flow is in-memory only and self-expires after 15 minutes (`_MCP_DASHBOARD_OAUTH_TTL`); `systemctl --user restart hermes-dashboard` on the VM clears it immediately.

Grafana is **not** on this tunnel — it is private, Traefik-fronted at `grafana-health.cynexia.net` like every other homelab service (LAN/Tailscale only).

After changing hostnames in `homelab/health/cloudflared.yaml`, every hostname needs a proxied CNAME to `1a4245a3-5264-420c-9893-b45ff25a0214.cfargotunnel.com`.
`make route-health-dns` mints them all, but it shells out to `cloudflared tunnel route dns`, which needs an **origin certificate** at `~/.cloudflared/cert.pem`.
On a machine that has never run `cloudflared tunnel login` that file does not exist, the target aborts under `set -euo pipefail` on the *first* hostname, and a newly added one is never reached — `cloudflared` is not in `make check-tools`, so nothing warns first.
Either run `cloudflared tunnel login` once, or create the single record through the Cloudflare API (zone `2bf4553c3f994e36202b5f574577d2e5`), which is also the only way to set the record comment this zone uses as its provenance note.
`hermes-app.cynexia.com` was created that way.

To recreate the credentials Secret, `make create-health-cloudflared-secret`.

## MCP behind Cloudflare Access

Since 2026-08-22 the InfluxDB MCP server is a plain single-container Deployment + Service (`influxdb-mcp`, port 3000) and **auth lives entirely at the Cloudflare edge**: an Access app on `mcp.cynexia.com` with Managed OAuth — RFC 8414/9728 metadata served by Access, dynamic client registration enabled, 15m access tokens against a 336h (2-week) grant session.
The app's only policy is `allow_cynexia_com`, which requires `email_domain: cynexia.com`.
Identity providers (account state as of 2026-08-23): the app accepts all of the account's identity providers (`allowed_idps: []` means "all"); the account has exactly two — one-time PIN and Cloudflare — and the `allow_cynexia_com` policy lists both as login methods.

Dynamic client registration is gated by a **redirect-URI allowlist** (`oauth_configuration.dynamic_client_registration.allowed_uris` on the Access app).
A client whose callback is not listed gets `400 invalid_client_metadata: "redirect_uri is not allowed by the account configuration"` at registration — this is what blocked the Hermes agent until 2026-08-22.
As of 2026-08-23 the list holds Claude's two callbacks (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`) and `https://hermes.cynexia.com/api/mcp/oauth/callback/*` (a trailing `/*` wildcards sub-paths); localhost and loopback clients are allow-any.
**Every new MCP client host needs its callback added**, by a GET-then-full-PUT of the app.
Like everything else about this app, the list is account-side state this repo cannot restore: re-creating the Access app means re-entering it.
This setup replaced the Pomerium proxy (daily re-auth from its 14h session expiry; DCR disabled, locking out non-allowlisted MCP clients).
The retired Google OAuth client is kept until 2026-08-29 as a fallback; delete it after that if nothing has needed it.

**An IP-bypass Access policy on a Managed OAuth app silently breaks OAuth bootstrap** for every client egressing from that IP: the MCP SDK only starts an OAuth flow when it receives a 401 challenge, and a bypassed request reaches the origin with 200, so no flow ever starts.
Clients that already hold tokens keep working — token refresh goes directly to `cynexia.cloudflareaccess.com`'s token endpoint, which the bypass never touches — so the failure appears only for **fresh** token stores.
The observed symptom (Hermes, new profile, 2026-08-23): "The server responded, but no OAuth token was obtained — this provider may require a manually-registered OAuth client."

The first fix, applied 2026-08-23, split the shared reusable bypass policy ("bypass from home or access token or hetzner", id `110997f7`) into a Hetzner half and a home half, leaving the other eight apps' behavior unchanged.

**Both halves are now gone.**
On August 25, 2026 the Access estate was simplified: "bypass from home" and "bypass from hetzner or service token" were detached from all eight apps and deleted, along with four other reusable policies and two service tokens.
No application is open to an IP any more.
Interactive access is `allow_cynexia_com` everywhere; the four monitored VPS apps additionally carry `service-auth-monitoring`, a `non_identity` policy holding the `Uptime` service token.
See [uptime-kuma.md](uptime-kuma.md#the-cloudflare-access-trap).

The Access application for `mcp.cynexia.com` is named `health-data-mcp`, and it carries **no bypass policy at all** — only `allow_cynexia_com`.
This is deliberate, for two reasons: every vantage, including home and the VPS, gets the 401 that bootstraps MCP OAuth, and the `Data MCP` uptime-kuma monitor's pinned `["401"]` stays truthful when probing from the VPS's Hetzner IP.
**Do not attach any bypass policy, or any service-token policy, to this app.**

The origin is authless in HTTP mode and does not validate the `Cf-Access-Jwt-Assertion` header Access injects — accepted deliberately: Pomerium fronted the same authless origin, which ignored its injected identity too.
The tunnel is the only path in from the internet, and Access gates the hostname.

**RESIDUAL RISK — the gate fails OPEN.**
The old gate was committed here and failed closed (no Pomerium → 502).
The new gate is Access dashboard/API state tracked nowhere in this repo: delete or disable the app and cloudflared serves the authless origin raw to the internet, silently — and a rebuild from this repo (`make apply-homelab` + `make route-health-dns`) republishes the hostname with no guarantee the app still exists.
After any rollback, rebuild or account-side change, `curl -s -o /dev/null -D - https://mcp.cynexia.com/mcp` must return 401 before the hostname is trusted; the `Data MCP` uptime-kuma monitor is pinned to exactly `["401"]` so a naked origin alarms ([uptime-kuma.md](uptime-kuma.md)).

In-cluster exposure is unchanged in kind from the 2026-08 sidecar era: flannel does not enforce NetworkPolicy, and upstream `ghcr.io/mnbf9rca/influxdb-mcp-server` (built multi-arch from source — there is no official image) binds `0.0.0.0` with no `--bind` flag, so pod-IP:3000 was reachable from any pod even as a sidecar; the restored Service only re-adds DNS discoverability.
Any in-cluster pod can query InfluxDB read-only through it.
Documented in `influxdb-mcp.yaml`.
Queued: a bind-flag patch, and reinstating a NetworkPolicy if the CNI is ever swapped to Cilium.

**The MCP tools do not reveal the InfluxDB org.**
`query-data` requires an `org` parameter, but nothing tool-visible names it, and bucket discovery is exposed only as MCP resources, which agent frameworks generally do not surface.
Agents therefore need the org name (`cynexia`) supplied out-of-band — a Hermes skill provides it.
With the org known, agents are self-sufficient through `query-data`: `buckets()` lists the readable buckets, and `import "influxdata/influxdb/schema"` with `schema.measurements(bucket: ...)`, `schema.measurementFieldKeys(...)` and `schema.measurementTagKeys(...)` discovers the schema.
Server-side improvements (an org default, a list-buckets tool, MCP instructions) are tracked in this repo's issue #47.

## InfluxDB bootstrap

`make health-influx-bootstrap` creates the buckets, the v1 DBRP mapping and the v1-compat auth user (garmin-grafana needs InfluxDB 1.x-style auth), then prints two scoped tokens — ingester write-only, MCP+Grafana read-only — for one-time manual paste into 1Password.
InfluxDB 2.9 hash-stores tokens server-side, so **the printed value is the only copy, ever**.

Token extraction uses `--json | jq -r .token`, not `--hide-headers` + awk column parsing: the multi-word `-d` description strings shift awk's column and it silently captures a description fragment instead of the token.
`jq` is therefore a hard dependency and is asserted by `make check-tools`.

`make health-influx-cloudflare-bootstrap` does the same job for the Cloudflare analytics bucket.
It creates `cloudflare` with `-r 0` (infinite retention — expiring the copy would defeat the entire point) and prints **two** tokens:

| Token | Paste into | Scope |
|---|---|---|
| Cloudflare ingest | `op://Homelab/health-influxdb/cloudflare-token` | read **and** write on `cloudflare` |
| Replacement read-only | `op://Homelab/health-influxdb/read-token` | read on all four buckets |

The ingest token needs read as well as write because the job's resume point is `max(_time)` read back out of the bucket.

The read token has to be **replaced**, not amended: InfluxDB offers no way to add a bucket to an existing auth, so Grafana and the MCP connector cannot see `cloudflare` until a new token exists.
Order matters — paste, `make apply-homelab`, restart `grafana` and `influxdb-mcp`, and only **then** `influx auth delete` the superseded auth.
Delete it first and you lock Grafana and the connector out until the new Secret has rolled.

## Backups and restore

The `influx-backup` CronJob runs at 02:30 daily, ahead of the 03:00 restic sweep.
Despite the name it is the whole namespace's logical backup pass, and it writes three things:

- a native `influx backup` (14 generations),
- a per-bucket, 8-day-windowed line-protocol export (60 generations, gzip), over an **explicit** bucket list: `apple_metrics apple_workouts garmin cloudflare`, and
- a consistent point-in-time copy of Grafana's SQLite database, `grafana/<date>-grafana.db` (14 generations)

to the `health-dumps` PVC on `local-path`.
Because that PVC lives on the node's SSD, the existing hostPath restic→B2 CronJob picks it up for free — no separate off-cluster wiring needed.

### The Grafana dump

`grafana.db` used to exist in backups only as part of the nightly restic sweep of the live PVC: a file read page by page while Grafana was writing it, so possibly torn, and gated on size alone.
That is enough to survive losing the node and not enough to roll back a bad Grafana major, which migrates the schema in place on first start — reverting the image tag does not revert the database.

`homelab/health/scripts/grafana-sqlite-backup.py` closes that.
It runs in the `influx-backup` pod, not in the grafana pod, because grafana's image has neither `sqlite3` nor `python3`; `local-path` is node-local and ReadWriteOnce is a per-*node* constraint, so the backup pod mounts the `grafana-data` PVC **read-only** at `/grafana` alongside the running Grafana.
The copy is taken with SQLite's online backup API — the same mechanism as the CLI's `.backup`, which takes a read lock, copies whole pages, and restarts itself if a writer commits mid-copy.

Two consequences worth knowing before changing anything:

- **It is Python, not the `sqlite3` CLI.**
  `alpine/k8s:1.36.0` ships no `sqlite3` binary.
  A second image in the pod, or an `apk add sqlite` at 02:30, were both rejected — the health namespace pins every image and a nightly backup should not depend on a package CDN.
  `py3-pip` brings `python3`, Alpine builds `python3` against SQLite, and `Connection.backup()` is the same C API.
- **It depends on Grafana's `wal = false` default.**
  A rollback-journal database opens read-only cleanly; a WAL one needs to create a `-shm` and would fail at open.
  Turning WAL on (`GF_DATABASE_WAL=true`) means the mount in `homelab/health/backups.yaml` must become read-write in the same change, or the dump stops.

Nothing is published unverified.
The copy is written to a `.tmp-` staging file, reopened, and must return exactly `ok` from `PRAGMA integrity_check`, contain schema objects and clear a byte floor before it is `os.replace`d into position — so a failed run leaves last night's artifact intact rather than truncating it.
A `.backup` of an empty or truncated source succeeds and yields a structurally valid, current-mtime, *empty* database, which a freshness-and-size gate cannot tell from a good one; the read-back is what catches it.

The size floor is **measured**: the 2026-08-24 seed run published 2,039,808 bytes and 273 schema objects, so `MIN_BYTES` is 204800 — roughly an order of magnitude below it, the same convention as every other floor in the gate.
The `grafana-dump` row in `homelab/backup/restic-cronjob.yaml` carries the same number; raise the two together.
Each run reports the current size as `grafana_kib=` in the `health-influx-backup` heartbeat.

**Adding a bucket means adding it to that list**, or it is silently never exported — the same class of bug as the VPS backup gate's expected-set assertion ([monitoring.md](monitoring.md)), and the reason the list is explicit rather than a wildcard over `influx bucket list`.
A named bucket that does not exist is now a **named fatal error**: the pipeline `influx bucket list | awk` exits with awk's status, so a failed lookup used to leave the bucket ID empty and sail straight past `set -eu` into an opaque `export-lp` error.
Consequence for ordering: run `make health-influx-cloudflare-bootstrap` **before** the apply that adds `cloudflare` here, or the next night's export fails.

**A new bucket is three edits, not two.**
Create it, add it to the `for B in ...` list in `influx-export-lp.sh`, **and** raise `LP_EXPECTED` in `homelab/health/scripts/influx-backup.sh`.
That last one is the denominator of the `buckets=n/m` the `health-influx-backup` heartbeat carries, and it is a literal because the bucket list lives in the other pod's script and cannot be read from the driver.
Nothing breaks if it drifts — the export already fails by name on a bucket it cannot find, so on the success path n always equals the real count — but a `buckets=5/4` in the heartbeat is the visible tell that somebody edited one and not the other.

**InfluxDB restore drill:** `influx restore --full` self-defeats — it clobbers its own auth mid-restore.
Use scoped `influx restore --bucket <name>` instead.
First drill passed 2026-07-26.
Quarterly drills must also exercise the still-untested disaster-recovery path: `--full` onto a brand-new, never-`setup` instance.

### Restoring Grafana from a dump

Restoring means replacing a file Grafana holds open, so **Grafana must be stopped first**.
Copying over a live `grafana.db` produces a database that is neither the old one nor the new one.

1. **Pick the dump.**
   The last 14 live on the PVC; anything older comes back from restic.

   ```sh
   kubectl -n health get pods -l app=grafana                # note the node, if you care
   kubectl -n health run dumps --rm -it --restart=Never \
     --image=busybox:1.37 --overrides='
       {"spec":{"volumes":[{"name":"d","persistentVolumeClaim":{"claimName":"health-dumps"}}],
        "containers":[{"name":"dumps","image":"busybox:1.37","stdin":true,"tty":true,
        "command":["sh"],"volumeMounts":[{"name":"d","mountPath":"/dumps"}]}]}}' \
     -- sh -c 'ls -l /dumps/grafana'
   ```

   For an older one, restore it out of B2 first — `restic restore <snapshot> --target /restore --include '*_health_health-dumps/grafana/*'` from the backup namespace's job image, per [homelab.md](homelab.md).

2. **Stop Grafana.**
   `replicas: 0` rather than a delete: the Deployment stays, and nothing reopens the database while the file is being swapped.

   ```sh
   kubectl -n health scale deployment/grafana --replicas=0
   kubectl -n health wait --for=delete pod -l app=grafana --timeout=120s
   ```

3. **Replace `grafana.db`.**
   With Grafana stopped, its PVC can be mounted by a throwaway pod that also mounts the dumps PVC.
   Keep the outgoing file — a restore that turns out to be the wrong generation is recoverable only if you did.

   ```sh
   kubectl -n health run grafana-restore --rm -it --restart=Never \
     --image=busybox:1.37 --overrides='
       {"spec":{"volumes":[
          {"name":"g","persistentVolumeClaim":{"claimName":"grafana-data"}},
          {"name":"d","persistentVolumeClaim":{"claimName":"health-dumps"}}],
        "containers":[{"name":"r","image":"busybox:1.37","stdin":true,"tty":true,
        "command":["sh"],"securityContext":{"runAsUser":472,"runAsGroup":472},
        "volumeMounts":[{"name":"g","mountPath":"/var/lib/grafana"},
                        {"name":"d","mountPath":"/dumps","readOnly":true}]}]}}'
   ```

   Then, inside that pod — substituting the dump you chose:

   ```sh
   cd /var/lib/grafana
   mv grafana.db grafana.db.pre-restore
   rm -f grafana.db-wal grafana.db-shm grafana.db-journal   # stale sidecar files
   cp /dumps/grafana/2026-08-24-grafana.db grafana.db
   chown 472:472 grafana.db      # the image runs as uid/gid 472; fsGroup covers the mount, not a new file
   exit
   ```

4. **Start Grafana and verify.**

   ```sh
   kubectl -n health scale deployment/grafana --replicas=1
   kubectl -n health rollout status deployment/grafana --timeout=180s
   kubectl -n health logs deploy/grafana | grep -i 'migrat\|error' | head
   ```

   Then check the things the file actually carries, in the UI at `https://grafana-health.cynexia.net`: **log in** (users and the admin password hash live in this database — the `GF_SECURITY_ADMIN_PASSWORD` env var only resets the admin user at startup), open two or three **dashboards** and confirm panels render, and check **Connections → Data sources**.
   Data sources are provisioned from the `grafana-datasources` Secret, not from the database, so they should be present regardless — if they are not, the problem is the provisioning mount, not the restore.

5. **Clean up** `grafana.db.pre-restore` once the restored instance has been used for a day or two, not before.

**Before a Grafana major upgrade**, take a dump on demand rather than trusting last night's: the migration runs on first start of the new version and is not reversible.
That is what `make health-upgrade` is for — see [Upgrading the health stack](#upgrading-the-health-stack) below.

## Upgrading the health stack

Renovate proposes the image bumps; `make health-upgrade` takes the rollback before you apply them.
Run it from the `cynexia-homelab` context:

    make health-upgrade

It creates a one-off Job from `cronjob/influx-backup`, waits for it, tails the log, then stops.
It applies nothing, merges nothing and edits no pin — checking out the Renovate pull request, rebasing it, reading the diff, applying, verifying, merging and deciding to roll back all stay manual.
The banner it prints is the runbook for the rest, and it is written **deploy-then-merge**: check out the pull request, apply from the branch, confirm the cluster is healthy, and only then merge.
`master` records what has been deployed, never intent.

**It covers both stateful components.**
The CronJob is named for InfluxDB by history, but it takes the Grafana SQLite dump as well, through a read-only mount of the `grafana-data` PVC.
So a health-stack bump has a logical rollback for each.

**It verifies nothing of its own.**
`influx-backup.sh` already asserts its own artifacts — the shipped scripts are non-empty, every expected bucket exists, every prune glob matches something, and the Grafana dump clears its byte and schema-object floors — and it fails the Job if any of that does not hold.
A second, weaker copy of those assertions inside the Makefile would only create a place for the two to disagree, so the target's verdict *is* the Job's exit status.

**Those are existence checks, so they say nothing about age.**
A stale dump satisfies every one of them.
Artifact freshness is checked thirty minutes later by a different Job — this CronJob runs at 02:30 and the restic gate at 03:00, with its 30-hour window described in [monitoring.md](monitoring.md) — so a `health-upgrade` that passes proves the dump exists and is well-formed, not that anything upstream is still producing data.

**Where the numbers are — and they moved on August 26, 2026.**
They used to be in the healthchecks.io ping body only.
The heartbeat that replaced it is one line and carries only `verdict=`, `buckets=n/m` and `grafana_kib=`, so the exit trap now also prints a `detail:` line to the pod log carrying everything the body did: the native-dump size (`native_kib=`, `native_mib=`), the line-protocol size and file count (`lp_kib=`, `lp_files=`, one export per bucket) and the three prune counts.
`make health-upgrade` tails that log, so the numbers are in front of you rather than in a third party's Events page.

**On failure, the log ends with whatever failed.**
That is a `FATAL:` line when `influx-backup.sh` or one of the scripts it runs recognises the fault and names it, and the underlying tool's own error — kubectl's, influx's — when it does not.
Do not expect a particular shape: read the tail.
The heartbeat carries `failed_step=` whenever the script exited normally, which is every failure except a kill — an out-of-memory kill, a node eviction or the active deadline leaves no exit trap to run and so no push at all, which is what turns the monitor DOWN by silence instead.

**A Grafana major is not a tag revert.**
Grafana migrates `grafana.db` in place on first start, so rolling back a failed major means restoring the dump this target took, not changing the tag back.
Read the restore runbook above before you start one.

**A manual dump pushes to `health-influx-backup`.**
The Job inherits the CronJob's pod spec, push URL included, so the monitor's heartbeat history shows the manual run alongside the nightly ones.
That is expected; do not read it as an out-of-schedule nightly backup.

**The wait is 600 seconds, and that number is measured.**
The two retained nightly runs took 26 and 25 seconds start to completion, and a timed run of the target itself took 27 — a manual `--from=cronjob/` Job behaves like a scheduled one.
So the wait is roughly twenty times the observed span — room for a cold image pull and for years of growth, and still far below the CronJob's own `activeDeadlineSeconds` of 3600, which is what kills a hung run.
If the wait expires, the target says so, leaves the Job in place to be inspected, and exits non-zero.
It never reports a dump it did not watch finish.

**It refuses to start if a dump is already running, whatever it is called.**
`concurrencyPolicy: Forbid` governs only the Jobs the CronJob itself creates, so the target does its own check — and it filters on the Job's `ownerReferences`, not on its name.
`kubectl create job --from=cronjob/influx-backup` sets that owner and a `cronjob.kubernetes.io/instantiate: manual` annotation, so a by-hand dump is as visible to the guard as a nightly one and you can name it anything.
The one thing it cannot see is a Job someone hand-rolls with a copied pod spec and no owner reference, which no procedure here produces.

**Other pinned images in this namespace have no independent rollback story** — the apple-health-ingester, garmin-fetch-data, influxdb-mcp and the cloudflared sidecar are all stateless, so their rollback *is* a tag revert and they need no dump.
Only InfluxDB and Grafana hold state here.

## Cloudflare analytics ingest

`homelab/health/cloudflare-analytics.yaml`.
Hourly CronJob at `37 * * * *` that copies Cloudflare edge traffic data into the `cloudflare` bucket before Cloudflare deletes it.

Cloudflare's Free plan keeps **8 days** of per-hostname analytics and rejects any GraphQL query wider than **1 day**.
That window answers "what is happening right now" and is useless for "was this normal?"
— the question a webshell sweep raises.
This job is the retention fix; nothing about the Cloudflare configuration changes, and the token is read-only.

### Shape

| | |
|---|---|
| Zones | `cynexia.com` and `making-tracks.app` |
| Dataset | `httpRequestsAdaptiveGroups`, grouped by `datetimeHour` |
| Bucket | `cloudflare`, **infinite** retention, raw hourly rows, no downsampling |
| Measurement | `http_requests`; tags `zone`, `host`, `path`, `status`, `country`; fields `count`, `sample_interval` |
| Monitoring | The `homelab-cloudflare-analytics` uptime-kuma push monitor: `up` on exit 0, `down` otherwise |

Two bookkeeping measurements share the bucket: `ingest_status` (one point per committed chunk) and `ingest_gap` (see below).

### Why the script is Python

Scheduled work in this repo defaults to POSIX `sh` in a mounted script file.
This job is Python, for three reasons, each a failure mode this repo has already hit:

- **Cloudflare answers a failed query with HTTP 200 and an `errors` array in the body.**
  Telling that apart from "no traffic" by grepping JSON in `sh` is precisely the shape that made `ingest-freshness` report STALE for 25 days.
- Rows must be **aggregated in memory** before writing.
  Path truncation merges several source paths into one series and row-cap subdivision splits one hour across several responses; both need keyed summation.
- Tag values are **user-controlled URL paths** and need real line-protocol escaping.

Standard library only, so there is no `pip install` at run time and the job depends on nothing but the two APIs it talks to.

### The resume rule

The watermark is `max(_time)` over the `cloudflare` bucket, read back from InfluxDB on every run.
There is no state file, no PVC and no ConfigMap cursor, because all three can disagree with what was stored — after a restore, after a manual delete, after a partial write.
The data is its own watermark and cannot drift from itself.

Every run then rewinds **2 hours** behind that watermark, because the final hour of the previous run was almost certainly still in progress when it was written.
Re-ingestion is free: same measurement, same tag set and same timestamp overwrite in InfluxDB.

Backfill runs in **23-hour chunks** (Cloudflare rejects anything wider than a day), at most **8 chunks per run**.
Chunks are committed oldest-first and only when *every* zone succeeded for that chunk — commit a chunk in which one zone failed and the watermark jumps past hours that zone never covered, which Cloudflare then deletes.

A successfully-queried chunk with **zero rows** still writes its `ingest_status` point.
Without that, eight genuinely quiet days would look identical to eight days of broken ingestion and would trip the gap alarm below for no reason.

**A chunk's points are written oldest-first, and that ordering is part of the resume rule.**
A chunk over 5,000 series is sent to InfluxDB in several batches, so a later batch can fail with earlier ones already durably stored.
Because the watermark is `max(_time)` over what *is* stored and the next run rewinds only 2 hours behind it, a surviving partial write has to be a **prefix** of the chunk in time.
Ordered any other way — the code once sorted on the tag tuple, zone first — a surviving first batch could carry points from the last hour of a 23-hour chunk while its first hours went unwritten; the watermark would jump past them, the 2-hour rewind would fall short, and Cloudflare would delete them.
The `ingest_status` marker is appended after every data point for the same reason.

### Gaps are permanent, so they are loud

If the rewound start is older than Cloudflare's retention, those hours are gone and no future run can recover them.
The job then:

1. logs the exact missing range,
2. writes an `ingest_gap` point (fields `missing_hours`, `gap_end`) timestamped at the gap start, so the hole is visible in Grafana instead of reading as a quiet week, and
3. **exits non-zero**, so `homelab-cloudflare-analytics` is pushed DOWN with `verdict=gap`.

It still ingests everything that *is* still available in the same run.
The alarm fires once: the next run's watermark is current again, which is the intended behaviour — a permanent hole should be recorded permanently, not re-alerted hourly.

`ingest_gap` deliberately uses a field named `missing_hours`, not `count`.
The watermark query filters on `_field == "count"`, so a gap marker can never advance the watermark and claim the hole was filled.

To surface gaps in Grafana, add an **annotation** query on `ingest_gap` to the Cloudflare dashboard.
A panel over `http_requests` alone will not show them.

### Cardinality

Path is by far the highest-cardinality dimension — karakeep alone emits a distinct path per asset UUID — so paths are truncated to their first two segments, with `/*` appended when segments were dropped (`/api/v1` and `/api/v1/*` stay distinguishable).
Hosts listed in the CronJob's `FULL_PATH_HOSTS` env var keep their full path.
It is empty by default; every host added there trades series cardinality for path detail.

### Sampling

`sample_interval` is stored per point and **never applied**.
Whether Cloudflare's `count` is already extrapolated is a property of the dataset, not something this job should silently assume, and a chart that quietly switches from real counts to estimates is the kind of lie this repo has a rule about.
Observed values are 1.03–1.14 — effectively unsampled at this volume.
**Confirm the relationship once against the Cloudflare dashboard for a known hour before building any panel that multiplies by it.**

### Row-cap subdivision

`httpRequestsAdaptiveGroups` caps a response at 10,000 rows and this dataset offers no cursor.
A chunk returning exactly the cap is assumed truncated, halved, and re-queried — recursively, down to a 1-minute floor.
Aggregation makes the halves recombine into the same per-hour totals.
A run is capped at 180 GraphQL calls; exhausting that budget is a loud failure, not a silent truncation, so the watermark stays put and the next run retries.
Cloudflare's user limit is 300 queries per 5 minutes and burning it would break the next several hourly runs too.

### First-run setup

The job cannot run until four things exist.
None of them are created by `make apply-homelab`.

1. **Cloudflare API token**, scoped **`Zone.Analytics: Read` only**, covering both zones.
   The job never writes to Cloudflare, so any edit scope is blast radius bought for nothing.
   Store as `op://Homelab/cloudflare/api-token`.
2. **Zone tags**, as one field `op://Homelab/cloudflare/zone-ids` holding `cynexia.com=<zoneid>,making-tracks.app=<zoneid>`.
   Zone IDs are not passwords, but they identify the account and this repo is public, so they are resolved at apply time like everything else.
   Mark it `[text]`, not concealed — it is an identifier, and a concealed value makes the vault harder to debug.
3. **uptime-kuma push monitor** `homelab-cloudflare-analytics`, 3600s interval with one retry at 7200s.
   Token into `op://Homelab/health-healthchecks/cloudflare-kuma-push-token`.
4. **InfluxDB bucket and token**: `make health-influx-cloudflare-bootstrap`.
   See below.

Then `make apply-homelab`, and force the first run rather than waiting an hour:

```bash
kubectl -n health create job --from=cronjob/cloudflare-analytics cf-analytics-manual
kubectl -n health logs job/cf-analytics-manual
```

The first run seeds from the retention floor (~8 days back) and reports no gap, because nothing was ever lost.
Read the log: it names every chunk, the row count per zone, and the GraphQL budget consumed.

**Smoke-test the query shape on that first run.**
The `avg { sampleInterval }` selection is taken from the GraphQL schema, not from a doc page that spells it out; if Cloudflare names it differently the run fails loudly with the `errors` array in the log, and the fix is one line in `homelab/health/scripts/cloudflare-analytics-ingest.py`.
It cannot fail silently.

## Garmin re-authentication (annual)

Tokens on the `garmin-tokens` PVC last roughly a year.
When they expire:

1. **Scale `garmin-grafana` to 0 first.**
   A crashlooping pod with an expired token fires a multi-factor-authentication SMS at the operator on every restart.
2. Run the interactive login pod.
   It needs `enableServiceLinks: false` — the influxdb Service's injected `INFLUXDB_PORT=tcp://...` otherwise crashes the script's `int()` parse — plus the full InfluxDB v1 env block, because the script demo-writes to InfluxDB before it shows the login prompt.
3. Scale back to 1.

**Keep `replicas: 0` committed while paused** — `make apply-homelab` resurrects any uncommitted scale-down.

## Why probes exist here (2026-08-18 Pomerium wedge)

> **Historical.**
> Pomerium was removed 2026-08-22 — this failure mode and its custom probe target no longer exist.
> The section stays because it is why every HTTP-serving workload in this namespace carries probes.

Do not strip the liveness/readiness probes on the health workloads: they were added in response to a real, silent 18.5-hour outage.

On **2026-08-18 at 20:57Z** the Pomerium pod (all-in-one, v0.33.0) stopped serving HTTP entirely while its container stayed `Running`/`Ready` with **0 restarts for 12 days**.
Its control-plane goroutines kept running — the identity-manager logged "updating user info" every 10 minutes throughout — but every request, including Pomerium's own `/.well-known/*` endpoints, timed out having returned zero bytes.
This was verified from inside the cluster against the Service, so it was not a tunnel or edge problem; the MCP sidecar upstream was healthy the whole time.

Nothing noticed for 18.5 hours, because the `pomerium` container had no liveness or readiness probe: Kubernetes considered a process that answered no requests to be healthy, and this namespace's scheduled checks watch *data freshness*, not the auth proxy.

A `kubectl rollout restart` restored service immediately — 401 in 0.86s afterwards versus a 20s hang before.
**Root cause of the wedge itself is not established**; treat it as unknown rather than assuming a specific Pomerium bug.
The lesson that *is* established is the failure mode: process-alive is not service-alive, so anything in this namespace that serves HTTP needs a probe that actually exercises an HTTP endpoint.

### The probe target is deliberately not the documented one

Pomerium's liveness probe here is **`/ping` on :80**, not the `/healthz` on :28080 that Pomerium's docs and its Ingress Controller use.
This is not an oversight, and "correcting" it re-creates the blind spot:

- `:28080` is a plain Go listener with **Envoy nowhere in its path**.
  Its `envoy.server` field is a ≤30s-stale cache of the Envoy admin thread reporting lifecycle state LIVE — which it was for all 18.5 hours.
  The documented probe would have stayed green throughout.
- `:80/ping` traverses listener → worker → HTTP connection manager → ext_authz → control-plane cluster: the exact path that returned zero bytes.
  It answered 200 in 6.7 ms unauthenticated when tested, on Envoy's catch-all vhost, so the probe needs no `host:` field (kubelet dials the pod IP).
- Readiness on `:28080/readyz` is kept as a *complement*, catching databroker/config-sync failures `/ping` cannot see.
  It required adding `health_check_addr: :28080` to the ConfigMap — the default `127.0.0.1:28080` is unreachable by kubelet, which also makes the upstream example probe broken as written.
- A startup probe on `/startupz` (5-minute budget) keeps the tight liveness (`periodSeconds: 15`, `failureThreshold: 4` ≈ 60 s to restart) from firing during databroker sync.

Full policy and the cross-cluster probe inventory: [monitoring.md](monitoring.md).

**Upstream status:** this failure matches no known issue across Pomerium v0.32–v0.34 (searched issues and merged PRs for unresponsive/stuck/hang/deadlock/goroutine-leak/MCP-hang, plus every issue opened since 2026-01-01).
It may be unreported.
One **unconfirmed** hypothesis worth attaching if it recurs: v0.33 added an ext_proc filter for MCP response interception, enabled per-route on MCP routes, opening a gRPC stream per MCP request with `MessageTimeout` = 10s and **no `failure_mode_allow`** — a stream leak there is a plausible resource-exhaustion path on an MCP-only deployment.
There is no evidence that is what happened.
Pre-restart logs and the pod description are in the 2026-08-18 session scratchpad for comparison.

## Monitoring

Three uptime-kuma **push** monitors; tokens in 1Password item `health-healthchecks`.
They replaced four healthchecks.io checks on August 26, 2026 — the two ingest checks merged, because one CronJob checks both buckets in one process and two monitors would have been one signal counted twice.
The names were kept so the estate reads as one inventory across the change.
Roster and per-monitor settings: [uptime-kuma.md](uptime-kuma.md#push-monitors).

| Monitor | Interval / retry | Signals failure by |
|---|---|---|
| `health-ingest` | 1d / 12h | silence |
| `health-influx-backup` | 1d / 6h | **a `down` push from an EXIT trap** |
| `homelab-cloudflare-analytics` | 1h / 2h | **a `down` push from the exit path** |

**`health-ingest` signals failure by silence; the other two do not.**

- `influx-backup` pushes `up` or `down` from an EXIT trap, so a failure is DOWN within a minute and is distinguishable from a never-scheduled run.
  It did not always: the report used to be the script's last statement under `set -eu`, so a failing prune, a missing ConfigMap key or a dead influxdb pod produced *exactly nothing* until the silence bound expired some 30 hours later.
  The accepted cost of the conversion is that a transient fault — an influxdb pod mid-restart when `kubectl exec` lands — now alerts instead of self-healing into silence.
  The heartbeat carries `verdict=`, `buckets=n/m` and `grafana_kib=`; the pod log's `detail:` line carries every size and prune count.
  `ttlSecondsAfterFinished` is 48h so the Job's own logs outlive a weekend.
- `ingest-freshness` (every 6h) pushes `up` **only when BOTH buckets hold InfluxDB data less than 24h old**, and pushes nothing at all otherwise — so a real ingest gap surfaces as an absent heartbeat instead of being masked by an unrelated cron firing on schedule.
  It **always exits 0 on purpose**: the signal is the absent push, not a failed Job.
  Do not "fix" it into a non-zero exit, and do not give it a `down` path — that would trade a 36-hour tolerance for a 6-hour one on a signal that depends on the operator syncing a watch.
  Every `up` push carries `apple_age_h=` and `garmin_age_h=`, which is the merged monitor's only per-path resolution: the last message before the silence names which bucket was ageing.
  The pod log carries the full per-bucket verdict and keeps "stale" apart from "query failed", which the monitor's one bit cannot.
- `cloudflare-analytics` (hourly) is Python and pushes `up` on rc 0 and `down` otherwise, the unrecoverable-gap path included, so a failure is distinguishable from a never-scheduled run without waiting for the silence bound.
  Pushes are best-effort and can never fail the job.

**There is no `/start` equivalent on the push API, and none of these three has one.**
A push is a heartbeat carrying a status, so `activeDeadlineSeconds` is the whole of the hang bound and the monitor's interval plus retry is the silence bound.

All three are bounded by `timeZone: "UTC"` and `activeDeadlineSeconds` (3600 for `influx-backup`, 300 for `ingest-freshness`, 1200 for `cloudflare-analytics`) — with `concurrencyPolicy: Forbid` and no deadline, one hung run blocks every subsequent run with nothing alerting.
`influx-backup` sets `startingDeadlineSeconds: 3600` and `cloudflare-analytics` 1800; `ingest-freshness` deliberately does not, since it runs again in six hours.

This namespace's monitors watch **data freshness**, not the edge — which is why the 2026-08-18 Pomerium wedge went unnoticed (that proxy has since been removed).
External availability of `mcp.cynexia.com` and the other tunnel hostnames is layer 3, in [uptime-kuma.md](uptime-kuma.md#monitor-list).

## Secret rotation

Per `health-*` 1Password item: edit the item → `make apply-homelab` → restart the consuming pod.
**No `direnv reload` step**: secrets are resolved per command by `op run` at apply time, so nothing is cached in your shell to refresh (reload only matters if `OP_SERVICE_ACCOUNT_TOKEN` itself changed).
See [apply-workflow.md](apply-workflow.md#rotating-a-secret).

InfluxDB tokens specifically: mint the replacement with the `health-influx-bootstrap` pattern, update 1Password, apply, then delete the old auth server-side.

If a real secret value is ever disclosed, log it in `secrets-to-rotate.md` at the repo root — see the honesty-box rule in `AGENTS.md`.

## Known state

**Verified working 2026-07-26:**

- The Claude.ai connector — read queries succeed, and a write probe correctly 403s (the MCP read-token has no write scope; server-log-verified).
- The HAE ingest path: `https://hae.cynexia.com/api/healthautoexport/v1/influxdb/ingest?target=iphone-rob`, bearer token `op://Homelab/health-hae/auth-token`, JSON, Batch Requests ON for large exports.
  Hourly aggregates cover 2020–2025, raw data from 2026-01-01.
  Keep the same URL and tags on every export or you get duplicate series.

**Verified working 2026-08-22:** the Access Managed OAuth path — unauthenticated `GET /mcp` 401s at the edge with a `resource_metadata` pointer, and the advertised discovery chain serves Access metadata with a `registration_endpoint`.
The claude.ai connector is reconnected through the one-time-PIN flow and verified 2026-08-22: reads return data.

**Resolved 2026-08-23:** the Hermes registration failure open on 2026-08-22 had two causes, fixed on successive days: the missing DCR allowlist entry for the Hermes callback (fixed 2026-08-22), then the IP-bypass Access policy suppressing the 401 that bootstraps the OAuth flow (fixed 2026-08-23 by the bypass-policy split — see [MCP behind Cloudflare Access](#mcp-behind-cloudflare-access)).

**Tech debt / deferred:**

- Garmin points can't carry a `person` tag (upstream limitation of the v1-compat write path); Apple points get a hardcoded `person=rob` static tag instead of a real multi-person model.
  The Phase 2 facade / person-registry design is expected to absorb this.
- Cloudflare Access service-token in front of the tunnel hostnames (the bearer token plus the Access app's email policy suffices for now; also the path to true end-to-end `Data MCP` monitoring — see [uptime-kuma.md](uptime-kuma.md)).
- Grafana alert rules (Phase 3, pending data accumulation).
- PSA hardening from `baseline` to `restricted`.
