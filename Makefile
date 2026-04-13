# Homelab cluster apply targets.
# Assumes direnv has loaded 1Password-backed env vars from .envrc.
# All secret placeholders in manifests are substituted at apply time via envsubst.

SHELL := /bin/bash

# Expected kubectl context for the homelab cluster. Override on the command line
# (e.g. `make apply-homelab HOMELAB_CONTEXT=test`) only if you know what you're doing.
HOMELAB_CONTEXT ?= cynexia-homelab

# Vars that must be set before applying Phase 2+ manifests that reference them.
# Phase 0 Makefile targets (check-tools, build-homelab with empty kustomizations)
# do not strictly require these — require-vars is called from apply/diff only.
REQUIRED_VARS := B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_PASSWORD RESTIC_REPOSITORY \
                 ROUTE53_ACCESS_KEY_ID ROUTE53_SECRET_ACCESS_KEY \
                 ACME_EMAIL HEALTHCHECK_UUID

# Explicit envsubst allowlist. CRITICAL: envsubst with no allowlist substitutes
# EVERY $VAR / ${VAR} token in the stream, including shell variables embedded in
# upstream manifests (e.g. local-path-provisioner's setup script uses "$VOL_DIR"
# which envsubst would eat, breaking the helper pod). Passing an explicit list
# limits substitution to only our own placeholders.
#
# Add new entries here as new workloads/secrets come online.
ENVSUBST_VARS := $${B2_ACCOUNT_ID} $${B2_ACCOUNT_KEY} $${RESTIC_PASSWORD} $${RESTIC_REPOSITORY} \
                 $${ROUTE53_ACCESS_KEY_ID} $${ROUTE53_SECRET_ACCESS_KEY} \
                 $${ACME_EMAIL} \
                 $${HEALTHCHECK_UUID}
# Note: TAILSCALE_AUTH_KEY is deliberately NOT in ENVSUBST_VARS.
# Tailscale auth keys are one-shot and only needed for initial node
# registration. Steady-state Omni config never contains TS_AUTHKEY.
# For bootstrap/add-a-node, use `make bootstrap-tailscale`, which has
# its own dedicated envsubst allowlist.

.PHONY: help
help:
	@echo "Homelab cluster targets:"
	@echo "  check-tools     - verify required CLI tools are installed"
	@echo "  check-context   - assert kubectl current-context matches HOMELAB_CONTEXT ($(HOMELAB_CONTEXT))"
	@echo "  build-homelab   - run 'kustomize build | envsubst' and print to stdout"
	@echo "  diff-homelab    - show kubectl diff against the current cluster"
	@echo "  apply-homelab   - apply the built manifests to the current cluster"
	@echo "  require-vars    - assert all REQUIRED_VARS are set (preflight)"

.PHONY: check-tools
check-tools:
	@ok=1; \
	for tool in kubectl kustomize envsubst op direnv talosctl omnictl; do \
	  if ! command -v $$tool >/dev/null 2>&1; then \
	    echo "MISSING: $$tool"; ok=0; \
	  else \
	    echo "OK:      $$tool"; \
	  fi; \
	done; \
	if [ $$ok -eq 0 ]; then exit 1; fi

.PHONY: require-vars
require-vars:
	@missing=0; set=0; \
	for v in $(REQUIRED_VARS); do \
	  if [ -z "$${!v:-}" ]; then \
	    echo "MISSING: $$v"; missing=1; \
	  else \
	    set=$$((set+1)); \
	  fi; \
	done; \
	if [ $$missing -ne 0 ]; then \
	  echo "Tip: uncomment the relevant exports in .envrc and run 'direnv reload'"; \
	  exit 1; \
	fi; \
	echo "OK: $$set / $$set required vars set"

.PHONY: build-homelab
build-homelab:
	@out=$$(kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)'); \
	if [ -z "$$out" ]; then \
	  echo "OK: kustomize build succeeded (no resources yet)"; \
	else \
	  echo "$$out"; \
	fi

.PHONY: check-context
check-context:
	@current=$$(kubectl config current-context 2>/dev/null); \
	if [ -z "$$current" ]; then \
	  echo "ERROR: no kubectl current-context set"; exit 1; \
	fi; \
	if [ "$$current" != "$(HOMELAB_CONTEXT)" ]; then \
	  echo "ERROR: kubectl current-context is '$$current' but expected '$(HOMELAB_CONTEXT)'"; \
	  echo "Fix: kubectl config use-context $(HOMELAB_CONTEXT)"; \
	  echo "Or override for a different target: make apply-homelab HOMELAB_CONTEXT=<name>"; \
	  exit 1; \
	fi; \
	echo "OK: context is '$$current'"

.PHONY: diff-homelab
diff-homelab: require-vars check-context
	@kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)' | kubectl diff -f - || true

.PHONY: apply-homelab
apply-homelab: require-vars check-context
	@kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)' | kubectl apply -f -

# Create jottacloud-backup secret from 1Password. The RCLONE_CONFIG field is
# multi-line, so it can't go through the envsubst pipeline. This target reads
# each field via `op read` and feeds them to kubectl directly. Idempotent.
.PHONY: create-jotta-secret
create-jotta-secret: check-context
	@kubectl create secret generic jottacloud-backup-secrets \
	  --namespace jottacloud-backup \
	  --from-literal="KOPIA_PASSWORD=$$(op read 'op://Homelab/jottacloud-backup/KOPIA_PASSWORD')" \
	  --from-literal="S3_ACCESS_KEY=$$(op read 'op://Homelab/jottacloud-backup/S3_ACCESS_KEY')" \
	  --from-literal="S3_SECRET_KEY=$$(op read 'op://Homelab/jottacloud-backup/S3_SECRET_KEY')" \
	  --from-literal="RCLONE_CONFIG=$$(op read 'op://Homelab/jottacloud-backup/RCLONE_CONFIG')" \
	  --dry-run=client -o yaml | kubectl apply -f -

# Apply Talos machine config patches to Omni. Each file under
# homelab/talos/machineconfig-patches/ is a full ConfigPatch resource YAML.
# Patches with ${VAR} placeholders are substituted from env vars first.
#
# omnictl itself has no kustomize/loop support, so we iterate in shell.
# Idempotent: omnictl apply replaces existing resources by ID.
#
# IMPORTANT: `omnictl apply -f -` does NOT accept stdin (the `-` is
# interpreted as a literal filename and fails `stat -: no such file`).
# Native stdin support was rejected upstream (siderolabs/omni#1193,
# closed "not planned" Dec 2025). The only supported pattern is a real
# file path on disk. Since we need envsubst for ${VAR} placeholders
# (e.g. TAILSCALE_AUTH_KEY), we write substituted content to a per-patch
# temp file and shred+unlink it on every exit path via `trap`. Each
# iteration runs in its own subshell with `set -euo pipefail` so any
# failure aborts cleanly and the trap fires before exit.
.PHONY: apply-talos
apply-talos:
	@for f in homelab/talos/machineconfig-patches/*.yaml; do \
	  case "$$f" in *.tpl) continue ;; esac; \
	  ( \
	    set -euo pipefail; \
	    tmp=$$(mktemp -t talos-patch.XXXXXXXX); \
	    trap '{ shred -u "$$tmp" 2>/dev/null || rm -f "$$tmp"; }' EXIT INT TERM; \
	    echo "applying $$f"; \
	    envsubst '$(ENVSUBST_VARS)' < "$$f" > "$$tmp"; \
	    omnictl apply -f "$$tmp"; \
	  ) || exit 1; \
	done; \
	echo "OK: all Talos patches applied"

# One-shot Tailscale extension bootstrap for a single node. Applies a temporary
# machine-scoped ConfigPatch to Omni containing only TS_AUTHKEY. After the node
# registers on the tailnet, run `clear-tailscale-bootstrap` to remove the patch.
#
# Requirements:
#   TAILSCALE_AUTH_KEY must be set in the shell env. Mint a fresh one-shot key
#     in the Tailscale admin, export it, run this target, unset it. Do NOT cache
#     consumed keys in 1Password.
#   TALOS_MACHINE_ID defaults to the single machine in the homelab cluster.
#     Override explicitly for multi-node rollouts:
#       TALOS_MACHINE_ID=<id> make bootstrap-tailscale
#
# See homelab/talos/machineconfig-patches/320-homelab-tailscale-extension.yaml
# for the rationale behind the split-patch design.
TALOS_MACHINE_ID ?= $(shell omnictl get clustermachine -l omni.sidero.dev/cluster=homelab -o jsonpath 2>/dev/null | awk 'NR==1 {print $$1}')

.PHONY: bootstrap-tailscale
bootstrap-tailscale:
	@test -n "$$TAILSCALE_AUTH_KEY" || { \
	  echo "ERROR: TAILSCALE_AUTH_KEY not set in the shell environment."; \
	  echo "  Mint a one-shot auth key in the Tailscale admin console (Settings → Keys),"; \
	  echo "  then: export TAILSCALE_AUTH_KEY=tskey-auth-..."; \
	  exit 1; \
	}
	@test -n "$(TALOS_MACHINE_ID)" || { \
	  echo "ERROR: TALOS_MACHINE_ID not set and could not be auto-detected from omnictl."; \
	  echo "  Run: omnictl get clustermachines -l omni.sidero.dev/cluster=homelab"; \
	  echo "  Then: TALOS_MACHINE_ID=<id> make bootstrap-tailscale"; \
	  exit 1; \
	}
	@( \
	  set -euo pipefail; \
	  tmp=$$(mktemp -t tailscale-bootstrap.XXXXXXXX); \
	  trap '{ shred -u "$$tmp" 2>/dev/null || rm -f "$$tmp"; }' EXIT INT TERM; \
	  TALOS_MACHINE_ID='$(TALOS_MACHINE_ID)' \
	    envsubst '$${TAILSCALE_AUTH_KEY} $${TALOS_MACHINE_ID}' \
	    < homelab/talos/machineconfig-patches/bootstrap-tailscale-authkey.yaml.tpl > "$$tmp"; \
	  omnictl apply -f "$$tmp"; \
	); \
	echo ""; \
	echo "################################################################"; \
	echo "# Bootstrap patch applied for machine $(TALOS_MACHINE_ID)"; \
	echo "#"; \
	echo "# Wait ~30s, then verify the node joined the tailnet:"; \
	echo "#   tailscale status         (from any tailnet device)"; \
	echo "#"; \
	echo "# Once confirmed, CLEAR THE BOOTSTRAP PATCH:"; \
	echo "#   make clear-tailscale-bootstrap TALOS_MACHINE_ID=$(TALOS_MACHINE_ID)"; \
	echo "#"; \
	echo "# Leaving it behind is a disaster-recovery tripwire: on a state-"; \
	echo "# volume wipe, the node would try to re-auth with a consumed key."; \
	echo "################################################################"

.PHONY: clear-tailscale-bootstrap
clear-tailscale-bootstrap:
	@test -n "$(TALOS_MACHINE_ID)" || { \
	  echo "ERROR: TALOS_MACHINE_ID not set and could not be auto-detected from omnictl."; \
	  exit 1; \
	}
	@omnictl delete configpatch "900-bootstrap-tailscale-authkey-$(TALOS_MACHINE_ID)" \
	  && echo "OK: bootstrap patch removed for machine $(TALOS_MACHINE_ID)"
