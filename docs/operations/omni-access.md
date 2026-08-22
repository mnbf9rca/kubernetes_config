# Bootstrapping a new workstation

Getting `omnictl`, `kubectl` and `talosctl` working on a machine that has **never**
connected to the Omni instance. Everything below was verified end-to-end on a clean
machine on 2026-08-19.

This repo is public: the Omni service URL and the sign-in identity are **not** written
here as literal values. They live in 1Password and every command below reads them at
run time with `op read`, the same convention the rest of the repo uses for secrets.

| What | 1Password reference |
|---|---|
| Omni service URL | `op://Homelab/omni/service_url` |
| Omni sign-in identity (email) | `op://Homelab/omni/email` |

Vault names in `op://` references are **case-insensitive** — `op://Homelab/…` and
`op://homelab/…` both resolve, which is why `.envrc` and this file historically differed.
Docs use `Homelab` consistently; don't "fix" a lowercase reference you find elsewhere on
the assumption that it's broken.

> The Omni identity is **not** the Google identity used for Grafana in the
> `health` namespace. They are separate accounts with separate sign-in flows. Signing
> in with the Google account when Omni asks for its identity will not produce a working
> config.

## The failure mode this runbook exists for

With no omniconfig on disk, `omnictl` does **not** error out saying "no configuration".
It silently falls back to its built-in default context, whose URL is
`grpc://127.0.0.1:8080`, and every command then fails with a connection error:

```
rpc error: code = Unavailable desc = connection error: ... dial tcp 127.0.0.1:8080: connect: connection refused
```

**`connection refused` on 127.0.0.1:8080 means "there is no omniconfig", not "Omni is
down".** Check `omnictl config contexts` before you go looking at the Omni instance.
The same symptom appears if a stale `default` context is the selected one — `omnictl
config add` does not switch contexts for you, which is why step 2 below is a separate
command.

## Prerequisites

Install the toolchain, then assert it:

```bash
brew install siderolabs/tap/omnictl talosctl kubectl kustomize jq direnv gettext 1password-cli
make check-tools     # kubectl kustomize envsubst op direnv talosctl omnictl jq
```

`envsubst` ships in the `gettext` keg and may need `brew link --force gettext` on a
fresh Mac. Sign in to the 1Password CLI (`op signin`) before running anything that
calls `op read`.

## Bootstrap sequence

```bash
# 1. Create the omniconfig context (writes ~/.talos/omni/config)
omnictl config add cynexia \
  --url "$(op read 'op://Homelab/omni/service_url')" \
  --identity "$(op read 'op://Homelab/omni/email')"

# 2. Select it — `add` does not make the new context current
omnictl config context cynexia

# 3. First authenticated call: opens a browser for the Omni sign-in and mints
#    the SideroV1 PGP key at ~/.talos/keys/cynexia-<identity>.pgp
omnictl get clusters

# 4. Kubernetes access: merges the OIDC-authenticated context into ~/.kube/config
omnictl kubeconfig --cluster homelab --merge
omnictl kubeconfig --cluster vps --merge      # optional, for the VPS cluster
```

Step 4 creates the kubectl contexts `cynexia-homelab` and `cynexia-vps` — the exact
names the Makefile's `check-context` / `check-vps-context` preflights assert before any
cluster write.

### talosctl equivalent

`omnictl kubeconfig` has a direct counterpart for the Talos API. talosctl always goes
through the Omni proxy; there is no direct-to-node path in normal operation:

```bash
omnictl talosconfig --cluster homelab --merge     # merges into ~/.talos/config
talosctl config context cynexia-homelab
talosctl -n "$(kubectl --context cynexia-homelab get nodes -o jsonpath='{.items[0].metadata.name}')" version
```

Nodes are addressed by **node name**, never by IP: `talosctl -n 10.100.0.100 ...` fails
with `node not found, cannot resolve its management address`, because the node's
management address is resolved by Omni from the machine's name.

Omitting `--cluster` downloads the generic Omni-wide talosconfig, which
also works against machines in maintenance mode (useful when a node has no cluster yet).

## Where the state lands

| Path | Written by | Contents |
|---|---|---|
| `~/.talos/omni/config` | `omnictl config add` | omniconfig: context name, service URL, SideroV1 identity |
| `~/.talos/keys/<context>-<identity>.pgp` | first authenticated `omnictl`/`talosctl` call | SideroV1 auth key, one per context+identity, minted by the browser sign-in |
| `~/.kube/config` | `omnictl kubeconfig --merge` | `cynexia-homelab` / `cynexia-vps` contexts (Omni OIDC auth) |
| `~/.talos/config` | `omnictl talosconfig --merge` | talosconfig pointing at the Omni proxy |

Both default paths are overridable by env var, which is the clean way to run against a
second Omni instance without clobbering the primary config:

- `OMNICONFIG` overrides `~/.talos/omni/config` (also `--omniconfig`)
- `SIDEROV1_KEYS_DIR` overrides `~/.talos/keys` (also `--siderov1-keys-dir`)

`$XDG_CONFIG_HOME/omni/config` is **deprecated** — omnictl only reads it as a last
resort for an existing file and never writes there. If a machine appears to have a
working config that this runbook can't find, check that path before concluding the
config is missing.

## Verify

```bash
omnictl config contexts                  # `cynexia` marked CURRENT, URL is not 127.0.0.1
omnictl get clusters                     # expect: homelab, vps
kubectl config get-contexts              # expect: cynexia-homelab, cynexia-vps
kubectl --context cynexia-homelab get nodes
make check-context                       # asserts current-context == cynexia-homelab
```

## Secrets for the apply workflow

Cluster access alone is not enough to run `make apply-homelab` / `make apply-vps` —
those need 1Password-backed values. See [apply-workflow.md](apply-workflow.md); the short
version is `direnv allow` in the repo root, which exports **only**
`OP_SERVICE_ACCOUNT_TOKEN`, then `make require-vars`, which re-enters under `op run` and
confirms every required variable resolves. Secret values are never exported into your
shell — the token is the one thing direnv provides.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `dial tcp 127.0.0.1:8080: connection refused` | No omniconfig, or the `default` context is selected | `omnictl config contexts`; run the bootstrap sequence, or `omnictl config context cynexia` |
| `Could not authenticate: open ~/.talos/keys/<ctx>-<user>.pgp` | Stale/absent SideroV1 key for that context+identity | Remove the stale talosctl context (`talosctl config remove <ctx> -y`, switching current context first if needed), refetch with `omnictl talosconfig --cluster homelab`, run any talosctl command and complete the browser sign-in. kubectl auth (Omni OIDC) is separate and unaffected. |
| Browser sign-in loops or authenticates the wrong account | Signed in with the Google/Grafana identity instead of the Omni identity | Sign out of Omni in the browser, redo the flow with `op://Homelab/omni/email` |
| `omnictl config add` succeeded but commands still hit 127.0.0.1 | `add` does not switch contexts | `omnictl config context cynexia` |
| `node not found, cannot resolve its management address` | talosctl addressed a node by IP | Address by node name (`kubectl get nodes -o name`) |
| kubectl works, talosctl doesn't (or vice versa) | The two auth paths are independent — OIDC for kubectl, SideroV1 PGP for talosctl | Fix only the broken one; they share nothing but the omniconfig |

Timestamp trap: a workstation in a non-UTC timezone renders `kubectl` AGE columns and
`describe` timestamps in local time — compare against `date -u`, not the wall clock,
before concluding something is stale or clock-skewed.
