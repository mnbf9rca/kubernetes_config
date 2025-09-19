#!/bin/sh

# Main backup orchestration script
# Uses set -e to exit on errors but preserves output visibility
set -e

# Configuration
LOG_FILE="/tmp/backup.log"
START_TIME=$(date +%s)
HEALTHCHECK_BASE_URL="${HEALTHCHECK_URL}"
ERRORS=""
SCRIPT_EXIT_CODE=0

# Initialize logging
echo "=== Proton Drive Backup Started at $(date) ===" | tee $LOG_FILE

# Log container build metadata as JSON for diagnostics
BUILD_INFO=$(cat <<EOF
{"version":"${CONTAINER_VERSION:-unknown}","git_sha":"${CONTAINER_VCS_REF:-unknown}","build_date":"${CONTAINER_BUILD_DATE:-unknown}","rclone":"${CONTAINER_RCLONE_VERSION:-unknown}","kopia":"${CONTAINER_KOPIA_VERSION:-unknown}"}
EOF
)
echo "BUILD_INFO: $BUILD_INFO" | tee -a $LOG_FILE

# Start healthcheck timer if URL is provided
if [ -n "$HEALTHCHECK_BASE_URL" ] && [ -n "$HEALTHCHECK_UUID" ]; then
    echo "[$(date)] Starting healthcheck timer..." | tee -a $LOG_FILE
    if curl -fsS --retry 3 --connect-timeout 10 "${HEALTHCHECK_BASE_URL}/${HEALTHCHECK_UUID}/start"; then
        echo "[$(date)] Healthcheck timer started successfully" | tee -a $LOG_FILE
    else
        echo "[$(date)] WARNING: Failed to start healthcheck timer" | tee -a $LOG_FILE
    fi
fi

# Function to log with timestamp
log() {
    echo "[$(date)] $1" | tee -a $LOG_FILE
}

# Debug: Check user and mount permissions
log "Current user: $(id)"
log "Data mount permissions: $(ls -lan /data 2>/dev/null || echo 'Failed to list /data')"

# Function to handle script exit
cleanup_and_exit() {
    local exit_code=$1
    local end_time=$(date +%s)
    local duration=$((end_time - START_TIME))

    log "=== Backup completed with exit code $exit_code after ${duration}s ==="

    # Send final notification
    /scripts/healthcheck-notify.sh $exit_code "$LOG_FILE" $duration

    exit $exit_code
}

# Set trap to handle exit
trap 'cleanup_and_exit $SCRIPT_EXIT_CODE' EXIT

# Smart rclone config management - copy from secret only when needed
SECRET_CONFIG="/tmp/rclone-secret/rclone.conf"
CONFIG_FILE="/config/rclone.conf"
CHECKSUM_FILE="/config/.rclone.conf.checksum"

if [ -f "$SECRET_CONFIG" ]; then
    # Calculate secret config checksum
    SECRET_CHECKSUM=$(sha256sum "$SECRET_CONFIG" | cut -d' ' -f1)

    # Check if we need to update the config
    NEEDS_UPDATE=false

    if [ ! -f "$CONFIG_FILE" ]; then
        log "rclone config not found, copying from secret..."
        NEEDS_UPDATE=true
    elif [ ! -f "$CHECKSUM_FILE" ]; then
        log "Checksum file missing, updating rclone config..."
        NEEDS_UPDATE=true
    else
        STORED_CHECKSUM=$(cat "$CHECKSUM_FILE" 2>/dev/null || echo "")
        if [ "$SECRET_CHECKSUM" != "$STORED_CHECKSUM" ]; then
            log "rclone config in secret changed, updating..."
            NEEDS_UPDATE=true
        fi
    fi

    if [ "$NEEDS_UPDATE" = "true" ]; then
        cp "$SECRET_CONFIG" "$CONFIG_FILE"
        chmod 600 "$CONFIG_FILE"
        echo "$SECRET_CHECKSUM" > "$CHECKSUM_FILE"
        log "rclone config updated successfully"
    else
        log "rclone config is up to date"
    fi
else
    log "WARNING: rclone config not found in secret at $SECRET_CONFIG"
fi

log "Starting Proton Drive sync process..."

# Run rclone sync - capture output and exit code separately
set +e
/scripts/rclone-sync.sh > /tmp/rclone.log 2>&1
RCLONE_EXIT=$?
cat /tmp/rclone.log | tee -a $LOG_FILE
set -e

if [ $RCLONE_EXIT -ne 0 ]; then
    log "ERROR: Rclone sync failed with exit code $RCLONE_EXIT"
    ERRORS="${ERRORS}rclone-sync:$RCLONE_EXIT "
    SCRIPT_EXIT_CODE=$RCLONE_EXIT
else
    log "Rclone sync completed successfully"
fi

log "Starting Kopia backup process..."

# Run Kopia backup - capture output and exit code separately
set +e
/scripts/kopia-backup.sh > /tmp/kopia.log 2>&1
KOPIA_EXIT=$?
cat /tmp/kopia.log | tee -a $LOG_FILE
set -e

if [ $KOPIA_EXIT -ne 0 ]; then
    log "ERROR: Kopia backup failed with exit code $KOPIA_EXIT"
    ERRORS="${ERRORS}kopia-backup:$KOPIA_EXIT "
    # Use first failure as the main exit code if not already set
    if [ $SCRIPT_EXIT_CODE -eq 0 ]; then
        SCRIPT_EXIT_CODE=$KOPIA_EXIT
    fi
else
    log "Kopia backup completed successfully"
fi

# Set final exit code based on errors
if [ -n "$ERRORS" ]; then
    log "Backup completed with errors: $ERRORS"
    # SCRIPT_EXIT_CODE already set to first failure above
else
    log "All backup operations completed successfully"
    SCRIPT_EXIT_CODE=0
fi