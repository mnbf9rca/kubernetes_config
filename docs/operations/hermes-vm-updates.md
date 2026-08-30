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
# Both trees clean, or stop. On 2026-08-27 the installed updater refused a dirty parked
# branch outright (exit 1, no stash, snapshot already taken); a stash path may still exist
# for other shapes of dirt, and what it later does with that stash is not something to rely
# on. A dirty webui tree aborts this procedure AFTER the agent has already migrated.
git -C ~/.hermes/hermes-agent status --porcelain
git -C ~/hermes-webui status --porcelain
# A COMMITTED local patch leaves both of those empty, so this is the only precondition that
# sees one. Read it as yes/no, never as a count to reason from: the clone is shallow, and a
# stale origin/main can only inflate it. Non-zero means stop and follow the routing below.
git -C ~/.hermes/hermes-agent rev-list --count origin/main..HEAD
# The estate's only alarm on a dead apt timer, and it fires only here. Two commands, not
# chained: chained, a missing stamp short-circuits and says nothing. Both must be quiet.
test -f /var/lib/apt/periodic/unattended-upgrades-stamp || echo 'STAMP MISSING - STOP'
find /var/lib/apt/periodic/unattended-upgrades-stamp -mtime +14 2>/dev/null
# The weekly docker-sandbox refresh pushes to no monitor, so its failure mode is silence and
# this is its only alarm. Two commands, not chained: take the hermes-sandbox-refresh job id
# from the first, read its runs with the second. Stop if the last SCHEDULED run is over 14
# days old or failed - a hand-triggered run records source=direct and proves nothing about
# the schedule. Absent from `cron list` means the job was dropped: reinstall it, hermes-vm.md
# step 6.
/home/hermes/.local/bin/hermes cron list
/home/hermes/.local/bin/hermes cron runs <hermes-sandbox-refresh job_id>
df -h /home     # 1 GiB free
date -u         # not within 90 minutes of 04:45 UTC: the reboot kills the session mid-run
```

**[VM]** A non-zero count usually means a local patch the operator committed on the VM, and that patch is the one thing in this procedure genuinely at risk.
The count can also fire without one: a shallow graft or a stale `origin/main` inflates it and never deflates it, so the tripwire cannot miss a patch but can raise a false alarm.
Read `git log --format='%h %an %s' origin/main..HEAD` before choosing — the same inflatable range, read for whose commits are in it rather than for a number.
Nothing of the operator's in that list means the count is an artifact, and the update proceeds as written below.
Commits authored on the VM mean stop, and carry them across in files rather than trusting the updater with them:

1. **[laptop]** Find the boundary at the forge, which has the whole history: `gh api repos/NousResearch/hermes-agent/commits/<sha>` returns 404 for a commit only the VM has, so run it against the shas that log listed.
   The oldest commit it 404s on is the first local commit, and the upstream commit beneath that one is both the patch-set boundary and the reset target.
2. `git format-patch -o ~/.hermes/local-patches/ <first-local-commit>^..HEAD`, copy the files off the VM, and compare the checksums at both ends.
   The `-o` is load-bearing: bare `format-patch` writes to the current directory, not the one this prose names.
   The files are the insurance, and they survive any stash, merge, abort or reset.
3. **[VM]** `git reset --hard <reset-target>` — the upstream sha from step 1, not `HEAD~1`, which drops exactly one commit however many there are.
   Confirm `git -C ~/.hermes/hermes-agent rev-parse HEAD` reads that sha, so the update fast-forwards rather than merging, and reapplying the patches becomes an explicit step rather than something the updater does or does not do.
4. Take the update as written below.
5. `git apply --check` each patch, then `git am` over them in their numbered order.
6. Verify the patched behaviour in [Verify](#verify).

**[VM]** Write the rollback record.
It is the rollback target, and it has to outlive a session the reboot can kill:

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

**[laptop]** Read what is actually incoming, which is the forge's compare between the installed sha and `origin/main`.
`hermes update` installs `origin/main`, not the release the notes describe: on August 27, 2026 `origin/main` stood 253 commits past the `v2026.8.27` tag that publishes v0.20.6, so the notes stop 253 commits short of what lands.
Pinning to a release is not available — `--branch` takes a branch, upstream maintains no release branch, and PyPI's `hermes-agent` trails the installed version.
Read the release bodies for the tags that fall inside the span, as context on it rather than as a description of it.
Do not substitute a commit-log grep for breaking-change markers: a sampled week held 1,687 commits and zero of them, so that check reports all-clear forever.

```sh
# What the span touches. The compare API caps at 250 commits and 300 files, so on a long
# span read total_commits and the file list as a floor, not an inventory. agent_sha must be a
# commit the forge has; the patch-file remedy resets there first, so it normally is. A local
# commit returns 404 - compare from the upstream commit beneath it.
gh api repos/NousResearch/hermes-agent/compare/<agent_sha>...<target_sha> \
  --jq '.total_commits, (.files[].filename)'
gh api repos/NousResearch/hermes-agent/releases \
  --jq '.[] | "== \(.tag_name) \(.published_at)\n\(.body)"'
```

**The sha is the identity; the version string is not.**
`hermes` reports `v0.20.6 latest` and the update log ends with `Update complete! (v0.20.6)` while the tree runs 253 commits past that tag, because the semver is carried in the source and moves only when upstream cuts a release.
Treat it as a lower bound on what is installed: two machines reporting the same version can run materially different code.
Never measure ancestry in the VM's clone beyond the yes/no in the [preconditions](#preconditions) and the list behind it — it is shallow, with 54 graft points on August 27, 2026, so `merge-base` and `rev-list` answer there with artifacts.
Ask the forge, which has the whole history.

**Pause signals.**
Stop and read first when: the Python floor moves; the `hindsight-client` pin changes ([hindsight.md](hindsight.md#the-client-on-the-hermes-vm)); `_config_version` jumps by more than one, or a release names a configuration *floor* or touches the update mechanism; a dependency is under 14 days old.

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

**[VM]** Confirm the snapshot exists before going further.
It is the only route back from a forward-only migration, and upstream's backup path warns and continues on its own failures, so nothing else will tell you whether one was written:

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

**The restart does not touch the docker terminal sandboxes**, which keep running the image they were created from; replacing a stale one is `hermes-sandbox-refresh`'s business and never this runbook's ([hermes-vm.md](hermes-vm.md#the-docker-terminal-sandboxes)).

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
# The turn above returns 200 whether or not the memory write behind it succeeded. Hindsight
# success is DEBUG-silent and failures WARN loudly (verified live 2026-08-27), so pass on the
# first grep is EMPTY output — any line it prints is a failure to read.
journalctl --user -u hermes-gateway --since '10 min ago' | grep -i hindsight
journalctl --user -u hermes-gateway --since '10 min ago' | grep '1Password: applied'
```

The `applied N secrets` line is fail-open: after a restart, a drop from its previous value means the secrets provider failed silently.

**[VM]** After the update — and after any `hermes import` that touches the `safer_web_reader` profile — dispatch one task on that board and diff the reader's live worker tool list against the four recorded in [safer-web-reader.md](safer-web-reader.md#the-tool-surface-which-is-the-whole-of-the-containment), because a widened list there is a silent containment loss that every task completing normally will hide.

If you reapplied a local patch, verify its behaviour here by importing the touched module in the venv's interpreter and asserting against it directly.
Do not run the patch's own tests: `pytest` is not in the runtime venv, and installing it moves the agent's dependencies, which is the hazard the constrained WebUI install above exists to avoid.

**[laptop]** Positive evidence of the memory write, from the server side — `max(created_at)` must postdate the chat turn:

```sh
kubectl --context cynexia-homelab -n hindsight exec deploy/hindsight-postgres -- \
  psql -U hindsight -d hindsight -Atc \
  "select max(created_at) from memory_units where bank_id = 'hermes-default'"
```

## Report

**[laptop]** On success only; on failure send nothing and diagnose with the session still open.
Read the two shas from the record and type them into the body — never interpolate a command's output into it, and never echo the URL, whose last path segment is the ping identifier.
The update's own fetch can move past the recorded `target_sha` (it did on 2026-08-27); when the update log's `Code updated!` line names a different sha, report that one — it is what is installed.

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

Then read `git -C ~/.hermes/hermes-agent stash list`: an entry may be work that was serving before this run, or may predate it.
Whether `hermes update` puts a stash back is unsettled — this runbook once said nothing pops them, while the `--keep-stash` help implies the default reapplies them — so rely on neither answer.
Read the working tree to find out what is actually there, and restore the entry by hand if it is not.
The patch files from the [preconditions](#preconditions) are the insurance against losing local work; the stash is not.

**[VM]** To restore state, which discards everything that happened after the snapshot was taken:

```sh
ls -1t ~/.hermes/backups/pre-update-*.zip | head -n1
/home/hermes/.local/bin/hermes import --force ~/.hermes/backups/pre-update-<stamp>.zip
```
