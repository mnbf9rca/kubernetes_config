# Multi-stage build for secure Proton Drive backup container
# Stage 1: Download and verify binaries
FROM alpine:3.22.1 AS builder

# Define versions with checksums for reproducible builds
ARG RCLONE_VERSION=1.71.0
ARG KOPIA_VERSION=0.21.1
ARG RCLONE_AMD64_SHA256=9bf52ea536bf3e63d51e0924f851c6c5
ARG RCLONE_ARM64_SHA256=f7156e73cbd4df4af93aef2f62ce49c0
ARG KOPIA_AMD64_SHA256=de47d6b8ae2dfd6199a81d428c9eaed58cfd96a167bdc20407d3264dd18d33ca
ARG KOPIA_ARM64_SHA256=11fad6d30b1e9c264d4a30a886212fcef59a2d174a349a041ee6b24312004b23

WORKDIR /tmp

# Install tools needed for download and verification
RUN apk add --no-cache curl ca-certificates

# Detect architecture and download rclone
RUN ARCH=$(uname -m) && \
    case ${ARCH} in \
        x86_64) \
            RCLONE_ARCH=amd64 && \
            EXPECTED_SHA256=${RCLONE_AMD64_SHA256} \
            ;; \
        aarch64) \
            RCLONE_ARCH=arm64 && \
            EXPECTED_SHA256=${RCLONE_ARM64_SHA256} \
            ;; \
        *) \
            echo "Unsupported architecture: ${ARCH}" && exit 1 \
            ;; \
    esac && \
    curl -sSL "https://github.com/rclone/rclone/releases/download/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${RCLONE_ARCH}.zip" -o rclone.zip && \
    echo "${EXPECTED_SHA256}  rclone.zip" | md5sum -c - && \
    unzip rclone.zip && \
    cp rclone-*/rclone /usr/local/bin/rclone && \
    chmod +x /usr/local/bin/rclone

# Detect architecture and download kopia
RUN ARCH=$(uname -m) && \
    case ${ARCH} in \
        x86_64) \
            KOPIA_SUFFIX=x64 && \
            EXPECTED_SHA256=${KOPIA_AMD64_SHA256} \
            ;; \
        aarch64) \
            KOPIA_SUFFIX=arm64 && \
            EXPECTED_SHA256=${KOPIA_ARM64_SHA256} \
            ;; \
        *) \
            echo "Unsupported architecture: ${ARCH}" && exit 1 \
            ;; \
    esac && \
    curl -sSL "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-${KOPIA_SUFFIX}.tar.gz" -o kopia.tar.gz && \
    echo "${EXPECTED_SHA256}  kopia.tar.gz" | sha256sum -c - && \
    tar -xzf kopia.tar.gz && \
    cp kopia-*/kopia /usr/local/bin/kopia && \
    chmod +x /usr/local/bin/kopia

# Stage 2: Runtime image
FROM alpine:3.22.1

# Install only essential runtime dependencies
RUN apk add --no-cache \
    ca-certificates \
    curl \
    jq \
    tzdata && \
    rm -rf /var/cache/apk/*

# Create non-root user
RUN addgroup -g 1000 backup && \
    adduser -D -G backup -u 1000 -s /bin/sh backup

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

# Health check endpoint (simple HTTP server on port 8080)
EXPOSE 8080

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
      com.example.rclone.version="${RCLONE_VERSION}" \
      com.example.kopia.version="${KOPIA_VERSION}"

# Default command runs the backup script
CMD ["/scripts/backup.sh"]