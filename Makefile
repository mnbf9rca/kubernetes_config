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
                 ACME_EMAIL

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
                 $${SONARR_API_KEY} $${RADARR_API_KEY} $${SABNZBD_API_KEY} \
                 $${HYDRA2_API_KEY} $${EMBY_API_KEY} \
                 $${JOTTA_USERNAME} $${JOTTA_PASSWORD} \
                 $${TAILSCALE_AUTH_KEY}

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
	@out=$$(kustomize build homelab/ | envsubst "$(ENVSUBST_VARS)"); \
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
	@kustomize build homelab/ | envsubst "$(ENVSUBST_VARS)" | kubectl diff -f - || true

.PHONY: apply-homelab
apply-homelab: require-vars check-context
	@kustomize build homelab/ | envsubst "$(ENVSUBST_VARS)" | kubectl apply -f -
