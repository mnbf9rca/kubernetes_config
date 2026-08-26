# Apply workflow and Makefile reference

How manifests get from this repo into a cluster, and how 1Password secrets get into
manifests without ever being committed. The behaviour-changing rules are summarised in
`AGENTS.md`; this file is the full reference.

## Secret flow: 1Password → `op run` → envsubst → kubectl

`.env.tpl` is committed and contains only `VAR=op://Vault/item/field` lines — no real
values. **Nothing resolves them into the ambient shell.** `.envrc` exports exactly one
variable:

```bash
export OP_SERVICE_ACCOUNT_TOKEN="$(op read 'op://homelab/1pw/service_account_key')"
```

Every target that needs secrets resolves them lazily, per command, through the Makefile's

```make
OP_RUN := op run --env-file=.env.tpl --
```

Targets that need secrets are split in two: a **public target** that runs the guards
(`check-context`, `check-vars-consistency`, `check-job-ttl-*`,
`check-script-substitution-*`) in the parent shell — before anything can
touch the cluster — and then re-enters make under `$(OP_RUN)`, and a private
**`_*-inner` target** that does the real work with the values present. `op run` resolves
the `op://` references in `.env.tpl` against the service-account token and injects the
real values into the environment of that **one child process**; envsubst substitutes them
into the manifests inside that same child.

Why this shape:

- **Nothing long-lived.** Secrets exist for the lifetime of a single command, not the
  lifetime of the shell — or of an agent session that inherited it. `printenv` in this
  directory shows the token and nothing else.
- **Masked output.** `op run` masks the child's stdout/stderr, so a stray `echo` or a
  rendered manifest scrolling past does not leak a value.
- **Scoped credential.** The service account is limited to the vaults this project needs.

> **The old `op inject` + `set -a` model is gone. Do not restore it.** `.envrc` no longer
> contains `set -a; eval "$(op inject -i .env.tpl)"; set +a`, and its absence is
> deliberate, not an unfinished edit. Restoring it would put every secret value back into
> the ambient environment of every process launched from this directory — the exact
> exposure the rework removed. If an apply target reports a var MISSING, the fix is a line
> in `.env.tpl`, not an `export` in `.envrc`.

Diagnostics when a value doesn't arrive:

- `make require-vars` — re-enters under `op run` and asserts every `REQUIRED_VARS` entry
  is set **and** resolved (a value still holding a literal `op://...` string is reported
  UNRESOLVED, which is what catches a silent resolution failure). A healthy tree prints
  `OK: 25 / 25 required vars set`.
- `direnv reload` — only relevant to `OP_SERVICE_ACCOUNT_TOKEN`. It does **not** refresh
  secret values, because no secret value is ever in the shell to refresh; see
  [rotation](#rotating-a-secret) below.
- If the service account cannot read the referenced vault item, the var reports MISSING
  even though `.env.tpl` looks right. Check vault scope before suspecting the template.

### Rotating a secret

Edit the 1Password item, then re-run the apply target and restart the consuming pod:

```bash
make apply-homelab                       # re-resolves every op:// reference from scratch
kubectl -n <ns> rollout restart deployment/<name>
```

There is **no `direnv reload` step** for a rotated value. Under `op run`, values are
resolved per command at apply time, so the next `make apply-*` picks the new value up
automatically. (`direnv reload` is still the fix if the *service-account token* itself
changed, or if `OP_SERVICE_ACCOUNT_TOKEN` is unset in your shell.)

### `op run` masks stdout, not environment variables

An earlier version of this document claimed `op run` sets the child's environment
variables to the literal 24-character string `<concealed by 1Password>`. **That was wrong,
and was retested on 2026-08-20.**

What is actually true:

- `op run` passes the **real values** in the child's environment. Verified by measuring
  two secrets of known length — 100 characters and 27 characters — inside the child: both
  arrived intact.
- What `op run` masks is the child's **stdout/stderr**. Any secret value that appears in
  the output stream is replaced with `<concealed by 1Password>` on the way to the
  terminal.
- The original diagnostic tell was a coincidence: `echo "len=${#ACME_EMAIL}"` returned 24
  because that value really is 24 characters long — the same length as the mask string —
  so the test could not tell a real value from the placeholder.

That distinction is the whole reason `build-*` and `apply-*` are different targets.

**The hazard — and note that no explicit `op run` wrapper is needed to hit it, because
the Makefile already wraps `build-*`:**

```bash
make build-homelab > out.yaml     # every secret in out.yaml is now the 24-char mask
kubectl apply -f out.yaml         # stores <concealed by 1Password> in every Secret
```

The apply reports success and the workloads then fail with garbage credentials — silent
corruption that doesn't look like a mistake at the point it happens. The same applies to
`tee`, and to copying a rendered manifest out of terminal scrollback.

`diff-*` and `apply-*` are safe because they keep the rendered stream **inside** the
`op run` child and pipe it straight into kubectl, so real values never cross stdout and
masking never sees them. **Render-then-apply is the risky shape; the one-step pipeline is
not.** `op run --no-masking` exists and is deliberately not used: an unmasked render is a
secret-shaped file on disk waiting to be committed or pasted.

One consequence worth knowing: because `kubectl diff` prints whole manifests, a change to
a secret **value** shows as *no change* in `make diff-*` output — both sides mask to the
same string. The server-side comparison and the apply both use the real value. To confirm
a specific secret landed, read it back with
`kubectl -n <ns> get secret <name> -o jsonpath=...`.

### Historical: `op inject` resolved commented lines

Kept only so the hazard isn't rediscovered and misapplied. Under the **old** `op inject`
model, `#TAILSCALE_AUTH_KEY=op://...` in `.env.tpl` still resolved — a shell `#` comment
did not short-circuit op's template substitution, so secrets could surface from lines that
looked disabled.

This no longer applies: `op inject` is used nowhere in this repo, and `op run --env-file`
skips commented lines entirely (tested). A commented-out line in `.env.tpl` today is
genuinely inert.

### The envsubst allowlist

`ENVSUBST_VARS` is an explicit allowlist, **passed single-quoted**. Never call envsubst
without one:

- With no allowlist, envsubst substitutes every `${VAR}` token in the stream, including
  shell variables embedded in upstream manifests (e.g. `$VOL_DIR` inside
  local-path-provisioner's helper-pod setup script), breaking them silently.
- With double-quoted args, the shell expands `${VAR}` before envsubst sees it,
  producing garbage arguments. Single-quoting preserves the literal tokens.

The Makefile keeps `ENVSUBST_VAR_NAMES` (plain names) as the single source of truth and
derives `ENVSUBST_VARS` (the `${VAR}` form) from it via `foreach`.

### Adding a secret is four edits, not three

1. the `VAR=op://Vault/item/field` line in **`.env.tpl`**,
2. the name in **`ENVSUBST_VAR_NAMES`** in the `Makefile`,
3. the name in **`REQUIRED_VARS`** in the `Makefile` (VPS vars go in the `VPS_*` lists),
4. the `${VAR}` placeholder in the **manifest**.

Older wording in this repo said "three edits" and omitted `REQUIRED_VARS`. That is wrong:
`check-vars-consistency` asserts `ENVSUBST_VAR_NAMES ⊆ REQUIRED_VARS` and **hard-fails**
the apply if a substituted var is not also required.

What that check does and does not catch:

| Mistake | Caught? | Result |
|---|---|---|
| In `ENVSUBST_VAR_NAMES`, missing from `REQUIRED_VARS` | **Yes** — `check-vars-consistency` fails the apply | — |
| In `REQUIRED_VARS`, missing from `.env.tpl` | **Yes** — `require-vars` reports MISSING | — |
| Missing from **`ENVSUBST_VAR_NAMES`** | **No. Nothing catches this** | envsubst never substitutes the token, so the literal string `${VAR}` is written into the Secret and applied. The manifest looks fine and the workload gets a credential that is the placeholder text |

That third row is the reason to treat the allowlist edit as the one to double-check. To
confirm no placeholder survived the render after adding a var, run:

```sh
make build-<cluster> | grep -F "$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/${\1}/p' .env.tpl)"
```

The `sed` turns every name in `.env.tpl` into a `${VAR}` pattern, so the grep matches
only this repo's placeholders and prints nothing on a clean tree. Because the pattern
comes from `.env.tpl`, not `ENVSUBST_VAR_NAMES`, it also catches the uncaught row above:
a var added to `.env.tpl` but forgotten from the allowlist. Do not use a bare
`grep -F '${'` — the rendered stream contains ConfigMap-mounted shell scripts whose
parameter expansions (for example `${1:-}` and `${HC_UUID}`) match it about 20 times. A
`make check-*` guard target running this in the apply preflight is a possible follow-up.

`TAILSCALE_AUTH_KEY` is deliberately **not** in `ENVSUBST_VAR_NAMES`: auth keys are
one-shot and only needed for initial node registration, so steady-state config must
never contain one. `make bootstrap-tailscale` has its own dedicated allowlist and reads
the key from the ambient environment, which is why that one target expects an `export`.

### Multi-line secrets bypass envsubst

Multi-line values (like `rclone.conf`) break YAML parsing after substitution. The escape
hatch is a dedicated Makefile target that calls `op read` and pipes into
`kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -`.
`make create-jotta-secret` is the canonical pattern. Use it only for secrets that
genuinely can't be single-line; everything else flows through envsubst.

Note that 1Password **document** items (e.g. `health-cloudflared`) need
`op document get`, not `op read` — document items don't expose a plain field.

## Makefile targets

### Homelab

| Target | What it does |
|---|---|
| `check-tools` | Asserts `kubectl kustomize envsubst op direnv talosctl omnictl jq shellcheck` are on PATH |
| `check-context` | Asserts `kubectl current-context == cynexia-homelab` (override with `HOMELAB_CONTEXT=`) |
| `check-vars-consistency` | Asserts `ENVSUBST_VAR_NAMES` ⊆ `REQUIRED_VARS`. Runs in the parent shell, before the `op run` child exists. Cannot detect a var *missing* from `ENVSUBST_VAR_NAMES` |
| `check-job-ttl` | Asserts every standalone `kind: Job` sets `ttlSecondsAfterFinished`, across both clusters. `check-job-ttl-homelab` scopes it to one cluster and runs in the `diff-homelab`/`apply-homelab` preflight |
| `check-script-substitution` | Asserts no `configMapGenerator` script names an envsubst-allowlisted variable, across both cluster trees. `check-script-substitution-homelab` scopes the *scan* to one tree — both allowlists still apply — and runs in the `diff-homelab`/`apply-homelab` preflight |
| `check-ping-bodies` | Asserts no healthchecks.io ping body is built from a command's output, across both cluster trees. `check-ping-bodies-homelab` scopes the scan to one tree and runs in the `diff-homelab`/`apply-homelab` preflight |
| `check-script-lint` | Lints every script the clusters run, from the **rendered** stream rather than the source tree, plus the repo's Python. `check-script-lint-homelab` scopes the render to one cluster and runs in the `diff-homelab`/`apply-homelab` preflight. See below |
| `check-renovate-scope` | Asserts every container is in exactly one update mode — floating means keel, pinned means Renovate, never both — from the `kustomize build` render, one container at a time, across both clusters. `check-renovate-scope-homelab` scopes it to one cluster and runs in the `diff-homelab`/`apply-homelab` preflight, joining it as the fifth per-cluster guard and the third render-based one. See below |
| `require-vars` | Re-enters under `op run` and asserts every `REQUIRED_VARS` entry is set and not still an `op://` reference |
| `build-homelab` | `kustomize build homelab/ \| envsubst` to stdout under `op run`. **PREVIEW ONLY — secret values are masked.** No cluster contact. Never redirect this to a file and apply it |
| `diff-homelab` | Same pipeline into `kubectl diff`, inside the `op run` child (real values, printed diff masked) |
| `apply-homelab` | Same pipeline into `kubectl apply` with real values, after the guards above |
| `create-jotta-secret` | Imperative Secret creation for jottacloud-backup (multi-line rclone config) |
| `apply-talos` | envsubst + `omnictl apply` over `homelab/talos/machineconfig-patches/*.yaml`. **Not** wrapped in `op run` — no current patch contains a `${VAR}`, so the envsubst pass is a no-op today; a patch that needs a secret would have to be wrapped first |
| `bootstrap-tailscale` | One-shot Tailscale extension bootstrap for a node (see below) |
| `clear-tailscale-bootstrap` | Removes the one-shot bootstrap ConfigPatch |

### VPS

| Target | What it does |
|---|---|
| `check-vps-context` | Asserts `kubectl current-context == cynexia-vps` (override with `VPS_CONTEXT=`) |
| `check-vps-vars-consistency` / `require-vps-vars` | VPS equivalents of the homelab preflights |
| `check-job-ttl-vps` / `check-script-substitution-vps` / `check-ping-bodies-vps` / `check-script-lint-vps` | The per-cluster halves of the repo-wide checks, run in the `diff-vps`/`apply-vps` preflight. Scoping them per cluster is the point: a VPS-only fault must not block `apply-homelab`, and vice versa |
| `check-renovate-scope-vps` | The VPS half of the update-mode guard, scoped to the `vps/` render, and the fifth per-cluster guard in the `diff-vps`/`apply-vps` preflight. It keeps its own row rather than joining the one above because it arrived later, in the commit that widened Renovate's scope far enough for it to pass. See below |
| `build-vps` / `diff-vps` / `apply-vps` | Same pipeline and the same masking split over `vps/` with `VPS_ENVSUBST_VARS` |
| `route-vps-dns` | `cloudflared tunnel route dns cynexia-vps <host>` for every hostname in `vps/bootstrap/cloudflared/cloudflared.yaml` |
| `create-cloudflared-secret` | Imperative Secret creation for the VPS tunnel creds from `op://VPS/cloudflared/credentials-json` |

Targets that read a single field imperatively (`create-jotta-secret`,
`create-*-cloudflared-secret`, `health-influx-bootstrap`) call `op read` / `op document
get` directly rather than going through `op run`; they authenticate with the same
service-account token from `.envrc`.

The VPS block is a deliberate copy-paste of the homelab block rather than a
parameterised macro — reading `apply-vps` top to bottom is clearer than chasing a
generated target, and two clusters is not enough to justify the abstraction.

### Health namespace

| Target | What it does |
|---|---|
| `create-health-cloudflared-secret` | Recreates the health tunnel creds Secret via `op document get health-cloudflared` |
| `route-health-dns` | CNAMEs for every hostname in `homelab/health/cloudflared.yaml` onto the `cynexia-health` tunnel |
| `health-influx-bootstrap` | InfluxDB buckets, v1 DBRP mapping, v1-compat auth user, and the two scoped tokens — see [homelab-health.md](homelab-health.md) |

### `check-script-lint`: linting what the cluster actually runs

Until this landed, nothing the repo could run looked at any of its sixteen
shell and Python scripts. There was no shellcheck target, no ruff, no pyflakes,
no test runner, no `.github/workflows` and no pre-commit hook — every
shellcheck result that ever appeared in a review came from an agent typing the
command by hand. That is the same defect `check-job-ttl` and
`check-script-substitution` were each created to fix, and it is fixed the same
way: `scripts/check-script-lint.py`, wired as a per-cluster preflight
prerequisite of `diff-*` and `apply-*`.

Four decisions in it are load-bearing.

**It lints the render, not the source tree.** `homelab/backup/restic-cronjob.yaml`
carries roughly 430 lines of shell inline in a YAML block scalar, which a
source-tree lint walks straight past. So the check runs `kustomize build` and
pulls the shell back out of the rendered stream — from ConfigMap `data:` keys
(what a `configMapGenerator` produces) and from block scalars inside a
container's `args:`/`command:` list. The language of an inline block comes from
the interpreter the same container names in its `command:`. A block whose
interpreter cannot be identified is reported as *could not run*, never skipped:
an unlinted block of shell is exactly the hole the check exists to close.

Findings are reported against the source file wherever the snippet can be
located there — exact contiguous match allowing a constant indent — so
`homelab/backup/restic-cronjob.yaml:52` is somewhere you can go and edit, not a
line number in a 19,000-line render.

**`shellcheck -s sh`, never `-s bash`.** These scripts run under busybox ash
(`restic/restic`, `alpine/k8s`) and dash. `-s bash` would suppress SC3040 and
the whole SC3xxx portability family, which are the findings that matter here: a
bashism in an ash container is a backup job failing at 03:00, not a style nit.
`-s` overrides the shebang, which is the point. The deliberate
`# shellcheck disable=` directives in the scripts are honoured as written.

**Upstream findings are advisory.** `local-path-config`'s `setup`/`teardown`
keys really do run on the node, so they are linted and reported — but they come
from a remote base and cannot be fixed here, only forked. Failing every apply on
somebody else's style warning produces a gate people route around, and a
routed-around gate protects nothing. A snippet counts as ours when it can be
located in a repo file; the upstream ones are named in the OK output, so one of
your own that stops resolving is visible rather than silently downgraded.

**Missing tools are reported, never silently passed.** Exit 1 means a finding;
exit 2 means the check could not run — same convention as `check-job-ttl`, and
it matters for the same reason: a `kustomize build` that never rendered tells
you nothing about the scripts in it. `shellcheck` is treated as required (it is
in `check-tools`; `brew install shellcheck`) because skipping it restores the
hole. The Python phase always compiles every `*.py` and runs every `test_*.py`
— both stdlib, so both always available — and runs a real linter only if `ruff`,
`pyflakes` or `flake8` is genuinely installed, printing an explicit `SKIP`
naming what it probed when none is. As of 2026-08-21 none is installed on the
workstation, so Python is syntax-checked and tested but **not** linted.

The Python phase is repo-wide whichever cluster is named: it needs no render
and no cluster, so scoping it per-cluster would only leave the repo's own
tooling scripts unguarded.

### `check-renovate-scope`: one container, one update mode

Two mechanisms update this estate and each is silent when it stops. keel bumps
floating tags on a timer; Renovate proposes bumps for pinned ones. The rule is
**floating tag means keel, pinned tag means Renovate, never both** — and every
way of getting a container's mode wrong fails quietly. A pinned tag carrying
keel annotations is frozen while looking covered, because `keel.sh/match-tag`
on a pin only refreshes the digest. An incomplete keel annotation set is worse
than none, because without `match-tag` keel silently downgrades a semver tag to
`:latest`. A pinned tag with no keel annotations and outside Renovate's scope
receives nothing at all, while `homelab-update-watch` counts zero open pull
requests and stays green over it.

The version this replaced could see none of that. It asked whether a *file*
mentioned `keel.sh/policy` anywhere and whether a *file* pinned any image, so a
file holding one keel-managed Deployment and one pinned CronJob passed on the
Deployment's annotations and the CronJob was never examined. Namespaces are a
render property too: `kustomization.yaml` can set one, and a directory name is
not a namespace.

So the guard renders each cluster with its own `kustomize build` — an identical
render to the one `check-script-lint` produces, not a shared one — and judges
**one container at a time**.

Three decisions in it are load-bearing.

**keel annotations are a workload property, not a container property.** keel
reads the workload's annotations and applies them to the images it can track,
which are the floating ones. A Deployment whose app image floats and whose
quiesce sidecar is `alpine:3.20` is correct and intended: keel bumps the app,
Renovate bumps the sidecar. Smearing the workload's annotations across every
container would read four such sidecars on the VPS cluster as frozen. The frozen
verdict therefore needs the whole workload — keel annotations present *and*
nothing floating anywhere in it, so the annotations can only be about a pin.

**A bare major version stream is floating, not a pin.** `louislam/uptime-kuma:2`
moves on every 2.x release and `v2` is the same thing spelled differently, as is
a `-latest` suffix such as `ghcr.io/umami-software/umami:postgresql-latest`. A
*dotted* tag — `alpine:3.20`, `traefik:v3.3`, `postgres:16-alpine`,
`pgvector/pgvector:0.8.1-pg17` — is a pin that Renovate bumps, and calling any
of those floating would hand a reviewed bump to keel.

**Remote-base images are advisory, in every mode.** An image named by no file in
the cluster's own tree came from a remote base — cert-manager, the CSI drivers,
local-path-provisioner — so nothing here can edit the reference; it moves only
when the base's own ref moves. That is not the same as unreachable: the VPS
local-path base is pinned as `?ref=v0.0.31`, which the kustomize manager parses,
so Renovate proposes that bump even though the guard still calls the image
advisory. Failing an apply on
somebody else's manifest produces a gate people route around, so those are
printed as advisories and do not fail the check, exactly as `check-script-lint`
treats upstream findings. Ownership is therefore established *before* any
verdict, not only before the scope one: a remote base that ever shipped keel
annotations on a pinned tag would otherwise hard-fail an apply over a manifest
this repo cannot edit.

**The ownership lookup is confined to the cluster being analysed**, and that
confinement is load-bearing. Both trees name `restic/restic:0.17.3` and the same
keel digest, so a repo-wide lookup lets a watched homelab file vouch for a VPS
container nothing watches. Simulated with scope widened to `homelab/**` alone, a
repo-wide lookup dropped the VPS render from nine findings to six — `restic-backup`,
`restic-init` and `keel` all fell silent while `vps/backup/*.yaml` and
`vps/bootstrap/keel/keel.yaml` were still genuinely unwatched. The lookup also
compares extracted image values rather than searching raw file text, because a
substring search matches prose (`restic/restic:0.17.3` appears in three comment
sentences in `homelab/backup/restic-cronjob.yaml`) and has no right boundary
(`alpine:3.2` would be "owned" by any file naming `alpine:3.20`).

Scope is still a file question, because `managerFilePatterns` matches paths: for
each pinned, keel-free image the guard locates the repo file(s) naming it and
requires one of them to be matched by a `kubernetes.managerFilePatterns` entry
and not excluded by `ignorePaths`. Both manager blocks are validated for
patterns that match nothing, `kubernetes` and `kustomize`, because a typo in
either is the same silent-scope failure. `enabledManagers` is validated too: it
is a whitelist, so a `kustomize` block added without adding `kustomize` to that
list is inert configuration that reads like coverage, and dropping `kubernetes`
from it makes every scope verdict vacuous. Both are exit 2. Exit 1 means a
finding; exit 2 means the check could not run.

Floating tags are forbidden in `health`, `hindsight`, `ops` and `backup`.
`jottacloud-backup` is the single written exemption on the guard's
`FLOATING_EXEMPT` list: it is a CronJob whose pods pull `:latest` on every
scheduled run, so the schedule already delivers what keel would, which is why it
carries no keel annotations and needs none.

The targets are `check-renovate-scope-homelab` and `check-renovate-scope-vps`,
plus the bare `check-renovate-scope` which sweeps both. **Both per-cluster
targets run in their cluster's `diff-*` and `apply-*` preflight**, on the public
half, as of the 2026-08-26 commit that widened Renovate to `homelab/**` and
`vps/**`. Each chain now reads the same way: a context assertion, a
vars-consistency check, and **five per-cluster guards** — `check-script-substitution`,
`check-job-ttl`, `check-ping-bodies`, `check-script-lint` and `check-renovate-scope`,
each running as its own cluster's half.

Three of those five are **render-based**: `check-job-ttl`, `check-script-lint` and
`check-renovate-scope` each shell out to a full `kustomize build`.
`check-script-substitution` and `check-ping-bodies` do not — they scan source files under
the cluster trees. That subset decides wiring, not just vocabulary: the render-based
three are on the public half of the split only, because duplicating a full build onto the
inner half would double every apply's render cost. See the `GUARD PLACEMENT` block in the
`Makefile` before moving any of them.

Arming it needed that widening first, and the order is worth keeping in mind if
the scope ever narrows again. The guard cannot pass against a `renovate.json`
that watches only `homelab/health`, `homelab/ops` and `homelab/hindsight`: every
pinned, keel-free container outside those three genuinely receives nothing,
which is the estate's true state rather than a bug in the guard. Wiring a guard
into a preflight it does not pass makes an apply impossible and teaches the next
person to route around the gate. Widen scope, prove a clean run against both
renders, then arm — never the reverse.

## Talos machine config patches

Each file under `homelab/talos/machineconfig-patches/` is a full Omni `ConfigPatches`
resource (flat `metadata.type` schema, **not** a Kubernetes `apiVersion`/`kind`
wrapper — copy an existing patch rather than writing one from scratch). `make
apply-talos` iterates them, runs envsubst, and calls `omnictl apply` per file. It skips
`*.tpl` files, which are templates for the one-shot Tailscale bootstrap rather than
steady-state patches.

Why the temp-file dance in the recipe: `omnictl apply -f -` does **not** accept stdin —
the `-` is read as a literal filename and fails `stat -: no such file`. Native stdin
support was rejected upstream (siderolabs/omni#1193, closed "not planned", Dec 2025). So
each patch is substituted into a `mktemp` file that is `shred`ed on every exit path via
`trap`, inside a subshell with `set -euo pipefail`.

`apply-talos` is **homelab-only** — its glob is hardcoded to
`homelab/talos/machineconfig-patches/`. `vps/talos/machineconfig-patches/` has no
Makefile target; those patches are applied by hand with `omnictl apply -f <file>`.

### Tailscale bootstrap

Node registration on the tailnet needs a one-shot auth key, which must never live in
steady-state config. The flow:

```bash
export TAILSCALE_AUTH_KEY=tskey-auth-...        # mint fresh in the Tailscale admin
make bootstrap-tailscale                        # applies a machine-scoped ConfigPatch
tailscale status                                # confirm the node joined (~30s)
make clear-tailscale-bootstrap TALOS_MACHINE_ID=<id>
unset TAILSCALE_AUTH_KEY
```

`TALOS_MACHINE_ID` auto-detects the single homelab machine; set it explicitly for
multi-node rollouts. **Always clear the patch afterwards** — leaving it behind is a
disaster-recovery tripwire: on a state-volume wipe the node would try to re-auth with an
already-consumed key. Do not cache consumed keys in 1Password.

## `configured` is not drift

`make apply-homelab` permanently reports `configured` (not `unchanged`) for a known,
benign set of resources. **Don't chase it.**

- Every Secret shows `configured` on every apply, forever: `stringData` is write-only
  server-side (the API server never stores it back for comparison), so client-side
  apply's `last-applied-configuration` can never converge.
- Same for objects whose fields the API server silently normalises away — e.g. a PV
  declaring `storageClassName: ""`, which the server drops from `spec` entirely.
- Same for cert-manager's webhook configs, whose `caBundle` is injected post-apply by
  cainjector.

Verified 2026-07-27: `kubectl diff` over these resources is empty and
`kubectl apply --dry-run=server` is byte-identical to live state — the `caBundle`
survives intact, so this is **not** the classic cert-manager-caBundle-gets-stripped
footgun. `kubectl diff` showing nothing is the ground truth; `configured` in apply
output is not evidence of drift.
