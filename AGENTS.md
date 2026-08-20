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
| `docs/operations/monitoring.md` | How failures get noticed: probe policy and inventory, CronJob deadlines, healthchecks.io checks, the uptime-kuma manual runbook, and what none of it catches |

Design documents and implementation plans are local-only under the gitignored
`docs/superpowers/` tree (`specs/2026-04-11-talos-homelab-rebuild-design.md`,
`plans/2026-04-11-talos-homelab-rebuild.md`).

## Clusters at a glance

| | Homelab | VPS |
|---|---|---|
| kubectl context | `cynexia-homelab` | `cynexia-vps` |
| Omni cluster name | `homelab` | `vps` |
| Domain | `*.cynexia.net` (Route53) | `*.cynexia.com` (Cloudflare) |
| Exposure | Private — LAN/Tailscale only, except the `health` namespace's own `cynexia-health` tunnel | Public, via the `cynexia-vps` cloudflared tunnel + Cloudflare Access |
| Ingress | Traefik hostNetwork DaemonSet + cert-manager wildcard | cloudflared only (no Traefik, no cert-manager) |
| Apply | `make apply-homelab` | `make apply-vps` |

The two domains are unrelated zones on different providers. Don't cross them.

## Repo Layout

```
kubernetes_config/
├── .envrc                    # direnv entrypoint (loads 1Password-backed vars)
├── .env.tpl                  # op-template with VAR=op://... lines (committed; no real secret values)
├── Makefile                  # build/diff/apply per cluster + secret and bootstrap helpers
├── renovate.json             # scoped to homelab/health/** only (pinDigests)
├── secrets-to-rotate.md      # honesty box for disclosed secret values (identifiers only)
├── docs/                     # operational documentation (docs/superpowers/ is gitignored)
├── homelab/                  # Talos homelab cluster
│   ├── kustomization.yaml    # top-level: bootstrap + secrets + workloads + backup + health
│   ├── talos/                # Omni ConfigPatches resources (applied via `make apply-talos`)
│   ├── bootstrap/            # platform: namespaces (with PSA labels), local-path, NFS CSI, cert-manager, traefik, keel
│   ├── workloads/            # application workloads (one file per service, --- separated, no ns override)
│   ├── secrets/              # Secret manifests with ${VAR} envsubst placeholders
│   ├── health/               # health-data pipeline (no keel; pinned images)
│   └── backup/               # restic init Job + nightly CronJob (hostPath /var/mnt/ssd/local-path-provisioner)
├── vps/                      # Hetzner Talos cluster, same sub-layout (bootstrap/secrets/workloads/backup/talos)
├── scripts/                  # standalone helper scripts (karakeep tag maintenance)
├── legacy-microk8s/          # frozen reference copies of the old microk8s manifests
└── no_longer_used/           # retired manifests kept for reference
```

## Apply Workflow (conventions)

Full mechanics, target-by-target reference and failure modes:
`docs/operations/apply-workflow.md`. The rules that must not be broken:

- **Secrets reach manifests via `op run` + envsubst, resolved per command.** `.env.tpl`
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
  variables inside upstream manifests (e.g. `$VOL_DIR` in local-path-provisioner's helper
  pod); with double quotes the shell expands the tokens before envsubst sees them.
- **Adding a secret means four edits:** the `op://` line in `.env.tpl`, the name in
  `ENVSUBST_VAR_NAMES`, the name in `REQUIRED_VARS`, and the `${VAR}` placeholder in the
  manifest. `make check-vars-consistency` hard-fails if a substituted var is missing from
  `REQUIRED_VARS` — but **nothing** catches a var missing from `ENVSUBST_VAR_NAMES`: that
  ships the literal `${VAR}` into a Secret. Cheap confirmation after adding one:
  `make build-<cluster> | grep -F '${'` finds any placeholder that survived the render.
  Detail: `docs/operations/apply-workflow.md`.
- **Multi-line secrets can't go through envsubst** (they break YAML after substitution).
  Use a dedicated `make <service>-secret` target with
  `op read` + `kubectl create secret --dry-run=client -o yaml | kubectl apply -f -`;
  `make create-jotta-secret` is the canonical pattern. 1Password *document* items need
  `op document get`, not `op read`.
- **Apply targets assert the kubectl context first** (`check-context` /
  `check-vps-context`). Never bypass them.
- `make apply-homelab` reporting `configured` rather than `unchanged` for Secrets, some
  PVs and cert-manager webhooks is expected and is **not** drift — see the apply-workflow
  doc before investigating.

## File Conventions

- Each service is **one YAML file** under `homelab/workloads/` (or `vps/workloads/`)
  containing its Deployment, Service, Ingress and PVCs separated by `---`.
- **Every resource declares its own `namespace:` explicitly.** Do NOT add a top-level
  `namespace:` to `homelab/workloads/kustomization.yaml` — it would rewrite the namespace
  on every resource and break services that live outside `downloads`
  (e.g. jottacloud-backup).
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
- Every new Deployment must include the full keel annotation set above — **except** in
  the `health` namespace, which explicitly forbids keel: every image there is
  version/digest-pinned and Renovate proposes bumps instead
  (`docs/operations/homelab-health.md`).
- **Probes: readiness on every long-running container that serves traffic; liveness only
  where that probe can actually detect the failure *and* a restart is a safe remedy**
  (everything here is single-replica, so an over-eager liveness probe manufactures
  outages). **Always set
  `timeoutSeconds`** — the 1s default false-positives on a loaded node. **Probe the data
  plane, not a control-plane health endpoint**: the vendor-documented probe would have
  stayed green through the 2026-08-18 Pomerium wedge. **Never probe a backup/quiesce
  sidecar at all** — readiness drops the Pod from its EndpointSlice and liveness gets there
  via CrashLoopBackOff, so a backup fault takes the application offline; detect those at
  the artifact instead. Reasoning, per-service targets and the failures probes *don't*
  catch: `docs/operations/monitoring.md`.
- **Scheduled work gets a dead-man's-switch, not a probe.** Every CronJob sets
  `timeZone: "UTC"` and `activeDeadlineSeconds` (with `concurrencyPolicy: Forbid`, one
  hung run silently blocks every later run), plus `startingDeadlineSeconds` where a missed
  window should be retried rather than dropped. New jobs should ping healthchecks.io on
  **start and exit code** — the two restic jobs do; the three older ones ping on success
  only, so a failure shows up as silence. Inventory and per-job semantics:
  `docs/operations/monitoring.md`.
- **One-shot `Job`s must set `ttlSecondsAfterFinished`.** A Job's `spec.template` is
  immutable, so a completed Job that is never garbage collected pins the version of
  itself that ran. The next apply that changes it fails with `field is immutable`, and
  since `kubectl apply` continues past a failed resource and reports only through its exit
  code, the failure is quiet: everything else applies and the Job silently does not. The
  homelab `restic-init` Job lacked the field, survived from 2026-04-11 to 2026-08-20, and
  broke `diff-homelab` and `apply-homelab` for four months. Deleting the stale Job is the
  recovery; the TTL is the prevention.
- For new `hostPath`/`hostNetwork` workloads: elevate their namespace to PSA
  `privileged` in the cluster's `bootstrap/namespaces.yaml`. The cluster-wide enforce
  level is `baseline`.
- After adding a new secret placeholder: add it to `.env.tpl` and to **both**
  `ENVSUBST_VAR_NAMES` and `REQUIRED_VARS` in the `Makefile` (the `VPS_*` lists for VPS
  vars). No `direnv reload` is needed for a value — `op run` resolves references per
  command; reload only after changing `OP_SERVICE_ACCOUNT_TOKEN` itself.
- When creating 1Password items via `op item create`, explicitly type the fields: a bare
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
- **This repo is public.** Never write the Omni service URL, sign-in identity, or any
  other credential-adjacent value into a committed file — reference it as an `op://`
  path and read it at run time.
- Prefer `kubectl exec deployment/<name> -- sh -c '...'` plus `rollout restart` for
  in-container file tweaks rather than spinning up a helper pod.
- Documentation belongs in `docs/`, **referenced** from this file rather than included in
  it. When you learn something operational, write it into the relevant `docs/` file and
  add a pointer here only if it changes how an agent should edit the repo.

## Legacy Reference

`legacy-microk8s/` contains the original flat-layout microk8s manifests and
`no_longer_used/` holds retired manifests. Both are **frozen reference only** — do not
add new files to either. `legacy-microk8s/` exists to be deleted: remove it once the
Talos rebuild is fully operational and nothing is still being cross-referenced out of it. `README.md` at the repo root still describes the retired
microk8s/rancher setup and does not reflect the current clusters.
