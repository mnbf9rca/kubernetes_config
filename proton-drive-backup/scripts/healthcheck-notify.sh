#!/bin/sh

# Healthcheck notification script for healthchecks.io
set -e

# Parameters
EXIT_CODE="${1:-0}"
LOG_FILE="${2:-/tmp/backup.log}"
DURATION="${3:-0}"

# Configuration
HEALTHCHECK_BASE_URL="${HEALTHCHECK_URL}"
HEALTHCHECK_UUID="${HEALTHCHECK_UUID}"
MAX_PAYLOAD_SIZE=95000  # 95KB to stay under 100KB limit

# Function to log with timestamp
log() {
    echo "[$(date)] [HEALTHCHECK] $1"
}

# Check if healthcheck is configured
if [ -z "$HEALTHCHECK_BASE_URL" ] || [ -z "$HEALTHCHECK_UUID" ]; then
    log "Healthcheck not configured (missing URL or UUID), skipping notification"
    exit 0
fi

# Determine endpoint based on exit code
if [ "$EXIT_CODE" -eq 0 ]; then
    ENDPOINT="${HEALTHCHECK_BASE_URL}/${HEALTHCHECK_UUID}"
    STATUS="SUCCESS"
else
    ENDPOINT="${HEALTHCHECK_BASE_URL}/${HEALTHCHECK_UUID}/fail"
    STATUS="FAILURE"
fi

log "Sending $STATUS notification to healthchecks.io..."

# Prepare the payload
HOSTNAME=$(hostname 2>/dev/null || echo "unknown")
TIMESTAMP=$(date -Iseconds)

# Start building the message
MESSAGE="Proton Drive Backup Report
Status: $STATUS
Exit Code: $EXIT_CODE
Duration: ${DURATION}s
Hostname: $HOSTNAME
Timestamp: $TIMESTAMP

"

# Add log content if log file exists
if [ -f "$LOG_FILE" ]; then
    log "Including log content (prioritizing errors and important info)"

    # Calculate how much space we have left for logs
    MESSAGE_SIZE=$(echo "$MESSAGE" | wc -c)
    AVAILABLE_SPACE=$((MAX_PAYLOAD_SIZE - MESSAGE_SIZE - 200))  # 200 byte buffer

    if [ $AVAILABLE_SPACE -gt 0 ]; then
        # Add logs section header
        MESSAGE="${MESSAGE}=== LOG OUTPUT ===
"

        # For failures, prioritize errors and warnings
        if [ "$EXIT_CODE" -ne 0 ]; then
            log "Backup failed - extracting errors and warnings first"

            # Extract errors, warnings, and important lines
            PRIORITY_LOGS=$(grep -i -E "(error|fail|warning|exception|timeout|refused|denied|invalid|not found|permission|unauthorized)" "$LOG_FILE" 2>/dev/null | tail -50 || echo "")

            # Add summary statistics and final status
            SUMMARY_LOGS=$(tail -20 "$LOG_FILE" 2>/dev/null | grep -E "(completed|finished|failed|success|duration|files|bytes|statistics)" || echo "")

            # Combine priority content
            FILTERED_LOGS="${PRIORITY_LOGS}
=== FINAL STATUS ===
${SUMMARY_LOGS}"

            # If filtered logs are still too big, truncate them
            FILTERED_SIZE=$(echo "$FILTERED_LOGS" | wc -c)
            if [ "$FILTERED_SIZE" -gt "$AVAILABLE_SPACE" ]; then
                LOG_CONTENT=$(echo "$FILTERED_LOGS" | head -c "$AVAILABLE_SPACE")
                MESSAGE="${MESSAGE}... [FILTERED LOGS TRUNCATED - showing errors/warnings] ...
${LOG_CONTENT}"
            else
                MESSAGE="${MESSAGE}${FILTERED_LOGS}"
            fi
        else
            # For success, show summary stats and recent activity
            log "Backup succeeded - including summary and recent activity"

            # Get summary statistics
            SUMMARY_LOGS=$(grep -E "(completed|success|duration|files|bytes|transferred|statistics)" "$LOG_FILE" 2>/dev/null | tail -20 || echo "")

            # Get last part of log for context
            RECENT_LOGS=$(tail -30 "$LOG_FILE" 2>/dev/null || echo "")

            # Combine content
            SUCCESS_LOGS="=== SUMMARY ===
${SUMMARY_LOGS}

=== RECENT ACTIVITY ===
${RECENT_LOGS}"

            # Truncate if needed
            SUCCESS_SIZE=$(echo "$SUCCESS_LOGS" | wc -c)
            if [ "$SUCCESS_SIZE" -gt "$AVAILABLE_SPACE" ]; then
                LOG_CONTENT=$(echo "$SUCCESS_LOGS" | head -c "$AVAILABLE_SPACE")
                MESSAGE="${MESSAGE}... [LOGS TRUNCATED - showing summary] ...
${LOG_CONTENT}"
            else
                MESSAGE="${MESSAGE}${SUCCESS_LOGS}"
            fi
        fi
    else
        MESSAGE="${MESSAGE}[LOG TOO LARGE TO INCLUDE]"
    fi
else
    MESSAGE="${MESSAGE}[NO LOG FILE AVAILABLE]"
fi

# Send the notification
log "Sending notification to $ENDPOINT"

# Use curl to send the notification
CURL_EXIT=0
curl -X POST \
    --data-raw "$MESSAGE" \
    --user-agent "proton-backup/1.0" \
    --connect-timeout 30 \
    --max-time 60 \
    --retry 3 \
    --retry-delay 5 \
    --fail-with-body \
    --silent \
    --show-error \
    "$ENDPOINT" || CURL_EXIT=$?

if [ $CURL_EXIT -eq 0 ]; then
    log "Notification sent successfully"
else
    log "WARNING: Failed to send notification (curl exit code: $CURL_EXIT)"
    # Don't fail the entire backup for notification issues
fi

# Also send a separate log-only ping if we have detailed logs
if [ -f "$LOG_FILE" ] && [ "$EXIT_CODE" -ne 0 ]; then
    log "Sending detailed logs to log endpoint"

    # Send logs to the /log endpoint without changing check status
    LOG_ENDPOINT="${HEALTHCHECK_BASE_URL}/${HEALTHCHECK_UUID}/log"

    # Send last 90KB of logs to log endpoint
    tail -c 90000 "$LOG_FILE" 2>/dev/null | \
    curl -X POST \
        --data-binary @- \
        --user-agent "proton-backup/1.0" \
        --connect-timeout 30 \
        --max-time 60 \
        --retry 2 \
        --retry-delay 3 \
        --fail-with-body \
        --silent \
        --show-error \
        "$LOG_ENDPOINT" || log "WARNING: Failed to send detailed logs"
fi

log "Healthcheck notification process completed"