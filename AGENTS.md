# Kubernetes Config Repository

Personal Kubernetes cluster config: a home media/downloads stack plus a health-data
pipeline on **Talos Linux** (managed by Omni), and a second public-facing Talos cluster
on a Hetzner VPS.

This file is **background and conventions only** — what the repo is, how it's laid out,
and the rules to follow when editing it. Cluster-specific detail, runbooks and
procedures live under `docs/` and are referenced from here rather than duplicated.

## Documentation

| Document | Covers |
|---|---|
| `docs/operations/omni-access.md` | **Start here on a new machine.** Bootstrapping omnictl/kubectl/talosctl from zero, where omniconfig and SideroV1 keys land, Omni/talosctl troubleshooting |
| `docs/operations/apply-workflow.md` | Secret pipeline end to end, full Makefile target reference, Talos config patches, Tailscale bootstrap, why `apply` always says `configured` |
| `docs/operations/homelab.md` | Homelab cluster: platform stack, namespaces/workloads, NFS and storage, node network, DNS/Route53, encryption at rest, operational gotchas |
| `docs/operations/homelab-health.md` | The `health` namespace: ingest pipeline, image-pin rationale, InfluxDB bootstrap, backups/restore, Garmin re-auth, monitoring, probe rationale |
| `docs/operations/vps.md` | VPS cluster: shape, workloads, Cloudflare tunnel/Access, DB decisions, backups |
| `docs/operations/monitoring.md` | How failures get noticed: the triage table, probe policy and inventory, CronJob deadlines, the backup verification gates, healthchecks.io checks and ping bodies, and what none of it catches |
| `docs/operations/uptime-kuma.md` | Layer 3/4 runbook: creating uptime-kuma monitors by hand, per-monitor HTTP settings, the Cloudflare Access trap, the push monitors driven from inside the clusters and the bypass they need, the self-monitor |
| `docs/operations/hindsight.md` | The `hindsight` namespace: the self-hosted memory backend for the Hermes profiles — topology, auth, the canary, upgrade and restore runbooks, the restore drill, key rotation, and the removal path |
| `docs/operations/agent-mail.md` | Per-agent email for Hermes agents: Purelymail mailboxes on cynexia.io, per-profile mcp-email-server config, provisioning runbook, credential scheme, limits, and the deliberate monitoring/backup gaps |

Design documents and implementation plans are local-only under the gitignored
`docs/superpowers/` tree (`specs/2026-04-11-talos-homelab-rebuild-design.md`,
`plans/2026-04-11-talos-homelab-rebuild.md`).

## Clusters at a glance

| | Homelab | VPS |
|---|---|---|
| kubectl context | `cynexia-homelab` | `cynexia-vps` |
| Omni cluster name | `homelab` | `vps` |
| Domain | `*.cynexia.net` (Route53) | `*.cynexia.com` (Cloudflare) |
| Exposure | Private — LAN/Tailscale only, except the `health` namespace's own `cynexia-health` tunnel | Public, through the `cynexia-vps` cloudflared tunnel + Cloudflare Access |
| Ingress | Traefik hostNetwork DaemonSet + cert-manager wildcard | cloudflared only (no Traefik, no cert-manager) |
| Apply | `make apply-homelab` | `make apply-vps` |

The two domains are unrelated zones on different providers. Don't cross them.

## Repo Layout

```
kubernetes_config/
├── .envrc                    # direnv entrypoint (loads 1Password-backed vars)
├── .env.tpl                  # op-template with VAR=op://... lines (committed; no real secret values)
├── Makefile                  # build/diff/apply per cluster + secret and bootstrap helpers
├── renovate.json             # scoped to homelab/** and vps/** (pinDigests, off on the keel-managed trees)
├── secrets-to-rotate.md      # honesty box for disclosed secret values (identifiers only)
├── docs/                     # operational documentation (docs/superpowers/ is gitignored)
├── homelab/                  # Talos homelab cluster
│   ├── kustomization.yaml    # top-level: bootstrap + secrets + workloads + backup + health + hindsight
│   ├── talos/                # Omni ConfigPatches resources (applied via `make apply-talos`)
│   ├── bootstrap/            # platform: namespaces (with PSA labels), local-path, NFS CSI, cert-manager, traefik, keel
│   ├── workloads/            # application workloads (one file per service, --- separated, no ns override)
│   ├── secrets/              # Secret manifests with ${VAR} envsubst placeholders
│   ├── health/               # health-data pipeline (no keel; pinned images)
│   │   └── scripts/          # job scripts as real files + their tests; mounted via configMapGenerator
│   ├── ops/                  # cluster-wide operational jobs (the daily Renovate update watcher)
│   │   └── scripts/          # same pattern: real files + tests, via configMapGenerator
│   ├── hindsight/            # Hindsight memory backend for the Hermes profiles (no keel; pinned images)
│   │   └── scripts/          # nightly pg_dump + the 15-minute canary; mounted via configMapGenerator
│   └── backup/               # restic init Job + nightly CronJob (hostPath /var/mnt/ssd/local-path-provisioner)
├── vps/                      # Hetzner Talos cluster, same sub-layout (bootstrap/secrets/workloads/backup/talos)
├── scripts/                  # repo-level helpers (karakeep tags, FreshRSS WebSub status, the check-* guards)
├── legacy-microk8s/          # frozen reference copies of the old microk8s manifests
└── no_longer_used/           # retired manifests kept for reference
```

## Apply Workflow (conventions)

Full mechanics, target-by-target reference and failure modes:
`docs/operations/apply-workflow.md`. The rules that must not be broken:

- **Secrets reach manifests through `op run` + envsubst, resolved per command.** `.env.tpl`
  holds only `VAR=op://Vault/item/field` lines. `.envrc` exports **only**
  `OP_SERVICE_ACCOUNT_TOKEN` — no secret value ever enters the ambient environment. The
  `Makefile` defines `OP_RUN := op run --env-file=.env.tpl --`, and every build/diff/apply
  target runs its guards in the parent shell then re-enters make under it, so values exist
  inside one child process only. **The old `set -a` + `op inject` block in `.envrc` is
  gone deliberately — do not restore it.**
- **Never commit plaintext secret values.** `${VAR}` placeholders only.
- **`op run` masks stdout, not env vars** — corrected 2026-08-20; the previous claim in
  this file was a misdiagnosis. `op run` passes the **real** values in the child
  environment (verified intact at 100 and 27 characters); it redacts secrets in the
  child's **output**. (`len=${#ACME_EMAIL}` returning 24 was a coincidence — that value
  is genuinely 24 characters.) The real hazard is therefore rendering to a file — and
  `build-*` is already wrapped in `op run` by the Makefile, so no extra wrapper is needed
  to hit it: `make build-homelab > out.yaml` writes `<concealed by 1Password>` into the
  Secrets and `kubectl apply -f out.yaml` stores the mask. **Never render-then-apply**;
  `diff-*`/`apply-*` keep the stream inside the child, where values are real.
  Detail: `docs/operations/apply-workflow.md`.
- **`ENVSUBST_VARS` is an explicit allowlist, passed single-quoted.** Never call envsubst
  without one: with no allowlist it eats every `${VAR}` in the stream including shell
  variables inside upstream manifests (for example `$VOL_DIR` in local-path-provisioner's helper
  pod); with double quotes the shell expands the tokens before envsubst sees them.
- **Adding a secret means four edits:** the `op://` line in `.env.tpl`, the name in
  `ENVSUBST_VAR_NAMES`, the name in `REQUIRED_VARS`, and the `${VAR}` placeholder in the
  manifest. `make check-vars-consistency` hard-fails if a substituted var is missing from
  `REQUIRED_VARS`. A var missing from `ENVSUBST_VAR_NAMES` is caught too, but **at apply
  time only**: the Makefile's `PLACEHOLDER_SCAN` runs inside `apply-homelab` and
  `apply-vps` after rendering and before kubectl, and hard-fails naming any surviving
  `${VAR}` whose name is declared in `.env.tpl`, so nothing is applied and no literal
  placeholder reaches a Secret. Note the asymmetry: `diff-*` does **not** run that scan, so
  a diff can look clean while the apply refuses. To confirm no placeholder survived the
  render after adding one, run
  `make build-<cluster> | grep -F "$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/${\1}/p' .env.tpl)"`
  — it prints nothing on a clean tree. Do not use a bare `grep -F '${'`: shell
  parameter expansions inside ConfigMap-mounted scripts (for example `${1:-}`) match it.
  Detail: `docs/operations/apply-workflow.md`.
- **Multi-line secrets can't go through envsubst** (they break YAML after substitution).
  Use a dedicated `make <service>-secret` target with
  `op read` + `kubectl create secret --dry-run=client -o yaml | kubectl apply -f -`;
  `make create-jotta-secret` is the canonical pattern. 1Password *document* items need
  `op document get`, not `op read`.
- **Apply targets assert the kubectl context first** (`check-context` /
  `check-vps-context`). Never bypass them.
- **Deploy, then merge.** A PR branch is applied to the cluster and verified healthy
  **before** the PR merges: `master` records what has been successfully deployed, never
  intent. Apply from the branch checkout (the preflight guards still run), confirm the
  workload is healthy, then the operator merges. Never merge-then-apply.
- **Concurrent deployed-but-unmerged branches are last-apply-wins on shared files.** An
  apply reconciles the whole rendered tree, so every file the applying branch does not
  carry is reset to that branch's version — another branch's already-deployed change
  included, silently, with every job still green. On 2026-08-24 an apply from a branch cut
  from `master` reverted the deployed restic gate, and that night's backup verified
  without it. So before **any** apply, the branch must already contain every other
  deployed-but-unmerged change: rebase onto `origin/master`, **and** check the open pull
  requests for another that is deployed and touches the same files. `make diff-<cluster>`
  names every resource the apply would change — read that list first, and treat a resource
  the branch never touched as a revert until proven otherwise.
- `make apply-homelab` reporting `configured` rather than `unchanged` for Secrets, some
  PVs and cert-manager webhooks is expected and is **not** drift — see the apply-workflow
  doc before investigating.

## File Conventions

- Each service is **one YAML file** under `homelab/workloads/` (or `vps/workloads/`)
  containing its Deployment, Service, Ingress and PVCs separated by `---`.
- **Every resource declares its own `namespace:` explicitly.** Do NOT add a top-level
  `namespace:` to `homelab/workloads/kustomization.yaml` — it would rewrite the namespace
  on every resource and break services that live outside `downloads`
  (for example jottacloud-backup).
- NFS PVs and their PVCs live in the same service file as the workload that uses them.
- Services use `PUID=1999` / `PGID=1999` for file ownership on shared media.
- Linuxserver (Alpine-based) Deployments set `dnsPolicy: None` with
  `dnsConfig.nameservers: ["8.8.8.8", "8.8.4.4"]` — the default DNS policy doesn't
  resolve reliably for them.
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
- Every new Deployment must include the full keel annotation set above — **except**
  in the `health`, `ops`, `hindsight` and `backup` namespaces, which explicitly forbid
  keel, and **except keel itself**, which is digest-pinned on both clusters so the
  update engine cannot update itself (`homelab/bootstrap/keel/keel.yaml`,
  `vps/bootstrap/keel/keel.yaml`). The rule that decides which mode a workload is in:
  **floating tag means keel; pinned tag means Renovate; never both.**
  `match-tag: "true"` on a pinned tag only refreshes the digest, so a semver pin
  carrying keel annotations is frozen while looking covered.
- **Every pinned image in both clusters is watched, and keeping it that way is a
  standing obligation.** `renovate.json` scopes Renovate to `homelab/**` and `vps/**`
  as of 2026-08-26, so every version- or digest-pinned image in either tree — `health`,
  `ops`, `hindsight`, `backup`, keel itself, traefik and the VPS workloads alike —
  gets its bump as a pull request
  (`docs/operations/homelab-health.md`, `docs/operations/homelab.md`,
  `docs/operations/hindsight.md`). Two kinds of image sit outside that, and the guard
  treats them differently. An image from a **remote base** is named by no file here, so
  nothing can edit the reference — it moves only when the base's own ref moves.
  `check-renovate-scope` prints those as advisories. That is not the same as
  unreachable: `vps/bootstrap/local-path/kustomization.yaml` pins its base as
  `?ref=v0.0.31`, which the `kustomize` manager parses, so Renovate proposes that bump
  even though the image itself is still reported advisory. An image **embedded inside
  another resource** — local-path-provisioner ships its helper Pod as a block scalar in
  a ConfigMap — the guard cannot see at all, so it says nothing about it: silence, not
  an advisory. Everything else hard-fails, so a new pinned image that nothing watches
  cannot reach a cluster. `hindsight` is the sharpest case: it runs Alembic
  migrations on startup against the store holding an agent's memory, and those
  migrations are forward-only, so the pre-upgrade dump is the only rollback.
  `make hindsight-upgrade` takes it.
- **`pinDigests` is on at the top level and off on the keel-managed trees, and that
  split is load-bearing.** `pinDigest` is an updateType that fires on any Docker
  dependency without a digest, **floating tags included**, so top-level `pinDigests`
  over the widened scope would have Renovate propose "Pin Docker digests" against the
  images keel owns. Merging one recreates the pinned-tag-with-keel-annotations state
  this whole arrangement abolishes, and leaves keel rewriting the live digest every six
  hours against a repo holding a different one — so `make diff-homelab` reports a
  changed Deployment forever. The first `packageRule` turns it back off for
  `homelab/workloads/**`, `vps/workloads/**`, `vps/bootstrap/cloudflared/**` and both
  keel trees. **Adding a keel-annotated workload outside those paths means extending
  that rule in the same commit.**
  The rule matches whole **file paths**, not containers, so it also suppresses digest
  pinning for the pinned, keel-free containers that happen to share those files — the
  four `alpine:3.20` quiesce sidecars and both `postgres:16-alpine` containers. They
  still get version bumps, so nothing is broken; they simply arrive without a digest.
  That is the accepted cost of a path-scoped rule, not an oversight.
- **Probes: readiness on every long-running container that serves traffic; liveness only
  where that probe can actually detect the failure *and* a restart is a safe remedy**
  (everything here is single-replica, so an over-eager liveness probe manufactures
  outages). **Always set
  `timeoutSeconds`** — the 1s default false-positives on a loaded node. **Probe the data
  plane, not a control-plane health endpoint**: the vendor-documented probe would have
  stayed green through the 2026-08-18 Pomerium wedge. **Never probe a backup/quiesce
  sidecar at all** — readiness drops the Pod from its EndpointSlice and liveness gets there
  through CrashLoopBackOff, so a backup fault takes the application offline; detect those at
  the artifact instead. Reasoning, per-service targets and the failures probes *don't*
  catch: `docs/operations/monitoring.md`.
- **Scheduled work gets a dead-man's-switch, not a probe.** Every CronJob sets
  `timeZone: "UTC"` and `activeDeadlineSeconds` (with `concurrencyPolicy: Forbid`, one
  hung run silently blocks every later run), plus `startingDeadlineSeconds` where a missed
  window must be retried rather than dropped. New jobs must ping healthchecks.io on
  **start and exit code** — the two restic jobs, `cloudflare-analytics` and `influx-backup`
  do; the two ingest checks and `jottacloud backup` ping on success only, so a failure
  shows up as silence. For the ingest checks that is deliberate and must not change;
  jottacloud's ping comes from `backup.sh` inside a third-party image. `update-watch`
  sends **no `/start` at all**, also deliberately: it pings `/log` when it could not read
  GitHub, and a `/start` with no success inside the grace would turn every such run into
  a false alarm. Do not "complete" its ping set. Inventory and
  per-job semantics: `docs/operations/monitoring.md`.
- **A ping body is a disclosure channel.** Every ping carries a short `key=value` summary
  (`summary=` first, printable ASCII), and **never a command's output** — the exec'd influx
  scripts carry the operator token on argv, and a failing `wget` quotes the ping URL, which is
  the check's write credential. The rule is blanket because a script cannot sort the tiers
  below apart at runtime. Emit a count, an age, a size, a path built from a literal glob, or a
  verdict from a fixed enum.
  `make check-ping-bodies` enforces it and is the only thing that catches
  `M=$(cmd); emit "error=$M"`. The body also travels with the alert: upstream's email,
  webhook, Slack, Telegram, Matrix, GitHub and MS Teams transports all read it into the
  notification, so a failure body reaches every channel this account has configured — a
  list nobody has enumerated. Policy, the accepted residuals and that open item:
  `docs/operations/monitoring.md`.
- **A new InfluxDB bucket in the `health` namespace means two edits, not one:** create it
  (a `make health-influx-*-bootstrap` target) **and** add it to the explicit bucket list in
  `homelab/health/backups.yaml`. A bucket missing from that list is silently never
  exported; a bucket in the list that does not exist now fails the nightly job by name.
  Bootstrap before applying (`docs/operations/homelab-health.md`).
- **One-shot `Job`s must set `ttlSecondsAfterFinished`.** A Job's `spec.template` is
  immutable, so a completed Job that is never garbage collected pins the version of
  itself that ran. The next apply that changes it fails with `field is immutable`, and
  since `kubectl apply` continues past a failed resource and reports only through its exit
  code, the failure is quiet: everything else applies and the Job silently does not. The
  homelab `restic-init` Job lacked the field, survived from 2026-04-11 to 2026-08-20, and
  broke `diff-homelab` and `apply-homelab` for four months. Deleting the stale Job is the
  recovery; the TTL is the prevention. `make check-job-ttl` enforces it across both
  clusters, and `diff-*`/`apply-*` run the per-cluster variant as a preflight, so it
  cannot be forgotten. The preflight is a context assertion, a vars-consistency check,
  and **five render-based guards that each run as their cluster's half** —
  `check-script-substitution`, `check-job-ttl`, `check-ping-bodies`,
  `check-script-lint` and `check-renovate-scope` — on all four of `diff-homelab`,
  `apply-homelab`, `diff-vps` and `apply-vps`. `check-renovate-scope` joined them on
  2026-08-26, in the commit that widened Renovate far enough for it to pass.
  Jobs *generated by a CronJob* are exempt and the check ignores
  them: each run gets a unique name, so they never collide, and pile-up is bounded by
  `successfulJobsHistoryLimit`.
- **Logic lives in a script file, never in an inline YAML string.** Anything with
  branching, parsing or loops goes in a real file under a `scripts/` path and reaches the
  cluster through a `configMapGenerator`, as
  `homelab/health/scripts/cloudflare-analytics-ingest.py` does. A file can be imported,
  unit-tested, linted and diffed line by line; a 600-line block scalar can do none of
  those, and reviewing one means extracting it by hand first. Two things the generator
  needs: set `namespace:` on the generator entry (kustomize only rewrites a name
  reference when referrer and referent agree on namespace — omit it and the ConfigMap
  lands in `default` *and* the workload keeps pointing at the unsuffixed name), and leave
  the content-hash suffix ON, so editing a script rolls the workload that mounts it.
  Short straight-line `sh`/`kubectl` still belongs inline: the rule is about logic, not
  length.
- **A generated script must never name an `ENVSUBST_VAR_NAMES` variable.** Generator
  files ride the same stream as every manifest, so envsubst rewrites them — and it
  substitutes the **bare `$NAME`** form, not just `${NAME}` (verified). The homelab
  allowlist holds `RESTIC_REPOSITORY`, `RESTIC_PASSWORD`, `B2_ACCOUNT_ID` and
  `B2_ACCOUNT_KEY`, which are exactly the names restic reads, so
  `echo "repo at $RESTIC_REPOSITORY"` in a script publishes the real B2 URL inside a
  **ConfigMap**; `$RESTIC_PASSWORD` publishes the repository password in plaintext. No
  placeholder survives, so `check-placeholder-coverage` sees nothing. `make
  check-script-substitution` is the guard, and `diff-*`/`apply-*` run the per-cluster
  variant as a preflight, alongside `check-ping-bodies`. If a script needs such a value,
  indirect it:
  give the container a differently named env var (`secretKeyRef` for a secret) and use
  that name in the script. For the same reason, **do not share one script between the
  two clusters** when it touches restic: the VPS names are `VPS_`-prefixed and the
  homelab ones are not, so a shared file is safe under one tree and leaking under the
  other.
- **Every script is linted from the RENDER, as POSIX `sh`.** `make check-script-lint`
  runs `kustomize build`, pulls the shell back out of ConfigMap `data:` keys *and* out
  of inline `args:`/`command:` block scalars, and runs `shellcheck -s sh` over it — plus
  compiling every `*.py` and running every `test_*.py`. `diff-*`/`apply-*` run the
  per-cluster variant as a preflight, so it cannot be forgotten. Two rules when working
  on a script: never "fix" a finding by switching the check to `-s bash` (these run under
  busybox ash and dash, and SC3xxx is the whole point), and if a warning is genuinely
  wrong add a narrow `# shellcheck disable=SCxxxx` **with a stated reason**, as the
  existing ones do. `shellcheck` is now a required tool (`make check-tools`);
  `brew install shellcheck`. Findings from upstream bases are advisory and do not fail
  the check. Rationale and the extraction rules:
  `docs/operations/apply-workflow.md`.
- **Every container is in exactly one update mode, and `make check-renovate-scope`
  proves it.** Floating tag means keel; pinned tag means Renovate; never both. The
  guard renders each cluster and judges one container at a time: a complete keel
  annotation set on a floating tag is legal; the same set on a **pinned** tag is
  the frozen state (`match-tag` only refreshes the digest) and fails; an
  **incomplete** set fails on any tag, because a missing `match-tag` silently
  downgrades a semver tag to `:latest`. A pinned, keel-free image must be named by
  a repo file **in the same cluster's tree** that is inside
  `kubernetes.managerFilePatterns` and outside `ignorePaths` — a `packageRule` alone
  does **not** widen scope, and the per-cluster confinement is load-bearing, because
  both trees name many of the same images and a repo-wide lookup would let a watched
  homelab file vouch for an unwatched VPS container. keel annotations are a
  **workload** property: a pinned sidecar beside a floating app image is
  Renovate's, not frozen, so only a workload with nothing floating in it can be
  frozen. Floating tags are forbidden in the `health`, `hindsight`, `ops` and
  `backup` namespaces; `jottacloud-backup` is the one written exemption on that
  guard's `FLOATING_EXEMPT` list, because it is a CronJob whose pods pull
  `:latest` on every scheduled run and so needs no keel. Images from remote
  bases are advisory.
- For new `hostPath`/`hostNetwork` workloads: elevate their namespace to PSA
  `privileged` in the cluster's `bootstrap/namespaces.yaml`. The cluster-wide enforce
  level is `baseline`.
- After adding a new secret placeholder: add it to `.env.tpl` and to **both**
  `ENVSUBST_VAR_NAMES` and `REQUIRED_VARS` in the `Makefile` (the `VPS_*` lists for VPS
  vars). No `direnv reload` is needed for a value — `op run` resolves references per
  command; reload only after changing `OP_SERVICE_ACCOUNT_TOKEN` itself.
- When creating 1Password items with `op item create`, explicitly type the fields: a bare
  `field=value` defaults to **concealed**, so mark non-secret fields (emails, IDs, UUIDs,
  hostnames) as `field[text]=value` and keep only actual secrets concealed.
  Wrongly-concealed non-secrets make the vault harder to debug; visible secrets are worse.
- **Secret disclosure → honesty box.** If you (human or agent) expose a real secret value
  anywhere it doesn't belong — terminal output, an agent report/transcript, chat, a log or
  scratch file — do two things immediately: (1) tell the operator in your next message,
  and (2) append a row to `secrets-to-rotate.md` at the repo root identifying the secret
  by its `op://` reference or k8s secret/key, how it was disclosed, and by whom.
  Identifiers only — never the value. Err on the side of logging: a false-positive row
  costs one unnecessary rotation; a silent disclosure costs the assumption of
  confidentiality. This applies even when the exposure feels harmless (short-lived token,
  local-only transcript, immediately-cleared scrollback).
- **Know the difference between a secret and an identifier.** Conflating them wastes
  rotations, clutters the honesty box, and — worse — makes agents refuse to log or print
  things that are perfectly fine, which hides real diagnostics. Three tiers:
  1. **Secrets grant access.** Tokens, passwords, private keys, API keys, session cookies,
     the 1Password service-account token. Disclosure means honesty box **and** rotation.
  2. **Spam-target identifiers grant no access but let a stranger cause a nuisance.**
     healthchecks.io ping UUIDs are the case that matters here: anyone holding one can ping
     your check and mask a genuine failure. Keep them out of this public repo (`op://`
     reference only) — but a transcript or a pod log is **not** a disclosure, they need
     **no rotation**, and they get **no honesty-box row**. This has been ruled three times.
  3. **Ordinary identifiers are not sensitive at all.** Restic repository URIs, B2 and
     InfluxDB bucket names, Cloudflare zone IDs, PVC UUIDs, namespaces, FreshRSS usernames,
     hostnames. They grant nothing and enable nothing. They are fine in pod logs, in ping
     bodies and in agent output. Keep the account-identifying ones out of committed files;
     otherwise leave them alone.
  When in doubt ask "what can someone *do* with this?" — not "does it look secret?".
- **`ENVSUBST_VAR_NAMES` membership is not a secrecy classification.** `RESTIC_REPOSITORY`
  and the bucket names are on that list because envsubst would *substitute* them into a
  rendered ConfigMap, which is a mechanical hazard, not because the values are secret. Do
  not infer sensitivity from that list.
- **This repo is public.** Never write the Omni service URL, sign-in identity, or any
  other credential-adjacent value into a committed file — reference it as an `op://`
  path and read it at run time.
- **Nothing ships in the operator's name without explicit approval.** Upstream pull
  requests, issues on third-party trackers, pushes to public forks, and comments on
  other people's repositories are all publicly attributed to the operator. Prepare
  the work locally — branches, commits, drafted PR and issue text — and present it
  for review; the operator says when each item is published, one item at a time.
  Merging pull requests in this repo when asked is fine: the gate is third-party
  visibility, not git mechanics.
- Prefer `kubectl exec deployment/<name> -- sh -c '...'` plus `rollout restart` for
  in-container file tweaks rather than spinning up a helper pod.
- Documentation belongs in `docs/`, **referenced** from this file rather than included in
  it. When you learn something operational, write it into the relevant `docs/` file and
  add a pointer here only if it changes how an agent edits the repo.
- **Documentation, not agent memories.** Do not record repo, cluster, or account state in
  an agent's private memory system — that hides operational knowledge from the operator,
  from other agents, and from review. Anything worth remembering goes in `docs/` (or this
  file, per the rule above), where it is versioned, diffable and shared. This applies to
  facts about adjacent infrastructure the repo touches (VMs, DNS, Cloudflare account
  state), not just the manifests themselves.

## Legacy Reference

`legacy-microk8s/` contains the original flat-layout microk8s manifests and
`no_longer_used/` holds retired manifests. Both are **frozen reference only** — do not
add new files to either. `legacy-microk8s/` exists to be deleted: remove it once the
Talos rebuild is fully operational and nothing is still being cross-referenced out of it. `README.md` at the repo root still describes the retired
microk8s/rancher setup and does not reflect the current clusters.
