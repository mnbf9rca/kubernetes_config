#!/bin/sh

# Kopia backup script for S3-compatible storage (Backblaze B2)
set -e

# Configuration - no defaults for critical paths to prevent masking config failures
KOPIA_CONFIG_FILE="${KOPIA_CONFIG_FILE}"
KOPIA_CACHE_DIR="${KOPIA_CACHE_DIR}"
KOPIA_LOG_DIR="${KOPIA_LOG_DIR}"
SOURCE_PATH="${SOURCE_PATH}"
REPOSITORY_PASSWORD="${KOPIA_PASSWORD}"
S3_BUCKET="${S3_BUCKET:-${B2_BUCKET}}"
S3_ENDPOINT="${S3_ENDPOINT}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-${B2_ACCOUNT_ID}}"
S3_SECRET_KEY="${S3_SECRET_KEY:-${B2_APPLICATION_KEY}}"
S3_REGION="${S3_REGION:-us-west-000}"

# Function to log with timestamp
log() {
    echo "[$(date)] [KOPIA] $1"
}

# Validate required environment variables
if [ -z "$KOPIA_CONFIG_FILE" ]; then
    log "ERROR: KOPIA_CONFIG_FILE environment variable is required"
    exit 1
fi

if [ -z "$KOPIA_CACHE_DIR" ]; then
    log "ERROR: KOPIA_CACHE_DIR environment variable is required"
    exit 1
fi

if [ -z "$KOPIA_LOG_DIR" ]; then
    log "ERROR: KOPIA_LOG_DIR environment variable is required"
    exit 1
fi

if [ -z "$SOURCE_PATH" ]; then
    log "ERROR: SOURCE_PATH environment variable is required"
    exit 1
fi

if [ -z "$REPOSITORY_PASSWORD" ]; then
    log "ERROR: KOPIA_PASSWORD environment variable is required"
    exit 1
fi

if [ -z "$S3_BUCKET" ] || [ -z "$S3_ACCESS_KEY" ] || [ -z "$S3_SECRET_KEY" ]; then
    log "ERROR: S3_BUCKET, S3_ACCESS_KEY, and S3_SECRET_KEY environment variables are required"
    exit 1
fi

if [ -z "$S3_ENDPOINT" ]; then
    log "ERROR: S3_ENDPOINT environment variable is required (e.g., s3.us-west-000.backblazeb2.com)"
    exit 1
fi

# Validate source path exists
if [ ! -d "$SOURCE_PATH" ]; then
    log "ERROR: Source path $SOURCE_PATH does not exist"
    exit 1
fi

# Create necessary directories
mkdir -p "$KOPIA_CACHE_DIR" "$KOPIA_LOG_DIR"

log "Starting Kopia backup process..."
log "Source path: $SOURCE_PATH"
log "S3 bucket: $S3_BUCKET"
log "S3 endpoint: $S3_ENDPOINT"
log "Config file: $KOPIA_CONFIG_FILE"

# Set Kopia environment variables
export KOPIA_PASSWORD="$REPOSITORY_PASSWORD"
export KOPIA_CONFIG_PATH="$KOPIA_CONFIG_FILE"

# Smart repository connection management
# Only reconnect when parameters change or on first run
PARAMS_HASH_FILE="/config/.s3-params.hash"

# Calculate current S3 parameters hash
CURRENT_HASH=$(echo "${S3_BUCKET}${S3_ENDPOINT}${S3_ACCESS_KEY}${S3_SECRET_KEY}${S3_REGION}" | sha256sum | cut -d' ' -f1)

# Check if repository connection needs to be established/updated
NEEDS_CONNECT=false

if [ ! -f "$KOPIA_CONFIG_FILE" ]; then
    log "Repository config not found, connecting to S3 repository..."
    NEEDS_CONNECT=true
elif [ ! -f "$PARAMS_HASH_FILE" ]; then
    log "Parameter hash missing, reconnecting to S3 repository..."
    NEEDS_CONNECT=true
else
    STORED_HASH=$(cat "$PARAMS_HASH_FILE" 2>/dev/null || echo "")
    if [ "$CURRENT_HASH" != "$STORED_HASH" ]; then
        log "S3 parameters changed, reconnecting to repository..."
        NEEDS_CONNECT=true
    fi
fi

if [ "$NEEDS_CONNECT" = "true" ]; then
    # Connect to existing S3 repository (Backblaze B2 in S3 mode)
    # Use stable client identity to avoid multiple client entries in repository
    kopia repository connect s3 \
        --bucket="$S3_BUCKET" \
        --access-key="$S3_ACCESS_KEY" \
        --secret-access-key="$S3_SECRET_KEY" \
        --endpoint="$S3_ENDPOINT" \
        --region="$S3_REGION" \
        --cache-directory="$KOPIA_CACHE_DIR" \
        --log-dir="$KOPIA_LOG_DIR" \
        --log-dir-max-age=14d \
        --override-hostname="proton-backup-client" \
        --override-username="backup"

    # Store parameter hash to detect future changes
    echo "$CURRENT_HASH" > "$PARAMS_HASH_FILE"
    log "Connected to S3 repository successfully with stable client identity: backup@proton-backup-client"
else
    log "Repository already connected and parameters unchanged"
fi

# Verify repository connection
if ! kopia repository status 2>/dev/null; then
    log "ERROR: Repository connection verification failed"
    exit 1
fi

# Show repository info
log "Repository information:"
kopia repository status

# Check if source is already configured for backup
SNAPSHOT_SOURCE="$SOURCE_PATH"
if ! kopia snapshot list "$SNAPSHOT_SOURCE" 2>/dev/null | grep -q "$SNAPSHOT_SOURCE"; then
    log "Configuring snapshot policy for $SNAPSHOT_SOURCE"

    # Set snapshot policy
    kopia policy set "$SNAPSHOT_SOURCE" \
        --retention-mode=locked \
        --retention-period=1y \
        --compression=zstd \
        --before-folder-action= \
        --after-folder-action= \
        --ignore-cache-dirs=true \
        --one-file-system=true

    log "Snapshot policy configured"
fi

# Create snapshot
log "Creating snapshot of $SNAPSHOT_SOURCE..."

# Get start time for this snapshot
SNAPSHOT_START_TIME=$(date +%s)

kopia snapshot create "$SNAPSHOT_SOURCE" \
    --description="Proton Drive backup $(date -Iseconds)" \
    --tags="source:proton-drive,automated:true"

KOPIA_EXIT=$?

if [ $KOPIA_EXIT -eq 0 ]; then
    SNAPSHOT_END_TIME=$(date +%s)
    SNAPSHOT_DURATION=$((SNAPSHOT_END_TIME - SNAPSHOT_START_TIME))

    log "Snapshot created successfully in ${SNAPSHOT_DURATION}s"

    # Show snapshot info
    log "Latest snapshots:"
    kopia snapshot list "$SNAPSHOT_SOURCE" --max-results=5

    # Show repository statistics
    log "Repository statistics:"
    kopia content stats

    # Clean up old snapshots according to policy
    log "Running snapshot maintenance..."
    kopia snapshot expire --delete 2>/dev/null || log "No snapshots to expire"

    # Repository maintenance (optional, but good practice)
    log "Running repository maintenance..."
    kopia maintenance run --full 2>/dev/null || log "Maintenance completed with warnings"

else
    log "ERROR: Snapshot creation failed with exit code $KOPIA_EXIT"
    exit $KOPIA_EXIT
fi

log "Kopia backup process completed successfully"