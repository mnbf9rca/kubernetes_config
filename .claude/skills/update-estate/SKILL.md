---
name: update-estate
description: Run the periodic estate update session for this repo - every open Renovate pull request, the hand-managed kustomize pins, Talos and Kubernetes through Omni, the Hermes VM update runbook, and the closing dead-man ping. Use when the operator asks to update and patch homelab and vps, when a security advisory names a component this estate runs, or when the estate-update healthchecks.io check is close to its 45-day period.
---

# Update the estate

Everything here updates one of two ways.
Keel and the VM's `unattended-upgrades` run on their own timers.
Everything else updates in this session, every 4 to 6 weeks — the Hermes VM included, which is a runbook this session follows rather than a timer — and the session ends by pinging one dead-man check.

Run a session when:

- the operator asks for it ("update and patch homelab and vps");
- an advisory in the FreshRSS `security` category names a component this estate runs.
  That is an out-of-band session, now, not at the next calendar slot;
- the `estate-update` check is close to its 45-day period.

The reference material lives in [docs/operations/estate-updates.md](../../../docs/operations/estate-updates.md): the version ledger, the advisory feeds, why the remaining kustomize pins are hand-managed, the Omni upgrade and backup mechanics.
The Hermes VM step has its own runbook, [hermes-vm-updates.md](../../../docs/operations/hermes-vm-updates.md).
Read both as you work the steps; this file is the session, not the reference.

## Hard gates

These are refusals.
When you cannot satisfy one, stop and tell the operator what blocked you.

1. **Never merge before you have deployed and verified.**
   `master` records what is running, never what is intended.
   The order is always: check out the branch, apply, verify, then merge.
2. **Merge only what this runbook prescribes.**
   Steps 2, 3 and 4 name it: the Renovate pull requests, the hand-managed pins, the version ledger.
   Everything the session invents - a runbook correction, a new guard, a rule, a better command - goes on the findings branch and merges once, after review.
   The test is "did the runbook ask me to make it?", not "is this change good?".
   A session revises its own conclusions as later work contradicts them, and a mid-session merge forecloses that.
   At the first invention, cut that branch from `master` and commit it there - never inside a pull request's worktree, which is force-pushed, merged and removed.
3. **Read `make diff-<cluster>` in full before every apply.**
   Not the summary - every resource it names.
   A resource in that list your branch never touched is another branch's deployed work about to be reverted.
   **Treat it as a revert until you have proved otherwise**, by finding the branch that deployed it.
   Read it through the two filter pipelines in `AGENTS.md` ("An agent reads a diff through a filter") - one prints the resource list, the other the body with base64 Secret values masked - which is how you read every line without a resolved secret landing in the transcript.
4. **Carry every deployed-but-unmerged branch before you apply.**
   Rebase onto `origin/master`, then read the open pull requests for another branch that is already deployed and touches the same files.
   An apply reconciles the whole rendered tree: every file your branch does not carry is reset to your branch's version, silently, with every job still green.
   This cost a reverted restic gate on August 24, 2026.
5. **Never bypass the context guards.**
   Never pass `HOMELAB_CONTEXT=` or `VPS_CONTEXT=` on the command line, and never invoke an `_*-inner` Makefile target directly.
6. **Never render then apply.**
   `make build-* > file` writes the literal string `<concealed by 1Password>` into every Secret, and the apply reports success.
   Use `make diff-*` and `make apply-*`, whose pipelines keep real values inside one process.
7. **Dump before you upgrade anything stateful, and show the dump succeeded.**
   Then keep going - no per-item pause for approval.
8. **Squash merges only.**
   `gh pr merge <n> --squash --delete-branch`, run from outside the pull request's worktree.
   Merge commits and rebase merges are disabled on the repository.
9. **Never print a resolved secret, and never build a ping body from a command's output.**
   If a real secret value does reach your output, tell the operator in your next message and add a row to `secrets-to-rotate.md` before doing anything else.

## Step 0 - Open the session

- [ ] `git fetch origin` and confirm the working tree is clean.
- [ ] Assert the Omni etcd backups.
      A backup is the only recovery path for a bad Talos or Kubernetes upgrade, so this gates Step 4:

      omnictl get etcdbackupoverallstatus -o yaml
      omnictl get etcdbackupstatus -o json | jq -r '"\(.metadata.id) \(.spec.lastbackuptime.seconds | todate)"'

      Expect `configurationname: s3`, an empty `configurationerror`, and a converted `lastbackuptime` inside the last day for each cluster.
      `lastbackuptime.seconds` is raw Unix seconds, which is why the second command converts it.
      If a backup is stale or the configuration reports an error, say so to the operator and **skip Step 4** for that cluster.
      Never run `omnictl get etcdbackups3configs` - it prints the storage access key and secret in plaintext.
- [ ] If `first-session.md` exists in this skill's directory, work it now, before Step 1, and delete it when its three items pass.
      The file is `first-session.md`, beside this one.
      It is deliberately named here rather than linked: the first session deletes it, and a link to a deleted file is a dead link in every session after that.
- [ ] Tell the operator what you found and what you are about to do.
      Then work Steps 1 to 6 without pausing for per-item approval.

## Step 1 - Preflight

- [ ] `git checkout master && git pull --ff-only`.
- [ ] List every open pull request and classify it:

      gh pr list --repo mnbf9rca/kubernetes_config --state open \
        --json number,title,author,headRefName,updatedAt

- [ ] For every open pull request that is **not** from `renovate[bot]`, ask the operator whether it is deployed-but-unmerged.
      A branch that is already applied to a cluster must be carried into whatever you apply next, or your apply reverts it.
- [ ] Read the Renovate dependency dashboard issue.
      Majors wait there for approval rather than opening automatically.
      Approve what this session intends to take; leave the rest.
      **PostgreSQL majors are never a tag edit** - they are a dump, a fresh volume and a restore, and they stay refused in `renovate.json`.

## Step 2 - Every open Renovate pull request

Work them one at a time, oldest first.
Nothing here waits for per-item approval.

**Two pull requests can be individually invalid, and then "one at a time" is the wrong instruction.**
`make check-keel-fresh-parity` requires `homelab/ops/keel-fresh.yaml` and `vps/ops/keel-fresh.yaml` to stay identical outside a stated list, and it has no per-cluster half - so an image bump that arrives as one pull request per copy makes *either one alone* refuse `apply-homelab` and `apply-vps` both.
Renovate split exactly that on August 28, 2026.
When a guard rejects two open pull requests taken singly: build one branch carrying both changes, confirm the guard passes, apply to each cluster and verify, then push and merge the two back to back.
**Never close either one unmerged** - that is unsupported and snoozes the update-watch monitor.
Any future enforced copy pair inherits this shape.

**Read the change before you take it.**
This applies to the pinned set only: keel-floating workloads are keel's by the two-modes rule and stay unread.
Depth is proportionate - a patch span gets a skim, a major gets a real read, a PostgreSQL major stays refused.

1. **Why is this pinned where it is?**
   The repository first: the comment on the line, the docs, `git log -L` over it.
   Where the reason was never written down, the vendor's release notes across the span are where it usually lives - a breaking change immediately above the pin is probably the reason it sits there, and probably still standing.
2. **What changed across the span?**
   Ask this only once the pin has no standing reason to hold.
   Read for work the tag edit cannot do itself: a migration, a component versioned inside the stored data, a renamed setting, a manual step named in a release body.
   Handle each one or record it.
3. **Declining is a configuration change, not a pull request action.**
   Never close a Renovate pull request by hand, and never leave one open you have decided not to merge.
   Encode the hold in `renovate.json` - an `allowedVersions` cap or a disable, with a `description` carrying the reason and the date - and Renovate withdraws its own pull request.
   That recorded reason is what makes question 1 answerable next session.
   Scope the cap deliberately: `<1.42` still takes patches, an exact version freezes the line.

`getmeili/meilisearch` in `vps/workloads/karakeep.yaml` is the worked example.
Question 1: karakeep's own compose file pins v1.41.0, so the pin is the pairing karakeep tests against, and that reason still stands.
Question 2 therefore does not arise - and it would not have been cheap, because Meilisearch does not convert its index across that span on startup.
Question 3: the hold lives in `renovate.json` naming karakeep's pin as the condition, so the pull request withdraws itself and returns when karakeep moves.

**Dump first where the service holds state:**

| The pull request touches | Take the dump with |
|---|---|
| `homelab/hindsight/**` | `make hindsight-upgrade` |
| `homelab/health/**` and it moves the InfluxDB or Grafana image | `make health-upgrade` |
| a VPS stateful workload (umami-postgres, karakeep, meilisearch, uptime-kuma) | no dump — the nightly restic sweep is the accepted floor. A PostgreSQL **major** is refused in `renovate.json` and is never a tag edit: it is a dump, a fresh volume and a restore |
| anything else | no dump |

Hindsight runs forward-only Alembic migrations against the store holding an agent's memory, so **the dump is the only rollback**.
If `make health-upgrade` is missing, stop and tell the operator.
Do not hand-roll a substitute dump.

**The per-pull-request loop:**

- [ ] Read this pull request's status checks first, before you spend an apply on it:

      gh pr view <n> --repo mnbf9rca/kubernetes_config --json statusCheckRollup

      A `PENDING` or failing `renovate/stability-days` means the release is younger than `minimumReleaseAge` and this pull request is **not this session's work**: leave it open, do not apply it, and name it at the close.
      Nothing on the repository enforces the check, so reading it is the whole of the control - the rule is in `AGENTS.md`.
- [ ] `kubectl config use-context cynexia-homelab` or `kubectl config use-context cynexia-vps`, matching the cluster this pull request touches.
      The Makefile's `check-context` and `check-vps-context` guards read `kubectl config current-context` and refuse otherwise, and the loop alternates between clusters.
- [ ] Give this pull request its own worktree and work it there:

      git worktree add ../kubernetes_config-worktrees/pr-<n> --detach
      cd ../kubernetes_config-worktrees/pr-<n> && gh pr checkout <n> && git rebase origin/master

      Merge in any other deployed-but-unmerged branch you identified in Step 1.
      Every `make` target works from a worktree: `op run` reads `OP_SERVICE_ACCOUNT_TOKEN` out of the shell environment, so there is no `direnv allow` to repeat.
- [ ] Take the dump if the table above calls for one.
      Print the result.
      A failed dump ends this pull request - move to the next one and report it at the close.
- [ ] `make diff-homelab` or `make diff-vps`.
      **Read every resource it names.**
      Confirm only the image lines you expect have moved.
      Apply gate 3 to anything else.
- [ ] If this pull request changes any `*-init-job.yaml`, clear the completed Job before you apply.
      A Job's `spec.template` is immutable and `restic-init` sets `ttlSecondsAfterFinished: 86400`, so for a day after any apply recreates it, a second apply that moves its image fails on that one resource - and `kubectl apply` continues past the failure, leaving the tree half-updated with a non-zero exit as the only sign (the TTL rule in `AGENTS.md`).
      A session that applies twice in one afternoon hits this; one that runs every 4 to 6 weeks finds the Job already collected:

      kubectl -n backup get job restic-init -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image
      kubectl -n backup delete job restic-init

      Deleting it once it reports `Complete=True` is safe here: `restic-init.sh` probes the repository before initialising, so a re-run is a no-op.
- [ ] `make apply-homelab` or `make apply-vps`.
- [ ] Wait for the rollout and then verify by hand, from the table below.
- [ ] `git push --force-with-lease`.
      The rebase rewrote the branch, so the pull request head must be updated before you merge - otherwise `gh pr merge` merges the tree you did not deploy, `master` never receives the work you carried, and the next session's apply reverts it.
      That is the August 24, 2026 incident, reached by procedure rather than by accident.
      Renovate may reset or recreate a branch you force-pushed; that is normal and costs nothing, because the merge lands first.
- [ ] Free the branch before you merge: return to the **main checkout**, then `git worktree remove ../kubernetes_config-worktrees/pr-<n>`.
      Both halves of the merge need this.
      `gh` checks out the default branch as its own cleanup step, which fails with `fatal: 'master' is already used by worktree at <path>`; and `--delete-branch` runs `git branch -D`, which git refuses for a branch a worktree still has checked out, so `gh` exits non-zero with `failed to delete local branch <b>` **after the merge has already succeeded**.
      Either message reads like a failed merge and invites a retry, at the one point in the session where `master` and the cluster are meant to agree.
- [ ] `gh pr merge <n> --squash --delete-branch`, from the main checkout.
- [ ] Confirm the outcome from the API rather than from the exit status:

      gh pr view <n> --repo mnbf9rca/kubernetes_config --json state,mergedAt

- [ ] `git checkout master && git pull --ff-only`.

**Verify by triggering the job, not by waiting for its schedule.**
Use a timestamped name so the Job never collides, and let its own `ttlSecondsAfterFinished` collect it:

| What changed | Verify with |
|---|---|
| `homelab/hindsight/**` | `kubectl -n hindsight rollout status deploy/hindsight --timeout=600s`, then create a Job from `cronjob/hindsight-canary` and wait for it |
| `homelab/health/**` ingest or InfluxDB | `kubectl -n health rollout status deploy/<name> --timeout=600s`, then a Job from `cronjob/ingest-freshness` |
| `homelab/health/**` backup path | a Job from `cronjob/influx-backup` |
| `homelab/health/**` Cloudflare analytics | a Job from `cronjob/cloudflare-analytics` |
| `homelab/ops/**` | a Job from `cronjob/update-watch` in namespace `ops` |
| `homelab/backup/**` or `vps/backup/**` | a Job from `cronjob/restic-backup` in namespace `backup` |
| a VPS workload | `kubectl -n vps rollout status deploy/<name> --timeout=600s`, then confirm its uptime-kuma monitor is UP |

The command shape, with `hindsight-canary` as the example:

    ts=$(date -u +%Y%m%d%H%M%S); kubectl -n hindsight create job --from=cronjob/hindsight-canary "now-$ts"

then wait for it and read its log:

    kubectl -n hindsight wait --for=condition=complete job/now-<ts> --timeout=300s
    kubectl -n hindsight logs job/now-<ts> --tail=20

A Job created `--from=cronjob/...` inherits the whole pod spec, so nothing drifts, and it is exempt from `make check-job-ttl` because it is CronJob-shaped.
**Never add a `kind: Job` manifest to the tree for this** - a completed Job pins its own immutable `spec.template` and breaks the next apply quietly.

**A hand-triggered Job also fires the CronJob's dead-man signal.**
It inherits the whole pod spec, ping URL included, so a successful verification run resets that job's timer - the healthchecks.io check for the two restic jobs, and the uptime-kuma **push monitor** for everything else, which is most of what the session touches ([uptime-kuma.md](../../../docs/operations/uptime-kuma.md#push-monitors)).
If the CronJob's own schedule is broken - suspended, a bad `timeZone`, or wedged by `concurrencyPolicy: Forbid` - your green verification hides it for another full period, which is 26 hours for `homelab-restic` and could be the whole 4-to-6-week cadence for anything the session touches.
That is the estate's dominant failure mode, manufactured by hand.

So after **every** hand-triggered verification, confirm the schedule itself is intact:

    kubectl -n <ns> get cronjob <name> \
      -o custom-columns=NAME:.metadata.name,SUSPEND:.spec.suspend,SCHEDULE:.spec.schedule,LAST:.status.lastScheduleTime

`SUSPEND` must be `false`, `SCHEDULE` must be what the manifest says, and `LAST` must be inside one period of now.
A `LAST` older than one period means the schedule stopped firing - report it, and do not treat your own triggered run as evidence that it works.

## Step 3 - The hand-managed kustomize pins

Renovate covers image tags, and its `kustomize` manager covers the VPS local-path base, which pins its version as a `?ref=` on the URL.
The three files below repeat the version inside the URL path, where the manager cannot see it, so the session bumps them by hand.

| File | Upstream repository | Occurrences |
|---|---|---|
| `homelab/bootstrap/local-path/kustomization.yaml` | `rancher/local-path-provisioner` | 1 |
| `homelab/bootstrap/nfs-csi/kustomization.yaml` | `kubernetes-csi/csi-driver-nfs` | 9 — four URLs naming it **twice** each, and a comment |
| `homelab/bootstrap/cert-manager/kustomization.yaml` | `cert-manager/cert-manager` | 2 — the URL and a comment |

- [ ] Check each upstream: `gh release list -R <owner>/<repo> --limit 5`.
- [ ] For each one that moved, branch from `master`, edit **every** occurrence, and read the upstream release notes for anything that is not a version bump.
- [ ] `make diff-<cluster>` and read it.
      A base bump changes many resources at once, so this diff is long and gate 3 still applies to every line of it.
- [ ] `make apply-<cluster>`, then confirm the component is healthy: cert-manager's controller and webhook Ready, the CSI node and controller pods Running, the local-path provisioner Running.
- [ ] Publish and merge, in that order: `git push -u origin HEAD`, then `gh pr create --fill`, then `gh pr merge <n> --squash --delete-branch` once the apply is verified.
      Nothing on GitHub exists until you push it.

## Step 4 - Talos and Kubernetes through Omni

Skip this step for a cluster whose etcd backup failed the Step 0 assertion.

**Read the control-plane count out of the ledger in [estate-updates.md](../../../docs/operations/estate-updates.md#the-version-ledger) before you plan an upgrade, and confirm it against the cluster.**
Do not carry a remembered number: the VPS control plane grew from one node to three, and the count decides what kind of operation this is.

    kubectl --context <ctx> get nodes -l node-role.kubernetes.io/control-plane

- [ ] Read the current versions and what Omni will allow:

      omnictl get clusters -o json | jq '{id:.metadata.id, talos:.spec.talosversion, k8s:.spec.kubernetesversion}'
      omnictl get talosupgradestatus <cluster> -o yaml
      omnictl get kubernetesupgradestatus <cluster> -o yaml

      `.spec.upgradeversions` is the list of targets Omni permits.
      Omni refuses unsupported paths, so this list is the plan, not a suggestion.
- [ ] Choose targets by the rule: **the latest patch of every intermediate minor**, one minor at a time.
      Talos migrations are tested only between adjacent minors.
      A Talos upgrade does not move Kubernetes; do them as separate operations.
- [ ] Run the Kubernetes pre-checks before committing:

      omnictl cluster kubernetes upgrade-pre-checks <cluster> --to <version>

- [ ] Upgrade through the Omni UI: **Clusters → the cluster → Update Talos**, then **Update Kubernetes**.
      Do not edit the `Clusters.omni.sidero.dev` resource by hand to change a version - that path is undocumented.
      `talosctl upgrade-k8s` is denied by Omni's RBAC here, `--dry-run` included, so it is not an option either.
- [ ] After the Kubernetes upgrade completes, **read the bootstrap manifest diff before anything applies it**.
      Omni holds these back deliberately so it cannot overwrite hand edits:

      omnictl get kubernetesupgrademanifeststatus -o yaml
      omnictl cluster kubernetes manifest-sync <cluster>

      `manifest-sync` defaults to `--dry-run` true and prints what it would do.
      Read it in full, then apply what suits this cluster with `--dry-run=false`.
      The UI equivalent is **Bootstrap Manifests** in the left navigation.
      A non-zero `outofsync` after a Kubernetes upgrade means the data-plane components - kube-proxy, the CNI and CoreDNS - have not moved with the control plane, so read it as a version gap rather than a queue and sync it in the session that created it: [Bootstrap manifests](../../../docs/operations/estate-updates.md#bootstrap-manifests), which also carries the one-liner that collapses the backlog to the lines that actually differ.
      Do not run `talosctl get manifests -o yaml` unfiltered to inspect the sources - it embeds the bootstrap-token Secret.
- [ ] Verify: `kubectl --context <ctx> get nodes -o wide` shows the new versions and the node `Ready`; every namespace's pods return to Running.
- [ ] Update the version ledger in `docs/operations/estate-updates.md` - the versions, the control-plane counts and node names, and the "Confirmed" date, even when nothing moved.
      Commit it on a branch, `git push -u origin HEAD`, `gh pr create --fill`, then squash-merge it.
      There is nothing to apply: it is documentation.

If a node is stuck, wait first: `Rebooting` and `Installing` mean the upgrade is still running.
Then read `omnictl machine-logs <machine-id>`, then the serial console; Talos allows no SSH.
Never delete machines at the infrastructure provider, never add control-plane nodes to repair quorum, and never `kubectl delete node` a control-plane node during a stalled upgrade.
Recovery paths are in [estate-updates.md](../../../docs/operations/estate-updates.md#recovering-a-bad-upgrade).

## Step 5 - The Hermes VM

There is no updater on the VM.
**Follow [hermes-vm-updates.md](../../../docs/operations/hermes-vm-updates.md) end to end** and do not improvise a shorter path: its steps carry the reasons they exist, and it is the whole control - no guard in this repository reads a runbook.

- [ ] Open one ssh session to `hermes@hermes.cynexia.net` and keep it open for the whole step.
      The runbook's `[VM]` blocks are written for a shell already open; its `[laptop]` blocks run locally.

      `loginctl enable-linger hermes` must be set on the VM, because the runbook's update step runs detached under `systemd-run --user`.
      A failure that mentions the user manager or `DBUS_SESSION_BUS_ADDRESS` is missing linger - say that to the operator rather than working around it.
- [ ] Work the runbook in order: Preconditions, Change analysis, Update, Verify.
      **Any precondition that fails stops the step.**
      Report the stop to the operator; do not update around it.
- [ ] Read the change analysis for work the update cannot do on its own: a configuration schema change, a renamed setting, a plugin that needs re-registering, a manual migration step named in a release body.
      Handle each one, and say what you did at the close.
      When a manual step is not safe to take unattended, stop and describe it to the operator.
- [ ] On a Verify failure, take the runbook's Rollback with the session still open, and send no ping.
      On success, take its Report step, which pings the `hermes-update` check from the laptop with the body the runbook specifies.
      **That ping belongs to the runbook's report step and nowhere else** - do not ping `hermes-update` from the closing `estate-update` step, and do not fold the two checks together.
- [ ] Reboot only if the VM asks for it:

      ssh hermes@hermes.cynexia.net 'test -e /var/run/reboot-required && echo reboot-required || echo none'

      `none` is a common and correct answer even after a kernel update: the VM runs `unattended-upgrades` with an automatic reboot at 04:45, so it may already have consumed the flag on its own.
      Do not force a reboot to make this look tidy.

      If it prints `reboot-required`, check that a non-interactive reboot is even possible before you try one - there is no tty on this connection, so a sudo password prompt fails or hangs:

      ssh hermes@hermes.cynexia.net 'sudo -n true && echo SUDO-OK || echo SUDO-NEEDS-PASSWORD'

      On `SUDO-NEEDS-PASSWORD`, stop here: tell the operator the VM wants a reboot and that passwordless sudo is not configured for it, and let them do it.
      On `SUDO-OK`:

      ssh hermes@hermes.cynexia.net 'sudo -n systemctl reboot'

      Wait for the VM to come back, then confirm the app answers a real request rather than a health endpoint.
      Re-run the runbook's [Verify](../../../docs/operations/hermes-vm-updates.md#verify) block: its chat turn is that real request, and `/health` answers `status: ok` even when the agent cannot import.
      The daily `hermes-app-alive` check is not a unit you can start.
      It is a `no_agent` cron job inside the default gateway that runs at 05:45 UTC, one hour after the reboot window, and pushes its uptime-kuma monitor then - so the next scheduled beat is the independent confirmation.
      To pull it forward, take the job id from `hermes cron list` and run `hermes cron run <job_id>`, then read the caveat in [hermes-vm.md](../../../docs/operations/hermes-vm.md): a hand-triggered run resolves its push token in the CLI's own process, so it can pass while the 05:45 scheduled run still fails.

## Step 6 - Close the session

- [ ] Confirm `master` contains everything you deployed: `git status` clean, no unmerged pull request that has been applied.
- [ ] Count what you did: pull requests merged, pins bumped, and one verdict for the VM.
- [ ] Say what you left unmerged and why: every pull request held back on a `PENDING` check or a hold you encoded in `renovate.json`, and everything gate 2 put on the findings branch rather than into `master` - a runbook correction, a guard, a rule.
      The close is where the operator learns that work exists and is waiting for one review.
- [ ] Push the findings branch and open one pull request for it: `git push -u origin HEAD`, then `gh pr create --fill`.
      It merges in a later session, because the deploy-then-merge extension wants the prose exercised first - but nothing on GitHub exists until you push it.
- [ ] Ping the `estate-update` check.
      **Every `<...>` below is a placeholder.**
      Replace the two counts with numbers you tallied yourself, and replace the verdict with one value from the enum written inline beside it:

      op read 'op://Homelab/estate-update/healthcheck-uuid' | { read -r u; \
        printf 'summary=estate-update session complete\nrenovate_prs_merged=<N>\nbootstrap_pins_bumped=<N>\nhermes_vm=<current|updated|rebooted|skipped>\n' \
        | curl -fsS -m 10 --data-binary @- -o /dev/null -w 'ping_http=%{http_code}\n' \
          "https://hc-ping.com/$u" 2>/dev/null; }

      **Substitute all three placeholders before you run it. A ping that still contains `<` is a fabricated record,** and a plausible wrong value in a body that travels to every notification channel is worse than an obviously broken one.

      Expect `ping_http=200`.
      Keep it as one command: the UUID must never reach your output or a file.
      `2>/dev/null` is deliberate - a failing curl quotes the URL it was given, and that URL is the check's write credential.
      If the ping does not return 200, retry once, then tell the operator rather than pasting the URL anywhere.

      The body follows the repo's rule: `summary=` first, printable ASCII `key=value` per line, values that are counts you tallied or verdicts from a fixed enum.
      **Never put a command's output in it.**
- [ ] Report to the operator: what merged, what was bumped, what upgraded, what the VM needed, anything you deliberately left, and anything that failed.
