# uptime-kuma runbook

uptime-kuma on the VPS cluster is layer 3 of the four detection layers in [monitoring.md](monitoring.md#the-four-layers): the external check that sees a tunnel or edge failure that no in-pod probe can.
This file is the procedure for maintaining it.
The policy — why the layer exists, and what it still does not catch — stays in [monitoring.md](monitoring.md).

**Create monitors by hand in the UI and record them here.** uptime-kuma v2 offers no supported programmatic path: monitor CRUD is Socket.IO only, the one API-key-protected HTTP route is `/metrics`, the REST API issue (#118) has been open since 2021 with two bridge PRs closed unmerged, and the community Python wrapper stops at v1.23.2.

To read the monitor inventory, query `kuma.db` read-only:

```bash
kubectl -n vps exec deploy/uptime-kuma -- \
  sqlite3 -readonly /app/data/kuma.db 'select name, url, type, active from monitor'
```

**`/metrics` omits monitors created after the process started, until it restarts.**
Using `/metrics` as an inventory produces a wrong answer; `kuma.db` is the reliable source.
The quiesce sidecar backs up `kuma.db` nightly, so a rebuild restores the monitors.

## Settings for every HTTP monitor

| Field | Value | Why |
|---|---|---|
| Monitor type | HTTP(s) | — |
| Heartbeat interval | 60s | `blog.cynexia.com` uses 15s; every other monitor uses 60s |
| Retries | 3 | The default of 0 alerts on a single blip |
| Heartbeat retry interval | 60s | — |
| Request timeout | 20s | Separates "slow" from "wedged" |
| Max redirects | **0** | Defeats the Access trap below. Required on every Access-protected monitor; the monitors on hosts with no Access app keep the default |
| Accepted status codes | per monitor | — |
| Certificate expiry, ignore TLS | defaults | TLS terminates at the Cloudflare edge |

Skip keyword monitors. uptime-kuma evaluates the keyword only after the status check passes, so a keyword adds nothing, and `saveErrorResponse` already captures Cloudflare's error body into the alert, which makes a `1033` diagnosable.

## The Cloudflare Access trap

An Access-protected hostname answers an unauthenticated request with a 302 to the Cloudflare login page.
At the default `maxredirects: 10`, the monitor follows it, gets 200 from Cloudflare's login app, and reports UP while the tunnel, pod and node are all dead.

Two mitigations are in place: every Access-protected monitor sets `maxredirects: 0`, and the four monitors whose Access app demands a credential also send service-token headers, so the request reaches the origin (`proxy.cynexia.com` is the deliberate exception — see below).

The `Uptime` service token authenticates against the `service-auth-monitoring` Access policy, which is attached to exactly four apps: `Umami analytics`, `changedetection`, `hermes` and `n8n`.
The token was created on August 25, 2026 and expires on **August 24, 2031**.
On expiry all four monitors fail at once with a 302.
Four simultaneous reds therefore mean expired or wrong headers, not an outage — check the headers first.

A **second** service token exists and is deliberately separate: `vps-proxy-access`, created September 2, 2026, expiring **September 1, 2031**.
It is attached to the app-scoped `service-auth-homelab-proxy` policy on the `homelab-proxy` application, which gates `proxy.cynexia.com`, and to nothing else.
Reusing `Uptime` here would have widened four monitors' credential to cover an open proxy.
When it expires, the residential egress proxy breaks and every proxied changedetection watch errors — no monitor turns red, because the `proxy.cynexia.com` monitor asserts the Access challenge, which a dead token does not change.

Paste the headers as JSON in the monitor's **Headers** box:

```json
{
  "CF-Access-Client-Id": "<op://VPS/uptime-kuma/CF-Access-Client-Id>",
  "CF-Access-Client-Secret": "<op://VPS/uptime-kuma/CF-Access-Client-Secret>"
}
```

Read the values with `op read` as you paste them; never write them into this repo.
A wrong or missing header produces a 302, which fails the monitor — the correct, loud outcome.

**The `rss.cynexia.com` and `Karakeep` monitors need no headers.**
Path-scoped Access apps (`freshrss api`, `karakeep api`) serve those two URLs, and both apps carry the anonymous `bypass` policy, so each returns 200 with no credential.
Verified August 25, 2026 by requesting both without headers.
An earlier version of this file claimed one token covered `analytics`, `rss`, `keep`, `watch` and `n8n`.
That was wrong: an IP bypass policy supplied the access, and that policy was deleted on August 25, 2026.

On an app that does demand a credential, a bypass path is no substitute for the token: the glob `/foo/*` does not match bare `/foo`, so a bypassed health path needs both destinations.

### The push path is bypassed at the edge

Every push monitor in this estate is driven by something that holds no Access credential — a CronJob inside a cluster, or, for `hermes-app-alive`, a cron job inside the hermes agent on the off-cluster VM — so without a bypass the edge answers 302 and no push monitor could ever report UP.
An Access application named **`uptime-kuma push`** carries that bypass, created **August 26, 2026**.
It covers two destinations: `uptime.cynexia.com/api/push/*` and the bare `uptime.cynexia.com/api/push`.
The wildcard is the load-bearing one — a push URL always carries its token as a path segment, so every real request matches it — and the bare form is present only because `/foo/*` does not match bare `/foo`, so the pair is written together and neither is "tidied" away later.

It is attached to the **existing reusable bypass policy** (`b8dbe397-8b45-44ca-a57a-4131e82cb3a1`) rather than a new one, matching the four apps already there, with a 24h session, App Launcher hidden and no IdPs.
Application id `ff3b2581-1975-48d1-866c-e02e8d2e0593`.
Nothing else on this host is bypassed: `/api/push/<token>` accepts a heartbeat and exposes no dashboard, no monitor list and no settings.
The authoritative bypass inventory is the Access-bypass table in [vps.md](vps.md#cloudflare-access-bypasses); this section is the operational note.

Verified at creation from outside both clusters, and worth repeating after any Access change from a network with no Access session:

```bash
# A bogus token: the bypass must let the request REACH kuma, which then rejects it.
curl -s -o /dev/null -w '%{http_code}\n' 'https://uptime.cynexia.com/api/push/notarealtoken'
curl -s -o /dev/null -w '%{http_code}\n' 'https://uptime.cynexia.com/api/push'
curl -s -o /dev/null -w '%{http_code}\n' 'https://uptime.cynexia.com/dashboard'
```

Observed on August 26, 2026: the bogus token returned **404** — the request reached kuma and kuma rejected the token, which is the proof the bypass works — and the bare path returned **200**.
`/dashboard`, `/` and `/api/status-page/heartbeat/x` all returned **302**, proving the rest of the host is still gated.
A `302` on the push path means the bypass is missing, scoped to the wrong destination, or written as the bare path only.

`mcp.cynexia.com` (homelab) is also Access-protected but deliberately carries no service token and no bypass policy — its monitor expects the edge's 401 itself (see the monitor list), and `maxredirects: 0` still applies.

## Monitor list

Each path mirrors the service's in-pod probe target, so a monitor failing while the probe passes isolates the fault to the tunnel or the edge.
`uptime.cynexia.com` is absent on purpose: uptime-kuma checking its own hostname reports nothing it can deliver.

Monitor names below match `kuma.db` as of August 25, 2026.
The Access column names the application that answers the URL and the policies attached to it, so you can tell a credential fault from an outage without opening the dashboard.

VPS cluster, Access-protected:

| Monitor | URL | Access app and policies | Accepted status codes |
|---|---|---|---|
| `analytics.cynexia.com` | `https://analytics.cynexia.com/api/heartbeat` | `Umami analytics`: `service-auth-monitoring`, `allow_cynexia_com` | `["200-299"]` |
| `watch.cynexia.com` | `https://watch.cynexia.com/` | `changedetection`: `service-auth-monitoring`, `allow_cynexia_com` | `["200-299"]`; add `302` if you enable changedetection's password |
| `n8n.cynexia.com` | `https://n8n.cynexia.com/healthz` | `n8n`: `service-auth-monitoring`, `allow_cynexia_com` | `["200-299"]` |
| `rss.cynexia.com` | `https://rss.cynexia.com/api/` | `freshrss api`: `bypass` — send no headers | `["200-299"]` |
| `Karakeep` | `https://keep.cynexia.com/api/health` | `karakeep api`: `bypass` — send no headers | `["200-299"]` |

The first three carry the service-token headers.
The last two must not: their apps admit anonymous requests, and adding headers there would imply a credential that nothing checks.

Homelab health tunnel:

| Monitor | URL | Access app and policies | Accepted status codes |
|---|---|---|---|
| `Data MCP` | `https://mcp.cynexia.com/mcp` | `health-data-mcp`: `allow_cynexia_com` | exactly `["401"]` — see below |
| `hae.cynexia.com` | `https://hae.cynexia.com/` | none | `["401"]` |
| `hermes` | `https://hermes.cynexia.com/api/health` | `hermes`: `service-auth-monitoring`, `allow_cynexia_com` | `["200-299"]` — see below |
| `proxy.cynexia.com` | `https://proxy.cynexia.com/` | `homelab-proxy`: `service-auth-homelab-proxy` — send no headers | exactly `["302"]` — see below |

Not Access-protected, and unrelated to either cluster's tunnels:

| Monitor | URL | Note |
|---|---|---|
| `blog.cynexia.com` | `https://blog.cynexia.com` | 15s interval, the only monitor that departs from 60s |
| `family-foqos.app` | `https://family-foqos.app` | — |
| `recordwell.app website` | `https://recordwell.app/` | — |
| `Auth API health` | `https://api.recordwell.app/health/ready` | — |
| `auth API live` | `https://api.recordwell.app/health/live` | — |
| `recordwell` | `https://` | **Broken.** The URL is a bare scheme with no host. Fix it or delete the monitor |

The `hermes` monitor probes the Hermes dashboard on the hermes VM — the tunnel's one off-cluster origin.
A 200 proves edge, tunnel, cloudflared and the dashboard process end to end.
`/api/health` is on the dashboard's unauthenticated allowlist, so the origin asks for nothing; the credential this monitor needs is the one Access asks for.

The health tunnel publishes a fourth hostname, `hermes-app.cynexia.com` (hermes-webui, for the Hermex iOS app).
**That hostname still has no monitor, by decision.**
What changed on August 26, 2026 is that the service behind it is now checked from *inside* the VM instead, once a day, by the `hermes-app-alive` push monitor below.

**Do not "improve" that push monitor into a GET against this hostname.**
An HTTP monitor is the wrong instrument here for two independent reasons.
`GET /health` returns `status: ok` straight through the broken-venv failure worth catching — the unit stays `active` and the endpoint stays green while every chat turn answers `AIAgent not available` — so the check would be green over the outage it exists for.
And the chat path needs a login session, which a monitor cannot perform.
The daily check sidesteps both by running on the VM: it deep-imports `run_agent` from the shared venv, the assertion the HTTP surface cannot make.

If a monitor is ever added here anyway, it is not a copy of the `hermes` monitor: this Access app authenticates every request with Service Auth, so the monitor must send the service-token headers and set `maxredirects: 0`.
Residuals are in [monitoring.md](monitoring.md#what-this-does-not-catch); the check's own triage is in [hermes-vm.md](hermes-vm.md#reading-a-down-hermes-app-alive).

**The triage here inverted on August 25, 2026.**
The monitor used to reach the origin because it probes from the VPS's Hetzner IP, which an Access bypass policy admitted, and a 302 or 401 therefore meant that policy had lost the VPS IP.
That bypass is deleted.
The monitor now presents the `Uptime` service-token headers and matches the `service-auth-monitoring` policy, so a 302 or 401 means the headers are wrong, missing or expired.
Check the headers first; the token expires on August 24, 2031, and its expiry fails this monitor together with `analytics.cynexia.com`, `watch.cynexia.com` and `n8n.cynexia.com`.
Do not widen the accepted set to swallow the 302.

`hae.cynexia.com`'s fast 401 is a true end-to-end signal: the hostname has no Access app, so the 401 comes from the origin pod, proving the tunnel, cloudflared and the pod all serve — exactly what was false during the 2026-08-18 Pomerium wedge.

**`proxy.cynexia.com` exists to be refused, and its accepted set must stay pinned.**
The monitor sends no service-token headers and sets `maxredirects: 0`, so the edge answers it with the Access challenge and it never reaches the origin.
Admitting it would defeat it: the origin is tinyproxy, which authenticates nobody, so a deleted or disabled Access application does not close the hostname — it publishes an open HTTP proxy egressing from the operator's home address.
This monitor is the only thing in the estate that detects that state, and it detects it by going DOWN when the challenge stops arriving.
Never widen the set to include `200` or `400`: those are what a naked tinyproxy answers.
The pinned code is the one observed at rollout on September 2, 2026; if the edge ever changes it, re-confirm with one unauthenticated `curl` and re-pin, rather than widening.

**`Data MCP` is edge-only, by decision.**
`mcp.cynexia.com` sits behind Cloudflare Access (Managed OAuth), which answers the unauthenticated probe at the edge, before the tunnel — so this monitor no longer proves the tunnel or the pod.
The accepted set is pinned to exactly `["401"]` on purpose and must never be widened: an outage (timeout, 5xx, `1033`) falls outside it, and so does a 2xx/404 from a naked origin — which is what a deleted or disabled Access app looks like, because that gate fails OPEN ([homelab-health.md](homelab-health.md#mcp-behind-cloudflare-access)).

**The mcp.cynexia.com Access app carries no bypass policy — keep it that way.**
Unlike the other monitored hostnames, the app holds only the `allow_cynexia_com` policy.
The edge's 401 does two jobs: it is the status this monitor pins, and it is what starts every MCP client's OAuth flow — the MCP SDK begins OAuth only after a 401, so a bypassed client gets 200 from the origin and no flow ever starts.
An IP bypass breaks both: it broke Hermes profile OAuth until the bypass was removed on 2026-08-23, and it would let this monitor's probe (which egresses from the Hetzner IP) reach the origin, so the monitor could never return its pinned 401.
Do not re-attach any bypass policy to this app.

**The monitor must send `{"Accept": "application/json"}` in its Headers field.**
Access decides browser-vs-client on the `Accept` header: uptime-kuma's default is browser-like (`text/html,…`), so Access classifies the probe as a browser and answers `302` to the login page instead of the non-browser `401` — verified 2026-08-22 with two curls differing only in that header.
Without the custom header the monitor stays DOWN forever against a perfectly healthy edge.
Do not "fix" that by accepting `302` — keep the pinned `["401"]` and fix the header, so the accepted set keeps meaning "Access challenged a non-browser client".

Accepted residual: the mcp tunnel route and the `influxdb-mcp` HTTP handler have no external monitor, so a wedged handler surfaces only when a client fails.
A service token plus a Service Auth policy would close that gap.

That option stays declined, but one of its three original reasons no longer holds.
Two still do: `hae.cynexia.com` proves tunnel and cloudflared end to end, and the pod has in-cluster TCP probes.
The third — that a service token means a standing secret in uptime-kuma's config — was overtaken on August 25, 2026, when the `Uptime` token was introduced for four VPS apps.
The estate now holds such a secret deliberately: the arrangement it replaced granted nine apps to every VPS pod and logged nothing, whereas the token opens four apps and logs every use, and `uptime.cynexia.com` now sits behind `allow_cynexia_com` so the UI that renders the secret is no longer public.
Adding the same arrangement to `mcp.cynexia.com` is a live option, not a closed one.

**The `Data MCP` monitor still must not get a token**, whatever is decided about the tunnel-route gap.
Its pinned `["401"]` depends on the edge challenging the probe, and a credential that reached the origin would make the pinned status unreachable.

Before you add a status code to any set, observe it once:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://hae.cynexia.com/
```

Widening a set to swallow whatever appears stops the monitor being a monitor.

## Push monitors

A push monitor receives a heartbeat instead of sending a request, which is what lets a job inside a cluster drive it without exposing anything.
Each token lives in 1Password and reaches its manifest through the `op run` + envsubst pipeline, typed `[text]` because it is a tier-2 spam-target identifier and not a secret: holding one lets a stranger push a heartbeat and mask a real failure, and grants nothing else.
So it stays out of the public repository, needs no rotation, and earns no honesty-box row if it turns up in a transcript or a pod log.

There is **no `/start` equivalent** on this API and there must not be a synthetic one: a push is a heartbeat carrying a status.
The hang bound is the job's own `activeDeadlineSeconds`; the silence bound is the interval plus retries below.

**Almost every push in this estate is made from inside a cluster, outbound, through the Cloudflare Access bypass described above.**
That outbound direction is what lets the private homelab cluster — which uptime-kuma cannot reach, because it probes from a Hetzner IP and every `*.cynexia.net` name resolves to a LAN address — report to a monitor at all.
It is also why the bypass is load-bearing rather than a convenience: without it every push monitor here would be permanently DOWN.
The one exception to "from inside a cluster", `hermes-app-alive`, is described below the table — and it needs the same bypass, from further away.

**Some monitors deliberately receive nothing on some runs, so silence is not always a fault.**
`health-ingest` pushes only when both its buckets are fresh, and `homelab-update-watch` pushes nothing when it could not read GitHub.
Both stand in for healthchecks.io's `/log` ping, which recorded an event and changed no state; kuma has two states and no third kind, so "record nothing" becomes "send nothing".
So a monitor that has not moved in a while may be working exactly as designed, and its **silence bound is the interval plus retry** in the table below — not any per-run signal.
Read the last message it did receive, and the pod log, before treating a gap as an incident.

**One trap is Python-only and it is silent.**
Cloudflare answers urllib's default `Python-urllib/3.x` User-Agent with HTTP 403 and `error code: 1010` before the request reaches kuma — measured in-cluster on August 26, 2026, where the default agent got 403/1010 and a named one got kuma's own 404 for a bogus token, from the same URL in the same process.
Both Python jobs therefore set an explicit `User-Agent` on the push and both suites assert it; curl and wget are unaffected.
It matters because a failed push is swallowed by design, so the only symptom would be a monitor that never goes UP.

**A silent runner is proof the heartbeat landed.** kuma answers an unknown or inactive token with `404 {"ok":false,"msg":"Monitor not found or not active."}`, which `curl -f` and busybox `wget` both treat as a failure, and every runner prints a fixed line when its push fails.
So a forced run whose log carries no push-failure line has already proved the token, the bypass and the monitor.
Confirming UP in the UI is the second half, not the first.

Create one by hand in the kuma UI — see the note on monitor creation above.
Set the type to **Push**, take the token from the generated push URL — the last path segment, and nothing else — and store it in 1Password.
The manifest, never the script, assembles the URL: the `env:` block sets `PUSH_URL: "https://uptime.cynexia.com/api/push/${TOKEN_VAR}"`, and only `PUSH_URL` reaches the runner.
A generated script rides the same envsubst stream as its manifest and envsubst rewrites the bare `$NAME` form too, so a script naming the allowlisted variable would publish the token inside a ConfigMap; `make check-script-substitution` enforces the rename.

The last column records **per-job semantics**, not a uniform contract: each job decides for itself what it pushes and when, and they genuinely differ.
Read it per row rather than assuming up-on-success everywhere.

| Monitor | Token | Interval / retries | Pushed by, and on what |
|---|---|---|---|
| `homelab-keel-fresh` | `op://Homelab/keel-fresh/kuma-push-token` | 86400s, 1 retry at 21600s | `keel-fresh` CronJob in `ops`, from an EXIT trap: `up` on exit 0, `down` on any failure. Never silent on a failure it can observe |
| `vps-keel-fresh` | `op://VPS/keel-fresh/kuma-push-token` | 86400s, 1 retry at 21600s | `keel-fresh` CronJob in the VPS `ops` namespace, from an EXIT trap: `up` on exit 0, `down` on any failure. The same contract as the row above, from the cluster this uptime-kuma runs on |
| `health-influx-backup` | `op://Homelab/health-healthchecks/backup-kuma-push-token` | 86400s, 1 retry at 21600s | `influx-backup` CronJob in `health`, from an EXIT trap: `up` on exit 0, `down` otherwise. `msg` carries `verdict=`, `buckets=n/m` and `grafana_kib=`, plus `failed_step=` and `error=` on a failure |
| `homelab-cloudflare-analytics` | `op://Homelab/health-healthchecks/cloudflare-kuma-push-token` | 3600s, 1 retry at 7200s | `cloudflare-analytics` CronJob in `health`, Python: `up` on rc 0, `down` otherwise, the unrecoverable-gap path included. `msg` carries `verdict=` from `ok\|incomplete\|gap\|failed`, `chunks=n/m`, `rows=` and `series=` |
| `health-ingest` | `op://Homelab/health-healthchecks/ingest-kuma-push-token` | 86400s, 1 retry at 43200s | `ingest-freshness` CronJob in `health`, **success only**: `up` when BOTH buckets are under 24h, and **nothing at all** when either is stale or the query failed. One monitor for two buckets because one process checks both. `msg` carries `apple_age_h=` and `garmin_age_h=` on every push — the last message before the silence is what names which path was ageing |
| `homelab-hermes-pull` | `op://Homelab/hermes-backup/kuma-push-token` | 86400s, 1 retry at 7200s | `hermes-pull` CronJob in `backup`, from an EXIT trap: `up` on exit 0, `down` otherwise. `msg` carries `verdict=`, `zip_kib=` and `sha256_match=yes\|no` |
| `hindsight-pg-dump` | `op://Homelab/hindsight/kuma-push-token` | 86400s, 1 retry at 7200s | `hindsight-pg-dump` CronJob in `hindsight`, from an EXIT trap: `up` on exit 0, `down` otherwise. `msg` carries `verdict=`, `dump_kib=`, `tables=` and `kept=` |
| `hindsight-canary` | `op://Homelab/hindsight/canary-kuma-push-token` | 3600s, 1 retry at 1800s | `hindsight-canary` CronJob in `hindsight`, from an EXIT trap: `up` when retain and recall both pass, `down` when either fails. `msg` carries `verdict=` from that script's enum plus both HTTP statuses |
| `homelab-update-watch` | `op://Homelab/update-watch/kuma-push-token` | 86400s, 1 retry at 21600s | `update-watch` CronJob in `ops`, Python: `up` on a green verdict, `down` on a determinate red, and **nothing at all** on an indeterminate one. `msg` carries `verdict=`, `next=` and the counters |
| `jottacloud-backup` | `op://Homelab/jottacloud-backup/kuma-push-token` | 21600s, 1 retry at 7200s | The `jottacloud-backup-scheduled` CronJob's own image, on success only. This repo does not build that image and does not control the request — see the note below |
| `hermes-app-alive` | `op://hermes/hermes-app-alive/kuma-push-token` | 86400s, 1 retry at 21600s | A `no_agent` cron job inside `hermes-gateway` on the hermes VM at 05:45 UTC, `up` on exit 0 and `down` on failure, from an EXIT trap |

Each row's interval and retry mirror the period and grace of the healthchecks.io check it replaced, so nothing got quieter or noisier in the move (August 26, 2026).

`hermes-app-alive` is the exception to that sentence, and to the claim above that every push comes from inside a cluster.
Three things about it are unlike every other row here.

- **It is the only push monitor driven from outside both clusters.**
  Every other one is a CronJob in a namespace; this is a cron job inside the hermes agent on an off-cluster Debian VM.
  It relies on the same `/api/push/*` Access bypass, which is what widens that bypass's blast radius past "the clusters": remove or narrow it and this monitor goes permanently DOWN over a perfectly healthy VM, along with every other push monitor here.
- **Its token lives in the `hermes` vault, not `Homelab`**, because the VM's 1Password service account can see only that vault — which is what lets the VM resolve it for itself.
  Anyone looking for it in `Homelab` will not find it.
  No manifest in this estate assembles its `PUSH_URL`: the monitor and the 1Password field are created by hand during the install in [hermes-vm.md](hermes-vm.md#installing-or-reinstalling), and the token reaches the script as an injected environment variable that the script turns into a URL.
- It replaced no healthchecks.io check, so its interval and retry mirror nothing.
  They are chosen: a 24-hour heartbeat with one 6-hour retry, matching a check that runs once a day, which means a missing beat alarms about 30 hours after the last good one.
  The runbook is [hermes-vm.md](hermes-vm.md#reading-a-down-hermes-app-alive).

### The one monitor whose request this repo does not control

`jottacloud-backup` is driven by `ghcr.io/mnbf9rca/jottacloud-backup`, whose `scripts/healthcheck-notify.sh` joins two container environment keys — `HEALTHCHECK_URL` and `HEALTHCHECK_UUID` — with a single `/`.
Those key names are fixed by the image; only their values changed.
So the ConfigMap sets `HEALTHCHECK_URL: "https://uptime.cynexia.com/api/push"` and `HEALTHCHECK_UUID` to the push token.

Its request shape was **measured, not assumed**, on August 26, 2026, because the whole migration of this job rested on somebody else's script:

- On success it makes a **POST**, with the tail of the backup log as the body. kuma's push route accepts a POST — checked against the live endpoint with a bogus token, which answered kuma's own `{"ok":false,"msg":"Monitor not found or not active."}` rather than Express's "Cannot POST" page, so the request reached the handler. kuma reads `status` and `msg` from the **query string**, so the body is discarded and the heartbeat lands as `up` with kuma's default message.
  Observed live: `{"ok":true}`.
- On failure it appends `/fail`, and on every run it makes a second POST to `/log`. kuma routes neither, so both answer `404 Cannot POST`.
  **That is the contract this repo wants**: a failed backup pushes nothing and the monitor goes DOWN by silence at its interval plus retry.
- The cost is one `WARNING: Failed to send…` line per unrouted request, from the image's `--fail-with-body` curl treating a 404 as an error: **one** on a successful run, from the `/log` POST alone, and **two** on a failed one, where the `/fail` POST 404s as well.
  Cosmetic, and not a fault.
  A successful run on August 26, 2026 logged exactly one.

If a future image version changes any of that — a suffix on the success path, or a switch to a method kuma does not route — this job stops reporting silently, and the monitor goes DOWN.
Re-check the three bullets above after any bump.

## Reviewing who used the token

`service-auth-monitoring` is a `non_identity` policy, so unlike a bypass it writes an entry for every request it admits.
That log is the only inventory of the four apps' machine callers, and the only channel that would show a second party presenting the `Uptime` token.

Read it through the Cloudflare API, filtered to the four apps by their `aud` values:

```
GET /accounts/{account_id}/access/logs/access_requests?limit=1000&since=<ISO8601>
```

Review it about a week after any change to the token or the policy, and again when investigating an unexplained 302.
Expect only the four monitors.
Anything else is either a caller nobody documented or a leaked credential; treat an unfamiliar source as the latter until you can name it.

To enable changedetection's password, see the note in the monitor list above.
After August 25, 2026 the `Uptime` token is the only wall in front of `watch.cynexia.com` — changedetection has no origin login of its own, so anyone holding the token reaches the application in full.
Umami, n8n and hermes each keep their own origin login behind Access.

## The self-monitor (layer 4)

uptime-kuma shares the VPS tunnel, node and scheduler with most of what it watches, so it cannot report its own death.
Add one monitor that GETs a healthchecks.io ping URL.

| Field | Value |
|---|---|
| Monitor type | **HTTP(s)** |
| Name | `vps-uptime-kuma-alive` |
| URL | `https://hc-ping.com/<op://VPS/uptime-kuma/healthcheck-uuid>` |
| Interval | 300s |
| Accepted status codes | `["200-299"]` |
| Max redirects | 0 |

**Do not use a Push monitor.**
A Push monitor waits to *receive* a ping, which is the opposite of what this needs.
This monitor must *send* one.

The check `vps-uptime-kuma-alive` runs a 5m period with a 15m grace.
If the pod, node or scheduler dies, the pings stop and healthchecks.io alerts from outside both clusters.
The monitor's own UP/DOWN state is irrelevant — the signal lives at healthchecks.io.
