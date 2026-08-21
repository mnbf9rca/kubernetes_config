# uptime-kuma runbook

uptime-kuma on the VPS cluster is layer 3 of the four detection layers in
[monitoring.md](monitoring.md#the-four-layers): the external check that sees a tunnel or edge
failure no in-pod probe can. This file is the procedure for maintaining it. The policy that
says why it exists, and what it still does not catch, stays in
[monitoring.md](monitoring.md).

**Create monitors by hand in the UI and record them here.** uptime-kuma v2 offers no
supported programmatic path: monitor CRUD is Socket.IO only, the one API-key-protected HTTP
route is `/metrics`, the REST API issue (#118) has been open since 2021 with two bridge PRs
closed unmerged, and the community Python wrapper stops at v1.23.2.

To read the monitor inventory, query `kuma.db` read-only:

```bash
kubectl -n vps exec deploy/uptime-kuma -- \
  sqlite3 -readonly /app/data/kuma.db 'select name, url, type, active from monitor'
```

**`/metrics` omits monitors created after the process started, until it restarts.** Using
`/metrics` as an inventory produces a wrong answer; `kuma.db` is the reliable source. The
quiesce sidecar backs up `kuma.db` nightly, so a rebuild restores the monitors.

## Settings for every HTTP monitor

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

## The Cloudflare Access trap

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

## Monitor list

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

## The self-monitor (layer 4)

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
