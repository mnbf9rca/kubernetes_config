# Kubernetes Config Repository

Personal Kubernetes cluster config for a home media/downloads stack running on **microk8s**.

## What This Repo Is

Flat directory of Kubernetes YAML manifests and a few helper scripts. No CI/CD, no kustomize, no templating. Files are applied manually with `kubectl apply -f <file>`. The one exception is **immich**, which uses a Helm chart with `immich-values.yaml`.

## Cluster Stack

- **microk8s** on Linux
- **MetalLB** for LoadBalancer IPs (pool: `10.100.0.200-10.100.0.254`, see `addresspool.yaml`)
- **nginx ingress** (microk8s built-in)
- **cert-manager** with `ClusterIssuer` for Let's Encrypt TLS
- **Longhorn** for distributed block storage (data at `/mnt/longhorn`)
- **NFS CSI driver** for shared storage from NAS

## Domain

All services exposed via `*.cynexia.net` ingress rules.

## Namespaces

| Namespace | Purpose |
|---|---|
| `downloads` | Media management apps (sabnzbd, radarr, sonarr, mylar3, lazylibrarian, hydra, komga, caddy, changedetection) |
| `immich` | Photo library (immich + postgresql) |
| `open-webui` | LLM stack (ollama + open-webui) |
| `proxy` | tinyproxy |
| `cloudflare-tunnel` | cloudflared daemon |
| `ingress` | Ingress resources |
| `longhorn-system` | Longhorn storage |
| `metallb-system` | MetalLB |
| `kube-system` | Dashboard |
| `default` | Data migrator |

## NFS Servers

| Server | Typical paths |
|---|---|
| `fs.cynexia.net` | `/tank/appdata/*`, `/tank/largeappdata/*` |
| `10.10.10.1` | `/tank/video/`, `/tank/comics/`, `/tank/largeappdata/immich-data` |
| `pve2.cynexia.net` | `/tank/comics/`, mylar3 downloads |

## File Conventions

- Each service is **one YAML file** containing all its resources (Deployment/StatefulSet, Service, Ingress, PV, PVC) separated by `---`.
- File extensions: `.yaml` and `.yml` are both used (no pattern, just historical).
- `no_longer_used/` contains deprecated service configs — don't reference these as active.
- Container images are mostly `linuxserver.io` for media apps.
- Services use `PUID=1100` / `PGID=1100` (see `create_uid_gid.sh`).
- Secrets (longhorn backup creds, postgres passwords) use placeholder values — real values are applied manually.

## Helm-Managed Services

Only **immich** uses Helm:
```
helm install immich oci://ghcr.io/immich-app/immich-charts/immich -n immich -f immich-values.yaml
```

## Key Files

| File | Purpose |
|---|---|
| `README.md` | Setup guide for microk8s, longhorn, NFS, dashboard |
| `addresspool.yaml` | MetalLB IP address pool |
| `ClusterIssuer.yaml` | cert-manager Let's Encrypt config |
| `persistent-nfs-storage.yaml` | NFS StorageClass |
| `namespace-downloads.yaml` | Downloads namespace definition |
| `immich-values.yaml` | Helm values for immich chart |
| `data-migrator.*` | Custom container for migrating data between NFS paths |
| `create_uid_gid.sh` | Creates the shared UID/GID (1100) on the host |

## When Editing

- Keep the one-file-per-service pattern.
- Put all resources for a service in a single file with `---` separators.
- Match the namespace of existing similar services.
- NFS PVs need both a PV and PVC defined in the same file.
- Ingress annotations should include cert-manager TLS and basic-auth where appropriate.
