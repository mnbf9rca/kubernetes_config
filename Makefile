# Homelab cluster apply targets.
# Assumes direnv has loaded 1Password-backed env vars from .envrc.
# All secret placeholders in manifests are substituted at apply time via envsubst.

SHELL := /bin/bash

# Vars that must be set before applying Phase 2+ manifests that reference them.
# Phase 0 Makefile targets (check-tools, build-homelab with empty kustomizations)
# do not strictly require these — require-vars is called from apply/diff only.
REQUIRED_VARS := B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_PASSWORD RESTIC_REPOSITORY \
                 ROUTE53_ACCESS_KEY_ID ROUTE53_SECRET_ACCESS_KEY

.PHONY: help
help:
	@echo "Homelab cluster targets:"
	@echo "  check-tools     - verify required CLI tools are installed"
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
	@missing=0; \
	for v in $(REQUIRED_VARS); do \
	  if [ -z "$${!v:-}" ]; then \
	    echo "ERROR: required env var $$v is not set (is direnv loaded?)"; \
	    missing=1; \
	  fi; \
	done; \
	if [ $$missing -ne 0 ]; then \
	  echo "Tip: uncomment the relevant exports in .envrc and run 'direnv reload'"; \
	  exit 1; \
	fi

.PHONY: build-homelab
build-homelab:
	@kustomize build homelab/ | envsubst

.PHONY: diff-homelab
diff-homelab: require-vars
	@kustomize build homelab/ | envsubst | kubectl diff -f - || true

.PHONY: apply-homelab
apply-homelab: require-vars
	@kustomize build homelab/ | envsubst | kubectl apply -f -
