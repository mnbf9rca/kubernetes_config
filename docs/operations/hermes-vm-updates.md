# Updating the Hermes VM

The whole update procedure for the Hermes application stack on VM 103 (`hermes.cynexia.net`), run by an agent or the operator, roughly weekly, with someone watching.
Nothing schedules it: `hermes update` sometimes carries a step that needs judgement — a migration prompt, a stash of local edits — and a script cannot exercise judgement.
Everything else about this VM is in [hermes-vm.md](hermes-vm.md).
Fetch vendor documentation fresh each session: <https://hermes-agent.nousresearch.com/docs/> (its `llms.txt` index, not a search), the `NousResearch/hermes-agent` releases, <https://github.com/nesquena/hermes-webui>, <https://hindsight.vectorize.io>.

**[VM]** blocks run in one `ssh hermes@hermes.cynexia.net` shell held open throughout, since later blocks read variables earlier ones set; **[laptop]** blocks run on the operator's machine.
**Always call `/home/hermes/.local/bin/hermes`**: `~/.local/bin` is on neither the non-interactive ssh PATH nor the transient unit's, so bare `hermes` fails.

The five user units, called *the five units* below, are `hermes-gateway`, `hermes-gateway-emh`, `hermes-gateway-hal`, `hermes-dashboard` and `hermes-webui`.
**`hermes update` restarts only the three gateways** — never the WebUI or the dashboard, neither of which runs the `hermes` entry point — so restarting the five is your job.
And **a health check in a fresh interpreter does not prove the running WebUI loaded the repaired code**: it serves the module in memory until restarted.

## Preconditions

**[VM]** Run all of these before anything mutates; any failure stops the session.
Output from the `find` means `unattended-upgrades` has not run in over 14 days: fix the apt timers before updating anything ([hermes-vm.md](hermes-vm.md#unattended-upgrades)).

```sh
# A dirty agent tree is stashed and switched off its parked branch and never restored;
# a dirty webui tree aborts the procedure AFTER the agent has already migrated.
git -C ~/.hermes/hermes-agent status --porcelain   # empty, or commit/discard first
git -C ~/hermes-webui status --porcelain           # empty, or commit/discard first
# The estate's ONLY alarm on a dead apt timer, and it fires only when you run it.
find /var/lib/apt/periodic/unattended-upgrades-stamp -mtime +14   # empty, or stop
df -h /home     # 1 GiB free; the snapshot alone is ~200 MiB
date -u         # not within 90 minutes of 04:45 UTC: the reboot ignores who is logged in
```

**[VM]** Persist the rollback record, which must outlive a session the reboot can kill:

```sh
printf 'agent_sha=%s\nagent_branch=%s\nwebui_sha=%s\nclient_version=%s\n' \
  "$(git -C ~/.hermes/hermes-agent rev-parse HEAD)" \
  "$(git -C ~/.hermes/hermes-agent rev-parse --abbrev-ref HEAD)" \
  "$(git -C ~/hermes-webui rev-parse HEAD)" \
  "$(~/.hermes/hermes-agent/venv/bin/pip show hindsight-client | sed -n 's/^Version: //p')" \
  > ~/.hermes/hermes-update.pre-run    # the venv's own pip, never a system one
cat ~/.hermes/hermes-update.pre-run
```

## Change analysis

**[VM]** Budget five minutes; it says what is coming and how much, not whether it is safe.

```sh
# --check EXITS 0 EITHER WAY: read its output, not its status.
/home/hermes/.local/bin/hermes update --check  # fetches; "N commits behind origin/main"
/home/hermes/.local/bin/hermes update --plan   # restart topology; the sha is what RUNS NOW
# So record the target yourself, after --check has fetched.
printf 'target_sha=%s\n' "$(git -C ~/.hermes/hermes-agent rev-parse --short origin/main)" \
  >> ~/.hermes/hermes-update.pre-run
git -C ~/.hermes/hermes-agent diff HEAD origin/main -- pyproject.toml  # pins, floors
grep -n '_config_version' ~/.hermes/hermes-agent/hermes_cli/config_defaults.py
git -C ~/.hermes/hermes-agent show origin/main:hermes_cli/config_defaults.py \
  | grep -n '_config_version'     # a jump means a migration will run
```

**Use the sha, never the version:** on 2026-08-27, 1,151 commits of change carried the identical `0.20.5` on both sides, and the checkout holds no tags at all.
The `pyproject.toml` diff is the highest-signal read — dependency pins, the Python floor, the `hindsight-client` pin; read [hindsight.md](hindsight.md#the-client-on-the-hermes-vm) before reacting to a change in that pin.

**[laptop]** Read the curated release bodies for the incoming range, derived from the installed sha's date — the VM's clone supplies no tags.
**Do not grep the commit log for breaking-change markers:** a sampled week held 1,687 commits and zero such markers, so that check reports all-clear forever.

```sh
gh api repos/NousResearch/hermes-agent/releases \
  --jq '.[] | "== \(.tag_name) \(.published_at)\n\(.body)"'
```

**Pause signals.** Stop and read first when: the Python floor moves; the `hindsight-client` pin changes; `_config_version` jumps by more than one; a release names a configuration *floor* or touches the update mechanism; a dependency is under 14 days old.

## Update

**[VM]** Run it detached: a foreground run dies with the session on `SIGHUP`.

```sh
systemd-run --user --collect --unit=hermes-update-manual \
  -- /home/hermes/.local/bin/hermes update --backup
journalctl --user -u hermes-update-manual -f
```

A transient **service** survives; the user manager forks it, so no `&` is wanted.
A `--scope` backgrounded with `&` does **not** — that client takes the session's `SIGHUP`.
`--collect` removes the unit on exit, so read the journal, which persists.
`tmux` is absent: if this fails, investigate the user manager ([hermes-vm.md](hermes-vm.md#lingering-is-a-precondition)).
**Unverified:** whether this update prompts for the config migration.
A prompt has nowhere to go in a detached unit, so if the journal stalls on one, stop the unit and re-run in the foreground.

**[VM]** Verify the snapshot, move the WebUI passenger, and restart the five units:

```sh
# Upstream's backup path warns and continues on failure, so "requested" is not "taken":
# STOP unless this names a file. The record predates the run; ~200 MiB is the normal size.
find ~/.hermes/backups -name 'pre-update-*.zip' -size +50M \
  -newer ~/.hermes/hermes-update.pre-run
git -C ~/hermes-webui fetch origin
git -C ~/hermes-webui checkout -f -B master origin/master   # tracks master; -f is intent
V=~/.hermes/hermes-agent/venv/bin
$V/pip freeze --local | grep -E '^[A-Za-z0-9_.-]+==' > /tmp/constraints.txt
# UNCONSTRAINED, or against an empty file, this moves the agent's own dependencies: the
# result passes every health check and fails every chat.
test -s /tmp/constraints.txt       # hard stop
$V/pip install -r ~/hermes-webui/requirements.txt -c /tmp/constraints.txt
rm /tmp/constraints.txt
systemctl --user restart hermes-gateway hermes-gateway-emh hermes-gateway-hal \
  hermes-dashboard hermes-webui
```

## Verify

In order.
Any failure sends you to [Rollback](#rollback).
**[VM]**

```sh
systemctl --user is-active hermes-gateway hermes-gateway-emh hermes-gateway-hal \
  hermes-dashboard hermes-webui
journalctl --user -u hermes-gateway --since '10 min ago' | grep '1Password: applied'
/home/hermes/.local/bin/hermes secrets onepassword status
cd /home/hermes && ~/.hermes/hermes-agent/venv/bin/python -c 'import run_agent'  # not in the checkout
curl -sS -m 10 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/health
# One real chat turn: key via -K stdin, never argv; pass = non-empty message content.
KEY=$(sed -n 's/^API_SERVER_KEY=//p' ~/.hermes/.env | head -1)   # never echo $KEY
case "$KEY" in ''|*[!A-Za-z0-9_-]*) echo 'no usable literal key - stop' ;; esac
printf 'header = "Authorization: Bearer %s"\n' "$KEY" | curl -sS -m 60 -K - \
  -H 'Content-Type: application/json' \
  -d '{"model":"default","messages":[{"role":"user","content":"Reply with one sentence."}]}' \
  http://127.0.0.1:8642/v1/chat/completions
journalctl --user -u hermes-gateway --since '10 min ago' | grep -i hindsight
```

**The secret count is the cheapest post-update check there is.** The journal line must match the count the provider declares — seven references and "applied 7 secrets" as of 2026-08-27 — because that path is fail-open and swallows errors, so an expired `OP_SERVICE_ACCOUNT_TOKEN` or an unreachable `op` shows up as a *smaller* number, not a failure.
`status` confirms the references survived the migration; whether it preserves the `secrets:` block is **unverified**, and this settles it.

The import catches the documented broken-venv failure: a venv missing `dotenv`, `httpx` or `openai` leaves every unit active and `/health` answering `status: ok` while the iOS app answers `AIAgent not available`.
Run it from outside the checkout, or the import resolves from source and passes on a venv the agent is not installed in.
The chat key reaches `curl` through a configuration file on stdin, so it never appears on argv, and the character gate is deliberate: an unresolved `op://…` reference fails it on its `:` and `/`, degrading visibly instead of sending a meaningless bearer token.
The test is that `choices[0].message.content` came back non-empty; model wording is not a contract.

**The Hindsight grep is not optional.** A chat turn returns 200 whether or not the memory write behind it succeeded, because that write is on a background path the response does not wait for, so without it a green run passes over a VM that has retained no memory in months.
`401 Invalid API key` is the known fault: see [First session](#first-session).

## Report

**[laptop]** On success only.
Read the values from the pre-run record and type them in:

```sh
curl -fsS -m 15 --data-binary 'summary=hermes update ok agent=<target_sha> webui=<webui_sha>' \
  "https://hc-ping.com/$(op read 'op://Homelab/hermes-update/healthcheck-uuid')"
```

Three rules govern that body, and this paragraph is the only thing enforcing them — no guard in this repository reads a runbook.
**Never interpolate a command's output into the body:** a failing command quotes what it was handed, and that is the reporting credential; emit a count, an age, a size, a sha, or a verdict from a fixed set.
**Never echo the URL**, in a terminal or a transcript; its last path segment is the ping identifier.
**On failure, send nothing** — silence is the correct alarm; diagnose with the session open.

## Rollback

Rollback restores **code and pinned versions only**.
`hermes update` rewrites `config.yaml`, advances a schema version and migrates a 54 MB `state.db` in place, and those migrations are forward-only: `hermes_cli/config_migrations.py` defines no downgrade, rollback or revert function at all.
Hence `--backup` on every run, and hence the snapshot hard stop.

**[VM]** Restore the code in this order, then re-run [Verify](#verify):

```sh
cat ~/.hermes/hermes-update.pre-run    # agent_sha, agent_branch, webui_sha, client_version
V=~/.hermes/hermes-agent/venv/bin
# Branch AND revision: update switches off a parked branch, and a checkout left on the wrong
# one is a state the next update refuses to work with.
git -C ~/.hermes/hermes-agent checkout -B <agent_branch> <agent_sha>
$V/pip install -e '/home/hermes/.hermes/hermes-agent[all]'
# The freeze comes AFTER the agent is restored: one taken from the broken state pins it.
$V/pip freeze --local | grep -E '^[A-Za-z0-9_.-]+==' > /tmp/constraints.txt
test -s /tmp/constraints.txt
git -C ~/hermes-webui checkout -f -B master <webui_sha>
$V/pip install -r ~/hermes-webui/requirements.txt -c /tmp/constraints.txt
$V/pip install 'hindsight-client==<client_version>'
rm /tmp/constraints.txt
systemctl --user restart hermes-gateway hermes-gateway-emh hermes-gateway-hal \
  hermes-dashboard hermes-webui
```

Two fallbacks: `git -C ~/.hermes/hermes-agent reflog` when the record file is missing, and `git -C ~/.hermes/hermes-agent stash list` afterwards.
`hermes update` stashes local changes before it pulls and nothing pops them, so an entry there is work that was serving before the run and is not serving now — and **an empty list does not prove none was lost.**

**[VM]** To recover state, restore the snapshot.
**This discards everything that happened after it was taken** — sessions, memories and configuration alike.
`--force` is required: without it a prompt fires whenever the target already has a configuration file, which on a live VM is always.

```sh
ls -1t ~/.hermes/backups/pre-update-*.zip | head -n1
/home/hermes/.local/bin/hermes import --force ~/.hermes/backups/pre-update-<stamp>.zip
```

## First session

The first run validates this page's mechanics, not its weekly ergonomics.
As of 2026-08-27 the checkout is 1,151 commits behind `origin/main`, a `_config_version` 38 migration is pending and the semantic version does not move.
Take it supervised, and expect a 04:45 reboot the first night after install: a kernel reboot is already pending.

**The Hindsight 401 is a diagnostic, not a diagnosis.** Every memory write in two months of gateway journal has failed with `401 Invalid API key`, and the server returns a byte-identical 401 for a missing key and a wrong one, so establish which before changing anything.
The key is in no file on the VM: hermes's 1Password provider resolves `secrets.onepassword.env.HINDSIGHT_API_KEY: op://hermes/hindsight/tenant-api-key` at startup, from all three homes (`~/.hermes/config.yaml` and the `emh` and `hal` profiles' own `config.yaml`), into the gateway process's environment alone.
That is not the reference [hindsight.md](hindsight.md#rotating-the-tenant-api-key) records for the VM, so read the VM's own configuration first.

**[VM]** Confirm a key resolves at all, with the two 1Password commands from [Verify](#verify).
The provider declared 7 references and startup applied 7 on 2026-08-27, so this is confirmation, not a hunt; if nothing resolves, set a key ([hindsight.md](hindsight.md#rotating-the-tenant-api-key)).

**[laptop]** If one resolves, compare truncated digests — **never raw, never printed**:

```sh
op read 'op://hermes/hindsight/tenant-api-key' | shasum -a 256 | cut -c1-12
kubectl --context cynexia-homelab -n hindsight get secret hindsight \
  -o jsonpath='{.data.tenant-api-key}' | base64 -d | shasum -a 256 | cut -c1-12
```

Matching digests mean the fault is elsewhere; differing digests mean the profiles present a key the server does not accept.
If the laptop's credential cannot see the `hermes` vault, take the first digest on the VM with `sha256sum`.
**Fixing the key is the operator's, not this runbook's:** diagnose, report the verdict, and stop.
