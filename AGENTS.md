# Kubernetes Config Repository

Personal Kubernetes cluster config: a home media/downloads stack plus a health-data pipeline on **Talos Linux** (managed by Omni), and a second public-facing Talos cluster on a Hetzner VPS.

This file is **background and conventions only** — what the repo is, how it's laid out, and the rules for editing it.
Cluster-specific detail, runbooks and procedures live under `docs/`, referenced from here rather than duplicated.

## Documentation

| Document | Covers |
|---|---|
| `docs/operations/omni-access.md` | **Start here on a new machine.** Bootstrapping omnictl/kubectl/talosctl from zero, where omniconfig and SideroV1 keys land, Omni/talosctl troubleshooting |
| `docs/operations/apply-workflow.md` | Secret pipeline end to end, full Makefile target reference, Talos config patches, Tailscale bootstrap, why `apply` always says `configured` |
| `docs/operations/homelab.md` | Homelab cluster: platform stack, namespaces/workloads, NFS and storage, node network, DNS/Route53, encryption at rest, operational gotchas |
| `docs/operations/homelab-health.md` | The `health` namespace: ingest pipeline, image-pin rationale, InfluxDB bootstrap, backups/restore, Garmin re-auth, monitoring, probe rationale |
| `docs/operations/vps.md` | VPS cluster: shape, workloads, Cloudflare tunnel/Access, DB decisions, backups |
| `docs/operations/monitoring.md` | How failures get noticed: the triage table, probe policy and inventory, CronJob deadlines, the backup verification gates, the five healthchecks.io checks that remain, the twelve uptime-kuma push monitors, the disclosure rules for both, and what none of it catches |
| `docs/operations/uptime-kuma.md` | Layer 3/4 runbook: creating uptime-kuma monitors by hand, per-monitor HTTP settings, the Cloudflare Access trap, the push monitors driven from inside the clusters (and the one driven from the hermes VM) and the bypass they need, the self-monitor |
| `docs/operations/hindsight.md` | The `hindsight` namespace: the self-hosted memory backend for the Hermes profiles — topology, auth, the extraction LLM and its provider, the canary, upgrade and restore runbooks, the restore drill, key rotation, and the removal path |
| `docs/operations/estate-updates.md` | How the estate gets patched: the two update modes, the Talos/Kubernetes version ledger, the advisory feeds and the out-of-band rule, the hand-managed kustomize pins, and the Omni etcd-backup mechanism. The Hermes VM step is `docs/operations/hermes-vm-updates.md`; the session that does the work is the `/update-estate` skill |
| `docs/operations/hermes-vm.md` | The Hermes VM itself: lingering, triaging a DOWN `hermes-app-alive`, installing the kept components, `unattended-upgrades` with its automatic reboot, what the daily check does not watch, the trade the in-gateway cron job makes, the docker terminal sandboxes with their managed scope and per-profile mounts, the runbook for creating a profile, and the VM's own facts |
| `docs/operations/hermes-vm-updates.md` | The update runbook for the Hermes application stack, run by an agent or the operator roughly weekly: preconditions, change analysis, the detached update, verification, the report ping, and manual rollback. Steps and latent hazards only — everything observable at failure time is left to the agent running it |
| `docs/operations/safer-web-reader.md` | The quarantined web-reader profile and its completion broker: the four-tool surface, the envelope contract, the deployed configuration baseline, and its verification record |

Design documents and implementation plans are local-only under the gitignored `docs/superpowers/` tree (`specs/2026-04-11-talos-homelab-rebuild-design.md`, `plans/2026-04-11-talos-homelab-rebuild.md`).

## Clusters at a glance

| | Homelab | VPS |
|---|---|---|
| kubectl context | `cynexia-homelab` | `cynexia-vps` |
| Omni cluster name | `homelab` | `vps` |
| Domain | `*.cynexia.net` (Route53) | `*.cynexia.com` (Cloudflare) |
| Exposure | Private — LAN/Tailscale only, except the `cynexia-health` cloudflared tunnel, run from the `health` namespace | Public, through the `cynexia-vps` cloudflared tunnel + Cloudflare Access |
| Ingress | Traefik hostNetwork DaemonSet + cert-manager wildcard | cloudflared only (no Traefik, no cert-manager) |
| Apply | `make apply-homelab` | `make apply-vps` |

The two domains are unrelated zones on different providers.
Don't cross them.

## Repo Layout

```
kubernetes_config/
├── .envrc                    # direnv entrypoint (loads 1Password-backed vars)
├── .env.tpl                  # op-template with VAR=op://... lines (committed; no real secret values)
├── Makefile                  # build/diff/apply per cluster + secret and bootstrap helpers
├── renovate.json             # scoped to homelab/** and vps/** (pinDigests, off on the keel-managed trees)
├── secrets-to-rotate.md      # honesty box for disclosed secret values (identifiers only)
├── docs/                     # operational documentation (docs/superpowers/ is gitignored)
├── .github/workflows/        # the repo's one workflow: builds the InfluxDB MCP image
├── homelab/                  # Talos homelab cluster
│   ├── kustomization.yaml    # top-level: bootstrap + secrets + workloads + backup + health + hindsight
│   ├── talos/                # Omni ConfigPatches resources (applied via `make apply-talos`)
│   ├── bootstrap/            # platform: namespaces (with PSA labels), local-path, NFS CSI, cert-manager, traefik, keel
│   ├── workloads/            # application workloads (one file per service, --- separated, no ns override)
│   ├── secrets/              # Secret manifests with ${VAR} envsubst placeholders
│   ├── health/               # health-data pipeline (no keel; pinned images)
│   │   ├── scripts/          # job scripts as real files + their tests; mounted via configMapGenerator
│   │   └── mcp/              # build inputs for the InfluxDB MCP image (Dockerfile, pinned package, lockfile, --import hook)
│   ├── ops/                  # cluster-wide operational jobs (the daily Renovate update watcher)
│   │   └── scripts/          # same pattern: real files + tests, via configMapGenerator
│   ├── hindsight/            # Hindsight memory backend for the Hermes profiles (no keel; pinned images)
│   │   └── scripts/          # nightly pg_dump + the 15-minute canary; mounted via configMapGenerator
│   └── backup/               # restic init Job + nightly CronJob (hostPath /var/mnt/ssd/local-path-provisioner)
├── vps/                      # Hetzner Talos cluster, same sub-layout (bootstrap/secrets/workloads/backup/ops/talos)
├── hermes-vm/                # files that live on the hermes VM, not in a cluster
│   ├── scripts/              # the daily alive check + the weekly sandbox refresh (both hermes cron jobs) + the hand-run profile docker setup helper
│   ├── etc/                  # unattended-upgrades config + the two apt timer drop-ins + the managed-scope hermes config (/etc/hermes/config.yaml)
│   ├── plugins/              # canonical copies of the profile-scoped Hermes plugins (safer-reader-broker + its test)
│   ├── profiles/             # canonical copies of the per-profile SOUL.md personas
│   └── skills/               # canonical copies of promoted Hermes skills (untrusted-web-content-analysis)
├── scripts/                  # repo-level helpers (karakeep tags, FreshRSS WebSub status, the check-* guards)
├── legacy-microk8s/          # frozen reference copies of the old microk8s manifests
└── no_longer_used/           # retired manifests kept for reference
```

## Apply Workflow (conventions)

Full mechanics, target-by-target reference and failure modes: `docs/operations/apply-workflow.md`.
The rules that must not be broken:

- **Secrets reach manifests through `op run` + envsubst, resolved per command.**
  `.env.tpl` holds only `VAR=op://Vault/item/field` lines.
  `.envrc` exports **only** `OP_SERVICE_ACCOUNT_TOKEN` — no secret value ever enters the ambient environment.
  The `Makefile` defines `OP_RUN := op run --env-file=.env.tpl --`, and every build/diff/apply target runs its guards in the parent shell then re-enters make under it, so values exist inside one child process only.
  **The old `set -a` + `op inject` block in `.envrc` is gone deliberately — do not restore it.**
  Because `OP_SERVICE_ACCOUNT_TOKEN` lives in the shell environment once direnv has exported it, `op run` — and therefore every build/diff/apply target — works from **any directory in that shell, git worktrees included**: no avoiding worktrees for `op`-dependent work (operator ruling, 2026-08-27).
  direnv keys its allow record on path *and* content, though, so a fresh worktree's committed `.envrc` starts unallowed: if direnv reports `.envrc is blocked` on entering one, run `direnv allow` there once.
- **Never commit plaintext secret values.**
  `${VAR}` placeholders only.
- **`op run` masks stdout, not env vars** — corrected 2026-08-20; the previous claim in this file was a misdiagnosis.
  `op run` passes the **real** values in the child environment (verified intact at 100 and 27 characters); it redacts secrets in the child's **output**.
  (`len=${#ACME_EMAIL}` returning 24 was a coincidence — that value is genuinely 24 characters.)
  The real hazard is therefore rendering to a file — and `build-*` is already wrapped in `op run` by the Makefile, so no extra wrapper is needed to hit it: `make build-homelab > out.yaml` writes `<concealed by 1Password>` into the Secrets and `kubectl apply -f out.yaml` stores the mask.
  **Never render-then-apply**; `diff-*`/`apply-*` keep the stream inside the child, where values are real.
  **Never redirect `diff-*` either** — but not for the reason this file used to give.
  `kubectl diff` redacts a Secret's `data` itself, so a redirected diff is not the plaintext dump once claimed here.
  What neither mechanism covers is the residual: encoded or JSON-escaped secret material carried by a resource that is *not* a Secret, which kubectl does not redact and `op run`'s literal-plaintext match does not catch.
  A file on disk is where that residual gets committed or pasted, so the ban stands as defence in depth, and it also covers a future regression in kubectl's own redaction.
  Detail: `docs/operations/apply-workflow.md`.
- **An agent reads a diff through a filter, because its terminal is a transcript.**
  Two standing rules collide on `diff-*`: read every resource the diff names, and never print a resolved secret.
  An agent's terminal output is a conversation transcript that persists, so the human rule "read it on screen and move on" does not carry over.
  Two mechanisms already do the protecting, and neither of them is the filter.
  `kubectl diff` redacts a v1 Secret's `data` itself — `--show-secrets` defaults to false and no target here passes it — so a changed Secret prints `*** (before)` / `*** (after)` and never its values; the `stringData` manifests in this repo are covered too, because the comparison runs on server-side objects where `stringData` has already been folded into `data`.
  `op run` masks verbatim plaintext everywhere else in the stream.
  The residual is secret material that is neither: encoded or JSON-escaped, and carried by a resource that is not a Secret.
  The mechanism block above `diff-homelab` in the `Makefile` sets out what each one covers.
  The pipelines below are a reading aid and a second line of defence, not the protection.
  To get the resource list, which is what the `/update-estate` skill's read-the-diff gate is actually about:

      make diff-homelab 2>&1 | grep -E '^diff -u -N' | sed -E 's#.*/(LIVE|MERGED)-[0-9]+/##' | awk '{print $NF}' | sort -u

  An empty list means the tree agrees with the cluster only if the guards' `OK:` lines went past first — `2>&1 | grep` discards a guard's failure text and the pipe discards make's exit status — so on an empty list re-read it through the masking pipeline below, which passes error text through.
  To read the body, with long base64-looking values masked:

      make diff-homelab 2>&1 | sed -E 's/^([-+ ]?[[:space:]]*[A-Za-z0-9_.-]+:[[:space:]]+)[A-Za-z0-9+/=]{24,}[[:space:]]*$/\1<redacted-base64>/'

  Two things that mask does not do: it does not touch a value shorter than 24 characters, and it only sees a single `key: value` line, so a block-scalar payload such as the `last-applied-configuration` annotation passes through untouched.
  It leaves image references intact, because every image reference in this repo carries a tag or an `@sha256:` digest and so misses the pattern — by convention, not by guarantee.
  Neither command redirects, and neither may be changed into one.
  When a session's tooling refuses either pipeline — the worktree Bash guard did so on 2026-09-02 — read every resource the list names directly instead: the live object with `kubectl get <kind> <name> -n <ns> -o yaml`, plus the branch diff for the file that renders it.
  Never skip the read, and never redirect to a file.
- **`ENVSUBST_VARS` is an explicit allowlist, passed single-quoted.**
  Never call envsubst without one: with no allowlist it eats every `${VAR}` in the stream, including shell variables inside upstream manifests (`$VOL_DIR` in local-path-provisioner's helper pod); with double quotes the shell expands the tokens before envsubst sees them.
- **Adding a secret means four edits:** the `op://` line in `.env.tpl`, the name in `ENVSUBST_VAR_NAMES`, the name in `REQUIRED_VARS`, and the `${VAR}` placeholder in the manifest.
  `make check-vars-consistency` hard-fails if a substituted var is missing from `REQUIRED_VARS`.
  A var missing from `ENVSUBST_VAR_NAMES` is caught too, but **at apply time only**: the Makefile's `PLACEHOLDER_SCAN` runs inside `apply-homelab` and `apply-vps` after rendering and before kubectl, and hard-fails naming any surviving `${VAR}` whose name is declared in `.env.tpl`, so nothing is applied and no literal placeholder reaches a Secret.
  The asymmetry matters: `diff-*` does **not** run that scan, so a diff can look clean while the apply refuses.
  To confirm no placeholder survived the render after adding one, run `make build-<cluster> | grep -F "$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/${\1}/p' .env.tpl)"` — it prints nothing on a clean tree.
  Do not use a bare `grep -F '${'`: shell parameter expansions inside ConfigMap-mounted scripts (for example `${1:-}`) match it.
  Detail: `docs/operations/apply-workflow.md`.
- **Multi-line secrets can't go through envsubst** (they break YAML after substitution).
  Use a dedicated `make <service>-secret` target with `op read` + `kubectl create secret --dry-run=client -o yaml | kubectl apply -f -`; `make create-jotta-secret` is the canonical pattern. 1Password *document* items need `op document get`, not `op read`.
- **Apply targets assert the kubectl context first** (`check-context` / `check-vps-context`).
  Never bypass them.
- **Agent work happens in an isolated git worktree, never in the main checkout** (`.claude/worktrees/` or equivalent; operator ruling, 2026-08-27).
  Several agents and sessions share that checkout at once, so its current branch is not yours to assume: run `git branch --show-current`, confirm it, and only then commit.
  A guidance edit that day landed on another agent's branch because the shared checkout had been switched mid-flight.
- **The main session is a strict orchestrator.**
  The operator's session — the main Claude Code context — coordinates and does not do the work.
  Every task, however small, is dispatched to a subagent or a Workflow: reading a document, setting up a worktree's `direnv allow` and lint baseline, running a command, writing a file, drafting a memo.
  The main session only classifies the task, dispatches it, relays the result in plain words, asks the operator the decisions and holds the approval gates.
  Work done in the main session pollutes the coordinating context, slows the loop and duplicates what the agents were dispatched for.
  Operator ruling, 2026-09-02, after the main session ran a worktree setup and a lint baseline itself while three research agents were already running.
- **Deploy, then merge.**
  A PR branch is applied to the cluster and verified healthy **before** the PR merges: `master` records what has been successfully deployed, never intent.
  Apply from the branch checkout (the preflight guards still run), confirm the workload is healthy, then the operator merges.
  Never merge-then-apply.
  **This covers a change to a procedure someone follows** — a runbook, a skill, a gate, guidance that governs a task — including one with nothing to apply to a cluster.
  For those, the apply is *running the thing on a real session*: work the runbook end to end, follow the guidance through the task it governs, exercise the skill.
  Reading a procedure proves only that it parses; running it is what finds the step that names a file that moved, the assertion that cannot be satisfied from the tool available, the count that is wrong.
  A prose change that has only been read is intent, and `master` does not record intent.
  Merge it when the session that exercised it is finished, so the corrections it turned up land on the same branch rather than in a follow-up PR.
  A change that governs no procedure — a records update, a corrected reference, wording — merges on review.
  It follows that a session driven by a runbook merges only what that runbook prescribes: everything the session invents — a runbook correction, a guard, a rule — goes on the findings branch, which merges once, after the operator has reviewed it, and the test is "did the runbook ask me to make it?", not "is it good?".
- **A branch held open across a session is rebased onto a freshly fetched `origin/master` before every commit to it, not only before an apply.**
  A branch that lacks a commit presents its absence as a deletion, so a stale branch is a revert of everything merged since it was cut — documentation-only branches included, because merging one still rewrites the files it is behind on.
  The rebase-before-apply rule does not cover this: a branch that never reaches a cluster still reaches `master`.
  The check is one command, read before you commit and again before you merge:

      git fetch origin && git diff --stat origin/master..HEAD

  The fetch is half the check, not a formality: `origin/master` is a remote-tracking ref that advances only on a fetch or a pull, so against a stale one the two-dot diff can only ever name the branch's own files and the check passes green on a branch that is eleven merges behind.
  A `git rebase origin/master` on that same stale ref is a no-op for the same reason.
  It must name only the files that branch exists to change.
  Any other file is a revert until proven otherwise, exactly as in `make diff-<cluster>`.
  On 2026-08-28 a findings branch held open across one session drifted eleven merges behind, and its diff named 22 workload files it had never touched.
- **An image tag is not always the whole version. Ask what the image installed into the data.**
  A container image bump replaces the binary and nothing else.
  Anything the software wrote *into its own data* on first start keeps the version it was written with, and no image bump moves it.
  A PostgreSQL extension is the case this estate has: `pgvector` ships its shared library in the image, but the `vector` entry in `pg_extension` keeps whatever `CREATE EXTENSION` recorded, and only `ALTER EXTENSION ... UPDATE` moves it.
  Hindsight ran the 0.8.6 library against a 0.8.1 catalog entry for as long as nobody looked.
  The same shape applies to any in-database component versioned separately from its image, and to on-disk schema versions generally.
  When a stateful image moves, ask what version the data claims, and compare the two:

      kubectl -n <ns> exec deploy/<db> -c postgres -- psql -U <user> -d <db> -tAc \
        "SELECT extname, extversion FROM pg_extension;"

  Then find out whether the gap matters before acting.
  **Read the vendor's own manifests first** — an upstream that pins the component tells you the pin is deliberate, and an upstream that floats it tells you the version is not load-bearing.
  For a PostgreSQL extension, the upgrade scripts settle it: an empty script means the release changed only the library, so the fix is already live and the update is a relabel.
  Record which of the two it was, because the next reader inherits the same question.
  The worked case, the command and the estate's decision on it are step 5 of the upgrade runbook in `docs/operations/hindsight.md`.
- **Read a PR's status checks before merging it, and treat a check that has not reported as a refusal.**
  `renovate.json` sets `minimumReleaseAge` to `3 days`, so every Renovate pull request carries a `renovate/stability-days` check that is `PENDING` until the release is three days old.
  The check is the whole of the stability policy — nothing on the repository enforces it — so merging past a `PENDING` one silently discards the wait the policy exists to impose.
  Read it, for any PR, with:

      gh pr view <n> --repo mnbf9rca/kubernetes_config --json statusCheckRollup

  A `PENDING` or failing check means the pull request is not this session's work: leave it open and say so at the close.
  This repository runs **one** workflow, `.github/workflows/influxdb-mcp-image.yml`: on a pull request it builds the InfluxDB MCP image from `homelab/health/mcp/`, asserts the added tool is registered, and pushes and signs it as `sha-<head sha>`; the merge then verifies that signature and promotes the same digest to `stable`.
  So a pull request touching those inputs carries three checks — `changes`, `lint` and `build` — and they are the whole of that change's review.
  Every other pull request runs no check at all, which is the normal case for human and agent work and has nothing to wait for; the rule bites on a check that exists and has not gone green.
  Deploy-then-merge does not override this — a pull request can be applied and healthy and still be too young to merge.
- **Concurrent deployed-but-unmerged branches are last-apply-wins on shared files.**
  An apply reconciles the whole rendered tree, so every file the applying branch does not carry is reset to that branch's version — another branch's already-deployed change included, silently, with every job still green.
  On 2026-08-24 an apply from a branch cut from `master` reverted the deployed restic gate, and that night's backup verified without it.
  So before **any** apply, the branch must already contain every other deployed-but-unmerged change: `git fetch origin`, rebase onto `origin/master`, **and** check the open pull requests for another that is deployed and touches the same files.
  `make diff-<cluster>` names every resource the apply would change — read that list first, and treat a resource the branch never touched as a revert until proven otherwise.
- `make apply-homelab` reporting `configured` rather than `unchanged` is expected and is **not** drift whenever the resource is absent from the immediately-preceding diff and a re-run diff is empty: that is a client-side apply patch the server converges away, and Secrets, some PVs and the cert-manager webhooks are examples of the class rather than the whole of it — two CronJobs joined them on 2026-08-28.
  Apply that rule, not a membership test; see the apply-workflow doc before investigating.

## File Conventions

- Each service is **one YAML file** under `homelab/workloads/` (or `vps/workloads/`) containing its Deployment, Service, Ingress and PVCs separated by `---`.
- **Every resource declares its own `namespace:` explicitly.**
  Do NOT add a top-level `namespace:` to `homelab/workloads/kustomization.yaml` — it would rewrite the namespace on every resource and break services that live outside `downloads` (for example jottacloud-backup).
- NFS PVs and their PVCs live in the same service file as the workload that uses them.
- Services use `PUID=1999` / `PGID=1999` for file ownership on shared media.
- Linuxserver (Alpine-based) Deployments set `dnsPolicy: None` with `dnsConfig.nameservers: ["8.8.8.8", "8.8.4.4"]` — the default DNS policy doesn't resolve reliably for them.
- Ingresses need no `tls:` block: Traefik serves the wildcard cert as its default.
- Secret manifests under `*/secrets/` contain only `${VAR}` placeholders.
- Every Deployment with auto-updates carries the full keel annotation set:
  ```yaml
  keel.sh/policy: force
  keel.sh/match-tag: "true"   # REQUIRED — without this keel silently downgrades :latest
  keel.sh/trigger: poll
  keel.sh/pollSchedule: "@every 6h"
  ```

## When Editing

- Keep the one-file-per-service pattern; keep all of a service's resources in that file.
- Every new Deployment must include the full keel annotation set above — **except** in the `health`, `ops`, `hindsight` and `backup` namespaces, which explicitly forbid keel, and **except keel itself**, which is digest-pinned on both clusters so the update engine cannot update itself (`homelab/bootstrap/keel/keel.yaml`, `vps/bootstrap/keel/keel.yaml`).
  The rule that decides which mode a workload is in: **floating tag means keel; pinned tag means Renovate; never both.**
  `match-tag: "true"` on a pinned tag only refreshes the digest, so a semver pin carrying keel annotations is frozen while looking covered.
- **Every pinned image in both clusters is inside Renovate's scope, and keeping it that way is a standing obligation.**
  `renovate.json` scopes Renovate to `homelab/**` and `vps/**` as of 2026-08-26, so every version- or digest-pinned image in either tree — `health`, `ops`, `hindsight`, `backup`, keel itself, traefik and the VPS workloads alike — gets its bump as a pull request (`docs/operations/homelab-health.md`, `docs/operations/homelab.md`, `docs/operations/hindsight.md`).
  Two kinds of image sit outside that, and the guard treats them differently.
  An image from a **remote base** is named by no file here, so nothing can edit the reference — it moves only when the base's own ref moves.
  `check-renovate-scope` prints those as advisories.
  That is not the same as unreachable: `vps/bootstrap/local-path/kustomization.yaml` pins its base as `?ref=v0.0.37`, which the `kustomize` manager parses, so Renovate proposes that bump even though the image itself is still reported advisory.
  An image **embedded inside another resource** — local-path-provisioner ships its helper Pod as a block scalar in a ConfigMap — the guard cannot see at all, so it says nothing about it: silence, not an advisory.
  Everything else hard-fails, so a new pinned image that nothing is configured to watch cannot reach a cluster.
  **In scope is not the same as watched, and `check-renovate-scope` only proves the first.**
  The guard's claim is structural — this image is named by a file inside `kubernetes.managerFilePatterns` and outside `ignorePaths` — and that claim stays true while the lookup behind it fails.
  Both keel images are the case in hand: correctly scoped, digest-pinned so that only Renovate can move them, and reported on the dependency dashboard on 2026-08-28 as `Failed to look up docker package ghcr.io/keel-hq/keel: no-result`.
  Nothing could have proposed a keel advisory, and every guard was green.
  Finding such a failure is no longer a manual read: since August 28, 2026 the daily `update-watch` job parses the **dependency-lookup warning block** on the Renovate dependency dashboard issue and pushes `verdict=renovate-lookup-failed`.
  The residual is acting on it — the alert carries a count, never the package names, so open that block when it fires, and never read a passing `check-renovate-scope` as evidence that a bump would arrive.
  A deliberate hold is the second way an in-scope image stops being watched: an `allowedVersions` cap or an `enabled: false` rule in `renovate.json` withholds the pull request by design, and only a hand edit lifts it.
  `hindsight` is the sharpest case: it runs Alembic migrations on startup against the store holding an agent's memory, and those migrations are forward-only, so the pre-upgrade dump is the only rollback.
  `make hindsight-upgrade` takes it.
  `health` is the same shape in miniature — a Grafana major migrates `grafana.db` in place on first start, so a tag revert is not a rollback there either; `make health-upgrade` takes that dump, and it covers the InfluxDB export in the same Job.
- **`pinDigests` is on at the top level and off on the keel-managed trees, and that split is load-bearing.**
  `pinDigest` is an updateType that fires on any Docker dependency without a digest, **floating tags included**, so top-level `pinDigests` over the widened scope would have Renovate propose "Pin Docker digests" against the images keel owns.
  Merging one recreates the pinned-tag-with-keel-annotations state this whole arrangement abolishes, and leaves keel rewriting the live digest every six hours against a repo holding a different one — so `make diff-homelab` reports a changed Deployment forever.
  The first `packageRule` turns it back off for `homelab/workloads/**`, `vps/workloads/**`, `vps/bootstrap/cloudflared/**` and both keel trees.
  **Adding a keel-annotated workload outside those paths means extending that rule in the same commit.**
  The rule matches whole **file paths**, not containers, so it also suppresses digest pinning for the pinned, keel-free containers that happen to share those files — the four `alpine:3.20` quiesce sidecars and both `postgres:16-alpine` containers.
  They still get version bumps, so nothing is broken; they arrive without a digest.
  That is the accepted cost of a path-scoped rule, not an oversight.
- **New machinery earns its place or it does not ship.**
  A resource, a probe, a check or a verification step is justified only if it (a) serves the application, (b) protects against lockout or data loss, or (c) feeds detection machinery that already exists.
  Everything else is ceremony — cut it while you write it.
  Four questions decide the common cases; all four fired on the one-day pinepods deployment (2026-08-27):
  1. **Does an existing dead-man's-switch already alert on this?**
     Then do not add a step that re-confirms it.
     A gate on next-morning restic confirmation detected nothing the gate's own alert would not, and the overnight wait held a deployed-but-unmerged branch open — the last-apply-wins window above.
     **The deploy-then-merge gate is workload health, not a backup cycle.**
  2. **Has the helper exactly one consumer and the app's own lifecycle?**
     Then it is a second container in the app's pod, not a Deployment plus Service plus DNS name: folding a single-consumer Valkey cache back in deleted two resources and a probe and lost nothing.
  3. **Can this acceptance criterion fail at runtime?**
     One that can only fail if you mis-edit the files you just reviewed is reading your own diff — delete it.
     "Port 8042 is unreachable from outside" asserted the absence of config the design never wrote.
  4. **Does a constant encode a count taken from somewhere else?**
     Re-derive it from that source rather than from your model of it, and confirm it against the live value after the apply.
     `IMAGE_FLOOR` counts deduplicated images across every container of every keel-annotated workload, so one two-container workload moves it by two.
- **A design review needs a seat whose only brief is deletion.**
  Brief one reviewer to hunt for machinery that exists to feel rigorous and name both what to delete and what of value is lost, then verify each finding adversarially — tell the verifier that "protects against neither lockout nor data loss" argues **for** the finding.
  That seat found four deletions two other reviewers missed (2026-08-27); the spec or plan's own author cannot fill it.
- **Probes: readiness on every long-running container that serves traffic; liveness only where that probe can actually detect the failure *and* a restart is a safe remedy** (everything here is single-replica, so an over-eager liveness probe manufactures outages).
  **Always set `timeoutSeconds`** — the 1s default false-positives on a loaded node.
  **Probe the data plane, not a control-plane health endpoint**: the vendor-documented probe would have stayed green through the 2026-08-18 Pomerium wedge.
  **Read the endpoint's handler at source before wiring any probe to it**: an endpoint that returns 200 unconditionally and puts its verdict in the body detects nothing a restart fixes, yet still times out during a database incident and restarts the single replica for a fault no restart repairs (2026-08-27).
  Assert on real health fields in the body from a kuma keyword monitor instead.
  **Never probe a sidecar at all** — backup/quiesce container and single-consumer cache alike — because readiness drops the Pod from its EndpointSlice and liveness gets there through CrashLoopBackOff, so one sidecar fault takes the application offline; detect a backup fault at the artifact instead.
  Reasoning, per-service targets and the failures probes *don't* catch: `docs/operations/monitoring.md`.
- **Scheduled work gets a dead-man's-switch, not a probe.**
  Every CronJob sets `timeZone: "UTC"` and `activeDeadlineSeconds` (with `concurrencyPolicy: Forbid`, one hung run silently blocks every later run), plus `startingDeadlineSeconds` where a missed window must be retried rather than dropped.
  **New scheduled work drives an uptime-kuma PUSH monitor, not a healthchecks.io check** (migrated 2026-08-26).
  Five checks remain at healthchecks.io; the inventory, and each check's reason for keeping its slot, is the table in `docs/operations/monitoring.md` — read that rather than an enumeration here, which drifted once already.
  The default contract for a new job is **`up` on exit 0 and `down` otherwise**, from an EXIT trap in the shell runners — the two hindsight jobs, `influx-backup`, `hermes-pull` and both `keel-fresh` jobs — or, in Python, from a module-level `try`/`except` that catches every exception and pushes on the way out, which is what `cloudflare-analytics` does.
  There is **no `/start` equivalent** and none may be invented: a push is a heartbeat carrying a status, so `activeDeadlineSeconds` is the whole of the hang bound and the monitor's interval plus retry is the silence bound.
  Two jobs deliberately push **nothing** on some runs and that must not change: `ingest-freshness` pushes only when both buckets are fresh, because a `down` on a stale bucket would trade a 36-hour tolerance for a 6-hour one on a signal that depends on the operator syncing a watch; and `update-watch` pushes nothing on an indeterminate run, because "I could not read GitHub" is neither a success nor a failure.
  `jottacloud-backup` is success-only too, but for a third reason — the request comes from an image this repo does not build.
  Inventory and per-job semantics: `docs/operations/monitoring.md` and `docs/operations/uptime-kuma.md`.
- **A ping body and a heartbeat message are both disclosure channels, and one rule set covers both.**
  Every one carries a short `key=value` summary (a verdict first, printable ASCII), and **never a command's output** — the exec'd influx scripts carry the operator token on argv, and a failing `wget` or `curl` quotes the URL it was handed, which is the reporting credential either way: a healthchecks.io ping UUID, or a kuma push **token**, which is the last path segment of `PUSH_URL`.
  The rule is blanket because a script cannot tell the tiers below apart at runtime.
  Emit a count, an age, a size, a path built from a literal glob, or a verdict from a fixed enum.
  `make check-ping-bodies` enforces it — it recognises a sink by FUNCTION NAME (`emit`, `say_err`, `fatal`; `hc_emit`, `hc_summary`) and never by the destination host, which is why the sinks kept their names through the 2026-08-26 migration — and it is the only thing that catches `M=$(cmd); emit "error=$M"`.
  What differs between the two destinations is only size and storage: a healthchecks.io body is multi-line and held by a third party; a kuma `msg` is **one line, cut at 200 characters**, held on the operator's own VPS.
  So a migrated runner emits the verdict first and the values an operator acts on next, then sacrifices to the cut whichever token carries least — `error=` last in `influx-backup` and `hermes-pull`, but the two fixed-width threshold literals in `update-watch`, whose variable-length token is the `next=` action and is protected in third place.
  Every runner prints the full detail to the pod log, where triage starts.
  Either one travels with the alert to every notification transport its destination has configured, a list nobody has enumerated on either side.
  Policy, the accepted residuals and that open item: `docs/operations/monitoring.md`.
- **Updating the hermes VM is a runbook, not a script, and it is deliberately not scheduled.**
  `hermes update` sometimes carries a step that needs judgement — a migration prompt, a stash of local edits — and a script cannot exercise judgement, so the procedure lives as prose an agent or the operator follows with the session open, roughly weekly: `docs/operations/hermes-vm-updates.md`.
  Everything else about the VM — lingering, the daily liveness check, the install, `unattended-upgrades` — is in `docs/operations/hermes-vm.md`.
  The wrapper that used to automate updates, with its two test harnesses, its systemd unit and its root-owned entry point, was deleted on 2026-08-27.
  Building it back is a design change and needs the operator, not a tidy-up.
  **Nothing under `hermes-vm/` is scheduled by systemd any more**: the `systemd/` directory was deleted on 2026-08-27, and both scheduled scripts — the daily liveness check and, since 2026-08-29, the weekly docker-sandbox refresh — run as hermes `no_agent` cron jobs inside the default gateway.
  The liveness check is read-only; the sandbox refresh pulls the pinned terminal image and removes idle stale containers, a mechanical judgement-free job, and neither is a precedent for scheduling `hermes update` itself.
  **Nothing mechanical guards runbook prose**, which is why its disclosure rules are written into the steps they govern rather than referenced.
  The files that remain under `hermes-vm/` are not rendered by kustomize, so `make check-script-lint` cannot see them — `make check-vm-scripts` is their guard, and it is `shellcheck -s sh` over every `*.sh` under `hermes-vm/scripts/` (the daily alive check; since 2026-08-29 the weekly docker-sandbox refresh; and since 2026-08-31 `hermes-profile-docker-setup.sh`, which is run by hand per new profile and is on no schedule) plus `scripts/check-ping-bodies.py hermes-vm`.
  It runs in **no** preflight, and the one workflow this repository has covers `homelab/health/mcp/` alone, so nothing runs it automatically: it is step 1 of the install procedure in `docs/operations/hermes-vm.md`, and the update runbook never calls it.
  Run it by hand after touching anything under `hermes-vm/`, and before copying any of it to the VM.
- **A new InfluxDB bucket in the `health` namespace means three edits, not one:** create it (a `make health-influx-*-bootstrap` target), add it to the explicit `for B in ...` list in `homelab/health/scripts/influx-export-lp.sh`, **and** raise `LP_EXPECTED` in `homelab/health/scripts/influx-backup.sh`, which is the denominator of the `buckets=n/m` the heartbeat carries.
  A bucket missing from the export list is silently never exported; a bucket in that list that does not exist fails the nightly job by name; and a stale `LP_EXPECTED` shows up as a visibly wrong `buckets=` and nothing worse.
  Bootstrap before applying (`docs/operations/homelab-health.md`).
- **One-shot `Job`s must set `ttlSecondsAfterFinished`.**
  A Job's `spec.template` is immutable, so a completed Job that is never garbage collected pins the version of itself that ran.
  The next apply that changes it fails with `field is immutable`, and since `kubectl apply` continues past a failed resource and reports only through its exit code, the failure is quiet: everything else applies and the Job silently does not.
  The homelab `restic-init` Job lacked the field, survived from 2026-04-11 to 2026-08-20, and broke `diff-homelab` and `apply-homelab` for four months.
  Deleting the stale Job is the recovery; the TTL is the prevention.
  `make check-job-ttl` enforces it across both clusters, and `diff-*`/`apply-*` run the per-cluster variant as a preflight, so it cannot be forgotten.
  The preflight is a context assertion, a vars-consistency check, **five per-cluster guards that each run as their cluster's half** — `check-script-substitution`, `check-job-ttl`, `check-ping-bodies`, `check-script-lint` and `check-renovate-scope` — and one guard with no half, `check-keel-fresh-parity`, on all four of `diff-homelab`, `apply-homelab`, `diff-vps` and `apply-vps`.
  `check-renovate-scope` joined them on 2026-08-26, in the commit that widened Renovate far enough for it to pass.
  **Three of the five are RENDER-BASED** — `check-job-ttl`, `check-script-lint` and `check-renovate-scope` each shell out to a full `kustomize build`.
  That subset is not trivia: it decides wiring.
  The render-based three sit on the PUBLIC half of the split only, because duplicating them would double every apply's render cost, while `check-script-substitution` and `check-ping-bodies` scan source files under the cluster trees and are cheap enough to run on both halves.
  Keep the distinction when editing either list.
  Jobs *generated by a CronJob* are exempt and the check ignores them: each run gets a unique name, so they never collide, and pile-up is bounded by `successfulJobsHistoryLimit`.
  `make check-workflows` — actionlint with shellcheck over `.github/workflows/` — is deliberately **not** in that preflight, because a workflow renders nothing and reaches no cluster; it runs in the workflow's own `lint` job on every push and pull request, and by hand.
- **A deliberate copy of a file across the two clusters must be enforced, not just commented.**
  `homelab/ops/` and `vps/ops/` hold the same `keel-fresh` runner and CronJob twice, because kustomize will not read a generator source outside its own root and because the alternative puts a VPS kubeconfig inside a homelab pod.
  The invariant that rests on is **edit them together**, and the two comments saying so — both in the VPS copies; neither homelab file carries the instruction — have never stopped anybody: a fix applied to one cluster and not the other is a dead-man's-switch that has quietly stopped switching on the cluster nobody looked at.
  `make check-keel-fresh-parity` enforces it by masking a short, stated list of sanctioned differences — the copy notes, `IMAGE_FLOOR`, the schedule, the monitor name, the two paths, the `nodeSelector`, the 1Password vault path, the token variable — and requiring the rest to be identical.
  Editing either copy means editing both; genuinely per-cluster behaviour means a new rule in that guard, in the same commit, with its reason written down.
  Every rule's span is **bounded to comment lines plus the line it names**: an earlier `IMAGE_FLOOR` rule used `.*?` across newlines and silently swallowed executable shell inserted above the assignment, which `check-script-lint` cannot catch either because the insertion is valid `sh`.
  Keep that property when adding a rule.
  **It has no per-cluster half**, so a divergence in the VPS copy blocks `apply-homelab` too.
  That is a ruling, not an accident: a divergence means one cluster's dead-man's-switch may be broken and nothing in the divergence itself says which, so neither cluster moves until it is resolved — and a per-cluster split is not even coherent, because the guard compares both trees and a homelab-only variant would fail on a VPS-only edit anyway.
  The coupling is real and is the kind that gets routed around under pressure; the answer is that the guard names the offending line and the fix.
  Any future copied pair should get the same treatment rather than a third comment.
- **Logic lives in a script file, never in an inline YAML string.**
  Anything with branching, parsing or loops goes in a real file under a `scripts/` path and reaches the cluster through a `configMapGenerator`, as `homelab/health/scripts/cloudflare-analytics-ingest.py` does.
  A file can be imported, unit-tested, linted and diffed line by line; a 600-line block scalar can do none of those, and reviewing one means extracting it by hand first.
  Two things the generator needs: set `namespace:` on the generator entry (kustomize only rewrites a name reference when referrer and referent agree on namespace — omit it and the ConfigMap lands in `default` *and* the workload keeps pointing at the unsuffixed name), and leave the content-hash suffix ON, so editing a script rolls the workload that mounts it.
  Short straight-line `sh`/`kubectl` still belongs inline: the rule is about logic, not length.
- **A generated script must never name an `ENVSUBST_VAR_NAMES` variable.**
  Generator files ride the same stream as every manifest, so envsubst rewrites them — and it substitutes the **bare `$NAME`** form, not just `${NAME}` (verified).
  The homelab allowlist holds `RESTIC_REPOSITORY`, `RESTIC_PASSWORD`, `B2_ACCOUNT_ID` and `B2_ACCOUNT_KEY`, which are exactly the names restic reads, so `echo "repo at $RESTIC_REPOSITORY"` in a script publishes the real B2 URL inside a **ConfigMap**; `$RESTIC_PASSWORD` publishes the repository password in plaintext.
  No placeholder survives, so `check-placeholder-coverage` sees nothing.
  `make check-script-substitution` is the guard, and `diff-*`/`apply-*` run the per-cluster variant as a preflight, alongside `check-ping-bodies`.
  If a script needs such a value, indirect it: give the container a differently named env var (`secretKeyRef` for a secret) and use that name in the script.
  For the same reason, **do not share one script between the two clusters** when it touches restic: the VPS names are `VPS_`-prefixed and the homelab ones are not, so a shared file is safe under one tree and leaking under the other.
- **Every script is linted from the RENDER, as POSIX `sh`.**
  `make check-script-lint` runs `kustomize build`, pulls the shell back out of ConfigMap `data:` keys *and* out of inline `args:`/`command:` block scalars, and runs `shellcheck -s sh` over it — plus compiling every `*.py` and running every `test_*.py`.
  `diff-*`/`apply-*` run the per-cluster variant as a preflight, so it cannot be forgotten.
  Two rules when working on a script: never "fix" a finding by switching the check to `-s bash` (these run under busybox ash and dash, and SC3xxx is the whole point), and if a warning is genuinely wrong add a narrow `# shellcheck disable=SCxxxx` **with a stated reason**, as the existing ones do.
  `shellcheck` is now a required tool (`make check-tools`); `brew install shellcheck`.
  Findings from upstream bases are advisory and do not fail the check.
  Rationale and the extraction rules: `docs/operations/apply-workflow.md`.
- **Every container is in exactly one update mode, and `make check-renovate-scope` proves it.**
  The guard renders each cluster and judges one container at a time: a complete keel annotation set on a floating tag is legal; the same set on a **pinned** tag is the frozen state (`match-tag` only refreshes the digest) and fails; an **incomplete** set fails on any tag, because a missing `match-tag` silently downgrades a semver tag to `:latest`.
  A pinned, keel-free image must be named by a repo file **in the same cluster's tree** that is inside `kubernetes.managerFilePatterns` and outside `ignorePaths` — a `packageRule` alone does **not** widen scope, and the per-cluster confinement is load-bearing, because both trees name many of the same images and a repo-wide lookup would let a watched homelab file vouch for an unwatched VPS container. keel annotations are a **workload** property: a pinned sidecar beside a floating app image is Renovate's, not frozen, so only a workload with nothing floating in it can be frozen.
  Floating tags are forbidden in the `health`, `hindsight`, `ops` and `backup` namespaces; `jottacloud-backup` is the one written exemption on that guard's `FLOATING_EXEMPT` list, because it is a CronJob whose pods pull `:latest` on every scheduled run and so needs no keel.
  Images from remote bases are advisory.
- For new `hostPath`/`hostNetwork` workloads: elevate their namespace to PSA `privileged` in the cluster's `bootstrap/namespaces.yaml`.
  The cluster-wide enforce level is `baseline`.
- A new secret placeholder takes the four edits listed under **Apply Workflow** above; use the `VPS_*` lists in the `Makefile` for VPS vars.
  No `direnv reload` is needed for a value — `op run` resolves references per command; reload only after changing `OP_SERVICE_ACCOUNT_TOKEN` itself.
- When creating 1Password items with `op item create`, explicitly type the fields: a bare `field=value` defaults to **concealed**, so mark non-secret fields (emails, IDs, UUIDs, hostnames) as `field[text]=value` and keep only actual secrets concealed.
  Wrongly-concealed non-secrets make the vault harder to debug; visible secrets are worse.
- **Secret disclosure → honesty box.**
  If you (human or agent) expose a real secret value anywhere it doesn't belong — terminal output, an agent report/transcript, chat, a log or scratch file — do two things immediately: (1) tell the operator in your next message, and (2) append a row to `secrets-to-rotate.md` at the repo root identifying the secret by its `op://` reference or k8s secret/key, how it was disclosed, and by whom.
  Identifiers only — never the value.
  Err on the side of logging: a false-positive row costs one unnecessary rotation; a silent disclosure costs the assumption of confidentiality.
  This applies even when the exposure feels harmless (short-lived token, local-only transcript, immediately-cleared scrollback).
- **Know the difference between a secret and an identifier.**
  Conflating them wastes rotations, clutters the honesty box, and — worse — makes agents refuse to log or print things that are perfectly fine, which hides real diagnostics.
  Three tiers:
  1. **Secrets grant access.**
     Tokens, passwords, private keys, API keys, session cookies, the 1Password service-account token.
     Disclosure means honesty box **and** rotation.
  2. **Spam-target identifiers grant no access but let a stranger cause a nuisance.** healthchecks.io ping UUIDs and uptime-kuma push tokens are the cases that matter here: anyone holding one can report a heartbeat and mask a genuine failure, and grants nothing else.
     Keep them out of this public repo (`op://` reference only) and type them `[text]` in the vault — but a transcript or a pod log is **not** a disclosure, they need **no rotation**, and they get **no honesty-box row**.
     This has been ruled three times.
     `jottacloud-backup`'s own image prints its push URL to the pod log on every run: same tier, no action.
  3. **Ordinary identifiers are not sensitive at all.**
     Restic repository URIs, B2 and InfluxDB bucket names, Cloudflare zone IDs, PVC UUIDs, namespaces, FreshRSS usernames, hostnames.
     They grant nothing and enable nothing.
     They are fine in pod logs, in ping bodies and in agent output.
     Keep the account-identifying ones out of committed files; otherwise leave them alone.
     When in doubt ask "what can someone *do* with this?"
     — not "does it look secret?".
- **`ENVSUBST_VAR_NAMES` membership is not a secrecy classification.**
  `RESTIC_REPOSITORY` and the bucket names are on that list because envsubst would *substitute* them into a rendered ConfigMap, which is a mechanical hazard, not because the values are secret.
  Do not infer sensitivity from that list.
- **This repo is public.**
  Never write the Omni service URL, sign-in identity, or any other credential-adjacent value into a committed file — reference it as an `op://` path and read it at run time.
- **Nothing ships in the operator's name without explicit approval.**
  Upstream pull requests, issues on third-party trackers, pushes to public forks, and comments on other people's repositories are all publicly attributed to the operator.
  Prepare the work locally — branches, commits, drafted PR and issue text — and present it for review; the operator says when each item is published, one item at a time.
  Merging pull requests in this repo when asked is fine: the gate is third-party visibility, not git mechanics.
- Prefer `kubectl exec deployment/<name> -- sh -c '...'` plus `rollout restart` for in-container file tweaks rather than spinning up a helper pod.
- Documentation belongs in `docs/`, **referenced** from this file rather than included in it.
  When you learn something operational, write it into the relevant `docs/` file and add a pointer here only if it changes how an agent edits the repo.
- **Markdown is not hard-wrapped** (operator ruling, 2026-08-27).
  Write one line per sentence, or per paragraph where a paragraph is one thought; let the editor wrap it.
  A sentence then owns a line in every diff, so a one-word change shows as a one-line change instead of reflowing the paragraph around it.
  This holds for every `.md` file in the repo, this one included: the whole corpus was reflowed on 2026-08-28, so a hard-wrapped paragraph now reads as a regression.
  The exception is **files that ship to a machine and are read with `cat` or `less`** — apt configuration, systemd units, shell scripts and their comments — which keep the roughly 80-column wrapping they have, because no editor wraps them where they are read. No Markdown file is in that set.
- **Documentation, not agent memories.**
  Do not record repo, cluster, or account state in an agent's private memory system — that hides operational knowledge from the operator, from other agents, and from review.
  Anything worth remembering goes in `docs/` (or this file, per the rule above), where it is versioned, diffable and shared.
  This applies to facts about adjacent infrastructure the repo touches (VMs, DNS, Cloudflare account state), not just the manifests themselves.
  It covers the operator's working-style preferences and process rulings too: how the operator wants agents to work is never a private-memory entry, it belongs in this file, where every session reads it.
  Restated on 2026-09-02 after an agent wrote a preference into its memory store despite this bullet.

## Legacy Reference

`legacy-microk8s/` contains the original flat-layout microk8s manifests and `no_longer_used/` holds retired manifests.
Both are **frozen reference only** — do not add new files to either.
`legacy-microk8s/` exists to be deleted: remove it once the Talos rebuild is fully operational and nothing is still cross-referenced out of it.
`README.md` at the repo root now describes the current Talos clusters (corrected 2026-08-28; it long described the retired microk8s/rancher setup).
