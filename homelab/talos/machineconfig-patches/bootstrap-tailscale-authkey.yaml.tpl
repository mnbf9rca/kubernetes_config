# ONE-SHOT BOOTSTRAP TEMPLATE — NOT PICKED UP BY `make apply-talos`.
#
# Consumed by `make bootstrap-tailscale`, which substitutes ${TAILSCALE_AUTH_KEY}
# and ${TALOS_MACHINE_ID} from the shell environment and applies a temporary
# machine-scoped ConfigPatch to Omni. The sibling file 320-homelab-tailscale-
# extension.yaml defines the steady-state (TS_HOSTNAME, TS_ROUTES); this file
# only provides TS_AUTHKEY, which is consumed on first extension boot to
# register the node with the tailnet.
#
# After the node has registered, run `make clear-tailscale-bootstrap` to delete
# this patch from Omni so no consumed secret lingers in the cluster config.
#
# ---
# WHY machine-scoped and not cluster-scoped:
#
# Omni applies patches in scope-bucket order (cluster → machine-set →
# cluster-machine → machine), then alphabetical by ID within each bucket.
# Machine-scoped patches always render AFTER all cluster-scoped patches
# regardless of their numeric prefix. Since Talos merges ExtensionServiceConfig
# env lists by APPENDING (no key-based dedup), we keep the two patches'
# environment variable sets disjoint: 320 (cluster) owns TS_HOSTNAME and
# TS_ROUTES, this (machine) owns TS_AUTHKEY only. No duplicates appear in
# the final rendered config.
#
# Source: https://docs.siderolabs.com/talos/v1.12/configure-your-talos-cluster/system-configuration/patching.md
# Source: https://github.com/siderolabs/omni/blob/main/internal/backend/runtime/omni/controllers/omni/internal/configpatch/configpatch.go
#
# ---
# WHY the 900- prefix:
#
# Scope-bucket order makes this purely cosmetic, but the 900- prefix
# documents "this is a late-applying, per-machine, post-bootstrap patch"
# and sorts it visually after the 300-series cluster patches.
metadata:
  namespace: default
  type: ConfigPatches.omni.sidero.dev
  id: 900-bootstrap-tailscale-authkey-${TALOS_MACHINE_ID}
  labels:
    omni.sidero.dev/cluster: homelab
    omni.sidero.dev/machine: ${TALOS_MACHINE_ID}
  annotations:
    name: bootstrap-tailscale-authkey
spec:
  data: |-
    apiVersion: v1alpha1
    kind: ExtensionServiceConfig
    name: tailscale
    environment:
      - TS_AUTHKEY=${TAILSCALE_AUTH_KEY}
