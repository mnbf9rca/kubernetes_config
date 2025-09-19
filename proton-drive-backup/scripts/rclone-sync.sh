#!/bin/sh

# Rclone sync script for Proton Drive
set -e

# Configuration - no defaults for critical paths to prevent masking config failures
RCLONE_CONFIG_FILE="${RCLONE_CONFIG_FILE}"
PROTON_REMOTE="${PROTON_REMOTE}"
LOCAL_PATH="${LOCAL_PATH}"
RCLONE_LOG_LEVEL="${RCLONE_LOG_LEVEL:-INFO}"

# Function to log with timestamp
log() {
    echo "[$(date)] [RCLONE] $1"
}

# Validate required configuration
if [ -z "$RCLONE_CONFIG_FILE" ]; then
    log "ERROR: RCLONE_CONFIG_FILE environment variable is required"
    exit 1
fi

if [ -z "$PROTON_REMOTE" ]; then
    log "ERROR: PROTON_REMOTE environment variable is required"
    exit 1
fi

if [ -z "$LOCAL_PATH" ]; then
    log "ERROR: LOCAL_PATH environment variable is required"
    exit 1
fi

if [ ! -f "$RCLONE_CONFIG_FILE" ]; then
    log "ERROR: Rclone config file not found at $RCLONE_CONFIG_FILE"
    exit 1
fi

# Create local directory if it doesn't exist
mkdir -p "$LOCAL_PATH"

log "Starting Proton Drive sync..."
log "Remote: $PROTON_REMOTE"
log "Local path: $LOCAL_PATH"
log "Config file: $RCLONE_CONFIG_FILE"

# Test connection first
log "Testing connection to Proton Drive..."
if ! rclone --config="$RCLONE_CONFIG_FILE" lsd "$PROTON_REMOTE:" --max-depth 1; then
    log "ERROR: Failed to connect to Proton Drive remote '$PROTON_REMOTE'"
    exit 1
fi

log "Connection test successful, starting sync..."

# Ensure destination directory exists and is writable
log "Checking current user and permissions..."
id
log "Checking data mount permissions..."
ls -la "/data" || true

log "Creating destination directory: $LOCAL_PATH"
mkdir -p "$LOCAL_PATH" || {
    log "ERROR: Failed to create directory $LOCAL_PATH"
    log "Data directory permissions:"
    ls -la "/data" || true
    log "Parent directory permissions:"
    ls -la "$(dirname "$LOCAL_PATH")" || true
    exit 1
}

# Perform the sync with comprehensive logging
# Using sync instead of copy to handle deletions
log "Starting rclone sync from $PROTON_REMOTE: to $LOCAL_PATH"

# Create rclone logs directory
RCLONE_LOG_DIR="/data/logs/rclone"
mkdir -p "$RCLONE_LOG_DIR"
RCLONE_LOG_FILE="$RCLONE_LOG_DIR/sync-$(date +%Y%m%d-%H%M%S).log"

rclone sync \
    --config="$RCLONE_CONFIG_FILE" \
    --log-level="$RCLONE_LOG_LEVEL" \
    --log-file="$RCLONE_LOG_FILE" \
    --log-file-max-age=7d \
    --stats=1m \
    --stats-one-line \
    --progress \
    --check-first \
    --create-empty-src-dirs \
    --exclude-if-present .rcloneignore \
    --retries=3 \
    --retries-sleep=30s \
    --timeout=10m \
    --contimeout=60s \
    --low-level-retries=10 \
    "$PROTON_REMOTE:" "$LOCAL_PATH"

RCLONE_EXIT=$?

if [ $RCLONE_EXIT -eq 0 ]; then
    log "Sync completed successfully"

    # Show final statistics
    log "Final sync statistics:"
    du -sh "$LOCAL_PATH" 2>/dev/null || log "Could not calculate directory size"

    # Count files
    FILE_COUNT=$(find "$LOCAL_PATH" -type f | wc -l)
    log "Total files synced: $FILE_COUNT"

else
    log "ERROR: Sync failed with exit code $RCLONE_EXIT"
    exit $RCLONE_EXIT
fi

log "Rclone sync process completed"