#!/bin/sh

# Main backup orchestration script
# Uses set -e to exit on errors but preserves output visibility
set -e

# Configuration
LOG_FILE="/tmp/backup.log"
START_TIME=$(date +%s)
HEALTHCHECK_BASE_URL="${HEALTHCHECK_URL}"
ERRORS=""

# Initialize logging
echo "=== Proton Drive Backup Started at $(date) ===" | tee $LOG_FILE

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
trap 'cleanup_and_exit $?' EXIT

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
else
    log "Kopia backup completed successfully"
fi

# If we have errors, exit with error code
if [ -n "$ERRORS" ]; then
    log "Backup completed with errors: $ERRORS"
    exit 1
else
    log "All backup operations completed successfully"
    exit 0
fi