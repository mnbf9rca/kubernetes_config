# Proton Drive Backup Container

A secure, automated container solution for backing up Proton Drive to Backblaze B2 using rclone and Kopia, designed to run in Kubernetes environments.

## Features

- **🔒 Security First**: Non-root user, minimal Alpine base, read-only filesystem, signed container images
- **📦 Multi-Architecture**: Supports both amd64 and arm64 architectures
- **🔄 Automated Updates**: Renovate integration for dependency management and security updates
- **📊 Monitoring**: Built-in healthchecks.io integration with detailed logging
- **🎯 Kubernetes Native**: Designed for microK8s, k3s, and other Kubernetes distributions
- **📋 Container Attestation**: Signed with cosign, includes SBOMs and vulnerability scanning

## Architecture

1. **rclone** downloads your entire Proton Drive to a local NFS mount
2. **Kopia** creates encrypted, deduplicated backups to Backblaze B2
3. **Healthchecks.io** provides monitoring and alerting
4. **Kubernetes CronJob** runs scheduled backups without manual intervention

## Quick Start

### 1. Setup Repository

This container should be in its own Git repository for the GitHub Actions workflow to function properly:

```bash
mkdir proton-drive-backup
cd proton-drive-backup
# Copy all files from this project
git init
git add .
git commit -m "Initial commit"
# Push to your GitHub repository
```

### 2. Configure Secrets

Create the Kubernetes secret with your credentials:

```bash
# Edit the secret template with your actual credentials
cp kubernetes/secret-template.yaml kubernetes/secret.yaml
# Edit kubernetes/secret.yaml with your values

# Apply the secret
kubectl apply -f kubernetes/secret.yaml
```

### 3. Update Configuration

Edit `kubernetes/persistent-volumes.yaml` to match your NFS server:
```yaml
nfs:
  server: 10.10.10.1  # Your NFS server IP
  path: "/tank/backup/proton-drive"  # Your NFS path
```

Edit `kubernetes/deployment.yaml` and `kubernetes/cronjob.yaml` to use your container image:
```yaml
image: ghcr.io/your-username/proton-drive-backup:latest
```

### 4. Deploy to Kubernetes

```bash
# Create namespace and resources
kubectl apply -f kubernetes/namespace.yaml
kubectl apply -f kubernetes/persistent-volumes.yaml
kubectl apply -f kubernetes/configmap.yaml
kubectl apply -f kubernetes/secret.yaml

# Deploy the application
kubectl apply -f kubernetes/deployment.yaml  # For manual runs/testing
kubectl apply -f kubernetes/cronjob.yaml    # For scheduled backups
```

## Configuration

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `RCLONE_CONFIG_FILE` | Path to rclone config | `/config/rclone.conf` |
| `PROTON_REMOTE` | Rclone remote name | `proton` |
| `LOCAL_PATH` | Local sync directory | `/data/proton` |
| `B2_BUCKET` | Backblaze B2 bucket name | `my-proton-backup` |
| `B2_ACCOUNT_ID` | B2 account ID | `abc123...` |
| `B2_APPLICATION_KEY` | B2 application key | `xyz789...` |
| `KOPIA_PASSWORD` | Repository encryption password | `secure-password` |
| `HEALTHCHECK_UUID` | Healthchecks.io check UUID | `abc-123-def-456` |

### Storage Requirements

- **NFS Volume**: Size of your Proton Drive + 20% buffer
- **Longhorn Volume**: 10Gi for Kopia cache and configuration
- **Temporary Storage**: 5Gi for logs and processing

### Rclone Configuration

Create your rclone configuration for Proton Drive:

```bash
# Install rclone locally
curl https://rclone.org/install.sh | sudo bash

# Configure Proton Drive
rclone config create proton drive username=your-email@proton.me password=your-password

# Encode the config for Kubernetes secret
cat ~/.config/rclone/rclone.conf | base64 -w 0
```

## Monitoring

The container integrates with [healthchecks.io](https://healthchecks.io) for monitoring:

1. Create a check on healthchecks.io
2. Copy the UUID from the check URL
3. Set `HEALTHCHECK_UUID` in your secret
4. The container will automatically ping on start/success/failure

### Log Output

The healthcheck notification includes:
- Backup status (success/failure)
- Duration of each step
- Error messages if any
- Truncated logs (within 100KB limit)

## Security

### Container Security

- ✅ Non-root user (uid/gid 1000)
- ✅ Read-only root filesystem
- ✅ No shell in final image
- ✅ Minimal Alpine base (~20MB)
- ✅ No package manager in runtime
- ✅ Security scanning with Trivy

### Kubernetes Security

- ✅ Pod Security Standards (restricted)
- ✅ Security contexts configured
- ✅ Resource limits enforced
- ✅ Secrets properly mounted
- ✅ Network policies ready

### Supply Chain Security

- ✅ Multi-arch container signing with cosign
- ✅ SBOM generation with syft
- ✅ Build provenance attestation
- ✅ Dependency scanning with Renovate
- ✅ Pinned dependencies with checksums

## Development

### Building Locally

```bash
# Build for current architecture
docker build -t proton-backup:local .

# Build for multiple architectures
docker buildx build --platform linux/amd64,linux/arm64 -t proton-backup:local .
```

### Testing

```bash
# Test the backup script locally
docker run --rm -it \
  -v /path/to/test/data:/data \
  -v /path/to/test/config:/config \
  -e KOPIA_PASSWORD=test \
  -e B2_BUCKET=test-bucket \
  proton-backup:local
```

### Manual Backup Run

```bash
# Run a one-time backup job
kubectl create job manual-backup-$(date +%s) \
  --from=cronjob/proton-backup-scheduled \
  -n proton-backup
```

## Troubleshooting

### Common Issues

1. **Permission Denied on NFS**
   - Ensure NFS export allows the backup user (uid 1000)
   - Check NFS mount options in PersistentVolume

2. **Rclone Authentication Failed**
   - Verify Proton Drive credentials in secret
   - Check rclone config encoding (base64)

3. **Kopia Repository Issues**
   - Verify B2 credentials and bucket permissions
   - Check repository password in secret

4. **Healthcheck Not Working**
   - Verify HEALTHCHECK_UUID is correct
   - Check network connectivity to healthchecks.io

### Viewing Logs

```bash
# View deployment logs
kubectl logs -f deployment/proton-backup -n proton-backup

# View CronJob logs
kubectl logs -f job/proton-backup-scheduled-<timestamp> -n proton-backup

# View all jobs
kubectl get jobs -n proton-backup
```

### Debug Mode

Set `RCLONE_LOG_LEVEL=DEBUG` in ConfigMap for verbose rclone output.

## Maintenance

### Updating Dependencies

Renovate automatically creates PRs for:
- Alpine base image updates
- GitHub Actions updates
- rclone/Kopia version updates

### Manual Updates

```bash
# Update to latest image
kubectl set image deployment/proton-backup backup=ghcr.io/your-username/proton-backup:latest -n proton-backup

# Restart CronJob (delete and recreate)
kubectl delete cronjob proton-backup-scheduled -n proton-backup
kubectl apply -f kubernetes/cronjob.yaml
```

## License

MIT License - see LICENSE file for details.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## Support

For issues and questions:
- GitHub Issues: [Create an issue](https://github.com/your-username/proton-drive-backup/issues)
- Security Issues: Email security@yourname.com