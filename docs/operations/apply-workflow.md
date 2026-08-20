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
(`check-context`, `check-vars-consistency`) in the parent shell — before anything can
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

That third row is the reason to treat the allowlist edit as the one to double-check. After
adding a var, `make build-<cluster> | grep -F '${'` is a cheap confirmation that no
placeholder survived the render.

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
| `check-tools` | Asserts `kubectl kustomize envsubst op direnv talosctl omnictl jq` are on PATH |
| `check-context` | Asserts `kubectl current-context == cynexia-homelab` (override with `HOMELAB_CONTEXT=`) |
| `check-vars-consistency` | Asserts `ENVSUBST_VAR_NAMES` ⊆ `REQUIRED_VARS`. Runs in the parent shell, before the `op run` child exists. Cannot detect a var *missing* from `ENVSUBST_VAR_NAMES` |
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
