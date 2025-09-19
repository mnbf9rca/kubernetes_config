# Container Signing Flow Explained

## The Common Misconception

**Question**: "Why does signing happen AFTER pushing? Shouldn't the signature go WITH the image?"

**Answer**: Container signatures are stored *separately* in the registry and reference the image by its content digest - which only exists after the image is pushed.

## How Container Signing Actually Works

### The Build → Push → Sign Flow

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ 1. BUILD    │───▶│ 2. SCAN      │───▶│ 3. PUSH      │───▶│ 4. SIGN      │
│             │    │              │    │              │    │              │
│ Create      │    │ Vulnerability│    │ Upload to    │    │ Create       │
│ container   │    │ scan with    │    │ registry     │    │ signature    │
│ image       │    │ Trivy        │    │              │    │ for digest   │
└─────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
                                              │                     │
                                              ▼                     │
                                       ┌──────────────┐            │
                                       │ Registry     │            │
                                       │ assigns      │            │
                                       │ content      │            │
                                       │ digest:      │            │
                                       │ sha256:abc.. │            │
                                       └──────────────┘            │
                                              │                     │
                                              └─────────────────────┘
```

### Why This Order is Correct

1. **Content Digest Creation**: The registry calculates a SHA256 digest of the image contents when you push
2. **Immutable Reference**: This digest is immutable - it's based on the exact bytes of the image
3. **Signature Target**: Cosign signs this digest, not the image tag or local image
4. **Registry Storage**: Both the image and signature are stored in the same registry

## Registry Storage Model

```
Registry (ghcr.io/user/proton-drive-backup)
├── Manifests/
│   ├── latest                           ← tag points to digest
│   └── sha256:abc123...                 ← image manifest (layers, config)
├── Blobs/
│   ├── sha256:def456...                 ← image layers
│   ├── sha256:ghi789...                 ← image config
│   └── sha256:jkl012...                 ← more layers
└── Signatures/
    └── sha256-abc123.sig                ← signature for image digest
```

## What Gets Signed

```
Image Digest: sha256:abc123def456...
    ↓
┌─────────────────────────────────────┐
│ Cosign creates signature for:       │
│ • Image digest (content hash)       │
│ • Build metadata (who, when, how)   │
│ • SBOM reference                    │
│ • Provenance data                   │
└─────────────────────────────────────┘
    ↓
Signature stored as: sha256-abc123def456.sig
```

## Verification Flow

When someone pulls the image:

```
1. docker pull ghcr.io/user/proton-drive-backup:latest
   ├─ Registry resolves tag → digest sha256:abc123...
   └─ Downloads image layers

2. cosign verify ghcr.io/user/proton-drive-backup@sha256:abc123...
   ├─ Looks for signature: sha256-abc123.sig
   ├─ Verifies signature against public key/certificate
   └─ Confirms image hasn't been tampered with
```

## Why Signatures "Travel With" the Image

The signature **does** travel with the image, just not as part of the image itself:

- **Same Registry**: Stored in the same registry namespace
- **Content Addressing**: Both reference the same immutable digest
- **Atomic Operations**: Registry stores them together
- **Discovery**: Tools automatically find signatures for a given digest

## Security Benefits of This Design

1. **Tamper Detection**: Any change to image → new digest → signature mismatch
2. **Non-Repudiation**: Signature proves who built the image
3. **Supply Chain**: Links image to specific build process and materials
4. **Verification**: Anyone can verify without rebuilding

## GitHub Actions Implementation

```yaml
# Build and scan first (don't push yet)
- name: Build Docker image (without pushing)
  uses: docker/build-push-action@...
  with:
    push: false          # ← Key: don't push yet
    load: true           # ← Load for scanning

# Scan the built image
- name: Run Trivy vulnerability scanner
  with:
    exit-code: 1         # ← Fail if vulnerabilities found

# Only if scan passes, push to registry
- name: Build and push multi-platform image
  if: success()          # ← Only if previous steps passed
  with:
    push: true           # ← Now we push
    # Registry assigns digest here

# Sign using the digest from push step
- name: Sign the container image
  run: |
    cosign sign --yes ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@${{ steps.push.outputs.digest }}
    #                                                          ^^^ This digest comes from push step
```

## Common Questions

**Q: Can't we sign before pushing?**
A: No, because the content digest doesn't exist until the registry processes the push.

**Q: What if someone replaces the image after signing?**
A: Impossible - any change creates a new digest, breaking the signature link.

**Q: Do I need to store signatures separately?**
A: No, container registries handle this automatically. Pull the image, signatures come with it.

**Q: What about air-gapped environments?**
A: Registry mirrors copy both images and signatures together.

This design ensures that signatures are cryptographically bound to image contents while remaining discoverable and verifiable by standard tools.