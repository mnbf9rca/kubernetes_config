# Cloudflare Tunnel + HTTP Proxy for changedetection.io

## Problem

changedetection.io runs on a VPS (Coolify, `91.99.27.26`). It needs to make outbound HTTP requests that appear to originate from the homelab's residential IP rather than the VPS IP. This requires an HTTP proxy on the homelab accessible from the VPS.

## Solution

Run tinyproxy (HTTP proxy) on the homelab MicroK8s cluster, exposed to the VPS via a Cloudflare Tunnel using TCP mode. A small cloudflared container on the VPS creates a local TCP forwarder that changedetection.io uses as its proxy.

The cloudflared deployment on the homelab is designed as a general-purpose Cloudflare Tunnel ingress point for the cluster, starting with the proxy route but extensible to expose any homelab service externally.

## Architecture

```
VPS (Coolify)                    Cloudflare Edge              Homelab K8s
=============                    ===============              ===========

changedetection.io               Tunnel: "homelab"            Namespace: proxy
  proxy: http://                   TCP passthrough              tinyproxy :8888
    cf-proxy-client:8888                                          |
       |                         proxy.cynexia.net                v
       v                           + Access Policy            Internet
cloudflared access tcp  <------->  (IP allowlist or          (home IP)
  --hostname proxy...              service token)
  --url 0.0.0.0:8888                                         Namespace: cloudflare-tunnel
       ^                                              <-----> cloudflared (tunnel run)
       |                                                        token via Secret
same Docker network
as changedetection
```

## Homelab K8s Resources

### Namespace: `cloudflare-tunnel`

General-purpose Cloudflare Tunnel ingress for the cluster.

**Deployment: cloudflared**
- Image: `cloudflare/cloudflared:latest`
- Command: `tunnel --no-autoupdate run`
- Tunnel token provided via Secret `tunnel-credentials`
- No Service needed (outbound connections only)
- Security: non-root, drop ALL capabilities, read-only root FS

**Secret: tunnel-credentials**
- Contains the tunnel token from Cloudflare Zero Trust dashboard

**NetworkPolicy:**
- Ingress: deny all
- Egress: allow all (needs to reach Cloudflare edge and internal services)

### Namespace: `proxy`

Locked-down HTTP proxy.

**Deployment: tinyproxy**
- Image: `tinyproxy/tinyproxy:latest`
- Port: 8888
- Config via ConfigMap
- Security: non-root, drop ALL capabilities, read-only root FS
- No PVC (stateless)

**Service: tinyproxy**
- Type: ClusterIP
- Port: 8888

**ConfigMap: tinyproxy-config**
- Allow all source connections (NetworkPolicy handles access control)
- No upstream filtering (needs to proxy to arbitrary targets)
- Log level: Info

**NetworkPolicy:**
- Ingress: only from pods in `cloudflare-tunnel` namespace
- Egress: allow all (proxy needs to reach arbitrary internet targets)

## Cloudflare Zero Trust Configuration

**Tunnel:** "homelab" (new named tunnel)

**Ingress rules (on dashboard):**
- `proxy.cynexia.net` -> TCP -> `tinyproxy.proxy.svc.cluster.local:8888`
- Additional routes added here as homelab services are exposed

**Access Policy:**
- Application: `proxy.cynexia.net`
- Policy: IP allowlist (`91.99.27.26`) or Service Token
- Prevents unauthorized use of the proxy

## VPS Configuration (Coolify)

**New Coolify application: cloudflared-proxy-client**
- Image: `cloudflare/cloudflared:latest`
- Command: `access tcp --hostname proxy.cynexia.net --url 0.0.0.0:8888`
- Must share a Docker network with the changedetection container
- Alternatively, connect via Coolify's inter-service networking

**changedetection.io update:**
- Set HTTP proxy to `http://cloudflared-proxy-client:8888`
  (using Docker DNS name of the cloudflared proxy client container)

## Security

- **No ports opened** on home router
- **Cloudflare Access** restricts tunnel endpoint to VPS IP only
- **NetworkPolicy** on homelab restricts tinyproxy ingress to cloudflared only
- **Non-root containers** with dropped capabilities throughout
- **tinyproxy** has no upstream filtering but is only reachable from cloudflared
- All traffic encrypted via Cloudflare Tunnel (TLS)

## Future Extensibility

The cloudflared tunnel can serve additional ingress routes:
- `jellyfin.cynexia.net` -> `http://jellyfin-service.downloads.svc.cluster.local:8096`
- `radarr.cynexia.net` -> `http://radarr-service.downloads.svc.cluster.local:7878`
- Any service reachable via `<svc>.<namespace>.svc.cluster.local`

Routes are added/removed on the Cloudflare Zero Trust dashboard without k8s changes.
