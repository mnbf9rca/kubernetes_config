# Homelab cluster apply targets.
#
# SECRET INJECTION MODEL (see .envrc)
#
# .envrc exports exactly one thing: OP_SERVICE_ACCOUNT_TOKEN. No secret values
# live in the ambient shell environment any more. Every target that needs
# secrets resolves them lazily, per command, by wrapping its pipeline in
# `op run --env-file=.env.tpl -- ...`. `op run` resolves the op:// references in
# .env.tpl and injects the real values into the environment of that ONE child
# process; envsubst then substitutes them into the manifests inside that child.
#
# Masking is left ON everywhere. `op run`'s masking applies to the child's
# stdout/stderr, NOT to its environment: inside the child, env vars hold real
# values (measured — a 100-char secret arrives as 100 chars, a 27-char one
# arrives intact). Only secrets the child *prints* are replaced with the literal
# 24-character string `<concealed by 1Password>`.
#
# (An earlier version of this repo's docs claimed `op run` blanked the child's
# env vars, citing `echo "len=$${VAR}"` returning 24. That was a coincidence:
# ACME_EMAIL happens to be exactly 24 characters long, the same length as the
# mask string, so the test could not tell a real value from the placeholder.)
#
# The env-vs-stdout distinction is what makes the build-* / apply-* split below
# matter. Read the comment above build-homelab before using build-* output.

SHELL := /bin/bash

# Wrapper for every target that needs resolved secrets. Masking deliberately ON.
# Targets that need secrets are split in two: a public target that runs the
# context/consistency guards in the parent shell and then re-enters make under
# $(OP_RUN), and a private `_*-inner` target that does the real work with the
# secrets present. Keeping the inner half as an ordinary make recipe means the
# single-quoted envsubst allowlist below needs no extra shell escaping.
OP_RUN := op run --env-file=.env.tpl --

# Expected kubectl context for the homelab cluster. Override on the command line
# (e.g. `make apply-homelab HOMELAB_CONTEXT=test`) only if you know what you're doing.
HOMELAB_CONTEXT ?= cynexia-homelab

# Vars that must be set before applying Phase 2+ manifests that reference them.
# Phase 0 Makefile targets (check-tools, build-homelab with empty kustomizations)
# do not strictly require these — require-vars is called from apply/diff only.
REQUIRED_VARS := B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_PASSWORD RESTIC_REPOSITORY \
                 RESTIC_HC_UUID \
                 ROUTE53_ACCESS_KEY_ID ROUTE53_SECRET_ACCESS_KEY \
                 ACME_EMAIL HEALTHCHECK_UUID \
                 HEALTH_HC_APPLE_UUID HEALTH_HC_GARMIN_UUID HEALTH_HC_BACKUP_UUID \
                 HEALTH_HC_CLOUDFLARE_UUID \
                 HEALTH_INFLUX_ADMIN_PASSWORD HEALTH_INFLUX_ADMIN_TOKEN \
                 HEALTH_INFLUX_GARMIN_V1_PASSWORD HEALTH_INFLUX_INGESTER_TOKEN \
                 HEALTH_INFLUX_READ_TOKEN HEALTH_INFLUX_CLOUDFLARE_TOKEN \
                 HEALTH_HAE_AUTH_TOKEN \
                 HEALTH_GARMIN_EMAIL HEALTH_GARMIN_B64_PASSWORD \
                 HEALTH_POMERIUM_GOOGLE_CLIENT_ID HEALTH_POMERIUM_GOOGLE_CLIENT_SECRET \
                 HEALTH_POMERIUM_COOKIE_SECRET HEALTH_POMERIUM_SHARED_SECRET \
                 HEALTH_GRAFANA_ADMIN_PASSWORD \
                 HEALTH_CF_API_TOKEN HEALTH_CF_ZONE_TAGS

# Explicit envsubst allowlist. CRITICAL: envsubst with no allowlist substitutes
# EVERY $VAR / ${VAR} token in the stream, including shell variables embedded in
# upstream manifests (e.g. local-path-provisioner's setup script uses "$VOL_DIR"
# which envsubst would eat, breaking the helper pod). Passing an explicit list
# limits substitution to only our own placeholders.
#
# ENVSUBST_VAR_NAMES is the plain list of substituted var names — single source
# of truth. ENVSUBST_VARS is the `${VAR}` form that envsubst actually consumes,
# derived from ENVSUBST_VAR_NAMES via foreach. Add new entries to
# ENVSUBST_VAR_NAMES only. The check-vars-consistency target below asserts
# every ENVSUBST_VAR_NAMES entry is also in REQUIRED_VARS so we never
# silently substitute an empty value into a manifest.
ENVSUBST_VAR_NAMES := B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_PASSWORD RESTIC_REPOSITORY \
                     RESTIC_HC_UUID \
                     ROUTE53_ACCESS_KEY_ID ROUTE53_SECRET_ACCESS_KEY \
                     ACME_EMAIL \
                     HEALTHCHECK_UUID \
                     HEALTH_HC_APPLE_UUID HEALTH_HC_GARMIN_UUID HEALTH_HC_BACKUP_UUID \
                     HEALTH_HC_CLOUDFLARE_UUID \
                     HEALTH_INFLUX_ADMIN_PASSWORD HEALTH_INFLUX_ADMIN_TOKEN \
                     HEALTH_INFLUX_GARMIN_V1_PASSWORD HEALTH_INFLUX_INGESTER_TOKEN \
                     HEALTH_INFLUX_READ_TOKEN HEALTH_INFLUX_CLOUDFLARE_TOKEN \
                     HEALTH_HAE_AUTH_TOKEN \
                     HEALTH_GARMIN_EMAIL HEALTH_GARMIN_B64_PASSWORD \
                     HEALTH_POMERIUM_GOOGLE_CLIENT_ID HEALTH_POMERIUM_GOOGLE_CLIENT_SECRET \
                     HEALTH_POMERIUM_COOKIE_SECRET HEALTH_POMERIUM_SHARED_SECRET \
                     HEALTH_GRAFANA_ADMIN_PASSWORD \
                     HEALTH_CF_API_TOKEN HEALTH_CF_ZONE_TAGS
ENVSUBST_VARS := $(foreach v,$(ENVSUBST_VAR_NAMES),$${$(v)})
# Note: TAILSCALE_AUTH_KEY is deliberately NOT in ENVSUBST_VAR_NAMES.
# Tailscale auth keys are one-shot and only needed for initial node
# registration. Steady-state Omni config never contains TS_AUTHKEY.
# For bootstrap/add-a-node, use `make bootstrap-tailscale`, which has
# its own dedicated envsubst allowlist.

.PHONY: help
help:
	@echo "Homelab cluster targets:"
	@echo "  check-tools     - verify required CLI tools are installed"
	@echo "  check-context   - assert kubectl current-context matches HOMELAB_CONTEXT ($(HOMELAB_CONTEXT))"
	@echo "  build-homelab   - render manifests to stdout (PREVIEW ONLY — secrets masked)"
	@echo "  diff-homelab    - show kubectl diff against the current cluster (secret values masked)"
	@echo "  apply-homelab   - apply the built manifests to the current cluster"
	@echo "  require-vars    - assert all REQUIRED_VARS resolve under op run (preflight)"
	@echo "  check-vars-consistency - assert every ENVSUBST_VAR_NAMES entry is in REQUIRED_VARS"
	@echo "  check-placeholder-coverage - assert no .env.tpl \$${VAR} survives the render (both clusters)"
	@echo ""
	@echo "VPS cluster targets:"
	@echo "  check-vps-context - assert kubectl current-context matches VPS_CONTEXT ($(VPS_CONTEXT))"
	@echo "  build-vps         - render manifests to stdout (PREVIEW ONLY — secrets masked)"
	@echo "  diff-vps          - show kubectl diff against the current cluster (secret values masked)"
	@echo "  apply-vps         - apply the built manifests to the current cluster"
	@echo "  require-vps-vars  - assert all VPS_REQUIRED_VARS resolve under op run"
	@echo "  create-cloudflared-secret - imperatively recreate the cloudflared creds Secret from 1P"
	@echo "  route-vps-dns     - create/update CNAMEs for every hostname in the cloudflared ConfigMap"
	@echo ""
	@echo "Health namespace targets:"
	@echo "  create-health-cloudflared-secret - imperatively recreate the health cloudflared creds Secret from 1P"
	@echo "  route-health-dns  - create/update CNAMEs for every hostname in the health cloudflared ConfigMap"
	@echo "  health-influx-bootstrap - bootstrap InfluxDB buckets/DBRP mapping/tokens for the health stack"
	@echo "  health-influx-cloudflare-bootstrap - create the 'cloudflare' bucket + mint its ingest/read tokens"

.PHONY: check-tools
check-tools:
	@ok=1; \
	for tool in kubectl kustomize envsubst op direnv talosctl omnictl jq; do \
	  if ! command -v $$tool >/dev/null 2>&1; then \
	    echo "MISSING: $$tool"; ok=0; \
	  else \
	    echo "OK:      $$tool"; \
	  fi; \
	done; \
	if [ $$ok -eq 0 ]; then exit 1; fi

# Assert every envsubst placeholder is also in REQUIRED_VARS. Prevents the
# class of bug where a manifest references a ${VAR} that isn't mandatory at
# apply time and silently substitutes an empty string. Wired into
# apply-homelab / diff-homelab preflight.
.PHONY: check-vars-consistency
check-vars-consistency:
	@missing=""; \
	for v in $(ENVSUBST_VAR_NAMES); do \
	  found=0; \
	  for r in $(REQUIRED_VARS); do \
	    if [ "$$v" = "$$r" ]; then found=1; break; fi; \
	  done; \
	  if [ $$found -eq 0 ]; then missing="$$missing $$v"; fi; \
	done; \
	if [ -n "$$missing" ]; then \
	  echo "ERROR: envsubst vars not in REQUIRED_VARS:$$missing"; \
	  echo "Every placeholder must also be required, or apply-homelab will"; \
	  echo "silently substitute empty strings into manifests. Fix by adding"; \
	  echo "the missing vars to REQUIRED_VARS in the Makefile."; \
	  exit 1; \
	fi; \
	echo "OK: ENVSUBST_VAR_NAMES is a subset of REQUIRED_VARS"

.PHONY: require-vars
require-vars:
	@$(OP_RUN) $(MAKE) --no-print-directory _assert-vars

# Raw assertion. Assumes REQUIRED_VARS are already in the environment, which is
# only true inside an `op run` child — that is now the sole place the values
# exist. Do not invoke directly; use `make require-vars`, or depend on it from
# an `_*-inner` target that is itself entered under $(OP_RUN).
#
# Semantics are unchanged from the pre-op-run version: an unset/empty var is
# MISSING, and a var that IS set but still holds a literal `op://...` reference
# is UNRESOLVED. The second check is the one that catches a silent resolution
# failure, so it must stay.
.PHONY: _assert-vars
_assert-vars:
	@missing=0; unresolved=0; set=0; \
	for v in $(REQUIRED_VARS); do \
	  val="$${!v:-}"; \
	  if [ -z "$$val" ]; then \
	    echo "MISSING: $$v"; missing=1; \
	  elif [ "$${val#op://}" != "$$val" ]; then \
	    echo "UNRESOLVED: $$v is still an op:// reference — op run did not resolve it"; \
	    unresolved=1; \
	  else \
	    set=$$((set+1)); \
	  fi; \
	done; \
	if [ $$missing -ne 0 ] || [ $$unresolved -ne 0 ]; then \
	  echo "Tip: values come from .env.tpl via 'op run'. Check that (a) the var has"; \
	  echo "     a VAR=op://... line in .env.tpl, (b) OP_SERVICE_ACCOUNT_TOKEN is"; \
	  echo "     exported ('direnv reload' in the shell that launched this), and"; \
	  echo "     (c) the service account can read the referenced vault item."; \
	  exit 1; \
	fi; \
	echo "OK: $$set / $$set required vars set"

# --- placeholder coverage -------------------------------------------------
#
# THE GAP THIS CLOSES: a manifest gains a `${SOME_VAR}` placeholder and the
# author forgets to add SOME_VAR to the envsubst allowlist. Nothing catches it
# today — check-vars-consistency only compares the allowlist against
# REQUIRED_VARS, so a placeholder that is in NEITHER list is invisible to it.
# envsubst leaves an unlisted token completely alone, so the literal string
# `${SOME_VAR}` is written into a live Secret and the apply reports success.
#
# THE RULE, and why the naive version is wrong: the rendered stream is FULL of
# surviving `${...}` tokens that are entirely correct — runtime shell variables
# inside CronJob scripts (`${HC_UUID}`, `${STALE_MINUTES}`, `${db}`, ...) which
# MUST NOT be substituted at render time. So "any leftover token is a bug" would
# false-positive on every cluster. The discriminator is ownership:
#
#   a leftover ${X} is a bug IF AND ONLY IF X is declared in .env.tpl
#
# i.e. X is one of OUR secrets (`X=op://...`), which means it was meant to be
# substituted and was not. Anything else is a runtime variable belonging to the
# container, and stays silent.
#
# Commented-out declarations (`#X=op://...`) count as declared: a token whose
# secret was deliberately disabled is still a bug if it is left in a manifest.
#
# Scans `${X}` only, not bare `$X`. envsubst honours both forms, but every
# placeholder in this repo uses the braced form, and scanning bare `$X` would
# match half of every embedded shell script.
#
# Requires resolved secrets: an unset var renders as an empty string and its
# token disappears, which would be a false negative. Hence _assert-vars is a
# prerequisite everywhere this is used.
#
# Callers set two shell vars first: `rendered` (the manifest text) and
# `cluster` / `allowlist` (for the message).
define PLACEHOLDER_SCAN
declared=$$(sed -nE 's|^[[:space:]]*#?[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=op://.*|\1|p' .env.tpl | sort -u); \
leftover=$$(printf '%s\n' "$$rendered" | grep -oE '[$$][{][A-Za-z_][A-Za-z0-9_]*[}]' | sed -E 's|^[$$][{]||; s|[}]$$||' | sort -u); \
bad=""; \
for t in $$leftover; do \
  for d in $$declared; do \
    if [ "$$t" = "$$d" ]; then bad="$$bad $$t"; break; fi; \
  done; \
done; \
if [ -n "$$bad" ]; then \
  echo "ERROR: [$$cluster] rendered manifests still contain .env.tpl placeholder(s):$$bad" >&2; \
  echo "  Each name above is declared in .env.tpl but is missing from $$allowlist," >&2; \
  echo "  so envsubst left it alone and the literal \$${NAME} would be written into" >&2; \
  echo "  the cluster (silently, inside a Secret). Fix: add it to $$allowlist in" >&2; \
  echo "  the Makefile. See the placeholder-coverage note above." >&2; \
  exit 1; \
fi; \
echo "OK: [$$cluster] no .env.tpl placeholder left unsubstituted"
endef

# Standalone entry point. The apply targets do NOT depend on this: they run the
# identical scan inline against the exact bytes they are about to apply, which
# costs no extra render. This target exists for running the check on its own
# (CI, or after editing a manifest), and it renders both trees to do so.
.PHONY: check-placeholder-coverage
check-placeholder-coverage:
	@$(OP_RUN) $(MAKE) --no-print-directory _check-placeholders-homelab _check-placeholders-vps

.PHONY: _check-placeholders-homelab
_check-placeholders-homelab: _assert-vars
	@set -o pipefail; \
	cluster=homelab; allowlist=ENVSUBST_VAR_NAMES; \
	rendered=$$(kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)') || { \
	  echo "ERROR: [homelab] render failed (kustomize or envsubst)" >&2; exit 1; \
	}; \
	$(PLACEHOLDER_SCAN)

.PHONY: _check-placeholders-vps
_check-placeholders-vps: _assert-vps-vars
	@set -o pipefail; \
	cluster=vps; allowlist=VPS_ENVSUBST_VAR_NAMES; \
	rendered=$$(kustomize build vps/ | envsubst '$(VPS_ENVSUBST_VARS)') || { \
	  echo "ERROR: [vps] render failed (kustomize or envsubst)" >&2; exit 1; \
	}; \
	$(PLACEHOLDER_SCAN)

# PIPELINE FAILURE HANDLING — every rendering pipeline below needs this.
#
# `kustomize build | envsubst | kubectl ...` hides mid-stream failures by
# default: bash reports only the LAST command's status, so if kustomize emits
# twenty documents and then dies, kubectl happily applies the partial set,
# exits 0, and the target reports success with resources silently missing.
# Total failure is caught only by accident, because kubectl errors on empty
# stdin. `set -o pipefail` (or an explicit PIPESTATUS check) closes that.
#
# pipefail is set per-recipe rather than globally via .SHELLFLAGS, because
# several unrelated recipes here deliberately tolerate a failing stage in a
# pipeline (grep -c returning 1 on no match, `|| true` idioms).

# PREVIEW ONLY — NEVER PIPE build-* OUTPUT INTO kubectl.
#
# build-homelab runs under `op run` with masking ON, so every secret value in
# the rendered YAML reaches stdout as the literal string `<concealed by
# 1Password>` (24 chars), not the real value. That is intentional: this output
# exists to be eyeballed by a human or read back into an agent transcript, and
# masking keeps real secrets out of the terminal, out of scrollback, and out of
# any file a reviewer might paste around.
#
# THE HAZARD:
#   make build-homelab > out.yaml && kubectl apply -f out.yaml
# would write `<concealed by 1Password>` into every Secret in the cluster. The
# apply reports success and the workloads then fail with garbage credentials —
# silent corruption that does not look like a mistake at the point it happens.
#
# `op run --no-masking` exists and would render real values here. It is
# deliberately NOT used: an unmasked render is a secret-shaped file on disk
# waiting to be committed or pasted. If you need real rendered values to reach
# a cluster, use `make diff-homelab` / `make apply-homelab` instead — those keep
# the rendered stream inside the op run child and pipe it straight into kubectl,
# so the real values never cross stdout and masking never sees them.
.PHONY: build-homelab
build-homelab:
	@$(OP_RUN) $(MAKE) --no-print-directory _build-homelab-inner

.PHONY: _build-homelab-inner
_build-homelab-inner:
	@set -o pipefail; \
	out=$$(kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)') || { \
	  echo "ERROR: render failed (kustomize or envsubst) — output above is incomplete" >&2; \
	  exit 1; \
	}; \
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

# GUARD PLACEMENT — read before editing the prerequisite lists below.
#
# check-context is declared on BOTH halves of the split, deliberately, and the
# duplication is load-bearing:
#   - on the public target, so a wrong context fails fast, before `op run`
#     resolves a single secret;
#   - on the `_*-inner` target, because the inner half is a reachable entry
#     point in its own right. `op run --env-file=.env.tpl -- make
#     _apply-homelab-inner` is a perfectly ordinary thing for a human or an
#     agent to type while debugging, and without the guard there it walks
#     straight into `kubectl apply` against whatever context happens to be
#     current. The guard must hold regardless of how the recipe is entered.
# The cost is one extra `kubectl config current-context` per run (the inner
# make is a separate process, so it re-runs the prerequisite). That is cheap.
# Do not "de-duplicate" these.
#
# WHAT IS AND IS NOT PROTECTED IN THE PRINTED DIFF
#
# `kubectl diff` output is safe to look at because KUBECTL REDACTS SECRET
# VALUES ITSELF — it prints `*** (before)` / `*** (after)` for changed Secret
# data rather than the values. That is the mechanism doing the work here.
#
# It is NOT op run's masking. op run masks by literal plaintext substring
# match over the child's stdout, and that does not survive ANY encoding of the
# value: kubectl renders live Secret data as base64, and a base64-encoded
# secret passes through op run completely unmasked (confirmed by test).
#
# Both mechanisms are in play on a real diff, and neither covers the other:
#   - Secret resources — kubectl redacts them itself. op run masking would not
#     have helped anyway, since the values are base64 by the time they print.
#   - Non-Secret resources carrying a secret VALUE in plaintext (a healthcheck
#     URL in a CronJob env var, a value embedded in a last-applied-configuration
#     annotation) — kubectl does NOT redact these, and op run masking is the
#     ONLY thing hiding them. Measured on a real `make diff-homelab`: 17 changed
#     resources, zero `***` markers (no Secret differed in that run) and three
#     values masked by op run inside CronJob manifests.
#   - The uncovered gap: a non-Secret resource carrying an ENCODED secret.
#     kubectl would not redact it and op run would not match it. Do not put
#     encoded secret material into non-Secret resources.
#
# General rule, not just for this target: never let op run masking be the SOLE
# protection for output that might carry a secret. It catches verbatim
# plaintext and nothing else — not base64, not URL-encoding, not JSON-escaped
# or hex. Anything that prints secret material must redact it itself (as
# kubectl diff does for Secrets) or must not be printed.
#
# Practical consequence: a changed Secret shows as `***`, so the diff tells you
# THAT it changed, never WHAT it changed to. Use
# `kubectl -n <ns> get secret <name> -o jsonpath=...` to confirm a value landed.
.PHONY: diff-homelab
diff-homelab: check-vars-consistency check-context
	@$(OP_RUN) $(MAKE) --no-print-directory _diff-homelab-inner

# The old `|| true` here swallowed EVERYTHING, including a kustomize failure.
# It existed only because `kubectl diff` exits 1 when it finds differences,
# which is the normal, successful outcome for this target. Blanket pipefail
# cannot tell those apart either (it reports the rightmost non-zero status).
# So inspect PIPESTATUS per stage instead, and apply kubectl diff's documented
# exit-code contract: 0 = no differences, 1 = differences found (SUCCESS here),
# >1 = kubectl itself failed. Streaming is preserved — the rendered manifest is
# never buffered into a shell variable.
.PHONY: _diff-homelab-inner
_diff-homelab-inner: check-context _assert-vars
	@kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)' | kubectl diff -f -; \
	st=($${PIPESTATUS[@]}); \
	if [ $${st[0]} -ne 0 ]; then echo "ERROR: kustomize build failed (exit $${st[0]}) — diff above is incomplete" >&2; exit 1; fi; \
	if [ $${st[1]} -ne 0 ]; then echo "ERROR: envsubst failed (exit $${st[1]}) — diff above is incomplete" >&2; exit 1; fi; \
	if [ $${st[2]} -gt 1 ]; then echo "ERROR: kubectl diff failed (exit $${st[2]})" >&2; exit $${st[2]}; fi; \
	exit 0

.PHONY: apply-homelab
apply-homelab: check-vars-consistency check-context
	@$(OP_RUN) $(MAKE) --no-print-directory _apply-homelab-inner

# RENDER FULLY, VERIFY, THEN APPLY — never stream straight into kubectl.
#
# The obvious shape, `kustomize build | envsubst | kubectl apply -f -`, applies
# as it goes. If the render dies partway, kubectl has ALREADY applied every
# document that reached it. pipefail makes the target exit non-zero, so the exit
# code is right — and the cluster is still half-updated, which is the part that
# matters. So: render to completion, check the render succeeded, check it is
# non-empty, run the placeholder scan, and only then invoke kubectl. A failure
# before that point applies nothing at all, and the message says so, so nobody
# goes hunting for partial state that does not exist.
#
# The rendered manifest is held in a shell variable, deliberately NOT a temp
# file: it contains real secret values, and a file leaves them on disk for a
# crash, a stray `set -x`, or the next person with read access to /tmp. The
# variable is local to this recipe's shell, never exported.
.PHONY: _apply-homelab-inner
_apply-homelab-inner: check-context _assert-vars
	@set -o pipefail; \
	cluster=homelab; allowlist=ENVSUBST_VAR_NAMES; \
	rendered=$$(kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)') || { \
	  echo "ERROR: render failed (kustomize or envsubst) — NOTHING was applied." >&2; \
	  echo "  kubectl was never invoked, so there is no partial state to clean up." >&2; \
	  exit 1; \
	}; \
	if [ -z "$$rendered" ]; then \
	  echo "ERROR: render produced no output — NOTHING was applied." >&2; \
	  exit 1; \
	fi; \
	$(PLACEHOLDER_SCAN); \
	printf '%s\n' "$$rendered" | kubectl apply -f -

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
	  --from-literal="DEST_REMOTE_PASSWORD=$$(op read 'op://Homelab/jottacloud-backup/JOTTA_CRYPT_PASSWORD')" \
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
#
# THIS IS A MUTATING TARGET AND IS SUBSTITUTION-SENSITIVE. It runs
# `envsubst '$(ENVSUBST_VARS)'`, and envsubst with an allowlist replaces an
# UNSET variable with the empty string — no error, no warning. Run outside an
# `op run` child (where none of the vars exist any more) any placeholder in a
# patch would be silently blanked, written to the live machine config by
# omnictl, and reported as `OK: all Talos patches applied`.
#
# Today no non-.tpl patch contains a placeholder, so this would be harmless by
# luck. That is not a safety property: AGENTS.md instructs contributors to add
# new placeholders to ENVSUBST_VARS, and the first one added to a Talos patch
# would corrupt a machine config on the next apply. So the same op run +
# _assert-vars preflight the cluster-apply targets use is applied here.
#
# Note the masking asymmetry that makes this safe: envsubst writes to a temp
# FILE, not to stdout, so the substituted content is never masked — the real
# values reach the file and omnictl. Masking only ever touches what the child
# prints.
#
# No context guard: this target talks to Omni via omnictl, not to a cluster via
# kubectl, so HOMELAB_CONTEXT is not the relevant selector. The Omni context is
# whatever ~/.talos/config / omniconfig currently points at.
.PHONY: apply-talos
apply-talos:
	@$(OP_RUN) $(MAKE) --no-print-directory _apply-talos-inner

.PHONY: _apply-talos-inner
_apply-talos-inner: _assert-vars
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
# Deliberately NOT wrapped in $(OP_RUN), and this target does not share the
# apply-talos defect: both variables it substitutes are explicitly asserted
# non-empty before any substitution happens (the two `test -n` guards below),
# so the empty-substitution failure mode cannot occur here. TAILSCALE_AUTH_KEY
# is also not in .env.tpl by design — one-shot keys are exported by hand for a
# single run and never cached in 1Password — so op run would have nothing to
# resolve for it. clear-tailscale-bootstrap performs no substitution at all and
# guards TALOS_MACHINE_ID the same way.
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

# --- VPS cluster ---
# Mirrors the homelab block. Separate context assertion, separate envsubst
# allowlist, separate secret preflight. Copy-paste duplication is deliberate —
# reading `apply-vps` top-to-bottom is clearer than chasing a parameterized
# macro. Two clusters is not enough to justify abstraction.

VPS_CONTEXT ?= cynexia-vps

VPS_REQUIRED_VARS := VPS_B2_ACCOUNT_ID VPS_B2_ACCOUNT_KEY VPS_RESTIC_PASSWORD \
                    VPS_RESTIC_REPOSITORY VPS_RESTIC_HC_UUID \
                    N8N_ENCRYPTION_KEY UMAMI_DB_PASSWORD UMAMI_APP_SECRET \
                    KARAKEEP_MEILI_MASTER_KEY KARAKEEP_NEXTAUTH_SECRET \
                    KARAKEEP_OPENAI_API_KEY

VPS_ENVSUBST_VAR_NAMES := $(VPS_REQUIRED_VARS)
VPS_ENVSUBST_VARS := $(foreach v,$(VPS_ENVSUBST_VAR_NAMES),$${$(v)})

.PHONY: check-vps-context
check-vps-context:
	@current=$$(kubectl config current-context 2>/dev/null); \
	if [ "$$current" != "$(VPS_CONTEXT)" ]; then \
	  echo "ERROR: kubectl context is '$$current', expected '$(VPS_CONTEXT)'"; \
	  exit 1; \
	fi

.PHONY: check-vps-vars-consistency
check-vps-vars-consistency:
	@missing=""; \
	for v in $(VPS_ENVSUBST_VAR_NAMES); do \
	  found=0; \
	  for r in $(VPS_REQUIRED_VARS); do \
	    if [ "$$v" = "$$r" ]; then found=1; break; fi; \
	  done; \
	  if [ $$found -eq 0 ]; then missing="$$missing $$v"; fi; \
	done; \
	if [ -n "$$missing" ]; then \
	  echo "ERROR: VPS envsubst vars not in VPS_REQUIRED_VARS:$$missing"; \
	  exit 1; \
	fi; \
	echo "OK: VPS_ENVSUBST_VAR_NAMES is a subset of VPS_REQUIRED_VARS"

.PHONY: require-vps-vars
require-vps-vars:
	@$(OP_RUN) $(MAKE) --no-print-directory _assert-vps-vars

# See _assert-vars for why this runs inside the op run child. Same semantics:
# empty => MISSING, still-an-op://-reference => UNRESOLVED.
.PHONY: _assert-vps-vars
_assert-vps-vars:
	@missing=0; unresolved=0; set=0; \
	for v in $(VPS_REQUIRED_VARS); do \
	  val="$${!v:-}"; \
	  if [ -z "$$val" ]; then echo "MISSING: $$v"; missing=1; \
	  elif [ "$${val#op://}" != "$$val" ]; then \
	    echo "UNRESOLVED: $$v still op:// — op run did not resolve it"; unresolved=1; \
	  else set=$$((set+1)); \
	  fi; \
	done; \
	if [ $$missing -ne 0 ] || [ $$unresolved -ne 0 ]; then \
	  echo "Tip: check .env.tpl has the VAR=op://... line, that"; \
	  echo "     OP_SERVICE_ACCOUNT_TOKEN is exported ('direnv reload'), and that"; \
	  echo "     the service account can read the VPS vault item."; \
	  exit 1; \
	fi; \
	echo "OK: $$set / $$set required VPS vars set"

# PREVIEW ONLY — same hazard as build-homelab. Masking is ON, so Secret values
# in this output are the literal `<concealed by 1Password>`, and
# `make build-vps > out.yaml && kubectl apply -f out.yaml` would write that
# placeholder into the cluster. `--no-masking` is deliberately not used; use
# `make diff-vps` / `make apply-vps` when you need real values. See the full
# explanation above build-homelab.
.PHONY: build-vps
build-vps:
	@$(OP_RUN) $(MAKE) --no-print-directory _build-vps-inner

.PHONY: _build-vps-inner
_build-vps-inner:
	@set -o pipefail; \
	out=$$(kustomize build vps/ | envsubst '$(VPS_ENVSUBST_VARS)') || { \
	  echo "ERROR: render failed (kustomize or envsubst) — output above is incomplete" >&2; \
	  exit 1; \
	}; \
	if [ -z "$$out" ]; then \
	  echo "OK: kustomize build succeeded (no resources yet)"; \
	else \
	  echo "$$out"; \
	fi

# check-vps-context is on BOTH halves for the same reason as check-context on
# the homelab targets: the `_*-inner` half is directly invocable and must fail
# closed on a wrong context no matter how it is entered. See the GUARD
# PLACEMENT note above diff-homelab.
.PHONY: diff-vps
diff-vps: check-vps-context check-vps-vars-consistency
	@$(OP_RUN) $(MAKE) --no-print-directory _diff-vps-inner

# See _diff-homelab-inner for why this is a PIPESTATUS check and not `|| true`.
.PHONY: _diff-vps-inner
_diff-vps-inner: check-vps-context _assert-vps-vars
	@kustomize build vps/ | envsubst '$(VPS_ENVSUBST_VARS)' | kubectl diff -f -; \
	st=($${PIPESTATUS[@]}); \
	if [ $${st[0]} -ne 0 ]; then echo "ERROR: kustomize build failed (exit $${st[0]}) — diff above is incomplete" >&2; exit 1; fi; \
	if [ $${st[1]} -ne 0 ]; then echo "ERROR: envsubst failed (exit $${st[1]}) — diff above is incomplete" >&2; exit 1; fi; \
	if [ $${st[2]} -gt 1 ]; then echo "ERROR: kubectl diff failed (exit $${st[2]})" >&2; exit $${st[2]}; fi; \
	exit 0

.PHONY: apply-vps
apply-vps: check-vps-context check-vps-vars-consistency
	@$(OP_RUN) $(MAKE) --no-print-directory _apply-vps-inner

# Same render-fully-then-apply shape as _apply-homelab-inner; see the note there.
.PHONY: _apply-vps-inner
_apply-vps-inner: check-vps-context _assert-vps-vars
	@set -o pipefail; \
	cluster=vps; allowlist=VPS_ENVSUBST_VAR_NAMES; \
	rendered=$$(kustomize build vps/ | envsubst '$(VPS_ENVSUBST_VARS)') || { \
	  echo "ERROR: render failed (kustomize or envsubst) — NOTHING was applied." >&2; \
	  echo "  kubectl was never invoked, so there is no partial state to clean up." >&2; \
	  exit 1; \
	}; \
	if [ -z "$$rendered" ]; then \
	  echo "ERROR: render produced no output — NOTHING was applied." >&2; \
	  exit 1; \
	fi; \
	$(PLACEHOLDER_SCAN); \
	printf '%s\n' "$$rendered" | kubectl apply -f -

# Re-create CNAMEs for every hostname in the cloudflared ConfigMap. Run once
# after adding a new hostname to vps/bootstrap/cloudflared/cloudflared.yaml,
# and once after a full cluster rebuild to re-attach every hostname to the
# current cynexia-vps tunnel UUID. Idempotent: cloudflared upserts the CNAME.
#
# The ConfigMap is the single source of truth for hostname <-> Service routing,
# so we grep the YAML for `- hostname:` lines rather than keeping a separate
# list. Not using yq because it's not in our toolchain and adding it for one
# grep would be silly.
.PHONY: route-vps-dns
route-vps-dns:
	@set -euo pipefail; \
	hosts=$$(grep -E '^[[:space:]]*- hostname:' vps/bootstrap/cloudflared/cloudflared.yaml | awk '{print $$3}'); \
	if [ -z "$$hosts" ]; then \
	  echo "No hostnames found in cloudflared ConfigMap — nothing to route"; \
	  exit 0; \
	fi; \
	for h in $$hosts; do \
	  echo "==> cloudflared tunnel route dns cynexia-vps $$h"; \
	  cloudflared tunnel route dns cynexia-vps "$$h"; \
	done

.PHONY: create-cloudflared-secret
create-cloudflared-secret: check-vps-context
	@set -euo pipefail; \
	creds=$$(op read 'op://VPS/cloudflared/credentials-json'); \
	if [ -z "$$creds" ]; then \
	  echo "ERROR: op read returned empty — refusing to create empty Secret"; \
	  exit 1; \
	fi; \
	printf '%s' "$$creds" | kubectl -n vps create secret generic cloudflared-credentials \
	  --from-file=credentials.json=/dev/stdin \
	  --dry-run=client -o yaml | \
	  kubectl -n vps apply -f -

# Re-create CNAMEs for every hostname in the health cloudflared ConfigMap. Run
# once after adding a new hostname to homelab/health/cloudflared.yaml, and once
# after a full cluster rebuild to re-attach every hostname to the current
# cynexia-health tunnel UUID. Idempotent: cloudflared upserts the CNAME.
.PHONY: route-health-dns
route-health-dns:
	@set -euo pipefail; \
	hosts=$$(grep -E '^[[:space:]]*- hostname:' homelab/health/cloudflared.yaml | awk '{print $$3}'); \
	for h in $$hosts; do \
	  echo "==> cloudflared tunnel route dns cynexia-health $$h"; \
	  cloudflared tunnel route dns cynexia-health "$$h"; \
	done

.PHONY: create-health-cloudflared-secret
create-health-cloudflared-secret: check-context
	@set -euo pipefail; \
	creds=$$(op document get health-cloudflared --vault Homelab); \
	if [ -z "$$creds" ]; then echo "ERROR: op document get returned empty"; exit 1; fi; \
	printf '%s' "$$creds" | kubectl -n health create secret generic health-cloudflared-credentials \
	  --from-file=credentials.json=/dev/stdin --dry-run=client -o yaml | kubectl -n health apply -f -

# Bootstrap InfluxDB buckets, v1 DBRP mapping, v1-compat auth user, and scoped
# tokens. Idempotent-ish: duplicate-create commands fail harmlessly (|| true).
# Prints the two scoped tokens for the operator to paste into 1Password
# (op://Homelab/health-influxdb/ingester-token and .../read-token) — 2.9 hash-
# stores tokens server-side, so these printed values are the only copies.
# Token extraction uses --json + `jq -r .token`, not --hide-headers + awk
# '{print $2}': influx CLI table output is whitespace/tab-padded, and the -d
# description strings here are multi-word, which shifts awk's column
# position and silently captures a description fragment instead of the token.
#
# This target is deliberately NOT wrapped in $(OP_RUN): its whole purpose is to
# print two freshly-minted tokens for the operator to paste into 1Password, and
# it should not run behind a masking layer. The one secret it consumes is read
# inline with `op read`, the same escape hatch create-jotta-secret and
# create-cloudflared-secret already use.
.PHONY: health-influx-bootstrap
health-influx-bootstrap: check-context
	@set -euo pipefail; \
	pod() { kubectl -n health exec deploy/influxdb -- "$$@"; }; \
	pod influx bucket create -n apple_workouts -o cynexia || true; \
	pod influx bucket create -n garmin -o cynexia || true; \
	GID=$$(pod influx bucket list -o cynexia -n garmin --hide-headers | awk '{print $$1}'); \
	AMID=$$(pod influx bucket list -o cynexia -n apple_metrics --hide-headers | awk '{print $$1}'); \
	AWID=$$(pod influx bucket list -o cynexia -n apple_workouts --hide-headers | awk '{print $$1}'); \
	pod influx v1 dbrp create --db GarminStats --rp autogen --bucket-id $$GID --default || true; \
	pod influx v1 auth create --username garmin --password "$$(op read 'op://Homelab/health-influxdb/garmin-v1-password')" \
	  --read-bucket $$GID --write-bucket $$GID -d "garmin-grafana v1-compat" || true; \
	echo "--- INGESTER TOKEN (paste into op://Homelab/health-influxdb/ingester-token):"; \
	pod influx auth create -o cynexia --write-bucket $$AMID --write-bucket $$AWID -d "apple ingester write-only" --json | jq -r .token; \
	echo "--- READ TOKEN (paste into op://Homelab/health-influxdb/read-token):"; \
	pod influx auth create -o cynexia --read-bucket $$AMID --read-bucket $$AWID --read-bucket $$GID -d "mcp+grafana read-only" --json | jq -r .token

# Bootstrap the `cloudflare` bucket for homelab/health/cloudflare-analytics.yaml.
#
# Run this ONCE, before the first `make apply-homelab` that includes the
# CronJob. Same one-way-door as health-influx-bootstrap: InfluxDB 2.9
# hash-stores tokens server-side, so the values printed here are the ONLY copy
# that will ever exist. Paste them into 1Password before closing the terminal.
#
# -r 0 is infinite retention. The volume is tiny (~3.5k requests/day across two
# hostnames) and the entire point of this pipeline is to outlive Cloudflare's
# 8-day window, so expiring the copy would defeat it.
#
# The ingest token needs READ AS WELL AS WRITE: the job's resume point is
# max(_time) read back out of this bucket, not a stored cursor.
#
# It also re-mints the shared mcp+grafana read token to include the new bucket.
# InfluxDB has no way to add a bucket to an existing auth, so Grafana cannot see
# `cloudflare` until the read token is replaced. After pasting the new value into
# 1Password: `make apply-homelab`, restart grafana and pomerium, THEN delete the
# superseded auth with `influx auth delete`. Deleting it first locks Grafana and
# the MCP connector out until the new Secret has actually rolled.
#
# RUN THIS IN A PLAIN TERMINAL, NOT INSIDE AN AGENT SESSION. It prints two live
# InfluxDB tokens so you can paste them into 1Password, and the 1Password service
# account this repo uses is read-only, so there is no CLI path that writes them
# for you. Any token printed inside an agent session lands in that session's
# transcript and must then be rotated under the `secrets-to-rotate.md` rule.
# Running it via Claude Code's `!` prefix does NOT help: that executes in the
# session and the output still reaches the model.
.PHONY: health-influx-cloudflare-bootstrap
health-influx-cloudflare-bootstrap: check-context
	@set -euo pipefail; \
	pod() { kubectl -n health exec deploy/influxdb -- "$$@"; }; \
	bucket_id() { \
	  id=$$(pod influx bucket list -o cynexia -n "$$1" --hide-headers | awk '{print $$1}'); \
	  if [ -z "$$id" ]; then echo "FATAL: bucket '$$1' not found in org cynexia" >&2; exit 1; fi; \
	  printf '%s' "$$id"; \
	}; \
	mint() { \
	  tok=$$(pod influx auth create -o cynexia "$$@" --json | jq -r '.token // empty'); \
	  if [ -z "$$tok" ]; then echo "FATAL: influx auth create returned no token" >&2; exit 1; fi; \
	  printf '%s\n' "$$tok"; \
	}; \
	pod influx bucket create -n cloudflare -o cynexia -r 0 || true; \
	CFID=$$(bucket_id cloudflare); \
	AMID=$$(bucket_id apple_metrics); \
	AWID=$$(bucket_id apple_workouts); \
	GID=$$(bucket_id garmin); \
	echo "--- CLOUDFLARE INGEST TOKEN (paste into op://Homelab/health-influxdb/cloudflare-token):"; \
	mint --read-bucket $$CFID --write-bucket $$CFID -d "cloudflare analytics ingest rw"; \
	echo "--- REPLACEMENT READ TOKEN (paste into op://Homelab/health-influxdb/read-token):"; \
	mint --read-bucket $$AMID --read-bucket $$AWID --read-bucket $$GID --read-bucket $$CFID -d "mcp+grafana read-only (incl cloudflare)"; \
	echo "--- then: apply, restart grafana+pomerium, and only THEN delete the old read-only auth"
