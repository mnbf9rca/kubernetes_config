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

## 7. A pgvector image bump leaves the extension in the database behind — researched, and it is a no-op today

Pull request 65 moved `pgvector/pgvector` from `0.8.1-pg17` to `0.8.6-pg17`.
After the rollout, the database reports:

    vector installed=0.8.1 available=0.8.6 server=17.11

The image ships the 0.8.6 shared library and its SQL scripts, but the `vector` extension inside the `hindsight` database stays at the version it was created with until somebody runs `ALTER EXTENSION vector UPDATE`.
The PostgreSQL server itself moved from 17.6 to 17.11 in place, which is a patch release within the same major and needs no action.

### Is the extension version pinned by the vendor?

No.
Upstream hindsight's compose files use `pgvector/pgvector:pg${HINDSIGHT_DB_VERSION:-18}`, an unpinned tag that takes whatever pgvector ships for that PostgreSQL major.
Upstream expresses no opinion about the extension version at all.

Hindsight creates the extension with a bare `CREATE EXTENSION vector` in `_ensure_pgvector_extension_in_public`, naming no version, and never runs `ALTER EXTENSION vector UPDATE`.
It checks only that the extension exists and lives in the `public` schema.
So the extension version is whatever was current when the database was first initialized, and nothing upstream will ever move it.

This repository's own requirement is a floor, not a pin.
The comment in `homelab/hindsight/hindsight.yaml` states that pgvector 0.8.0 or later covers the one documented feature floor, iterative scans.
That is accurate: hindsight sets the `hnsw.iterative_scan` and `hnsw.max_scan_tuples` settings, and `hnsw.iterative_scan` arrived in pgvector 0.8.0.
The installed 0.8.1 clears that floor.

### Should the `ALTER` be applied?

It changes nothing functional today, because every pgvector release from 0.8.2 to 0.8.6 is a fix inside the C library:

- 0.8.2 fixed a buffer overflow with parallel HNSW index builds.
- 0.8.3 fixed possible index corruption with HNSW vacuuming.
- 0.8.4 fixed an `hnsw graph not repaired` error, an insert error during HNSW vacuuming, and memory exceeding `maintenance_work_mem`.
- 0.8.5 and 0.8.6 reduced memory usage and fixed an IVFFlat buffer overflow on 32-bit systems.

None of them adds a SQL function, type, operator or index method.
pgvector's own upgrade scripts confirm it: `vector--0.8.0--0.8.1.sql` through `vector--0.8.5--0.8.6.sql` are each 153 bytes, containing only the standard psql guard line and no statements.

The practical consequence is that the estate already has those fixes.
They live in the shared library, which was replaced when the container image was replaced, so the HNSW vacuuming corruption fix in 0.8.3 is active now.
`extversion` is a catalog label recording which SQL definitions are installed, and running the `ALTER` would relabel 0.8.1 to 0.8.6 and execute nothing.

### Why it is still worth doing

`vector--0.8.6--0.8.7.sql` is 11,833 bytes.
The next release does add SQL objects.
Once 0.8.7 ships, a database still labelled 0.8.1 needs an `ALTER` that replays five empty scripts and then a real one, and anybody looking at the version gap has to redo this analysis to know whether it is safe.
Keeping the label current makes the eventual real upgrade a single visible step.

**CLOSED on August 28, 2026.**
`ALTER EXTENSION vector UPDATE` was run against the `hindsight` database and the extension now reports `installed=0.8.6 available=0.8.6`.
The canary passed afterwards, with `retain_http=200`, `recall_http=200` and one result.
The step is now part of the upgrade runbook in `docs/operations/hindsight.md`, as step 5, merged in pull request 86.

Nothing remains to do here.
The heading is kept so the next reader does not repeat the research.

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

## 9. Pull request 75 needs a database upgrade flag, and was left open

Pull request 75 moves `getmeili/meilisearch` from `v1.41.0` to `v1.53.1` in `vps/workloads/karakeep.yaml`.
This session applied and merged every other Renovate pull request and left this one open.

**Why it is not a tag edit.**
Meilisearch does not upgrade its own database across that distance on startup.
Its documentation states that `--upgrade-db` performs an in-place upgrade and has existed since v1.51, and that `--experimental-dumpless-upgrade` is the equivalent for earlier targets.
Without one of them, the upgrade path is a dump and a restore.
The Deployment sets no `args` and no `command`, so applying the bump as it stands starts a v1.53.1 binary against a v1.41.0 index with no upgrade instruction.
The likely result is a Meilisearch that will not start, which takes karakeep's search offline until the tag is reverted.
The volume is a `PersistentVolumeClaim`, so a failed start does not destroy the index.

**A second reason, already written into the manifest.**
The container carries a comment warning that from v1.43.0, a successful `POST /tasks/compact` puts `/health` into a `mustRestart` state, and instructing the reader to re-check upstream before accepting a bump past that version.
This session checked.
Meilisearch pull request 6346, merged on April 22, 2026, made that change deliberately, so that Meilisearch Cloud restarts an instance after compaction.
It is intended behavior and there is nothing upstream to wait for.

Nothing in this repository calls `/tasks/compact`, and the endpoint is an administrative operation rather than something karakeep issues, so the estate does not trigger it today.
The manifest's comment predicts a "liveness restart loop", and that reading is worth revisiting: the flag is reset when the process restarts, so a compaction produces one restart, which is what upstream intends the probe to do.
A loop needs compaction to re-run on every start, which nothing here does.

**The decisive constraint is karakeep, not the index.**
The operator's ruling during this session is that losing the Meilisearch index is not serious, because karakeep rebuilds it, and the index holds a few thousand pages rather than a large corpus.
So neither a dump nor a restic snapshot gates this bump.
What matters is whether karakeep supports the Meilisearch version.
It does not.
`docker/docker-compose.yml` on karakeep's default branch pins `getmeili/meilisearch:v1.41.0`, which is exactly the version this estate runs.
The current pin matches the pairing karakeep tests against, and v1.53.1 is untested by upstream.

**Proposed fix.**
Leave pull request 75 open and take the bump when karakeep moves its own pin.
Do not close it unmerged, which is unsupported and snoozes the update-watch monitor.

Add a `packageRules` entry in `renovate.json` holding `getmeili/meilisearch` until karakeep's compose file moves, so the pull request stops reappearing every session.
Record the reason in the rule, because a version pin without a stated reason gets bumped by the next reader.

When the version does move, the upgrade still needs a mechanism.
Meilisearch will not convert a v1.41 index on startup: it needs a one-time `--upgrade-db` argument on the container, removed in a follow-up once the index is converted.
Rebuilding the index from karakeep is the alternative, and the operator has said that is acceptable.

Correct the manifest comment at the same time: it says the workload is pinned to v1.41.0 and predicts a restart loop, and both halves need rewriting once the version moves.

## 10. A restic image bump fails the apply while the `restic-init` Job still exists

Pull request 76 moved `restic/restic` from `0.17.3` to `0.19.1` in four files, two of which are `restic-init-job.yaml` on each cluster.
`make apply-homelab` failed:

    The Job "restic-init" is invalid: spec.template: Invalid value: {...}: field is immutable
    make: *** [Makefile:723: apply-homelab] Error 2

A Job's `spec.template` is immutable, and `restic-init` carries `ttlSecondsAfterFinished: 86400`.
So for the 24 hours after any apply recreates it, a second apply that changes its image fails on that resource.
Every other resource applied, and the CronJob image moved, so the cluster was left half-updated until the Job was deleted and the apply repeated.

This session hit it because it applied twice in one afternoon: the first apply, for pull request 58, recreated `restic-init` at `0.17.3`, and pull request 76 then tried to change it inside the TTL window.
A session spaced four to six weeks apart usually finds the Job already collected and never sees this.
That is what makes it worth writing down: it appears only when the estate is being worked hard, which is exactly when a half-applied tree is least likely to be noticed.

`AGENTS.md` documents the failure mode and names deleting the stale Job as the recovery.
The skill does not mention it, and the skill is what an agent reads during the session.

**Proposed fix:** add a step to the skill's Step 2 loop.
Before applying a pull request that changes any `*-init-job.yaml`, check for a live Job with the old spec and delete it if it has completed:

    kubectl -n backup get job restic-init -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image
    kubectl -n backup delete job restic-init

Deleting a Job that reports `Complete=True` is safe here, because `restic-init.sh` probes the repository before initializing and a re-run is a no-op.

## 11. The local-path provisioner base now ships probes the repository's own rule rejects

Pull request 72 moved the VPS local-path provisioner base from `v0.0.31` to `v0.0.37`.
The release notes named no behavior change, but the rendered diff added three probes to the provisioner Deployment: a liveness, a readiness and a startup probe, each with `timeoutSeconds: 1`.

`AGENTS.md` states that every probe must set `timeoutSeconds`, because the one-second default produces false positives on a loaded node.
The upstream base sets it explicitly to that same one second, which satisfies the letter of the rule and defeats its purpose.
`AGENTS.md` also restricts liveness probes to cases where a restart is a safe remedy, and notes that everything here is single-replica, so an over-eager liveness probe manufactures an outage.

The provisioner is stateless, so a restart is safe, and the rollout was clean with no restarts.
The exposure is a one-second timeout on a small shared VPS: a slow response makes the liveness probe restart the only storage provisioner on the cluster, which stalls volume provisioning.

**Proposed fix:** decide whether to accept upstream's probes or patch them.
The kustomization already carries patches for the helper-pod hostPath volumes, so raising `timeoutSeconds` on the three probes is a small addition in the same file.
Whichever is chosen, write the reason down, because the next base bump will present the same diff and the reader needs to know it was considered.

A second, smaller point from the same pull request: the helper-pod image inside the ConfigMap changed from `busybox` to `docker.io/library/busybox`.
It is still an untagged reference, which means `:latest`, and it is embedded in a block scalar where `check-renovate-scope` cannot see it at all.
That is pre-existing and documented, not caused by this bump.

## 12. cert-manager was taken to v1.20.3, not v1.21.1 — a decision to revisit

Step 3 bumped cert-manager from v1.20.2 to v1.20.3 rather than to v1.21.1, which is the latest release.

Both carry the same security fix.
`GHSA-8rvj-mm4h-c258`, rated HIGH, is that the default `cert-manager-edit` aggregate ClusterRole let a namespace user create ACME `Challenge` and `Order` resources directly, supplying attacker-controlled solver configuration while cert-manager loaded credentials from the ClusterIssuer's namespace.
That bypasses the Issuer's solver selectors.
Upstream says all users should upgrade.

v1.21.1 adds three known open issues, listed in its own release notes:

1. The controller crash-loops on any Certificate that sets `spec.renewal.policy: Disabled`, through a nil pointer dereference in the trigger controller.
2. An Issuer or ClusterIssuer that references a solver Secret which does not yet exist reports `Ready: False` with reason `InvalidSolver` and never self-corrects when the Secret appears, until a 10-hour resync, a spec change, or a controller restart.
3. Log spam from type assertion failures on every non-cert-manager-labelled Secret event, multiplied by seven sub-controllers. Cosmetic.

None of the three bites this estate as it stands.
No Certificate sets a renewal policy, that field is new in v1.21.
The `route53-credentials` Secret exists, so the solver validation passes.
The third is only noise.

The reason for staying on the patch is the blast radius rather than the probability.
This cluster has exactly one certificate, the `*.cynexia.net` wildcard that Traefik serves as its default, so cert-manager failing takes out TLS for every homelab service at the next renewal.
The patch delivers the security fix with no new known issues.

**Proposed fix:** take v1.21 at the next session, once the crash-loop in issue 9031 is closed upstream.
Check that issue first.
If it is still open, staying on the 1.20 line is still the better trade, and there will be a v1.20.4 by then.
