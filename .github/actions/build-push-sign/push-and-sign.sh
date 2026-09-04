#!/usr/bin/env bash
# Push the built image and sign the digest the registry gave it back.
#
# Keyless: the signature is made with the job's own OIDC identity and recorded
# in the public Rekor transparency log, so there is no key to store and none to
# rotate. It signs the DIGEST, so the signature survives `promote` retagging
# that digest as `stable`, and survives keel rewriting the Deployment's digest.
#
# RepoDigests, not `docker buildx imagetools inspect`: `docker push` writes the
# registry digest back onto the local image, so it is already here and already a
# full `image@sha256:...` reference. imagetools would be a second round trip
# returning a bare quoted digest to strip and re-join.
#
# One image, one tag, no loop. The loop over a multi-line tag list interpolated
# into the script body is what broke the first master push; there is nothing
# multi-line left to get wrong, and IMAGE arrives through the environment.
#
# cosign v3 writes the new bundle format by default, which GHCR stores as an
# OCI 1.1 referrer. If it ever rejects that, `--new-bundle-format=false` is the
# switch - here AND on the `cosign verify` in the workflow's promote job, or
# verification looks for a bundle this signature did not write.
set -euo pipefail

docker push "$IMAGE"
cosign sign --yes "$(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE")"
