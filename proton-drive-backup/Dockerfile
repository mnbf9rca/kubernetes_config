# Multi-stage build for secure Proton Drive backup container
# Stage 1: Download and verify binaries
FROM alpine:3.22.1 AS builder

# Define versions for reproducible builds (checksums fetched dynamically)
ARG RCLONE_VERSION=1.71.1
ARG KOPIA_VERSION=0.21.1

WORKDIR /tmp

# Install tools needed for download and verification
RUN apk add --no-cache curl ca-certificates

# Detect architecture and download rclone with dynamic checksum verification
RUN ARCH=$(uname -m) && \
    case ${ARCH} in \
        x86_64) \
            RCLONE_ARCH=amd64 \
            ;; \
        aarch64) \
            RCLONE_ARCH=arm64 \
            ;; \
        *) \
            echo "Unsupported architecture: ${ARCH}" && exit 1 \
            ;; \
    esac && \
    RCLONE_FILE="rclone-v${RCLONE_VERSION}-linux-${RCLONE_ARCH}.zip" && \
    echo "Downloading rclone ${RCLONE_VERSION} for ${RCLONE_ARCH}..." && \
    # Download checksums file
    curl -sSL "https://github.com/rclone/rclone/releases/download/v${RCLONE_VERSION}/SHA256SUMS" -o rclone-checksums.txt && \
    # Extract expected checksum for our file
    EXPECTED_SHA256=$(grep "${RCLONE_FILE}" rclone-checksums.txt | cut -d' ' -f1) && \
    if [ -z "$EXPECTED_SHA256" ]; then \
        echo "ERROR: Could not find checksum for ${RCLONE_FILE}" && \
        cat rclone-checksums.txt && \
        exit 1; \
    fi && \
    echo "Expected SHA256: ${EXPECTED_SHA256}" && \
    # Download and verify rclone
    curl -sSL "https://github.com/rclone/rclone/releases/download/v${RCLONE_VERSION}/${RCLONE_FILE}" -o rclone.zip && \
    echo "${EXPECTED_SHA256}  rclone.zip" | sha256sum -c - && \
    unzip rclone.zip && \
    cp rclone-*/rclone /usr/local/bin/rclone && \
    chmod +x /usr/local/bin/rclone && \
    # Cleanup
    rm -f rclone.zip rclone-checksums.txt && \
    rm -rf rclone-*

# Detect architecture and download kopia with dynamic checksum verification
RUN ARCH=$(uname -m) && \
    case ${ARCH} in \
        x86_64) \
            KOPIA_SUFFIX=x64 \
            ;; \
        aarch64) \
            KOPIA_SUFFIX=arm64 \
            ;; \
        *) \
            echo "Unsupported architecture: ${ARCH}" && exit 1 \
            ;; \
    esac && \
    KOPIA_FILE="kopia-${KOPIA_VERSION}-linux-${KOPIA_SUFFIX}.tar.gz" && \
    echo "Downloading kopia ${KOPIA_VERSION} for ${KOPIA_SUFFIX}..." && \
    # Download checksums file
    curl -sSL "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/checksums.txt" -o kopia-checksums.txt && \
    # Extract expected checksum for our file
    EXPECTED_SHA256=$(grep "${KOPIA_FILE}" kopia-checksums.txt | cut -d' ' -f1) && \
    if [ -z "$EXPECTED_SHA256" ]; then \
        echo "ERROR: Could not find checksum for ${KOPIA_FILE}" && \
        cat kopia-checksums.txt && \
        exit 1; \
    fi && \
    echo "Expected SHA256: ${EXPECTED_SHA256}" && \
    # Download and verify kopia
    curl -sSL "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/${KOPIA_FILE}" -o kopia.tar.gz && \
    echo "${EXPECTED_SHA256}  kopia.tar.gz" | sha256sum -c - && \
    tar -xzf kopia.tar.gz && \
    cp kopia-*/kopia /usr/local/bin/kopia && \
    chmod +x /usr/local/bin/kopia && \
    # Cleanup
    rm -f kopia.tar.gz kopia-checksums.txt && \
    rm -rf kopia-*

# Stage 2: Runtime image
FROM alpine:3.22.1

# Install only essential runtime dependencies
RUN apk add --no-cache \
    ca-certificates \
    curl \
    jq \
    tzdata && \
    rm -rf /var/cache/apk/*

# Create non-root user (using standard UID/GID 1999 for media/backup containers)
RUN addgroup -g 1999 backup && \
    adduser -D -G backup -u 1999 -s /bin/sh backup

# Copy binaries from builder stage
COPY --from=builder /usr/local/bin/rclone /usr/local/bin/rclone
COPY --from=builder /usr/local/bin/kopia /usr/local/bin/kopia

# Copy scripts
COPY scripts/ /scripts/
RUN chmod +x /scripts/*.sh && \
    chown -R backup:backup /scripts

# Create directories with proper ownership
RUN mkdir -p /data /config /tmp && \
    chown -R backup:backup /data /config /tmp

# Set up volumes
VOLUME ["/data", "/config"]

# Switch to non-root user
USER backup

# No exposed ports needed for CronJob operation

# Add OCI labels with metadata
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="Proton Drive Backup" \
      org.opencontainers.image.description="Secure container for backing up Proton Drive to Backblaze B2 using rclone and Kopia" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="Personal Project" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/your-username/proton-drive-backup" \
      org.opencontainers.image.base.name="alpine:3.22.1" \
      com.example.rclone.version="1.71.0" \
      com.example.kopia.version="0.21.1"

# Set build metadata as environment variables for script access
ENV CONTAINER_BUILD_DATE="${BUILD_DATE}" \
    CONTAINER_VCS_REF="${VCS_REF}" \
    CONTAINER_VERSION="${VERSION}" \
    CONTAINER_RCLONE_VERSION="1.71.0" \
    CONTAINER_KOPIA_VERSION="0.21.1"

# Default command runs the backup script
CMD ["/scripts/backup.sh"]