# Updating the Hermes VM

The update procedure for the Hermes application stack on VM 103 (`hermes.cynexia.net`), run by an agent or the operator, roughly weekly, with someone watching.
Everything else about this VM is in [hermes-vm.md](hermes-vm.md).
Vendor documentation, fetched fresh each session: <https://hermes-agent.nousresearch.com/docs/> (its `llms.txt` index, not a search), the `NousResearch/hermes-agent` releases, <https://github.com/nesquena/hermes-webui>.

**[VM]** blocks run in one `ssh hermes@hermes.cynexia.net` shell held open throughout; **[laptop]** blocks run on the operator's machine.
`~/.local/bin` is not on the non-interactive ssh PATH, so **always call `/home/hermes/.local/bin/hermes`**.

The five user units, called *the five units* below, are `hermes-gateway`, `hermes-gateway-emh`, `hermes-gateway-hal`, `hermes-dashboard` and `hermes-webui`.
Two things that will not announce themselves: **`hermes update` restarts only the three gateways**, so the WebUI and the dashboard keep serving the code already in memory until you restart them; and **an import check in a fresh interpreter says nothing about the running WebUI**, which is why the restart comes before Verify.

## Preconditions

**[VM]**

```sh
# Both trees clean, or stop: a dirty agent tree is stashed and never restored, and a dirty
# webui tree aborts this procedure AFTER the agent has already migrated.
git -C ~/.hermes/hermes-agent status --porcelain
git -C ~/hermes-webui status --porcelain
# The estate's only alarm on a dead apt timer, and it fires only here. Two commands, not
# chained: chained, a missing stamp short-circuits and says nothing. Both must be quiet.
test -f /var/lib/apt/periodic/unattended-upgrades-stamp || echo 'STAMP MISSING - STOP'
find /var/lib/apt/periodic/unattended-upgrades-stamp -mtime +14 2>/dev/null
df -h /home     # 1 GiB free
date -u         # not within 90 minutes of 04:45 UTC: the reboot kills the session mid-run
```

**[VM]** Write the rollback record. It is the rollback target, and it has to outlive a session the reboot can kill:

```sh
printf 'agent_sha=%s\nagent_branch=%s\nwebui_sha=%s\nclient_version=%s\n' \
  "$(git -C ~/.hermes/hermes-agent rev-parse HEAD)" \
  "$(git -C ~/.hermes/hermes-agent rev-parse --abbrev-ref HEAD)" \
  "$(git -C ~/hermes-webui rev-parse HEAD)" \
  "$(~/.hermes/hermes-agent/venv/bin/pip show hindsight-client | sed -n 's/^Version: //p')" \
  > ~/.hermes/hermes-update.pre-run
cat ~/.hermes/hermes-update.pre-run   # agent_branch reading HEAD means a detached checkout: stop
```

## Change analysis

**[VM]**

```sh
# --check exits 0 either way, so read its output. Neither command names the incoming ref, and
# the semver does not move across it, so the sha recorded below is the only identity there is.
/home/hermes/.local/bin/hermes update --check
/home/hermes/.local/bin/hermes update --plan
printf 'target_sha=%s\n' "$(git -C ~/.hermes/hermes-agent rev-parse --short origin/main)" \
  >> ~/.hermes/hermes-update.pre-run
git -C ~/.hermes/hermes-agent diff HEAD origin/main -- pyproject.toml   # pins, Python floor
grep -n '_config_version' ~/.hermes/hermes-agent/hermes_cli/config_defaults.py
git -C ~/.hermes/hermes-agent show origin/main:hermes_cli/config_defaults.py \
  | grep -n '_config_version'
```

**[laptop]** Read the release bodies for the incoming range. Do not substitute a commit-log grep for breaking-change markers: a sampled week held 1,687 commits and zero of them, so that check reports all-clear forever.

```sh
gh api repos/NousResearch/hermes-agent/releases \
  --jq '.[] | "== \(.tag_name) \(.published_at)\n\(.body)"'
```

**Pause signals.** Stop and read first when: the Python floor moves; the `hindsight-client` pin changes ([hindsight.md](hindsight.md#the-client-on-the-hermes-vm)); `_config_version` jumps by more than one, or a release names a configuration *floor* or touches the update mechanism; a dependency is under 14 days old.

## Update

**[VM]** Detached, because a foreground run dies with the ssh session and mid-migration is the worst place for that:

```sh
systemd-run --user --unit=hermes-update-manual \
  --setenv=HERMES_HOME=/home/hermes/.hermes \
  -- /home/hermes/.local/bin/hermes update --backup
while systemctl --user is-active --quiet hermes-update-manual; do sleep 10; done
systemctl --user show hermes-update-manual -p ActiveState -p Result -p ExecMainStatus
journalctl --user -u hermes-update-manual --no-pager | tail -40
systemctl --user reset-failed hermes-update-manual 2>/dev/null; true
```

Passed if `ActiveState=inactive`, `Result=success` and `ExecMainStatus=0`; `--collect` is omitted deliberately, because it destroys a failed unit and `systemctl show` then answers with defaults that read exactly like that triple.

**[VM]** Confirm the snapshot exists before going further. It is the only route back from a forward-only migration, and upstream's backup path warns and continues on its own failures, so nothing else will tell you whether one was written:

```sh
ls -lt ~/.hermes/backups/pre-update-*.zip | head -1
```

**[VM]** The WebUI, then the five units:

```sh
V=~/.hermes/hermes-agent/venv/bin
# Chained: an unconstrained install moves the AGENT's dependencies under it, and the result
# passes every check in this runbook and fails every chat afterwards.
git -C ~/hermes-webui fetch origin &&
git -C ~/hermes-webui checkout -f -B master origin/master &&
printf 'webui_target_sha=%s\n' "$(git -C ~/hermes-webui rev-parse --short HEAD)" \
  >> ~/.hermes/hermes-update.pre-run &&
$V/pip freeze --local | grep -E '^[A-Za-z0-9_.-]+==' > /tmp/constraints.txt &&
test -s /tmp/constraints.txt &&
$V/pip install -r ~/hermes-webui/requirements.txt -c /tmp/constraints.txt &&
rm /tmp/constraints.txt &&
systemctl --user restart hermes-gateway hermes-gateway-emh hermes-gateway-hal \
  hermes-dashboard hermes-webui
```

## Verify

**[VM]** Any failure sends you to [Rollback](#rollback).

```sh
systemctl --user is-active hermes-gateway hermes-gateway-emh hermes-gateway-hal \
  hermes-dashboard hermes-webui
cd /home/hermes && ~/.hermes/hermes-agent/venv/bin/python -c 'import run_agent'
curl -sS -m 10 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/health
# One chat turn on the default profile. The key reaches curl through a config file on stdin,
# never argv; never echo it. Pass is non-empty choices[0].message.content.
KEY=$(sed -n 's/^API_SERVER_KEY=//p' ~/.hermes/.env | head -1)
case "$KEY" in ''|*[!A-Za-z0-9_-]*) echo 'no usable literal key - STOP'; false ;; esac &&
printf 'header = "Authorization: Bearer %s"\n' "$KEY" | curl -sS -m 60 -K - \
  -H 'Content-Type: application/json' \
  -d '{"model":"default","messages":[{"role":"user","content":"Reply with one sentence."}]}' \
  http://127.0.0.1:8642/v1/chat/completions
# One memory write must appear: the turn above returns 200 whether or not the write behind it
# succeeded, so this grep is the only thing that checks it.
journalctl --user -u hermes-gateway --since '10 min ago' | grep -i hindsight
journalctl --user -u hermes-gateway --since '10 min ago' | grep '1Password: applied'
```

The `applied N secrets` line is fail-open: after a restart, a drop from its previous value means the secrets provider failed silently.

## Report

**[laptop]** On success only; on failure send nothing and diagnose with the session still open.
Read the two shas from the record and type them into the body — never interpolate a command's output into it, and never echo the URL, whose last path segment is the ping identifier:

```sh
curl -fsS -m 15 --data-binary 'summary=hermes update ok agent=<target_sha> webui=<webui_target_sha>' \
  "https://hc-ping.com/$(op read 'op://Homelab/hermes-update/healthcheck-uuid')"
```

## Rollback

Code and pinned versions only: `hermes update`'s configuration and `state.db` migrations are forward-only, so the `--backup` snapshot is the only thing that restores state.

**[VM]** In this order, then re-run [Verify](#verify):

```sh
cat ~/.hermes/hermes-update.pre-run   # or `git -C ~/.hermes/hermes-agent reflog` if it is gone
V=~/.hermes/hermes-agent/venv/bin
# Chained. Branch AND revision, because update switches off a parked branch. The freeze comes
# after the agent is restored, or it pins the broken state.
git -C ~/.hermes/hermes-agent checkout -B <agent_branch> <agent_sha> &&
$V/pip install -e '/home/hermes/.hermes/hermes-agent[all]' &&
$V/pip freeze --local | grep -E '^[A-Za-z0-9_.-]+==' > /tmp/constraints.txt &&
test -s /tmp/constraints.txt &&
git -C ~/hermes-webui checkout -f -B master <webui_sha> &&
$V/pip install -r ~/hermes-webui/requirements.txt -c /tmp/constraints.txt &&
$V/pip install 'hindsight-client==<client_version>' &&
rm /tmp/constraints.txt &&
systemctl --user restart hermes-gateway hermes-gateway-emh hermes-gateway-hal \
  hermes-dashboard hermes-webui
```

Then read `git -C ~/.hermes/hermes-agent stash list`: `hermes update` stashes local changes and nothing pops them, so an entry there is work that was serving before the run.

**[VM]** To restore state, which discards everything that happened after the snapshot was taken:

```sh
ls -1t ~/.hermes/backups/pre-update-*.zip | head -n1
/home/hermes/.local/bin/hermes import --force ~/.hermes/backups/pre-update-<stamp>.zip
```
