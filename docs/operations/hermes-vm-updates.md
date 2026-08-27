# Updating the Hermes VM

The whole update procedure for the Hermes application stack on VM 103 (`hermes.cynexia.net`), run by an agent or the operator, roughly weekly, with someone watching.
Nothing schedules it: `hermes update` sometimes carries a step that needs judgement — a migration prompt, a stash of local edits — and a script cannot exercise judgement.
Everything else about this VM is in [hermes-vm.md](hermes-vm.md).
Fetch vendor documentation fresh each session: <https://hermes-agent.nousresearch.com/docs/> (its `llms.txt` index, not a search), the `NousResearch/hermes-agent` releases, <https://github.com/nesquena/hermes-webui>, <https://hindsight.vectorize.io>.

**[VM]** blocks run in one `ssh hermes@hermes.cynexia.net` shell held open throughout; **[laptop]** blocks run on the operator's machine.
They are written for an interactive shell rather than wrapped in `ssh '…'` because the chat turn's JSON payload carries single quotes, double quotes and braces that no such wrapper survives; what carries state between blocks is the record file, not the shell.
**Always call `/home/hermes/.local/bin/hermes`**: `~/.local/bin` is on neither the non-interactive ssh PATH nor the transient unit's, so bare `hermes` fails.

The five user units, called *the five units* below, are `hermes-gateway`, `hermes-gateway-emh`, `hermes-gateway-hal`, `hermes-dashboard` and `hermes-webui`.
**`hermes update` restarts only the three gateways** — never the WebUI or the dashboard, neither of which runs the `hermes` entry point — so restarting the five is your job.
And **a health check in a fresh interpreter does not prove the running WebUI loaded the repaired code**: it serves the module in memory until restarted.

## Preconditions

**[VM]** Run all of these before anything mutates; any failure stops the session.
A missing stamp file, or any output from the `find`, means `unattended-upgrades` has not run in over 14 days: fix the apt timers before updating anything ([hermes-vm.md](hermes-vm.md#unattended-upgrades)).

```sh
# A dirty agent tree is stashed and switched off its parked branch and never restored;
# a dirty webui tree aborts the procedure AFTER the agent has already migrated.
git -C ~/.hermes/hermes-agent status --porcelain   # empty, or commit/discard first
git -C ~/hermes-webui status --porcelain           # empty, or commit/discard first
# The estate's ONLY alarm on a dead apt timer, and it fires only when you run it.
# PASS is: the file EXISTS and find prints nothing. A missing stamp would leave find's
# stdout empty too, so the test -f is what stops that reading as a pass.
test -f /var/lib/apt/periodic/unattended-upgrades-stamp &&
  find /var/lib/apt/periodic/unattended-upgrades-stamp -mtime +14
df -h /home     # 1 GiB free; the snapshot alone is ~200 MiB
date -u         # not within 90 minutes of 04:45 UTC: the reboot ignores who is logged in
```

**[VM]** Persist the rollback record, which must outlive a session the reboot can kill.
Every value in it is an ordinary identifier, so the default umask's 0644 is right and nothing needs tightening:

```sh
printf 'agent_sha=%s\nagent_branch=%s\nwebui_sha=%s\nclient_version=%s\n' \
  "$(git -C ~/.hermes/hermes-agent rev-parse HEAD)" \
  "$(git -C ~/.hermes/hermes-agent rev-parse --abbrev-ref HEAD)" \
  "$(git -C ~/hermes-webui rev-parse HEAD)" \
  "$(~/.hermes/hermes-agent/venv/bin/pip show hindsight-client | sed -n 's/^Version: //p')" \
  > ~/.hermes/hermes-update.pre-run    # the venv's own pip, never a system one
cat ~/.hermes/hermes-update.pre-run
```

**If `agent_branch` reads `HEAD`, stop.** The checkout is detached, and [Rollback](#rollback) would then create a branch literally named `HEAD`.
Put it on a real branch and re-take the record before going on.

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
systemd-run --user --unit=hermes-update-manual \
  --setenv=HERMES_HOME=/home/hermes/.hermes \
  -- /home/hermes/.local/bin/hermes update --backup
while systemctl --user is-active --quiet hermes-update-manual; do sleep 10; done
systemctl --user show hermes-update-manual -p ActiveState -p Result -p ExecMainStatus
journalctl --user -u hermes-update-manual --no-pager | tail -40
systemctl --user reset-failed hermes-update-manual 2>/dev/null; true
```

**The run passed only if all three read `ActiveState=inactive`, `Result=success` and `ExecMainStatus=0`.** Anything else goes to [Rollback](#rollback) — read the journal first.
`ActiveState` is in that list because the wait loop is what makes the other two mean anything: read on a unit that is still running, `Result` reports the `success` it was initialised with.
Do not take the next step's snapshot check as the completion signal: the snapshot is written at the START of the run, so it exists whatever happened afterwards.
A transient **service** survives; the user manager forks it, so no `&` is wanted.
A `--scope` backgrounded with `&` does **not** — that client takes the session's `SIGHUP`.
`--collect` is deliberately absent: it garbage-collects the unit on exit and takes `Result` with it, which is the only completion signal there is, so `reset-failed` clears the unit by hand instead.
The wait loop blocks; to watch the run as it goes, open a second ssh shell and `journalctl --user -u hermes-update-manual -f`.
`--setenv=HERMES_HOME` replaces what the deleted unit pinned: `hermes` resolves `uv` at `$HERMES_HOME/bin/uv` by absolute path, so an absent `HERMES_HOME`, not a short PATH, is what would stop the update finding its own tooling.
**Unverified:** that `uv` resolves inside the transient unit — the first supervised run confirms it.
**Unverified:** whether this update prompts for the config migration.
A prompt has nowhere to go in a detached unit, so if the journal stalls on one, stop the unit and re-run in the foreground — which accepts the `SIGHUP` exposure this section opens by forbidding, because a prompt needs a tty; do not start that re-run anywhere near 04:45.
`tmux` is absent: if `systemd-run --user` itself fails, investigate the user manager ([hermes-vm.md](hermes-vm.md#lingering-is-a-precondition)).

**[VM]** Verify the snapshot, move the WebUI passenger, and restart the five units:

```sh
# Upstream's backup path warns and continues on failure, so "requested" is not "taken":
# STOP unless this names a file. The record predates the run; ~200 MiB is the normal size.
find ~/.hermes/backups -name 'pre-update-*.zip' -size +50M \
  -newer ~/.hermes/hermes-update.pre-run
V=~/.hermes/hermes-agent/venv/bin
# CHAINED ON PURPOSE: every && below is a stop, not a note. Broken apart, a failed step
# would let the next one run - which is how an unconstrained install happens.
git -C ~/hermes-webui fetch origin &&
git -C ~/hermes-webui checkout -f -B master origin/master &&   # tracks master; -f is intent
printf 'webui_target_sha=%s\n' "$(git -C ~/hermes-webui rev-parse --short HEAD)" \
  >> ~/.hermes/hermes-update.pre-run &&
$V/pip freeze --local | grep -E '^[A-Za-z0-9_.-]+==' > /tmp/constraints.txt &&
# UNCONSTRAINED, or against an empty file, this moves the agent's own dependencies: the
# result passes every health check and fails every chat.
test -s /tmp/constraints.txt &&
$V/pip install -r ~/hermes-webui/requirements.txt -c /tmp/constraints.txt &&
rm /tmp/constraints.txt &&
systemctl --user restart hermes-gateway hermes-gateway-emh hermes-gateway-hal \
  hermes-dashboard hermes-webui
```

## Verify

**[VM]** In order; any failure sends you to [Rollback](#rollback).

```sh
systemctl --user is-active hermes-gateway hermes-gateway-emh hermes-gateway-hal \
  hermes-dashboard hermes-webui
journalctl --user -u hermes-gateway --since '10 min ago' | grep '1Password: applied'
/home/hermes/.local/bin/hermes secrets onepassword status
cd /home/hermes && ~/.hermes/hermes-agent/venv/bin/python -c 'import run_agent'  # not in the checkout
curl -sS -m 10 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/health
# One real chat turn: key via -K stdin, never argv; pass = non-empty message content.
KEY=$(sed -n 's/^API_SERVER_KEY=//p' ~/.hermes/.env | head -1)   # never echo $KEY
case "$KEY" in ''|*[!A-Za-z0-9_-]*) echo 'no usable literal key - STOP'; false ;; esac &&
printf 'header = "Authorization: Bearer %s"\n' "$KEY" | curl -sS -m 60 -K - \
  -H 'Content-Type: application/json' \
  -d '{"model":"default","messages":[{"role":"user","content":"Reply with one sentence."}]}' \
  http://127.0.0.1:8642/v1/chat/completions
journalctl --user -u hermes-gateway --since '10 min ago' | grep -i hindsight
```

`is-active` prints one `active` per unit and exits non-zero if any of the five is not, so its exit status alone is the pass condition.

**The secret count is the cheapest post-update check there is.** The journal line must match the count the provider declares — seven references and "applied 7 secrets" as of 2026-08-27 — because that path is fail-open and swallows errors, so an expired `OP_SERVICE_ACCOUNT_TOKEN` or an unreachable `op` shows up as a *smaller* number, not a failure.
`status` confirms the references survived the migration; whether it preserves the `secrets:` block is **unverified**, and this settles it.

The import catches the documented broken-venv failure: a venv missing `dotenv`, `httpx` or `openai` leaves every unit active and `/health` answering `status: ok` while the iOS app answers `AIAgent not available`.
Run it from outside the checkout, or the import resolves from source and passes on a venv the agent is not installed in.
The chat key reaches `curl` through a configuration file on stdin, so it never appears on argv, and the character gate is deliberate: an unresolved `op://…` reference fails it on its `:` and `/`, degrading visibly instead of sending a meaningless bearer token.
The test is that `choices[0].message.content` came back non-empty; model wording is not a contract.

**The Hindsight grep is not optional.** A chat turn returns 200 whether or not the memory write behind it succeeded, because that write is on a background path the response does not wait for, so without it a green run passes over a VM that has retained no memory in months.
Its pass condition is stated plainly because it is not yet demonstrable: **no successful write has ever been observed on this VM**, so nobody knows what the success line looks like.
Every occurrence in the journal is `401 Invalid API key`, whose cause is settled in [First session](#first-session) and whose fix is a gateway restart.
After that restart, the first success defines the line — record it here when you see it.

## Report

**[laptop]** On success only.
Read the values from the pre-run record and type them in.
Both are post-update by construction — `target_sha` was recorded from `origin/main` and `webui_target_sha` after the checkout, so the body describes what is now installed rather than a mix of vintages:

```sh
curl -fsS -m 15 --data-binary 'summary=hermes update ok agent=<target_sha> webui=<webui_target_sha>' \
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
# CHAINED ON PURPOSE, as in Update: a rollback that runs on past a failed step is worse
# than one that stops - it pins the broken state and reports success.
git -C ~/.hermes/hermes-agent checkout -B <agent_branch> <agent_sha> &&
$V/pip install -e '/home/hermes/.hermes/hermes-agent[all]' &&
# The freeze comes AFTER the agent is restored: one taken from the broken state pins it.
$V/pip freeze --local | grep -E '^[A-Za-z0-9_.-]+==' > /tmp/constraints.txt &&
test -s /tmp/constraints.txt &&
git -C ~/hermes-webui checkout -f -B master <webui_sha> &&
$V/pip install -r ~/hermes-webui/requirements.txt -c /tmp/constraints.txt &&
$V/pip install 'hindsight-client==<client_version>' &&
rm /tmp/constraints.txt &&
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

**The Hindsight 401 has a settled cause: a gateway older than its own configuration.** Every memory write in two months of journal failed with `401 Invalid API key`, and the running gateway had started on 2026-08-23 at 14:22 applying **six** secrets; `config.yaml` gained the `HINDSIGHT_API_KEY` reference on 2026-08-24 at 14:43, and nothing restarted the gateway afterwards, so the plugin initialised with an empty key and kept it.
**The fix is a gateway restart**, and it is the operator's to run.
The key was never wrong: the two vault copies and the live cluster secret were compared on 2026-08-27 and all three held the identical value.
Note that [hindsight.md](hindsight.md#rotating-the-tenant-api-key) names `op://Homelab/hermes/tenant-api-key` for the VM and **no such item exists** — a typo in that page, not a third copy.

The key is in no file on the VM: hermes's 1Password provider resolves `secrets.onepassword.env.HINDSIGHT_API_KEY: op://hermes/hindsight/tenant-api-key` at startup, from all three homes (`~/.hermes/config.yaml` and the `emh` and `hal` profiles' own `config.yaml`), into the gateway process's environment alone.
That is why the applied-count gate in [Verify](#verify) earns its place: a count one short of the declared references is exactly the state this VM lived in for four days, and it is the only cheap signal of it.

**[laptop]** The digest comparison stays, as the drift check the two-copy design needs — it is what proved the two vaults in sync. **Never compare raw values and never print one:**

```sh
op read 'op://hermes/hindsight/tenant-api-key' | tr -d '\n' | shasum -a 256 | cut -c1-12
kubectl --context cynexia-homelab -n hindsight get secret hindsight \
  -o jsonpath='{.data.tenant-api-key}' | base64 -d | tr -d '\n' | shasum -a 256 | cut -c1-12
```

**The `tr -d '\n'` on both sides is load-bearing.** `op read` ends its output with a newline and the cluster secret, a double-quoted YAML scalar, does not — so without it two identical keys always disagree and the comparison manufactures a mismatch it then tells you to act on.
The same applies to a VM-side reading, `op read … | tr -d '\n' | sha256sum | cut -c1-12`, for when the laptop's credential cannot see the `hermes` vault.
Matching digests mean the two copies are in sync; differing digests mean a rotation landed in one place only, and the profiles present a key the server will not accept.
**Fixing either fault is the operator's, not this runbook's:** diagnose, report the verdict, and stop.
