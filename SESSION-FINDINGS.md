# Findings from the first `/update-estate` session

Recorded on August 28, 2026, during the first run of the `/update-estate` skill.
Each finding names what happened, why it matters, and a proposed fix.
Another agent addresses these; delete this file once every finding is closed.

Two of the five are already fixed on this branch, in `AGENTS.md`.
The other three need work that this session deliberately did not do, so that recording the findings did not turn into three side quests.

## 1. The `health-upgrade` epilogue and the skill disagree on when to push

`make health-upgrade` prints a numbered next-steps block when the dump finishes.
Its step 3 is `git push --force-with-lease`, before the diff and the apply.
Step 4 of `.claude/skills/update-estate/SKILL.md` puts the same push after verification and immediately before the merge.

Both orderings reach the same gate, so neither is unsafe on its own.
The problem is that the estate prints two different procedures for the same action within one minute of each other, and an agent following the epilogue pushes a branch it has not yet proved deploys cleanly.
Guidance that disagrees with itself gets resolved by whichever copy the reader saw last.

`make hindsight-upgrade` prints the same shape of block and gets it right: its step 6 is the push, after the apply and the rollout check, with the merge at step 7.
So `health-upgrade` is the outlier, not the pattern.

**Proposed fix:** change the epilogue in the `health-upgrade` target to match `hindsight-upgrade` and the skill, so the push follows verification.

## 2. `apply-homelab` reports `jottacloud-backup-scheduled` as `configured` on a converged tree

`make apply-homelab` reported `cronjob.batch/jottacloud-backup-scheduled configured`.
That CronJob appeared in no diff, before or after the apply, and a second `make diff-homelab` afterwards returned nothing at all.
So the tree and the cluster agree, and the `configured` is server-side apply adopting field ownership rather than a real change.

`AGENTS.md` already tells the reader that `configured` rather than `unchanged` is expected and is not drift, but its list names only Secrets, some persistent volumes and the cert-manager webhooks.
An agent that has been told to treat anything outside that list as another branch's work being reverted will stop on this one, every time.

A second apply during this session, for pull request 65, showed the same behavior for `cronjob/cloudflare-analytics`.
It reported `configured` while appearing in no diff.
So this is a class of resource, not one resource.

**Proposed fix:** find every resource in the class by applying twice against a converged tree and reading the second run, then add them to that list in `AGENTS.md`.
`cronjob/jottacloud-backup-scheduled` and `cronjob/cloudflare-analytics` are the two seen so far.

## 3. Gate 2 and gate 8 collide when the reader is an agent — FIXED ON THIS BRANCH

Hard gate 2 of the skill requires reading `make diff-<cluster>` in full.
Hard gate 8 forbids printing a resolved secret.
`kubectl diff` prints Secret `data:` as base64, and an agent's terminal output is a transcript that persists, so the two gates cannot both be satisfied by reading the raw diff.

No Secret appeared in the homelab diff during this session, so the collision did not bite.
It will bite on the first session where a secret value changes.

**Fixed** by a new rule in `AGENTS.md` giving two pipelines: one that prints the resource list, which is what gate 2 is actually about, and one that prints the body with base64 values masked.
Neither redirects to a file, which is what keeps them clear of the existing mirror-hazard rule.

## 4. Renovate cannot look up the keel image, and nothing detects that

The Renovate dependency dashboard, issue 59, carries a repository problem:

> Renovate failed to look up the following dependencies: `Failed to look up docker package ghcr.io/keel-hq/keel: no-result`.
> Files affected: `homelab/bootstrap/keel/keel.yaml`, `vps/bootstrap/keel/keel.yaml`

Both keel images are digest-pinned so that the update engine cannot update itself, which makes Renovate the only thing that can move them.
Right now nothing can.

**What this session verified.**
The registry is not the problem.
An anonymous pull token from `ghcr.io/token` lists 24 tags for `keel-hq/keel`, so the package is public and readable without credentials.
The only release tags are `0.21.1` and `0.22.1`; everything else is `latest`, `master-*` or `nightly-*`.
Both clusters run `0.22.1`, so no update is being missed today.
`renovate.json` declares no `hostRules`, and neither keel file is inside `ignorePaths`.
The root cause is therefore in how the hosted Renovate app authenticates to `ghcr.io`, and diagnosing it needs the run logs at `developer.mend.io`, which this session did not open.

**Is `make check-renovate-scope` defective?**
Not against its stated contract.
It proves that a pinned image is named by a repository file inside `kubernetes.managerFilePatterns` and outside `ignorePaths`.
That is a structural claim, and it is true for keel.

The defect is in what the estate infers from it.
`AGENTS.md` states that every pinned image in both clusters is watched, and treats the guard as the proof.
The guard proves the configuration is right, not that the lookup succeeds, and those came apart here.
The daily `update-watch` job does not close the gap either: it counts open `renovate[bot]` pull requests and identifies the dashboard by title, and it recognizes the "Action Required: Fix Renovate Configuration" issue, but a repository problem inside the dashboard body is neither of those.
So a keel advisory would sit unproposed until somebody read issue 59 by hand.

**Proposed fix, in two parts.**
First, find the authentication cause in the Mend run logs and fix it, most likely with a `hostRules` entry for `ghcr.io`.
Second, close the detection gap, because the same failure will recur for a different image.
The cheapest option is to have `update-watch` read the dashboard body for a package lookup failure and treat it as `down`, which reuses a monitor that already exists rather than adding machinery.

## 5. `gh pr merge --delete-branch` fails its local step inside a worktree

Running `gh pr merge 58 --squash --delete-branch` from a worktree printed:

    failed to run git: fatal: 'master' is already used by worktree at '<path>'

The merge itself succeeded and the remote branch was deleted.
Only the local cleanup failed, because `gh` tries to check out the default branch in the current worktree and another worktree holds `master`.

This matters because the operator asked for one worktree per pull request, and that message reads like the merge failed.
An agent that believes it can retry the merge, or that treats this as a blocker, does the wrong thing at the one point in the runbook where `master` and the cluster are supposed to agree.

Running the same command from a worktree that is not the pull request's own worktree avoids it entirely.
Pull request 65 merged cleanly that way later in the same session.

**Proposed fix:** note in the skill's Step 2 that `gh pr merge` runs from outside the pull request's worktree.
Confirm the outcome with `gh pr view <n> --json state,mergedAt` rather than trusting the exit status, then remove the worktree with `git worktree remove` and delete the local branch.

## 6. `renovate/stability-days` is not enforced, and enforcing it has a trap

`renovate.json` sets `minimumReleaseAge` to `3 days`, so Renovate posts a `renovate/stability-days` status check on every pull request it opens.
This session read that check on all eight remaining open Renovate pull requests.
Seven report `SUCCESS`.
Pull request 71, "pin dependencies", reports `PENDING`, so its updates are younger than three days.

Nothing enforces the check.
The repository has no rulesets and no classic branch protection on `master`, so a pull request merges whether the check passed, failed, or never reported.
This session excluded pull request 71 by hand after reading the check.

**The trap in requiring it.**
A GitHub required status check blocks a pull request when the check is never reported at all, not only when it fails.
Renovate posts `renovate/stability-days` only on its own pull requests.
Make it required on `master` without qualification and every human or agent pull request deadlocks, because the check never arrives.
The repository runs no CI, so nothing else would report it either.

**Proposed fix, needing a decision first.**
Create a repository ruleset on `master` requiring `renovate/stability-days`, and add a bypass actor so that pull requests which never receive the check can still merge.
A bypass for the repository admin role is the smallest option, but the operator merges the Renovate pull requests too, so an admin bypass weakens the rule it enforces.
Confirm which trade the operator wants before creating the ruleset.

Enforcement in the ruleset does not replace reading the check during a session.
Read it before every merge:

    gh pr view <n> --repo mnbf9rca/kubernetes_config --json statusCheckRollup

## 7. A pgvector image bump leaves the extension in the database behind

Pull request 65 moved `pgvector/pgvector` from `0.8.1-pg17` to `0.8.6-pg17`.
After the rollout, the database reports:

    vector installed=0.8.1 available=0.8.6 server=17.11

The image ships the 0.8.6 shared library and its SQL scripts, but the `vector` extension inside the `hindsight` database stays at the version it was created with until somebody runs `ALTER EXTENSION vector UPDATE`.
The PostgreSQL server itself moved from 17.6 to 17.11 in place, which is a patch release within the same major and needs no action.

The hindsight canary passed after the upgrade, retaining and recalling with HTTP 200 and one result, so nothing is broken today.
The concern is drift: every future pgvector bump widens the gap between the library and the extension, and new index types or functions in later versions stay unavailable while the estate believes it is running the version in the manifest.

This session did not run the `ALTER`.
It is a schema change to the store holding an agent's memory, the runbook does not call for it, and the pull request only asked to move an image tag.
The pre-upgrade dump `pre-upgrade-20260828153152` covers it if it is done later.

**Proposed fix:** decide whether the extension should track the image.
If it should, add the `ALTER EXTENSION vector UPDATE` to the hindsight upgrade runbook in `docs/operations/hindsight.md`, after the rollout and before the canary, and state that the pre-upgrade dump is its rollback.
Then run it once to close the current 0.8.1 to 0.8.6 gap.

## 8. Renovate splits the keel-fresh pair into two pull requests, which the parity guard forbids

`homelab/ops/keel-fresh.yaml` and `vps/ops/keel-fresh.yaml` hold the same CronJob twice on purpose, and `make check-keel-fresh-parity` requires them to be identical outside a stated list of sanctioned differences.
The container image is not on that list.

Renovate opened the `curlimages/curl` bump as two pull requests: 70 for the homelab copy and 74 for the VPS copy.
Taking either one alone puts the two copies out of parity.
This session confirmed the consequence by rebasing pull request 70 alone and running the guard, which failed and named the image line.
The guard has no per-cluster half, so a divergence refuses `apply-homelab` and `apply-vps` alike.

The skill's Step 2 says to work the pull requests one at a time, oldest first.
Followed literally, that instruction produces an estate where neither cluster can be applied at all until the second pull request is taken.
The instruction to carry other branches covers deployed-but-unmerged work, not two open pull requests that are individually invalid.

**How this session handled it.**
It built a branch carrying both file changes, confirmed the guard passed, applied to homelab and then to the VPS, verified `keel-fresh` on both, and only then pushed and merged the two pull requests back to back.
Each pull request merged normally, so neither had to be closed unmerged, which is unsupported and snoozes the update-watch monitor.

**Proposed fix, in two parts.**
First, stop Renovate splitting them: add a `packageRules` entry in `renovate.json` grouping `homelab/ops/keel-fresh.yaml` and `vps/ops/keel-fresh.yaml` under one `groupName`, so the pair arrives as a single pull request.
Second, describe the general case in the skill's Step 2: when two open pull requests are individually invalid under a guard, deploy a branch carrying both, then merge them back to back.
Any future enforced copy pair inherits the same problem.
