# uptime-kuma runbook

This runbook covers monitor standards and access to uptime-kuma on the VPS cluster.
Detection policy, job-specific reporting contracts and accepted gaps belong in [monitoring.md](monitoring.md).
The live `kuma.db` is the record for monitor names, settings and group membership.

## Standard monitor settings

Use these settings for HTTP monitors unless a recorded reason requires a departure.

| Field | Standard | Reason |
|---|---|---|
| Monitor type | HTTP(s) | Use a body assertion only when a successful status can hide a fault |
| Method / cache-busting | GET / off | Normal request behavior; exceptions are recorded below |
| Heartbeat interval | 60s | Routine availability checks |
| Retries | 3 | Absorb a transient failure before alerting |
| Heartbeat retry interval | 60s | Allow a transient fault time to recover |
| Request timeout | 20s | Separate a slow response from a wedged service |
| Max redirects | 0 behind Access; default elsewhere | Prevent a login page from passing as a healthy origin |
| Accepted status codes | `200-299`, or the endpoint's recorded healthy response | Do not widen the set to hide a failure |
| Certificate expiry / ignore TLS | defaults | TLS terminates at the Cloudflare edge |

### Departures and their reasons

A departure is deliberate only when its reason is recorded.

- `family-foqos.app` and `recordwell.app website` enable cache-busting, also without a recorded reason.
- The two `api.recordwell.app` monitors use JSON Query and require `$.status` to equal `ok`, because HTTP 200 alone cannot establish application health.
- `hae.cynexia.com` accepts 401 because its origin requires authentication; the response proves the edge, tunnel and origin answered.
  It also sets `maxredirects` to 0 without an Access app; no reason for that departure is recorded.
- `Data MCP` and `homelab-proxy` check the Access challenge itself for the security reasons in [The Cloudflare Access trap](#the-cloudflare-access-trap).

Reach for a keyword or JSON-query assertion only when the status code cannot fail on its own.
An error status already fails the check, and `saveErrorResponse` captures the response body for diagnosis.
Observe a status code with `curl` before adding it to an accepted set.

The `uptime-kuma` container sets `NODE_OPTIONS=--network-family-autoselection-attempt-timeout=2000` to give each address two seconds to connect instead of abandoning slow Cloudflare handshakes after 250 milliseconds.

### Push monitors

A push monitor receives a job's heartbeat rather than sending a request.
Its interval and retry allowance follow the job's schedule and tolerated lateness; read the current values from the [live monitor table](#monitor-list).
The normal contract is `up` on success and `down` on failure.
There is no `/start` signal: the job's own deadline bounds a hang, and the monitor's interval plus retry bounds silence.

Two jobs have recorded reasons to send nothing on some runs.
`health-garmin-and-apple-ingest` stays silent unless both buckets are fresh, preserving the tolerance for an operator who has not synced a watch.
Its last successful message's `apple_age_h=` and `garmin_age_h=` identify the ageing source; the pod log separates stale data from a failed query.
`homelab-update-watch` stays silent on an unreadable dashboard or API failure because an unknown result is neither a healthy result nor a dependency-lookup failure.

### The one monitor whose request this repo does not control

`jottacloud-backup` is success-only because the upstream image sends a success POST that kuma accepts, but also sends `/fail` and `/log` POSTs that kuma does not route.
Kuma reads `status` and `msg` from the query string and discards the POST body.
A failed backup therefore reports by silence; the unrouted requests produce one cosmetic warning on success and two on failure.
Re-check the image's request contract after an update; a changed success path or method can stop reporting.

### The self-monitor (layer 4)

`vps-uptime-kuma-alive` is an HTTP monitor that sends a GET to its healthchecks.io ping URL, not a Push monitor.
It lets healthchecks.io notice kuma's death from outside the VPS, which kuma cannot report itself.
The external check's period and grace are recorded in [monitoring.md](monitoring.md#healthchecksio-checks); kuma's own UP/DOWN label is not that external signal.

## Reaching monitors

### UI

Open [uptime.cynexia.com](https://uptime.cynexia.com/) through Cloudflare Access.
The kuma admin username, password and one-time password are on `op://VPS/uptime-kuma`.
Create and edit monitors in the UI.
The live database records their configuration.

For direct access without the Cloudflare path, forward the Service locally:

```bash
kubectl --context cynexia-vps -n vps port-forward svc/uptime-kuma 13001:3001
```

Then open `http://127.0.0.1:13001` and use the kuma admin login.

### Editing monitors in bulk

Use the Socket.IO client `uptime-kuma-api` in a throwaway virtual environment against the port-forward above.
Read `op://VPS/uptime-kuma` credentials inside the Python process.
Pass username, password and the fresh `one-time password?attribute=otp` to `login`.
Never put credentials on argv or in terminal output.
An empty username or password produces `authIncorrectCreds`, which can resemble incorrect credentials.
Make the sweep read-only unless `--apply` is supplied.
Rehearse it before enabling writes.
`edit_monitor` merges supplied fields over the existing monitor; preserve the other fields.
Verify the change with a before-and-after diff of the read-only monitor query.

### Monitor list

Read the live monitor table without selecting authentication headers or push tokens:

```bash
kubectl --context cynexia-vps -n vps exec deploy/uptime-kuma -c uptime-kuma -- \
  sqlite3 -readonly -header -column /app/data/kuma.db \
  'SELECT id, name, type, url, interval, maxretries, retry_interval, timeout, maxredirects, accepted_statuscodes_json, keyword, json_path, expected_value, method, cache_bust, parent, active FROM monitor ORDER BY id;'
```

The self-monitor's URL contains its healthchecks.io ping identifier; keep query output out of committed files.
The container already ships `sqlite3`; no installation is needed.
The `parent` column records group membership.
`/metrics` can omit monitors created after process startup until the next restart, so it is not the inventory.

Raw heartbeats last about a day.
Status changes and the `stat_hourly` table provide the 30-day view; use them for older incidents.
Hourly failure counts include PENDING checks absorbed by retries, so they are not a count of outages.
The database stores UTC even though the container uses `Europe/London`.

### The Cloudflare Access trap

An unauthenticated request can receive a 302 to the Cloudflare login page.
A monitor that follows it gets HTTP 200 from the login page and reports UP even when the origin is down.
Set **Max redirects to 0** on every Access-protected monitor.

To check an origin protected by Service Auth, supply the service-token headers in the monitor's **Headers** box:

```json
{
  "CF-Access-Client-Id": "<op://VPS/uptime-kuma/CF-Access-Client-Id>",
  "CF-Access-Client-Secret": "<op://VPS/uptime-kuma/CF-Access-Client-Secret>"
}
```

Read the values from 1Password without writing them into this repository.
The `Uptime` token serves the `Umami analytics`, `changedetection`, `hermes` and `n8n` Access apps through `service-auth-monitoring`.
It expires on August 24, 2031; wrong, missing or expired headers can fail all four monitors together.
Changedetection has no origin login, so that token grants access to the full application.
Enabling changedetection's password requires adding `302` to the `watch.cynexia.com` monitor's accepted set.
The `hermes API` monitor needs only Access credentials because `/api/health` is on the VM dashboard's unauthenticated allowlist.
The `rss.cynexia.com` and `Karakeep` monitors need no headers because their path-scoped Access apps admit anonymous requests.

Two monitors intentionally check Access rather than the origin:

- `Data MCP` accepts exactly `["401"]` and sends `{"Accept": "application/json"}` so Access challenges it as a client.
  The pinned status detects a missing Access gate, which would expose the origin.
  Give this monitor no service token.
  Keep its Access app free of bypass policies; MCP clients need the 401 to start OAuth.
  The tunnel route and HTTP handler are outside this monitor's coverage.
- `homelab-proxy`, for `proxy.cynexia.com`, accepts exactly `["403"]` and sends no service-token headers.
  Its Access app has no identity provider, so it refuses the request instead of redirecting to a login page.
  The origin proxy has no authentication; a missing Access gate would publish an open proxy, so a successful origin response must fail this monitor.
  The separate `vps-proxy-access` token is scoped only to the `homelab-proxy` Access app through `service-auth-homelab-proxy`.
  It expires on September 1, 2031.
  On expiry, the residential proxy and every proxied changedetection watch fail without a monitor alert, because this monitor still receives the Access challenge.

### The push path is bypassed at the edge

The `uptime-kuma push` Access app bypasses authentication for both `uptime.cynexia.com/api/push/*` and bare `uptime.cynexia.com/api/push`.
The wildcard does not match the bare path, so both destinations are required.
The dashboard and the rest of the hostname remain protected.
The [VPS bypass inventory](vps.md#cloudflare-access-bypasses) records the account-side applications and policies.

Jobs in both clusters send pushes without Access credentials; `hermes-app-alive` and `hal-daily-check` on the off-cluster Hermes VM do the same.
Removing or narrowing this bypass stops their heartbeats even when the jobs are healthy.
It is how the private homelab reports to kuma: the VPS cannot reach the homelab's `*.cynexia.net` addresses, but the jobs can call outward.
Each push URL carries its monitor token, which lets a caller forge a heartbeat; keep it in 1Password and out of public files.

Python callers need an explicit User-Agent: Cloudflare rejected urllib's default agent with HTTP 403 / error 1010 during the August 26, 2026 check.
Curl and wget were unaffected.
Kuma rejects an unknown or inactive token with HTTP 404; a successful push response confirms the heartbeat landed.

After an Access change, check the bypass from a client with no Access session:

```bash
curl -s -o /dev/null -w '%{http_code}\n' 'https://uptime.cynexia.com/api/push/notarealtoken'
curl -s -o /dev/null -w '%{http_code}\n' 'https://uptime.cynexia.com/api/push'
curl -s -o /dev/null -w '%{http_code}\n' 'https://uptime.cynexia.com/dashboard'
```

The expected results are 404 for the bogus token, 200 for the bare push path and 302 for the dashboard.
A 302 on the push path means the bypass did not admit the caller.

### Cloudflare request logs

For `blog.cynexia.com` and the other Cloudflare-fronted hosts, query `httpRequestsAdaptiveGroups` through Cloudflare's GraphQL API.
Filter by hostname, UTC time window, client address and user agent; other pods share the VPS egress address.
Read `/cdn-cgi/trace` from inside the kuma pod to find its current client address.
Filter the user agent with `Uptime-Kuma%`.
Compare per-request timestamps, edge status and `sampleInterval`; requests near a minute boundary can land in the next minute's bucket.
A connection that fails before HTTP leaves no request row at Cloudflare.

The hourly copy is the `http_requests` measurement in the InfluxDB `cloudflare` bucket in the homelab's `health` namespace.
Use the health-data MCP to query by zone, host, UTC range and `client=uptime-kuma`.
Those counts are Cloudflare's sampled estimates, so small differences from kuma's check counts are expected.
