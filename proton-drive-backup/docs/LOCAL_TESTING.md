# Local Testing - Quick Start

## Setup rclone

1. Install rclone v1.71.0+
2. Login to Proton Drive in browser first (required)
3. Configure rclone:
```bash
rclone config
# n) New remote
# name: proton
# type: protondrive
# username: your-email@protonmail.com
# password: your-password
```

## Test Connection
```bash
rclone lsd proton:
```

## Run Script

Set environment variables:
```bash
export RCLONE_CONFIG_FILE="$HOME/.config/rclone/rclone.conf"
export PROTON_REMOTE="proton"
export LOCAL_PATH="/tmp/proton-backup-test"
```

Run:
```bash
./proton-drive-backup/scripts/rclone-sync.sh
```

## Docker Test

```bash
# Build
docker build -t proton-backup-test ./proton-drive-backup

# Run
docker run --rm -it \
  -v ~/.config/rclone/rclone.conf:/config/rclone.conf:ro \
  -v /tmp/proton-data:/data \
  -e RCLONE_CONFIG_FILE="/config/rclone.conf" \
  -e PROTON_REMOTE="proton" \
  -e LOCAL_PATH="/data/proton" \
  proton-backup-test \
  /scripts/rclone-sync.sh
```

## Common Issues

- **"protondrive not found"**: Update rclone
- **"Failed to connect"**: Login to Proton Drive web first
- **"Config file not found"**: Check RCLONE_CONFIG_FILE path