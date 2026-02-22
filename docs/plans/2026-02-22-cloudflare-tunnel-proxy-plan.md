# Cloudflare Tunnel + HTTP Proxy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy tinyproxy + cloudflared on the homelab MicroK8s cluster, connected via Cloudflare Tunnel to the VPS, so changedetection.io can make HTTP requests from the homelab's residential IP.

**Architecture:** Two new namespaces (`cloudflare-tunnel` for the general-purpose tunnel ingress, `proxy` for the locked-down HTTP proxy). cloudflared connects to Cloudflare edge. On the VPS, a `cloudflared access tcp` container creates a local forwarder that changedetection.io uses as its proxy.

**Tech Stack:** MicroK8s, tinyproxy, cloudflare/cloudflared, Cloudflare Zero Trust, Coolify (VPS)

**Design doc:** `docs/plans/2026-02-22-cloudflare-tunnel-proxy-design.md`

**Codebase patterns to follow:**
- Flat file structure at repo root (one YAML per app)
- `---` separators between resources in the same file
- Labels: `app: <name>`
- Security context: `runAsUser: 1999`, `allowPrivilegeEscalation: false`
- Naming: `<app>-deployment`, `<app>-service`
- Namespaces defined inline at top of YAML files

**Note:** The existing Caddy deployment (`caddy.yml`) uses `MY_DOMAIN=proxy.cynexia.net` which generates the hostname `komga.proxy.cynexia.net`. The Cloudflare Tunnel will create a DNS record for `proxy.cynexia.net` itself. Verify no existing DNS conflict before configuring the tunnel.

---

### Task 1: Create cloudflared YAML manifest

**Files:**
- Create: `cloudflared.yaml`

**Step 1: Write the manifest**

Create `cloudflared.yaml` at the repo root with these resources separated by `---`:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: cloudflare-tunnel
---
apiVersion: v1
kind: Secret
metadata:
  name: tunnel-credentials
  namespace: cloudflare-tunnel
type: Opaque
data:
  # Replace with base64-encoded tunnel token from Cloudflare Zero Trust dashboard
  # echo -n '<token>' | base64
  TUNNEL_TOKEN: PLACEHOLDER
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cloudflared-deployment
  namespace: cloudflare-tunnel
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cloudflared
  template:
    metadata:
      labels:
        app: cloudflared
    spec:
      containers:
      - name: cloudflared
        image: cloudflare/cloudflared:latest
        args:
        - tunnel
        - --no-autoupdate
        - run
        env:
        - name: TUNNEL_TOKEN
          valueFrom:
            secretKeyRef:
              name: tunnel-credentials
              key: TUNNEL_TOKEN
        securityContext:
          runAsNonRoot: true
          runAsUser: 1999
          allowPrivilegeEscalation: false
        resources:
          requests:
            cpu: "50m"
            memory: "64Mi"
          limits:
            cpu: "200m"
            memory: "128Mi"
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: cloudflared-network-policy
  namespace: cloudflare-tunnel
spec:
  podSelector:
    matchLabels:
      app: cloudflared
  policyTypes:
  - Ingress
  - Egress
  ingress: []  # deny all ingress
  egress:
  - {}  # allow all egress
```

**Step 2: Validate syntax**

Run: `kubectl apply --dry-run=client -f cloudflared.yaml`
Expected: All resources validated without error (Secret will warn about PLACEHOLDER but that's expected)

**Step 3: Commit**

```bash
git add cloudflared.yaml
git commit -m "Add cloudflared tunnel deployment manifest"
```

---

### Task 2: Create tinyproxy YAML manifest

**Files:**
- Create: `tinyproxy.yaml`

**Step 1: Write the manifest**

Create `tinyproxy.yaml` at the repo root:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: proxy
  labels:
    name: proxy
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: tinyproxy-config
  namespace: proxy
data:
  tinyproxy.conf: |
    Port 8888
    Listen 0.0.0.0
    Timeout 600
    MaxClients 50
    Allow 0.0.0.0/0
    LogLevel Info
    ViaProxyName "tinyproxy"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tinyproxy-deployment
  namespace: proxy
spec:
  replicas: 1
  selector:
    matchLabels:
      app: tinyproxy
  template:
    metadata:
      labels:
        app: tinyproxy
    spec:
      containers:
      - name: tinyproxy
        image: vimagick/tinyproxy:latest
        ports:
        - name: proxy-port
          containerPort: 8888
          protocol: TCP
        volumeMounts:
        - name: tinyproxy-config
          mountPath: /etc/tinyproxy/tinyproxy.conf
          subPath: tinyproxy.conf
        securityContext:
          runAsNonRoot: true
          runAsUser: 1999
          allowPrivilegeEscalation: false
        resources:
          requests:
            cpu: "50m"
            memory: "32Mi"
          limits:
            cpu: "200m"
            memory: "128Mi"
        livenessProbe:
          tcpSocket:
            port: proxy-port
          initialDelaySeconds: 10
          periodSeconds: 30
      volumes:
      - name: tinyproxy-config
        configMap:
          name: tinyproxy-config
---
apiVersion: v1
kind: Service
metadata:
  name: tinyproxy-service
  namespace: proxy
spec:
  selector:
    app: tinyproxy
  ports:
  - name: proxy-port
    protocol: TCP
    port: 8888
    targetPort: proxy-port
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: tinyproxy-network-policy
  namespace: proxy
spec:
  podSelector:
    matchLabels:
      app: tinyproxy
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: cloudflare-tunnel
    ports:
    - protocol: TCP
      port: 8888
  egress:
  - {}  # allow all egress (proxy needs to reach arbitrary internet targets)
```

**Note on image choice:** `vimagick/tinyproxy` is a lightweight Alpine-based image. If it doesn't work (e.g., UID 1999 issues), alternatives: build a custom image from Alpine, or adjust `runAsUser` to match the image default. Test this during Task 4.

**Note on NetworkPolicy:** The ingress rule uses `kubernetes.io/metadata.name` label which is automatically added by Kubernetes to all namespaces. This restricts tinyproxy ingress to only pods in the `cloudflare-tunnel` namespace.

**Step 2: Validate syntax**

Run: `kubectl apply --dry-run=client -f tinyproxy.yaml`
Expected: All resources validated without error

**Step 3: Commit**

```bash
git add tinyproxy.yaml
git commit -m "Add tinyproxy HTTP proxy deployment manifest"
```

---

### Task 3: Apply homelab resources and verify pods

**Prerequisites:** kubectl access to the MicroK8s cluster

**Step 1: Apply namespaces and tinyproxy first**

```bash
kubectl apply -f tinyproxy.yaml
```

**Step 2: Verify tinyproxy is running**

```bash
kubectl get pods -n proxy
```
Expected: `tinyproxy-deployment-xxxxx` in `Running` state

If the pod fails (e.g., CrashLoopBackOff), check logs:
```bash
kubectl logs -n proxy deployment/tinyproxy-deployment
```

Common issues:
- **UID mismatch**: The `vimagick/tinyproxy` image may not support `runAsUser: 1999`. Fix: remove `runAsUser` or set to the image's default user.
- **Config mount path**: Verify the config file path matches what the image expects. Check with `kubectl exec -n proxy deployment/tinyproxy-deployment -- cat /etc/tinyproxy/tinyproxy.conf`

**Step 3: Test tinyproxy locally from within the cluster**

```bash
kubectl run curl-test --rm -it --image=curlimages/curl --restart=Never -- \
  curl -x http://tinyproxy-service.proxy.svc.cluster.local:8888 http://httpbin.org/ip
```
Expected: JSON response showing the homelab's public IP address

**Step 4: Apply cloudflared (will not connect yet — needs tunnel token)**

```bash
kubectl apply -f cloudflared.yaml
```
Expected: Pod starts but likely crashes (PLACEHOLDER token). This is expected — we'll fix it in Task 5.

**Step 5: Commit any fixes**

If any YAML adjustments were needed (UID, config path, etc.), commit them:
```bash
git add cloudflared.yaml tinyproxy.yaml
git commit -m "Fix deployment issues found during initial apply"
```

---

### Task 4: Create Cloudflare Tunnel (manual — Zero Trust dashboard)

**This task is performed manually in the Cloudflare Zero Trust dashboard.**

**Step 1: Create a new tunnel**

1. Go to Cloudflare Zero Trust → Networks → Tunnels
2. Click "Create a tunnel"
3. Choose "Cloudflared" connector type
4. Name: `homelab`
5. Copy the tunnel token — you'll need this for Step 2

**Step 2: Configure the tunnel's public hostname**

1. In the tunnel config, add a public hostname:
   - Subdomain: `proxy`
   - Domain: `cynexia.net`
   - Type: **TCP**
   - URL: `tinyproxy-service.proxy.svc.cluster.local:8888`

**Step 3: (Optional) Add Cloudflare Access policy**

1. Go to Access → Applications → Add an application
2. Type: Self-hosted
3. Application domain: `proxy.cynexia.net`
4. Policy: Allow only IP `91.99.27.26` (VPS IP)

This prevents unauthorized use of the proxy. Can be skipped initially for testing, but strongly recommended for production.

---

### Task 5: Update cloudflared secret with real tunnel token

**Step 1: Encode the tunnel token**

```bash
echo -n '<paste-tunnel-token-here>' | base64
```

**Step 2: Update the secret in cloudflared.yaml**

Replace `PLACEHOLDER` in the `tunnel-credentials` Secret with the base64-encoded token.

**Step 3: Re-apply and verify**

```bash
kubectl apply -f cloudflared.yaml
kubectl rollout restart deployment/cloudflared-deployment -n cloudflare-tunnel
kubectl get pods -n cloudflare-tunnel
```
Expected: `cloudflared-deployment-xxxxx` in `Running` state

Check logs to confirm tunnel connection:
```bash
kubectl logs -n cloudflare-tunnel deployment/cloudflared-deployment
```
Expected: Lines like `Connection registered` or `Tunnel is connected`

**Step 4: Commit**

```bash
git add cloudflared.yaml
git commit -m "Update cloudflared with tunnel token"
```

**IMPORTANT:** The tunnel token is a secret. Consider whether you want it committed to this repo. If not, apply the secret separately via `kubectl create secret` and keep the PLACEHOLDER in the YAML file.

---

### Task 6: Configure VPS cloudflared access tcp (manual — Coolify)

**This task is performed on the VPS via Coolify dashboard.**

**Step 1: Create a new Coolify application**

1. In Coolify, create a new application in the same project as changedetection
2. Image: `cloudflare/cloudflared:latest`
3. Command/entrypoint: `access tcp --hostname proxy.cynexia.net --url 0.0.0.0:8888`

**Step 2: Network configuration**

The cloudflared-proxy-client container must be reachable from the changedetection container. In Coolify, either:
- Place them in the same project so they share a Docker network
- Or manually connect the networks

Verify they can reach each other:
```bash
ssh rob@91.99.27.26 'docker exec <changedetection-container> ping -c1 <cloudflared-proxy-client-container>'
```

**Step 3: Note the container's Docker DNS name**

The container name assigned by Coolify will be something like `cloudflared-proxy-client-<hash>`. You'll need this hostname for Task 7.

Alternatively, set a fixed hostname in Coolify's container settings if supported.

---

### Task 7: Configure changedetection.io proxy and end-to-end test

**Step 1: Set the proxy in changedetection.io**

1. Open changedetection.io web UI
2. Go to Settings → Requests
3. Set HTTP proxy to: `http://<cloudflared-proxy-client-container-name>:8888`
   (use the Docker DNS name from Task 6)

**Step 2: End-to-end test**

1. Add a test watch in changedetection.io for `http://httpbin.org/ip`
2. Trigger a check
3. Verify the response shows your **homelab's public IP**, not the VPS IP

**Step 3: Verify from homelab logs**

```bash
kubectl logs -n proxy deployment/tinyproxy-deployment
```
Expected: Log entries showing the proxied request from cloudflared

**Step 4: Verify tunnel is healthy**

```bash
kubectl logs -n cloudflare-tunnel deployment/cloudflared-deployment
```
Expected: No errors, active connection messages

---

### Task 8: Commit final state and update README

**Step 1: Ensure all YAML is committed**

```bash
git status
git add cloudflared.yaml tinyproxy.yaml
git commit -m "Finalize cloudflare tunnel and tinyproxy proxy deployment"
```

(Skip if already committed in prior tasks.)

**Step 2: (Optional) Update README with proxy/tunnel notes**

Add a brief section to README.md noting the cloudflare-tunnel and proxy namespaces and their purpose. This is optional — skip if you prefer minimal docs.
