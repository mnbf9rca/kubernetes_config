# Proton Drive Backup Container

Automated, secure backup of Proton Drive to Backblaze B2 using rclone and Kopia in Kubernetes.

## Features

- **🔒 Security First**: Non-root user, minimal Alpine base, signed container images
- **📦 Multi-Architecture**: Supports amd64 and arm64
- **📊 Monitoring**: Built-in healthchecks.io integration
- **🎯 Kubernetes Native**: CronJob-based scheduled backups

## How It Works

1. **rclone** downloads Proton Drive to local storage
2. **Kopia** creates encrypted backups to Backblaze B2
3. **CronJob** runs on schedule (default: daily at 2 AM)

## Quick Start

### 1. Configure Storage & Settings

Edit `kubernetes/persistent-volumes.yaml` for your NFS server:
```yaml
nfs:
  server: 10.10.10.1  # Your NFS server
  path: "/tank/backup/proton-drive"
```

Edit `kubernetes/configmap.yaml` for your environment:
```yaml
# Update these values:
S3_ENDPOINT: "s3.us-west-000.backblazeb2.com"     # Your B2 region
S3_BUCKET: "your-existing-kopia-bucket"           # Your bucket name
HEALTHCHECK_UUID: "your-healthchecks-io-uuid"     # Optional monitoring
BACKUP_SCHEDULE: "0 2 * * *"                      # Daily at 2 AM
```

### 2. Create Secrets

```bash
# Create rclone config locally first
rclone config  # Choose protondrive, enter email/password

# Create secret with all credentials
kubectl create secret generic proton-backup-secrets \
  --namespace=proton-backup \
  --from-file=RCLONE_CONFIG=$HOME/.config/rclone/rclone.conf \
  --from-literal=S3_ACCESS_KEY="your-b2-key-id" \
  --from-literal=S3_SECRET_KEY="your-b2-app-key" \
  --from-literal=KOPIA_PASSWORD="your-repo-password" \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 3. Deploy

```bash
kubectl apply -f kubernetes/pod-security-policy.yaml
kubectl apply -f kubernetes/persistent-volumes.yaml
kubectl apply -f kubernetes/configmap.yaml
kubectl apply -f kubernetes/serviceaccount.yaml
kubectl apply -f kubernetes/network-policy.yaml
# Secret created above with kubectl create
kubectl apply -f kubernetes/cronjob.yaml
```

## Configuration

### Required Secrets

| Variable | Description |
|----------|-------------|
| `RCLONE_CONFIG` | Base64-encoded rclone config file |
| `S3_ACCESS_KEY` | Backblaze B2 key ID |
| `S3_SECRET_KEY` | Backblaze B2 application key |
| `KOPIA_PASSWORD` | Repository encryption password |

### Update Secrets

```bash
# To update secrets (same command works for create/update)
kubectl create secret generic proton-backup-secrets \
  --namespace=proton-backup \
  --from-file=RCLONE_CONFIG=$HOME/.config/rclone/rclone.conf \
  --from-literal=S3_ACCESS_KEY="your-new-key" \
  --from-literal=S3_SECRET_KEY="your-new-secret" \
  --from-literal=KOPIA_PASSWORD="your-repo-password" \
  --dry-run=client -o yaml | kubectl apply -f -
```

## Monitoring

Optional [healthchecks.io](https://healthchecks.io) integration:
1. Create a check, copy the UUID
2. Set `HEALTHCHECK_UUID` in ConfigMap

## Manual Operations

```bash
# Run backup immediately
kubectl create job manual-backup-$(date +%s) \
  --from=cronjob/proton-backup-scheduled -n proton-backup

# View logs
kubectl logs -f $(kubectl get jobs -n proton-backup -o name | tail -1) -n proton-backup

# Update to latest image
kubectl delete cronjob proton-backup-scheduled -n proton-backup
kubectl apply -f kubernetes/cronjob.yaml
```

## Security Features

- Non-root user, read-only filesystem
- Dedicated ServiceAccount with minimal permissions
- Network policies restrict egress traffic
- Container signing and SBOM generation
- Automated vulnerability scanning


## Documentation

- [docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md) - Local testing instructions
- [docs/SIGNING_FLOW.md](docs/SIGNING_FLOW.md) - Container signing and verification explained
