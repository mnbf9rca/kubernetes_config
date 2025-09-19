# Multi-stage build for secure Proton Drive backup container
# Stage 1: Download and verify binaries
FROM alpine:3.22.1 AS builder

# Define versions with checksums for reproducible builds
ARG RCLONE_VERSION=1.71.0
ARG KOPIA_VERSION=0.21.1
ARG RCLONE_AMD64_SHA256=3ddbcfd535ef2e6eb00cd006831766537f1fef1c8baeed1ee4632e7bcc699e93
ARG RCLONE_ARM64_SHA256=b710ac2ded37261d2cc6ab046dcd644828944524cf1ee7c2b17dd746f0fd8684
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
    echo "${EXPECTED_SHA256}  rclone.zip" | sha256sum -c - && \
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