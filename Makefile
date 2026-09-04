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
                 RESTIC_HC_UUID HERMES_HC_UUID OPS_HC_UPDATE_UUID \
                 OPS_KUMA_KEEL_TOKEN OPS_KUMA_UPDATE_TOKEN HERMES_KUMA_TOKEN \
                 JOTTACLOUD_KUMA_TOKEN \
                 HEALTH_KUMA_BACKUP_TOKEN HEALTH_KUMA_CLOUDFLARE_TOKEN \
                 HEALTH_KUMA_INGEST_TOKEN HEALTH_KUMA_WITHINGS_TOKEN \
                 HEALTH_WITHINGS_CLIENT_ID HEALTH_WITHINGS_CLIENT_SECRET \
                 HINDSIGHT_KUMA_TOKEN HINDSIGHT_CANARY_KUMA_TOKEN \
                 ROUTE53_ACCESS_KEY_ID ROUTE53_SECRET_ACCESS_KEY \
                 ACME_EMAIL HEALTHCHECK_UUID \
                 HEALTH_HC_APPLE_UUID HEALTH_HC_GARMIN_UUID HEALTH_HC_BACKUP_UUID \
                 HEALTH_HC_CLOUDFLARE_UUID \
                 HEALTH_INFLUX_ADMIN_PASSWORD HEALTH_INFLUX_ADMIN_TOKEN \
                 HEALTH_INFLUX_GARMIN_V1_PASSWORD HEALTH_INFLUX_INGESTER_TOKEN \
                 HEALTH_INFLUX_READ_TOKEN HEALTH_INFLUX_CLOUDFLARE_TOKEN \
                 HEALTH_INFLUX_WITHINGS_TOKEN \
                 HEALTH_HAE_AUTH_TOKEN \
                 HEALTH_GARMIN_EMAIL HEALTH_GARMIN_B64_PASSWORD \
                 HEALTH_GRAFANA_ADMIN_PASSWORD \
                 HEALTH_CF_API_TOKEN HEALTH_CF_ZONE_TAGS \
                 HINDSIGHT_PG_PASSWORD HINDSIGHT_LLM_API_KEY \
                 HINDSIGHT_DEEPINFRA_API_KEY \
                 HINDSIGHT_TENANT_API_KEY HINDSIGHT_CP_ACCESS_KEY \
                 HINDSIGHT_HC_UUID HINDSIGHT_CANARY_HC_UUID

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
                     RESTIC_HC_UUID HERMES_HC_UUID OPS_HC_UPDATE_UUID \
                     OPS_KUMA_KEEL_TOKEN OPS_KUMA_UPDATE_TOKEN HERMES_KUMA_TOKEN \
                     JOTTACLOUD_KUMA_TOKEN \
                     HEALTH_KUMA_BACKUP_TOKEN HEALTH_KUMA_CLOUDFLARE_TOKEN \
                     HEALTH_KUMA_INGEST_TOKEN HEALTH_KUMA_WITHINGS_TOKEN \
                     HEALTH_WITHINGS_CLIENT_ID HEALTH_WITHINGS_CLIENT_SECRET \
                     HINDSIGHT_KUMA_TOKEN HINDSIGHT_CANARY_KUMA_TOKEN \
                     ROUTE53_ACCESS_KEY_ID ROUTE53_SECRET_ACCESS_KEY \
                     ACME_EMAIL \
                     HEALTHCHECK_UUID \
                     HEALTH_HC_APPLE_UUID HEALTH_HC_GARMIN_UUID HEALTH_HC_BACKUP_UUID \
                     HEALTH_HC_CLOUDFLARE_UUID \
                     HEALTH_INFLUX_ADMIN_PASSWORD HEALTH_INFLUX_ADMIN_TOKEN \
                     HEALTH_INFLUX_GARMIN_V1_PASSWORD HEALTH_INFLUX_INGESTER_TOKEN \
                     HEALTH_INFLUX_READ_TOKEN HEALTH_INFLUX_CLOUDFLARE_TOKEN \
                     HEALTH_INFLUX_WITHINGS_TOKEN \
                     HEALTH_HAE_AUTH_TOKEN \
                     HEALTH_GARMIN_EMAIL HEALTH_GARMIN_B64_PASSWORD \
                     HEALTH_GRAFANA_ADMIN_PASSWORD \
                     HEALTH_CF_API_TOKEN HEALTH_CF_ZONE_TAGS \
                     HINDSIGHT_PG_PASSWORD HINDSIGHT_LLM_API_KEY \
                     HINDSIGHT_DEEPINFRA_API_KEY \
                     HINDSIGHT_TENANT_API_KEY HINDSIGHT_CP_ACCESS_KEY \
                     HINDSIGHT_HC_UUID HINDSIGHT_CANARY_HC_UUID
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
	@echo "  check-job-ttl   - assert every standalone Job sets ttlSecondsAfterFinished (both clusters)"
	@echo "  check-script-substitution - assert no configMapGenerator script names an envsubst var"
	@echo "  check-ping-bodies - assert no ping body or heartbeat message is built from a command's output"
	@echo "  check-script-lint - shellcheck (-s sh) every script in the RENDER + compile/test the Python"
	@echo "  check-renovate-scope - assert every container is in exactly one update mode (both clusters)"
	@echo "                    keel for floating tags, Renovate for pinned ones, never both; per container"
	@echo "  check-renovate-scope-homelab / -vps - the per-cluster halves of that guard"
	@echo "  check-keel-fresh-parity - assert the two ops/keel-fresh copies have not diverged"
	@echo "                    (no per-cluster half: it compares the two trees against each other)"
	@echo "                    (check-job-ttl through check-keel-fresh-parity — those six — also run in"
	@echo "                     the diff-*/apply-* preflight; the first five per cluster, this one whole)"
	@echo "  check-workflows - actionlint (with shellcheck) over .github/workflows/ — not in the preflight"
	@echo ""
	@echo "Hermes VM targets (not cluster-applied):"
	@echo "  check-vm-scripts - shellcheck + ping-body scan the hermes VM's alive-check script"
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
	@echo "  health-upgrade    - take a verified pre-upgrade dump (InfluxDB + Grafana), then STOP"
	@echo "  create-health-cloudflared-secret - imperatively recreate the health cloudflared creds Secret from 1P"
	@echo "  route-health-dns  - create/update CNAMEs for every hostname in the health cloudflared ConfigMap"
	@echo "  health-influx-bucket-bootstrap BUCKET=<name> - create one bucket + mint its ingest token"
	@echo ""
	@echo "Hindsight namespace targets:"
	@echo "  hindsight-upgrade - take a verified pre-upgrade pg_dump, then STOP and print the manual half"

.PHONY: check-tools
check-tools:
	@ok=1; \
	for tool in kubectl kustomize envsubst op direnv talosctl omnictl jq shellcheck actionlint; do \
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

# Assert every standalone `kind: Job` sets spec.ttlSecondsAfterFinished. A
# completed Job that is never garbage collected pins its own immutable
# spec.template, and the next apply that changes it then fails quietly. Runs
# on raw `kustomize build` output, so it needs neither 1Password nor a cluster.
# Rationale and the incident behind it: AGENTS.md.
# Wired into the diff-* / apply-* preflight chains, per cluster: a VPS-only
# render fault must not block an unrelated `apply-homelab`. `check-job-ttl`
# with no suffix still checks both, for a manual sweep.
#
# It sits LAST in each chain, after the context assert. It shells out to
# `kustomize build`, which fetches the bootstrap layer's remote bases, so it is
# the one preflight worth seconds — no reason to pay them before telling you
# that kubectl is pointed at the wrong cluster.
#
# Deliberately NOT wired into build-*: that target's stdout IS the rendered
# manifest, and a prerequisite's `OK: ...` line would land at the top of it,
# corrupting `make build-homelab > out.yaml` and anything that parses it.
# `check-vars-consistency` sits outside build-* for the same reason.
.PHONY: check-job-ttl check-job-ttl-homelab check-job-ttl-vps
check-job-ttl:
	@scripts/check-job-ttl.py

check-job-ttl-homelab:
	@scripts/check-job-ttl.py homelab

check-job-ttl-vps:
	@scripts/check-job-ttl.py vps

# Mirror image of check-placeholder-coverage. That one catches a ${VAR} that
# SURVIVES the render; this one catches a $VAR that must never have been
# rendered in the first place.
#
# Files delivered by a configMapGenerator ride the same stream as every other
# manifest, so envsubst rewrites them too — and envsubst substitutes the BARE
# `$NAME` form, not only `${NAME}` (verified). A script that logs
# `$RESTIC_REPOSITORY` therefore ships the resolved B2 URL inside a ConfigMap,
# and `$RESTIC_PASSWORD` would ship the repository password in plaintext.
# Neither leaves a placeholder behind, so coverage-style checks see nothing.
# Full reasoning and the fix pattern: scripts/check-script-substitution.py.
# Per-cluster variants scope the SCAN to one cluster tree, not the allowlist:
# both allowlists are still applied to every script scanned, because a name
# that is inert under vps/ today goes live the moment a refactor shares that
# file with homelab/. Same build-* exclusion as check-job-ttl above.
.PHONY: check-script-substitution check-script-substitution-homelab check-script-substitution-vps
check-script-substitution:
	@scripts/check-script-substitution.py

check-script-substitution-homelab:
	@scripts/check-script-substitution.py homelab

check-script-substitution-vps:
	@scripts/check-script-substitution.py vps

# Third guard in the same family, and the one that keeps a REPORTED MESSAGE
# honest — a healthchecks.io ping body or an uptime-kuma heartbeat `msg`, which
# since 2026-08-26 are both in play and share one rule set.
#
# A healthchecks.io body leaves the estate: it is a third-party SaaS, the body is
# stored in their object storage, and it is repeated on every run until somebody
# fixes the script. A kuma msg stays on the operator's own VPS, but it is still
# written to a database, still repeated, and still read verbatim into every
# notification the monitor sends. So an `emit` call is a line in a public file
# either way.
#
# The rule it enforces (spec section 9.2) is: never build one from a command's
# output. restic error messages quote the repository URL; the two scripts
# influx-backup.sh execs into the influxdb pod pass the InfluxDB OPERATOR token
# on argv, so anything echoing argv would ship it nightly; and a failing wget or
# curl quotes the URL it was handed, which is the reporting credential either
# way — a ping UUID, or a push token as the last path segment of PUSH_URL.
#
# It recognises a sink by FUNCTION NAME, never by destination host, which is why
# `emit`/`hc_emit`/`hc_summary` kept their names through the migration. Its OK:
# line reports a sink-call COUNT: read it per file, not in aggregate. The
# aggregate fell when multi-line bodies collapsed into one-line messages; what
# must never happen is a file losing its last sink call or dropping out of the
# scan.
#
# Reads source files only — no cluster, no 1Password, no kustomize. So it is on
# BOTH halves of diff-*/apply-*, like check-script-substitution and unlike
# check-job-ttl: a leak guard must fail closed however the target is entered.
# Same build-* exclusion as the other two — build-*'s stdout IS the manifest.
.PHONY: check-ping-bodies check-ping-bodies-homelab check-ping-bodies-vps
check-ping-bodies:
	@scripts/check-ping-bodies.py

check-ping-bodies-homelab:
	@scripts/check-ping-bodies.py homelab

check-ping-bodies-vps:
	@scripts/check-ping-bodies.py vps

# ---------------------------------------------------------------------------
# check-keel-fresh-parity — the two ops/keel-fresh copies must stay in step
# ---------------------------------------------------------------------------
#
# homelab/ops and vps/ops hold a deliberate copy-paste pair: the same runner and
# the same CronJob, twice, because kustomize will not read a generator source
# outside its own root and because the alternative puts a VPS kubeconfig in a
# homelab pod. The invariant that arrangement rests on is EDIT THEM TOGETHER,
# and until this guard existed four source comments saying so were the whole of
# the enforcement. A fix applied to one cluster and not the other is a
# dead-man's-switch that has quietly stopped switching on the cluster nobody
# looked at — the very failure keel-fresh was built to remove.
#
# THE ONE GUARD HERE WITH NO PER-CLUSTER HALF, and that is structural rather
# than an omission: it compares the two trees AGAINST EACH OTHER, so "the VPS
# half of a homelab-versus-VPS comparison" does not exist. It takes no cluster
# argument and rejects one. The consequence, stated rather than discovered: a
# divergence introduced in the VPS copy blocks apply-homelab too. Correct —
# while the pair is out of step neither copy is trustworthy, and the fix is to
# finish the edit rather than route around the gate.
#
# Reads four files and shells out to no renderer, so it is cheap and sits on
# BOTH halves of diff-*/apply-*, like check-script-substitution and
# check-ping-bodies and unlike the three render-based guards.
.PHONY: check-keel-fresh-parity
check-keel-fresh-parity:
	@scripts/check-keel-fresh-parity.py

# ---------------------------------------------------------------------------
# check-script-lint — shellcheck + Python syntax/tests over the RENDERED stream
# ---------------------------------------------------------------------------
# Fourth guard in the same family, and the third instance of the same defect
# check-job-ttl and check-script-substitution were each created to fix: sixteen
# shell and Python scripts run this repo's backups and ingest jobs, and until
# this landed NOTHING the repo could run looked at any of them. No shellcheck
# target, no ruff, no test runner, no CI workflow, no pre-commit hook. Every
# shellcheck result that ever reached a review came from an agent typing the
# command by hand.
#
# It lints the RENDER, not the source tree: homelab/backup/restic-cronjob.yaml
# carries ~430 lines of shell inline in a block scalar, which a source-tree
# lint walks straight past. Shell is checked as POSIX `sh`, never bash — these
# run under busybox ash and dash, and `-s bash` would suppress SC3040 and the
# rest of the SC3xxx portability family, which are the findings that matter.
#
# Same shape as the three checks above: per-cluster variants so a VPS-only render
# fault cannot block an unrelated `apply-homelab`, the bare target sweeps both,
# and the recipe is a one-line shell-out — no inline Python, no inline shell.
# The Python phase is repo-wide whichever cluster is named; it needs no render
# and no cluster, so scoping it would only leave repo tooling unguarded.
#
# Wired into the PUBLIC half of the diff-*/apply-* chains only, exactly like
# check-job-ttl and for the same reason: it shells out to `kustomize build`, so
# duplicating it onto the `_inner` targets would double the render cost of
# every apply. Excluded from build-* like the others, because a prerequisite's
# `OK:` line at the top of that target's stdout would corrupt the manifest.
#
# Requires shellcheck on PATH (`make check-tools` lists it) and treats its
# absence as exit 2 — "could not run", never a silent pass.
.PHONY: check-script-lint check-script-lint-homelab check-script-lint-vps
check-script-lint:
	@scripts/check-script-lint.py

check-script-lint-homelab:
	@scripts/check-script-lint.py homelab

check-script-lint-vps:
	@scripts/check-script-lint.py vps
# --------------------------- end check-script-lint -------------------------

# Fifth guard, and the one that keeps the UPDATE path honest rather than the
# secret path. Two mechanisms update this estate and each is silent when it
# stops: keel for floating tags, Renovate for pinned ones. Every way of getting
# a container's mode wrong fails quietly — a pinned tag carrying keel
# annotations is frozen while looking covered, an incomplete keel set silently
# downgrades a semver tag to `:latest`, and a pinned tag outside Renovate's
# scope receives nothing at all while `homelab-update-watch` stays UP.
#
# It renders the cluster with `kustomize build` and evaluates ONE CONTAINER AT A
# TIME, so a file holding both a keel-managed Deployment and a pinned CronJob is
# judged twice rather than once. keel annotations are a WORKLOAD property, so a
# pinned sidecar beside a floating app image is Renovate's and not frozen; only
# a workload with nothing floating in it can be frozen. It reads renovate.json
# for scope; an image named by no file in ITS OWN cluster's tree came from a
# remote base and is advisory, like check-script-lint's upstream findings. The
# lookup is confined per cluster on purpose — the two trees name many of the
# same images, so a repo-wide one would let a watched homelab file vouch for an
# unwatched VPS container.
#
# Per-cluster variants like the other two RENDER-BASED guards — check-job-ttl
# and check-script-lint — so a VPS-only render fault cannot block an unrelated
# `apply-homelab`. Those three are the whole render-based set; the preflight's
# other two, check-script-substitution and check-ping-bodies, scan source files
# and shell out to nothing. Both variants sit on the PUBLIC half of their
# cluster's diff and apply chains, for the reason this block gives above: the
# guard shells out to a full `kustomize build`, so duplicating it onto the
# inner half would double every apply's render cost — and what it protects is
# the update path, not a secret, so nothing it catches can leak a value.
#
# ARMED 2026-08-26, in the commit that widened Renovate to homelab/** and vps/**
# and de-keeled the two frozen semver pins. Order mattered and still does: the
# guard cannot pass against a renovate.json that watches three namespaces,
# because every pinned, keel-free container outside them genuinely receives
# nothing — the estate's true state, not a bug in the guard. Widen scope first,
# prove a clean run against both renders, then arm. Never the reverse: wiring a
# guard into a preflight it does not pass makes an apply impossible and teaches
# the next person to route around the gate.
.PHONY: check-renovate-scope check-renovate-scope-homelab check-renovate-scope-vps
check-renovate-scope:
	@scripts/check-renovate-scope.py

check-renovate-scope-homelab:
	@scripts/check-renovate-scope.py homelab

check-renovate-scope-vps:
	@scripts/check-renovate-scope.py vps

# ---- check-vm-scripts ------------------------------------------------------
# The hermes VM's files are not rendered by kustomize, so check-script-lint
# cannot see them: it extracts shell from the RENDER. hermes-app-alive.sh still
# ships real logic, and it still posts an uptime-kuma push message, so it gets
# the same two guarantees through its own target — shellcheck as POSIX sh, and
# the ping-body leak guard pointed at the hermes-vm root.
#
# EVERY *.sh UNDER hermes-vm/scripts/ IS LINTED — the daily alive check and the
# weekly sandbox refresh as of 2026-08-29 — via a glob, so a third script is
# covered the day it lands. A glob that matches nothing would report OK having
# checked nothing, but the ping-body guard on the next line hard-fails if
# hermes-app-alive.sh goes missing (it is in that guard's REQUIRED_TARGETS), so
# the tree cannot silently move out from under this target. Updating
# the Hermes app stack is not a script here at all: it is a runbook an agent (or
# the operator) executes about weekly, docs/operations/hermes-vm-updates.md.
# The update wrapper that used to live beside the alive check, along with its
# two test harnesses, its systemd unit and its root-owned entry point, was
# deleted on 2026-08-27: a task that is always run with someone watching does
# not need a thousand lines of unwatched-failure machinery. Nothing mechanical
# guards runbook prose, and no guard in this repository reads it; that is the
# trade the deletion makes.
#
# Not wired into diff-*/apply-*: nothing here is applied to a cluster, so
# gating a cluster apply on it would be noise. That is a real cost — nothing
# runs it on a schedule and the one CI workflow here builds and lints only
# itself, so it can rot unnoticed. What keeps it honest is that it is run
# by hand before anything under hermes-vm/ is copied to the VM.
.PHONY: check-vm-scripts
check-vm-scripts:
	@shellcheck -s sh hermes-vm/scripts/*.sh
	@scripts/check-ping-bodies.py hermes-vm
	@echo "OK: hermes-vm scripts lint clean and ping-body safe"

# ---- check-workflows -------------------------------------------------------
# The shell inside a workflow's `run:` blocks is rendered by nothing here, so
# check-script-lint — which extracts shell from the kustomize RENDER — cannot
# see it. actionlint parses the workflow and hands each `run:` body to
# shellcheck, which is what catches a `for` loop over an interpolated
# multi-line output: that shipped to master on 2026-09-04 and broke the first
# image push with a syntax error.
#
# Every piece of shell in the delivery path is a `run:` block in that workflow,
# so this covers all of it. Keep it that way: actionlint lints WORKFLOWS and
# does NOT reach a composite action's `run:` bodies even when a workflow
# references it (verified), so shell moved under .github/actions/ would be shell
# nothing checks.
#
# NOT in the diff-*/apply-* preflight: a workflow renders nothing and reaches
# no cluster, so gating an apply on it would be noise. It runs in the `lint`
# job of .github/workflows/influxdb-mcp-image.yml on every push and pull
# request, and by hand here.
.PHONY: check-workflows
check-workflows:
	@actionlint -shellcheck=shellcheck .github/workflows/*.yml
	@echo "OK: workflows lint clean"

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
# check-script-substitution-<cluster> is on both halves for the same reason and
# passes the same cost test: it reads a dozen script files and the Makefile,
# and the failure it prevents is a real secret resolved into a ConfigMap, which
# costs a rotation to undo. A guard against that must hold however the recipe
# is entered.
#
# check-job-ttl-<cluster> is deliberately on the PUBLIC half only, and that
# asymmetry is considered, not an oversight. It shells out to a full
# `kustomize build`, which fetches the bootstrap layer's remote bases, so
# duplicating it would double the render cost of every apply — it fails the
# "that is cheap" test the rest of this block rests on. Its failure mode is
# also milder and reversible: a Job with no TTL is not garbage collected and
# the next apply that changes it fails quietly, which `kubectl delete job`
# undoes. No secret escapes. If it ever becomes cheap to run, put it here too.
#
# check-script-lint-<cluster> is on the PUBLIC half only for exactly the same
# reason — it shells out to the same full `kustomize build` — and its failure
# mode is likewise recoverable: a lint finding is a bug you have not shipped
# yet, not a secret you have to rotate.
#
# check-renovate-scope-<cluster> is on the PUBLIC half of all four chains, and
# has been since 2026-08-26. It belongs there for the same reason as
# check-job-ttl and check-script-lint: it shells out to a full
# `kustomize build`, and what it protects is the UPDATE path, not a secret —
# nothing it catches can leak a value.
#
# It arrived later than the other four because arming it needed a scope
# widening first, and that ORDER is the part worth keeping. The guard renders
# each cluster and judges one container at a time, so it has something to say
# about both clusters and exists as a per-cluster pair. Against the
# renovate.json that watched three namespaces it could not pass: every pinned,
# keel-free container outside them genuinely received nothing, which was the
# estate's true state rather than a bug in the guard. So the commit that
# widened Renovate to homelab/** and vps/** proved a clean run against both
# renders and armed both targets on all four chains in the same breath. If the
# scope is ever narrowed again, widen-prove-arm is the order — never wire a
# guard into a preflight it cannot pass, which makes an apply impossible and
# teaches the next person to route around the gate.
#
# check-keel-fresh-parity is on BOTH halves of all four chains, for
# check-script-substitution's reason rather than check-job-ttl's: it reads four
# files, runs no renderer, and must hold however the recipe is entered. It is
# the only guard here with NO per-cluster half — it compares the two trees
# against each other, so there is no half of it to take. It therefore appears
# identically on the homelab and VPS chains, and a divergence introduced in
# either copy blocks both clusters' applies. See the block above its target.
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
diff-homelab: check-vars-consistency check-context check-script-substitution-homelab check-job-ttl-homelab check-ping-bodies-homelab check-script-lint-homelab check-renovate-scope-homelab check-keel-fresh-parity
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
_diff-homelab-inner: check-context check-script-substitution-homelab check-ping-bodies-homelab check-keel-fresh-parity _assert-vars
	@kustomize build homelab/ | envsubst '$(ENVSUBST_VARS)' | kubectl diff -f -; \
	st=($${PIPESTATUS[@]}); \
	if [ $${st[0]} -ne 0 ]; then echo "ERROR: kustomize build failed (exit $${st[0]}) — diff above is incomplete" >&2; exit 1; fi; \
	if [ $${st[1]} -ne 0 ]; then echo "ERROR: envsubst failed (exit $${st[1]}) — diff above is incomplete" >&2; exit 1; fi; \
	if [ $${st[2]} -gt 1 ]; then echo "ERROR: kubectl diff failed (exit $${st[2]})" >&2; exit $${st[2]}; fi; \
	exit 0

.PHONY: apply-homelab
apply-homelab: check-vars-consistency check-context check-script-substitution-homelab check-job-ttl-homelab check-ping-bodies-homelab check-script-lint-homelab check-renovate-scope-homelab check-keel-fresh-parity
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
_apply-homelab-inner: check-context check-script-substitution-homelab check-ping-bodies-homelab check-keel-fresh-parity _assert-vars
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

# SSH private key for the hermes-pull CronJob (backup namespace). Multi-line,
# so it cannot ride the envsubst pipeline. This target deliberately does NOT
# copy create-jotta-secret's --from-literal shape, for two reasons specific to
# an SSH key:
#   1. `$$(op read ...)` command substitution strips the key's final newline,
#      and OpenSSH rejects an OPENSSH PRIVATE KEY block without one ("invalid
#      format") — the Secret would apply cleanly and the first pull would
#      fail. The pipe preserves op read's output byte for byte.
#   2. --from-literal puts the key bytes on kubectl's argv, briefly visible in
#      local process listings; --from-file=/dev/stdin keeps them off argv.
# The jotta values tolerate a stripped newline; a future SSH key must use this
# shape. Idempotent; re-run after rotating the key in 1Password.
# `set -o pipefail` so a failed `op read` (revoked token, renamed item) fails
# the target instead of applying a Secret with an empty key.
.PHONY: create-hermes-ssh-secret
create-hermes-ssh-secret: check-context
	@set -o pipefail; \
	op read 'op://Homelab/hermes-ssh-key/private key' \
	  | kubectl create secret generic hermes-ssh \
	      --namespace backup \
	      --from-file=id_ed25519=/dev/stdin \
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

# --- hindsight namespace ---------------------------------------------------
#
# `make hindsight-upgrade` — the pre-upgrade dump, and nothing else.
#
# DUMB, LOUD, AND IT STOPS BEFORE THE INTERESTING PART. Hindsight's migrations are
# forward-only, so THE DUMP IS THE ROLLBACK; taking it is the one step a human
# reliably skips under time pressure, and it is the only step this target
# automates. It never edits a pin, never merges a pull request and never applies:
# chaining into `make apply-homelab` would apply EVERY pending change in the tree,
# unreviewed. Merging, reading the diff, applying, watching the rollout and
# deciding to roll back all stay manual — they are judgement calls with well-worn
# guarded commands, and automating them produces a target nobody trusts enough to
# run.
#
# A flat recipe, not a parameterised macro: the Makefile's stated doctrine is that
# copy-paste duplication beats abstraction here, and a future `make health-upgrade`
# is meant to be a sibling copy. A scripts/*.sh helper was considered and rejected
# — `check-script-lint` only lints scripts that appear in the kustomize render, so
# a repo-level helper would be the estate's first unlinted shell file.
#
# NOTE THE `$$(date …)`. Inside a Make recipe, `$(date …)` is a MAKE variable
# reference and expands to the empty string. The timestamp is captured ONCE so
# every later step names the same Job.
#
# The Job is created with `--from=cronjob/…`, so it inherits the whole pod spec —
# image, Secret, script ConfigMap, PVC mount, ttlSecondsAfterFinished. Nothing is
# duplicated here to drift, it is CronJob-shaped and therefore exempt from
# check-job-ttl, and it self-collects. NO `kind: Job` MANIFEST IS ADDED TO THE
# TREE: that walks straight into the immutable-spec.template trap that broke
# apply-homelab for four months.
.PHONY: hindsight-upgrade
hindsight-upgrade: check-context
	@kubectl -n hindsight get cronjob hindsight-pg-dump >/dev/null 2>&1 || { \
	  echo "ERROR: cronjob/hindsight-pg-dump not found in namespace hindsight."; \
	  echo "  This target exists to take a verified pre-upgrade dump. A target that"; \
	  echo "  silently 'succeeds' without dumping is the worst possible outcome, so"; \
	  echo "  it refuses rather than guessing."; \
	  exit 1; \
	}
	@active=$$(kubectl -n hindsight get jobs \
	    -o jsonpath='{range .items[?(@.status.active)]}{.metadata.name}{"\n"}{end}' \
	  | grep -E '^(hindsight-pg-dump|pre-upgrade)' || true); \
	if [ -n "$$active" ]; then \
	  echo "ERROR: a dump Job is already running:"; \
	  echo "$$active" | sed 's/^/  /'; \
	  echo "  concurrencyPolicy: Forbid governs only CronJob-OWNED Jobs and cannot"; \
	  echo "  see a manual one, so this guard is what keeps two dumps apart. Wait for"; \
	  echo "  it, or watch it: kubectl -n hindsight logs -f job/<name>"; \
	  exit 1; \
	fi
	@set -e; \
	ts=$$(date -u +%Y%m%d%H%M%S); job=pre-upgrade-$$ts; \
	kubectl -n hindsight create job --from=cronjob/hindsight-pg-dump "$$job"; \
	if kubectl -n hindsight wait --for=condition=complete "job/$$job" --timeout=900s; then \
	  kubectl -n hindsight logs "job/$$job" --tail=20 || true; \
	  echo ""; \
	  echo "### Pre-upgrade dump complete: $$job"; \
	  echo "### The dump is the rollback. Migrations are forward-only."; \
	  echo "###"; \
	  echo "### Next, by hand:"; \
	  echo "### DEPLOY BEFORE YOU MERGE. master records what is running, not what is intended."; \
	  echo "###   1. gh pr checkout <n> for the Renovate \"hindsight stack\" PR, then"; \
	  echo "###      git rebase origin/master. Carry any other deployed-but-unmerged branch:"; \
	  echo "###      an apply reconciles the whole tree and would revert it."; \
	  echo "###   2. make diff-homelab      <- READ IT. Confirm only the image lines moved."; \
	  echo "###   3. make apply-homelab"; \
	  echo "###   4. kubectl -n hindsight rollout status deploy/hindsight --timeout=600s"; \
	  echo "###   5. Verify: the startup probe settles, then \`hermes memory status\` on VM 103"; \
	  echo "###   6. git push --force-with-lease  <- the rebase rewrote the branch. Without"; \
	  echo "###      this, gh pr merge merges the tree you did NOT deploy."; \
	  echo "###   7. Only now: gh pr merge <n> --squash --delete-branch, then git pull on master"; \
	  echo "###      (do NOT close it unmerged - not a supported move today, and it"; \
	  echo "###       snoozes homelab-update-watch; docs/operations/monitoring.md)"; \
	  echo "###   8. Confirm the homelab-update-watch monitor goes UP after the next"; \
	  echo "###      06:45 run"; \
	  echo "###      (or force one: kubectl -n ops create job --from=cronjob/update-watch now-$$ts)"; \
	  echo "###"; \
	  echo "### If it goes wrong, the restore runbook is in docs/operations/hindsight.md."; \
	else \
	  failed=$$(kubectl -n hindsight get "job/$$job" \
	    -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null || true); \
	  kubectl -n hindsight logs "job/$$job" --tail=40 || true; \
	  echo ""; \
	  if [ "$$failed" = "True" ]; then \
	    echo "### DUMP FAILED - do not upgrade."; \
	    echo "### The log above carries the script's own tables= and dump_bytes= numbers."; \
	  else \
	    echo "### DUMP STILL RUNNING after 900s - do not upgrade; do not delete the job."; \
	    echo "### Watch it: kubectl -n hindsight logs -f job/$$job"; \
	    echo "### 900s is shorter than the CronJob's own activeDeadlineSeconds (3600) on"; \
	    echo "### purpose: a pre-upgrade dump that has not finished in 15 minutes on this"; \
	    echo "### database is something to look at, not to wait out."; \
	  fi; \
	  echo "###"; \
	  echo "### The Job is left in place - its TTL collects it, and until then it is"; \
	  echo "### inspectable. The monitor goes DOWN either way: on the exit trap's"; \
	  echo "### 'down' push, or - if the Job is still running, or was killed before"; \
	  echo "### the trap could run - when the heartbeat interval plus retry expires."; \
	  exit 1; \
	fi

# --- health namespace ------------------------------------------------------
#
# `make health-upgrade` — the pre-upgrade dump, and nothing else. Closes issue #54.
#
# A FLAT SIBLING OF hindsight-upgrade, copy-pasted rather than parameterised, per
# this Makefile's stated doctrine that duplication beats abstraction at two
# instances. Read that target first; everything structural here is the same, and
# only the namespace, the CronJob name, the timeout and the banner differ.
#
# WHAT IT COVERS, WHICH IS BOTH STATEFUL COMPONENTS. The `influx-backup` CronJob
# is misnamed by history: it takes the InfluxDB logical export AND the Grafana
# SQLite dump (grafana-sqlite-backup.py, through a read-only mount of the
# grafana-data PVC). Issue #54's open question — whether Grafana needed a real
# logical backup before this target could be honest — was answered by building
# one, so this target's banner can promise a rollback for both.
#
# The target performs NO verification of its own. influx-backup.sh asserts its
# own artifacts — every expected bucket present, every prune glob matching, the
# Grafana dump over its byte and schema-object floors — and a second, weaker copy
# of those assertions would only create a place for the two to disagree. Note
# what that list does NOT include: those are existence checks, so a stale dump
# satisfies them. Artifact FRESHNESS is the restic gate's 30 h check, a different
# Job half an hour later.
#
# WHERE THE 600s COMES FROM. Measured, not guessed. The retained nightly Jobs ran
# 26s and 25s start-to-completion; a timed run of THIS target on 2026-08-26 took
# 27s (18:17:29 -> 18:17:56), so a manual `--from=cronjob/` Job behaves like a
# scheduled one and the nightly history is a fair guide. 600s is ~22x that, which
# buys a cold `alpine/k8s` pull and years of data growth, and it stays well under
# the CronJob's own activeDeadlineSeconds of 3600 — the pod is killed there
# regardless, so a longer wait would only sit watching a Job the cluster has
# already given up on. The failure mode of a tight timeout is the expensive one:
# FAILED printed over a healthy dump.
#
# NOTE THE `$$(date …)`. Inside a Make recipe `$(date …)` is a MAKE variable
# reference and expands to the empty string. The timestamp is captured ONCE so
# every later step names the same Job.
#
# The Job is created with `--from=cronjob/…`, so it inherits the whole pod spec —
# image, ServiceAccount, script ConfigMap, both PVC mounts, ttlSecondsAfterFinished
# (172800, confirmed on the real run), so it self-collects. NO `kind: Job`
# MANIFEST IS ADDED TO THE TREE: that walks straight into the
# immutable-spec.template trap that broke apply-homelab for four months. Nothing
# here is exempted from check-job-ttl, either — that guard reads the kustomize
# render, and a Job created at run time never appears in it.
#
# THE CONCURRENCY GUARD FILTERS ON THE OWNER, NOT THE NAME. `kubectl create job
# --from=cronjob/X` sets an ownerReferences entry naming X (controller: true) and
# a `cronjob.kubernetes.io/instantiate: manual` annotation — verified with a
# client-side dry run — so a manual Job is as identifiable as a scheduled one and
# NOTHING here depends on what anyone calls it. An earlier draft matched name
# prefixes instead, which missed the `grafana-predump` that this repo's own
# documentation used to tell an operator to create: the one collision the guard
# exists to catch. Names are a convention nothing enforces; the owner is set by
# the API server. The residual is a Job someone hand-rolls with a copied pod spec
# and no owner, which nothing here can see and no documented procedure produces.
# One corner of that residual is worth naming because the old guard did cover it:
# an OWNERLESS Job called exactly `influx-backup` leaves the owner field empty, so
# awk reads the name into $$1 and prints nothing, and the guard passes. Accepted —
# it needs someone to hand-roll a Job under the CronJob's own name.
.PHONY: health-upgrade
health-upgrade: check-context
	@kubectl -n health get cronjob influx-backup >/dev/null 2>&1 || { \
	  echo "ERROR: cronjob/influx-backup not found in namespace health."; \
	  echo "  This target exists to take a verified pre-upgrade dump. A target that"; \
	  echo "  silently 'succeeds' without dumping is the worst possible outcome, so"; \
	  echo "  it refuses rather than guessing."; \
	  exit 1; \
	}
	@active=$$(kubectl -n health get jobs \
	    -o jsonpath='{range .items[?(@.status.active)]}{.metadata.ownerReferences[*].name}{" "}{.metadata.name}{"\n"}{end}' \
	  | awk '$$1 == "influx-backup" { print $$2 }'); \
	if [ -n "$$active" ]; then \
	  echo "ERROR: a dump Job is already running:"; \
	  echo "$$active" | sed 's/^/  /'; \
	  echo "  concurrencyPolicy: Forbid governs only the Jobs the CronJob itself"; \
	  echo "  creates, so this guard is what keeps a manual dump off the staging"; \
	  echo "  path a running one is using. Wait for it, or watch it:"; \
	  echo "    kubectl -n health logs -f job/<name>"; \
	  exit 1; \
	fi
	@set -e; \
	ts=$$(date -u +%Y%m%d%H%M%S); job=pre-upgrade-$$ts; \
	kubectl -n health create job --from=cronjob/influx-backup "$$job"; \
	if kubectl -n health wait --for=condition=complete "job/$$job" --timeout=600s; then \
	  kubectl -n health logs "job/$$job" --tail=25 || true; \
	  echo ""; \
	  echo "### Pre-upgrade dump complete: $$job"; \
	  echo "### It covers BOTH stateful components: the InfluxDB logical export and"; \
	  echo "### the Grafana SQLite dump. The log above ends with the Grafana dump's"; \
	  echo "### own size and schema-object count, then influx-backup.sh's own"; \
	  echo "### 'detail:' line, which carries every artifact size and count"; \
	  echo "### (lp_files= is one export per bucket). The one-line heartbeat sent to"; \
	  echo "### the health-influx-backup monitor carries only the verdict, buckets="; \
	  echo "### and grafana_kib=, so the log above is the fuller record."; \
	  echo "###"; \
	  echo "### Next, by hand. DEPLOY, THEN MERGE - never the other way round:"; \
	  echo "###   1. gh pr checkout <the Renovate \"health stack\" PR>. Do NOT merge it"; \
	  echo "###      yet: master records what has been deployed, never intent. Do NOT"; \
	  echo "###      close it unmerged either - that is not a supported move today;"; \
	  echo "###      docs/operations/monitoring.md carries the reasoning."; \
	  echo "###   2. git rebase origin/master, and carry every other deployed-but-"; \
	  echo "###      unmerged branch that touches these files. Find them with:"; \
	  echo "###      gh pr list --state open   <- read the FILE list, not just titles"; \
	  echo "###   3. make diff-homelab      <- READ IT IN FULL. Only the image lines"; \
	  echo "###      may move, beyond the usual always-differs Secrets, PVs and"; \
	  echo "###      cert-manager webhooks. Anything else is a revert until proven."; \
	  echo "###   4. make apply-homelab"; \
	  echo "###   5. kubectl -n health rollout status deploy/influxdb --timeout=600s"; \
	  echo "###      kubectl -n health rollout status deploy/grafana  --timeout=600s"; \
	  echo "###   6. Verify ingest: force one freshness run and read its POD LOG -"; \
	  echo "###      kubectl -n health create job --from=cronjob/ingest-freshness now-$$ts"; \
	  echo "###   7. Open a Grafana dashboard and confirm it renders against InfluxDB."; \
	  echo "###   8. git push --force-with-lease   <- the rebase rewrote this branch."; \
	  echo "###      Without this, gh pr merge merges the tree you did NOT deploy;"; \
	  echo "###      --force-with-lease refuses if anyone else pushed to it since."; \
	  echo "###   9. ONLY NOW, with the cluster healthy:"; \
	  echo "###      gh pr merge --squash --delete-branch   (this repo squashes only)"; \
	  echo "###      git checkout master && git pull"; \
	  echo "###  10. Confirm the homelab-update-watch monitor is UP after the next 06:45 run"; \
	  echo "###      (or force one: kubectl -n ops create job --from=cronjob/update-watch now-$$ts)"; \
	  echo "###"; \
	  echo "### If it goes wrong, the restore runbook is in docs/operations/homelab-health.md."; \
	  echo "### A Grafana MAJOR migrates grafana.db in place on first start, so its"; \
	  echo "### rollback is a restore from the dump above, never a tag revert."; \
	else \
	  failed=$$(kubectl -n health get "job/$$job" \
	    -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null || true); \
	  kubectl -n health logs "job/$$job" --tail=40 || true; \
	  echo ""; \
	  if [ "$$failed" = "True" ]; then \
	    echo "### DUMP FAILED - do not upgrade."; \
	    echo "### The log ends with whatever failed: a FATAL: line from influx-backup.sh"; \
	    echo "### or from one of the scripts it runs, or else the underlying tool's own"; \
	    echo "### error. The health-influx-backup heartbeat names it as failed_step="; \
	    echo "### whenever the script exited normally - every failure except a kill"; \
	    echo "### (OOM, eviction, the active deadline), where no exit trap runs and"; \
	    echo "### so nothing is pushed at all."; \
	  else \
	    echo "### DUMP STILL RUNNING after 600s - do not upgrade; do not delete the job."; \
	    echo "### Watch it: kubectl -n health logs -f job/$$job"; \
	    echo "### 600s is shorter than the CronJob's own activeDeadlineSeconds (3600) on"; \
	    echo "### purpose: this export takes about 27 seconds, so one still running after"; \
	    echo "### 10 minutes is something to look at, not to wait out."; \
	  fi; \
	  echo "###"; \
	  echo "### The Job is left in place - its TTL collects it, and until then it is"; \
	  echo "### inspectable. The monitor goes DOWN either way: on the exit trap's"; \
	  echo "### 'down' push, or - if the Job is still running, or was killed before"; \
	  echo "### the trap could run - when the heartbeat interval plus retry expires."; \
	  exit 1; \
	fi

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
                    KARAKEEP_OPENAI_API_KEY VPS_OPS_KUMA_KEEL_TOKEN \
                    VPS_HOMELAB_PROXY_ACCESS_CLIENT_ID \
                    VPS_HOMELAB_PROXY_ACCESS_CLIENT_SECRET

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
diff-vps: check-vps-context check-vps-vars-consistency check-script-substitution-vps check-job-ttl-vps check-ping-bodies-vps check-script-lint-vps check-renovate-scope-vps check-keel-fresh-parity
	@$(OP_RUN) $(MAKE) --no-print-directory _diff-vps-inner

# See _diff-homelab-inner for why this is a PIPESTATUS check and not `|| true`.
.PHONY: _diff-vps-inner
_diff-vps-inner: check-vps-context check-script-substitution-vps check-ping-bodies-vps check-keel-fresh-parity _assert-vps-vars
	@kustomize build vps/ | envsubst '$(VPS_ENVSUBST_VARS)' | kubectl diff -f -; \
	st=($${PIPESTATUS[@]}); \
	if [ $${st[0]} -ne 0 ]; then echo "ERROR: kustomize build failed (exit $${st[0]}) — diff above is incomplete" >&2; exit 1; fi; \
	if [ $${st[1]} -ne 0 ]; then echo "ERROR: envsubst failed (exit $${st[1]}) — diff above is incomplete" >&2; exit 1; fi; \
	if [ $${st[2]} -gt 1 ]; then echo "ERROR: kubectl diff failed (exit $${st[2]})" >&2; exit $${st[2]}; fi; \
	exit 0

.PHONY: apply-vps
apply-vps: check-vps-context check-vps-vars-consistency check-script-substitution-vps check-job-ttl-vps check-ping-bodies-vps check-script-lint-vps check-renovate-scope-vps check-keel-fresh-parity
	@$(OP_RUN) $(MAKE) --no-print-directory _apply-vps-inner

# Same render-fully-then-apply shape as _apply-homelab-inner; see the note there.
.PHONY: _apply-vps-inner
_apply-vps-inner: check-vps-context check-script-substitution-vps check-ping-bodies-vps check-keel-fresh-parity _assert-vps-vars
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

# Shell helper shared by both health-influx-*-bootstrap targets: run an influx
# CLI command inside the influxdb pod, authenticated as admin.
#
# The influx CLI reads INFLUX_TOKEN, not DOCKER_INFLUXDB_INIT_ADMIN_TOKEN, and
# the CLI config the image writes at first init lives on an ephemeral path - so
# after any pod restart the CLI is unauthenticated and every call returns
# `401 Unauthorized`. Exporting it from the container's own environment keeps
# the admin token inside the cluster: reading it here with `op read` would put
# it in this shell's argv, visible to `ps`.
#
# Recursive `=`, so the `$$` survives into the recipe and Make expands it to a
# single `$` exactly as it did when this was written inline. The trailing `;`
# is deliberate - this is spliced into a one-shell recipe as its own statement.
INFLUX_POD_FN = pod() { kubectl -n health exec deploy/influxdb -- sh -c 'export INFLUX_TOKEN="$$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN"; exec "$$@"' _ "$$@"; };

# Create one health InfluxDB bucket and mint its read-and-write ingest token.
# One bucket per run:
#   make health-influx-bucket-bootstrap BUCKET=sleep
#
# RUN IN A PLAIN TERMINAL, NOT INSIDE AN AGENT SESSION. InfluxDB 2.9 hash-stores
# tokens, so the printed value is the only copy that will ever exist, and a token
# printed in a session lands in that transcript and must then be rotated under
# the secrets-to-rotate.md rule. Claude Code's `!` prefix does not help: that
# still executes in the session and the output still reaches the model.
#
# THE SHARED READ TOKEN NEEDS NO ATTENTION. It was re-minted once, org-wide, so
# it already reads every bucket in cynexia including one created after the mint.
#
# Why -r 0, why the token reads as well as writes, why the shared read token is
# not touched, and the three-edit rule for a new bucket: see the InfluxDB
# bootstrap section of docs/operations/homelab-health.md. The recipe's last line
# prints the three-edit reminder at the moment it applies.
.PHONY: health-influx-bucket-bootstrap
health-influx-bucket-bootstrap: check-context
	@set -euo pipefail; \
	: "$${BUCKET:?usage: make health-influx-bucket-bootstrap BUCKET=<name>}"; \
	$(INFLUX_POD_FN) \
	pod influx bucket create -n "$$BUCKET" -o cynexia -r 0 || true; \
	id=$$(pod influx bucket list -o cynexia -n "$$BUCKET" --hide-headers | awk '{print $$1}'); \
	if [ -z "$$id" ]; then echo "FATAL: bucket '$$BUCKET' not found in org cynexia" >&2; exit 1; fi; \
	tok=$$(pod influx auth create -o cynexia --read-bucket $$id --write-bucket $$id \
	  -d "$$BUCKET ingest read+write" --json | jq -r '.token // empty'); \
	if [ -z "$$tok" ]; then echo "FATAL: influx auth create returned no token" >&2; exit 1; fi; \
	echo "--- $$BUCKET INGEST TOKEN (paste into 1Password now — this is the only copy):"; \
	printf '%s\n' "$$tok"; \
	echo "--- next: add '$$BUCKET' to influx-export-lp.sh's bucket list and raise LP_EXPECTED in influx-backup.sh"
