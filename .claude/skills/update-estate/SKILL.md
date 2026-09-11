---
name: update-estate
description: Run interactive Talos, Kubernetes and remote-base updates a few times a year, and apply remaining Renovate pins.
---

# Update the estate

Runtime images float through Keel, or through fresh Job pulls.
Renovate covers images without a floating channel and the MCP build inputs.
Talos, Kubernetes and remote-base bundles remain interactive, a few times a year.
Hermes application updates are outside this skill.

Run a session when the operator requests it.
Run an earlier session when a relevant security advisory requires action.
Read [estate-updates.md](../../../docs/operations/estate-updates.md) for versions, advisory feeds, and Omni procedures.

## Hard gates

These are refusals.
When you cannot satisfy one, stop and tell the operator what blocked you.

1. **Never merge before you have deployed and verified.**
   `master` records what is running, never what is intended.
   The order is always: check out the branch, apply, verify, then merge.
3. **Read `make diff-<cluster>` in full before every apply.**
   Not the summary - every resource it names.
   A resource in that list your branch never touched is another branch's deployed work about to be reverted.
   **Treat it as a revert until you have proved otherwise**, by finding the branch that deployed it.
   Read it through the two filter pipelines in `AGENTS.md` ("An agent reads a diff through a filter") - one prints the resource list, the other the body with long base64-looking values masked.
   `kubectl diff` redacts Secret `data` itself and `op run` masks plaintext, so the pipelines are a reading aid and a second line of defence rather than the protection.
4. **Carry every deployed-but-unmerged branch before you apply.**
   `git fetch origin`, rebase onto `origin/master`, then read the open pull requests for another branch that is already deployed and touches the same files.
   Without the fetch the rebase is a no-op against a stale remote-tracking ref.
   An apply reconciles the whole rendered tree: every file your branch does not carry is reset to your branch's version, silently, with every job still green.
   This cost a reverted restic gate on August 24, 2026.
5. **Never bypass the context guards.**
   Never pass `HOMELAB_CONTEXT=` or `VPS_CONTEXT=` on the command line, and never invoke an `_*-inner` Makefile target directly.
6. **Never render then apply.**
   `make build-* > file` writes the literal string `<concealed by 1Password>` into every Secret, and the apply reports success.
   Use `make diff-*` and `make apply-*`, whose pipelines keep real values inside one process.
7. **Keep PostgreSQL major upgrades and restic repository-format migrations interactive.**
   Take a PostgreSQL dump before a major upgrade, then use a fresh volume and restore it.
8. **Squash merges only.**
   `gh pr merge <n> --squash --delete-branch`, run from outside the pull request's worktree.
   Merge commits and rebase merges are disabled on the repository.
9. **Never print a resolved secret, and never build a ping body from a command's output.**
   If a real secret value does reach your output, tell the operator in your next message and add a row to `secrets-to-rotate.md` before doing anything else.

## Step 0 - Open the session

- [ ] `git fetch origin` and confirm the working tree is clean.
- [ ] Assert the Omni etcd backups.
      A backup is the only recovery path for a bad Talos or Kubernetes upgrade, so this gates Step 3:

      omnictl get etcdbackupoverallstatus -o yaml
      omnictl get etcdbackupstatus -o json | jq -r '"\(.metadata.id) \(.spec.lastbackuptime.seconds | todate)"'

      Expect `configurationname: s3`, an empty `configurationerror`, and a converted `lastbackuptime` inside the last day for each cluster.
      `lastbackuptime.seconds` is raw Unix seconds, which is why the second command converts it.
      If a backup is stale or the configuration reports an error, say so to the operator and **skip Step 3** for that cluster.
      Never run `omnictl get etcdbackups3configs` - it prints the storage access key and secret in plaintext.
- [ ] If `first-session.md` exists in this skill's directory, work it now, before Step 1, and delete it when its three items pass.
      The file is `first-session.md`, beside this one.
      It is deliberately named here rather than linked: the first session deletes it, and a link to a deleted file is a dead link in every session after that.
- [ ] Tell the operator what you found and what you are about to do.
      Then complete the agreed session without per-item approval pauses.

## Step 1 - Preflight

- [ ] `git fetch origin && git rebase origin/master`.
- [ ] List every open pull request and classify it:

      gh pr list --repo mnbf9rca/kubernetes_config --state open \
        --json number,title,author,headRefName,updatedAt

- [ ] For every open pull request that is **not** from `renovate[bot]`, ask the operator whether it is deployed-but-unmerged.
      A branch that is already applied to a cluster must be carried into whatever you apply next, or your apply reverts it.
- [ ] Read the Renovate dependency dashboard issue.
      Investigate reported lookup failures.
      PostgreSQL channels stay within their selected major; a major change is a separate dump-and-restore operation.

## Step 2 - Every open Renovate pull request

Runtime floating tags belong to Keel or fresh Job pulls, not this loop.
The remaining local runtime pin is `alpine/k8s`, which has no floating channel.
MCP input updates normally automerge after their required checks.
The VPS local-path base remains Renovate-watched.

Read each remaining pin's release notes before applying its proposed change.
Keep the three-day stability wait.
Do not add a monthly approval window.

**The per-pull-request loop:**

- [ ] Read this pull request's status checks first, before you spend an apply on it:

      gh pr view <n> --repo mnbf9rca/kubernetes_config --json statusCheckRollup

      A `PENDING` or failing `renovate/stability-days` means the release is younger than `minimumReleaseAge` and this pull request is **not this session's work**: leave it open, do not apply it, and name it at the close.
      The rule is in `AGENTS.md` (read a pull request's status checks before merging).
- [ ] Read the pin's reason and the release notes.
- [ ] `kubectl config use-context cynexia-homelab` or `kubectl config use-context cynexia-vps`, matching the cluster this pull request touches.
      The Makefile's `check-context` and `check-vps-context` guards read `kubectl config current-context` and refuse otherwise, and the loop alternates between clusters.
- [ ] Give this pull request its own worktree and work it there:

      git worktree add ../kubernetes_config-worktrees/pr-<n> --detach
      cd ../kubernetes_config-worktrees/pr-<n> && gh pr checkout <n> && git fetch origin && git rebase origin/master

      Merge in any other deployed-but-unmerged branch you identified in Step 1.
      Every `make` target works from a worktree: `op run` reads `OP_SERVICE_ACCOUNT_TOKEN` out of the shell environment - if direnv reports `.envrc is blocked` on entering the new worktree, run `direnv allow` there once.
- [ ] `make diff-homelab` or `make diff-vps`.
      **Read every resource it names.**
      Confirm only the image lines you expect have moved.
      Apply gate 3 to anything else.
- [ ] If this pull request changes a standalone `kind: Job` - not a CronJob; today that is `homelab/backup/restic-init-job.yaml` and `vps/backup/restic-init-job.yaml` - clear the completed Job before you apply.
      A Job's `spec.template` is immutable and `restic-init` sets `ttlSecondsAfterFinished: 86400`, so for a day after any apply recreates it, a second apply that moves its image fails on that one resource - and `kubectl apply` continues past the failure, leaving the tree half-updated with a non-zero exit as the only sign (the TTL rule in `AGENTS.md`).
      A session that applies twice in one afternoon hits this; one that runs a few times a year finds the Job already collected:

      kubectl -n backup get job restic-init -o 'custom-columns=NAME:.metadata.name,COMPLETE:.status.conditions[?(@.type=="Complete")].status,IMAGE:.spec.template.spec.containers[0].image'
      kubectl -n backup delete job restic-init

      The quoting is required - zsh globs the unquoted `[?(...)]` and the command exits 1 before kubectl runs.
      Deleting it once it has finished - `Complete` or `Failed`, since a `Failed` Job still inside its TTL window blocks the apply identically - is safe here: `restic-init.sh` probes the repository before initialising, so the re-run the next apply triggers is a no-op.
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
If the CronJob's own schedule is broken - suspended, a bad `timeZone`, or wedged by `concurrencyPolicy: Forbid` - your green verification hides it for another full period, which is 26 hours for `homelab-restic` and can postpone a missing-schedule alert.
That is the estate's dominant failure mode, manufactured by hand.

So after **every** hand-triggered verification, confirm the schedule itself is intact:

    kubectl -n <ns> get cronjob <name> \
      -o custom-columns=NAME:.metadata.name,SUSPEND:.spec.suspend,SCHEDULE:.spec.schedule,LAST:.status.lastScheduleTime

`SUSPEND` must be `false`, `SCHEDULE` must be what the manifest says, and `LAST` must be inside one period of now.
A `LAST` older than one period means the schedule stopped firing - report it, and do not treat your own triggered run as evidence that it works.

## Step 3 - Talos and Kubernetes through Omni

Skip this step for a cluster whose etcd backup failed the Step 0 assertion.

**Read the control-plane count out of the ledger in [estate-updates.md](../../../docs/operations/estate-updates.md#the-version-ledger) before you plan an upgrade, and confirm it against the cluster.**
Do not carry a remembered number: the VPS control plane grew from one node to three, and the count decides what kind of operation this is.

    kubectl --context <ctx> get nodes -l node-role.kubernetes.io/control-plane

- [ ] `omnictl get configpatches` - reconcile against `homelab/talos/` and `vps/talos/`; see [estate-updates.md](../../../docs/operations/estate-updates.md#upgrading-talos-and-kubernetes-through-omni).
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

### Remote-base bundles in the Kubernetes session

Renovate proposes the VPS local-path `?ref=` bump; review that bundle in this Kubernetes session.
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

## Step 4 - Close the session

- [ ] Confirm `master` contains everything you deployed: `git status` clean, no unmerged pull request that has been applied.
- [ ] Count the merged pull requests and updated base pins.
- [ ] Report unapplied changes and blocked pull requests.
- [ ] Open a pull request for any remaining documentation corrections.
      Documentation changes need review, not a live exercise.

- [ ] Ping the `estate-update` check.
      **Every `<...>` below is a placeholder.**
      Replace the two counts with numbers you tallied yourself, using the session's results:

      op read 'op://Homelab/estate-update/healthcheck-uuid' | { read -r u; \
        printf 'summary=estate-update session complete\nrenovate_prs_merged=<N>\nbootstrap_pins_bumped=<N>\n' \
        | curl -fsS -m 10 --data-binary @- -o /dev/null -w 'ping_http=%{http_code}\n' \
          "https://hc-ping.com/$u" 2>/dev/null; }

      **Substitute both placeholders before you run it. A ping that still contains `<` is a fabricated record,** and a plausible wrong value in a body that travels to every notification channel is worse than an obviously broken one.

      Expect `ping_http=200`.
      Keep it as one command: the UUID must never reach your output or a file.
      `2>/dev/null` is deliberate - a failing curl quotes the URL it was given, and that URL is the check's write credential.
      If the ping does not return 200, retry once, then tell the operator rather than pasting the URL anywhere.

      The body follows the repo's rule: `summary=` first, printable ASCII `key=value` per line, values that are counts you tallied or verdicts from a fixed enum.
      **Never put a command's output in it.**
- [ ] Report merged changes, upgrades, deferred work, and failures.
