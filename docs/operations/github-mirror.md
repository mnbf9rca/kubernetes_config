# GitHub mirror and recovery

## What it is

Account loss means suspension, deletion or lockout of `mnbf9rca` without warning.
Local Git mirrors and JSON exports preserve enough data to resume development elsewhere without GitHub access.
Organisation and collaborator repositories are outside this backup's scope.

Late-detected compromise means a force-push, branch deletion, injected workflow or history rewrite discovered weeks or months later.
Daily restic snapshots provide a year of recovery history under credentials that cannot permanently delete object versions.
A mirror alone copies the damage on its next run.

## Topology

The `github-mirror` CronJob runs in the VPS `backup` namespace at 01:00 UTC.
Its `mirror` init container uses `ghcr.io/josegonzalez/python-github-backup:latest` for Git mirrors and JSON exports.
Its `restic` container uses `restic/restic:latest` for backup, retention, verification and the heartbeat.
Both containers mount the `github-mirror` local-path PVC at `/data`; its 10Gi request is not a quota.
The pod runs as UID/GID 1999 on `ubuntu-16gb-fsn1-2`, with writable `/tmp`, a six-hour deadline and no concurrent runs.
The restic container sets `RESTIC_CACHE_DIR=/tmp/restic-cache`, so its cache does not need `HOME`.

The existing 04:00 UTC sweep also carries this PVC into the other B2 repository as a free second copy.

## The key model

The job key has `listBuckets,listFiles,readFiles,writeFiles` and is restricted to this bucket.
It can hide versions but cannot permanently delete them.
Its ID and secret are `op://VPS/GitHub/b2-github-mirror-job-key-id` and `op://VPS/GitHub/b2-github-mirror-job-secret`.
The repository location and password are `op://VPS/GitHub/restic-github-mirror-repository` and `op://VPS/GitHub/restic-github-mirror-password`.
The bucket name is `op://VPS/GitHub/b2-github-mirror-bucket`.

The operator key adds `deleteFiles` for recovery from hide markers.
Its secret belongs in a vault the service account cannot read.
Its ID remains at `op://VPS/GitHub/b2-github-mirror-operator-key-id` so the operator can confirm the key matches.
The operator key never enters a cluster Secret or the job environment.

The lifecycle rule uses `daysFromHidingToDeleting: 365`, `daysFromUploadingToHiding: null` and an empty `fileNamePrefix`.
Keep `daysFromUploadingToHiding` null.
A value there would hide live restic packs.
Object Lock is deliberately off.
The `writeBuckets` capability can change the lifecycle rule; the job key does not have it.
The runner never runs `restic init`: a missing repository is a failure, not permission to replace hidden history.

## Layout on the PVC

```text
/data/
  repositories/<name>/repository/       bare Git mirror
  repositories/<name>/wiki/             bare wiki mirror, when available
  repositories/<name>/issues/           JSON issues, comments and events
  repositories/<name>/pulls/            JSON pull requests and related data
  repositories/<name>/releases/         JSON releases and downloaded assets
  repositories/<name>/labels/           JSON labels
  repositories/<name>/milestones/       JSON milestones
  gists/<id>/repository/                bare public-gist mirror
  account/starred.json                  starred repositories
  .status                              mirror result and counters
```

The exporter uses incremental issue and pull-request state beside the JSON files.
Each enumerated repository gets a separate export invocation, so a failed export does not prevent later repositories from running.
The starred list is exported once per run, separately from repository data.
Repositories missing upstream keep their local directories.
Fetch pruning removes refs inside each mirror, so earlier snapshots preserve deleted refs and rewritten history.
Gists are listed through the public endpoint `/users/mnbf9rca/gists` because the PAT has no gist permission.
GitHub offers only read-and-write permission for gists; secret gists are not backed up.
GitHub returns the same Git “not found” error for an empty wiki and one the token cannot read.
The mirror treats both as absent, so a successful run does not prove every wiki was backed up.

## Monitoring

The `vps-github-mirror` uptime-kuma push monitor uses `op://VPS/GitHub/kuma-github-mirror-token`.
It receives `up` on exit 0 and `down` otherwise from the restic runner's EXIT trap.
There is no start heartbeat.
An interval of 86400 seconds and one retry after 7200 seconds detects silence after about 26 hours.
A deadline kill can skip the trap and is detected through silence.
The first export can hit the six-hour deadline; the second night completes it from the incremental checkpoints.

The message carries `verdict=`, `repos=`, `forced_default=` and `deleted_default=`, followed by `failed_step=` on failure.
The mirror's `.status` also carries `gists=`, `mirror_failed=`, `export_failed=` and `export_gone=`.
After a repository export fails, the script checks that repository with an authenticated GET.
A confirmed 404 increments `export_gone`, logs the repository name and does not cause a down by itself.
The `export_failed` counter counts other nonzero exporter exits, including the separate account export, and causes a down.
An unconfirmed disappearance, including a failed follow-up request, remains an export failure.

| Verdict | Meaning |
|---|---|
| `ok` | No mirror or export failures were counted, restic completed, and both default-branch change counters are zero. |
| `enumerate-failed` | Repository/gist enumeration failed, or the owner repository list was empty. |
| `mirror-failed` | A Git mirror failed, or the mirror script caught an unexpected exception. |
| `export-failed` | At least one exporter invocation failed (`export_failed>0`), while Git mirroring succeeded. |
| `restic-failed` | A restic step failed while the mirror verdict was `ok`. |
| `default-branch-changed` | The mirror verdict was `ok`, but the final counter check failed; nonzero counters identify a default-branch rewrite or deletion. |

On a nonzero exit, the runner preserves any mirror verdict other than `ok`.
With an `ok` mirror verdict, failure at `STEP=verdict` produces `default-branch-changed`; failure at an earlier step produces `restic-failed`.
Read `failed_step=` for the failing runner phase and `.status` for the mirror result.
The reader can also emit `no-status` or `unknown` when status input is missing or incomplete; these are diagnostic fallback values.

A `down` with `forced_default>0` means an old default-branch commit is not an ancestor of the new tip, or Git could not prove ancestry.
Treat an unexplained rewrite as a possible compromise.
Read `deleted_default` for missing default-branch refs.
Other branches are not counted.

`github-backup` swallows some export failures, including some failed asset downloads.
Its zero exit code does not prove a complete export; the pod log is the record.
Monitor configuration is in [uptime-kuma.md](uptime-kuma.md#push-monitors).

## Runbook: initialise the repository, once

1. Confirm this is the first initialization, not recovery from hidden objects.
2. Confirm the `github-mirror` Secret exists in the VPS `backup` namespace.
3. Run the one-off initialization pod.

   ```sh
   kubectl --context cynexia-vps -n backup run restic-init-ghmirror --rm -it --restart=Never --image=restic/restic:latest --overrides='{"spec":{"containers":[{"name":"r","image":"restic/restic:latest","command":["restic","init"],"env":[{"name":"AWS_ACCESS_KEY_ID","valueFrom":{"secretKeyRef":{"name":"github-mirror","key":"AWS_ACCESS_KEY_ID"}}},{"name":"AWS_SECRET_ACCESS_KEY","valueFrom":{"secretKeyRef":{"name":"github-mirror","key":"AWS_SECRET_ACCESS_KEY"}}},{"name":"RESTIC_REPOSITORY","valueFrom":{"secretKeyRef":{"name":"github-mirror","key":"RESTIC_REPOSITORY"}}},{"name":"RESTIC_PASSWORD","valueFrom":{"secretKeyRef":{"name":"github-mirror","key":"RESTIC_PASSWORD"}}}]}]}}'
   ```

4. Record the successful initialization date below.

Do not repeat initialization after `restic cat config` reports a missing repository.
Use the unhide procedure for a mass-hide incident.

## Runbook: restore drill

Install restic on the laptop.

```sh
brew install restic
```

The drill uses only the job key for B2 access.
The commands below use Bash and keep secret references in the parent environment; `op run` resolves each child environment.
No privileged B2 key is needed.

1. Restore the latest `kubernetes_config` snapshot into a temporary directory.

   ```sh
   GHM_DRILL=$(mktemp -d)
   AWS_ACCESS_KEY_ID=op://VPS/GitHub/b2-github-mirror-job-key-id \
   AWS_SECRET_ACCESS_KEY=op://VPS/GitHub/b2-github-mirror-job-secret \
   RESTIC_REPOSITORY=op://VPS/GitHub/restic-github-mirror-repository \
   RESTIC_PASSWORD=op://VPS/GitHub/restic-github-mirror-password \
     op run -- restic restore latest --target "$GHM_DRILL/restore" \
       --include /data/repositories/kubernetes_config
   ```

   Restic preserves the restored path below the target directory ([restore reference](https://restic.readthedocs.io/en/stable/050_restore.html)).

2. Clone the restored mirror.

   ```sh
   git clone --no-hardlinks "$GHM_DRILL/restore/data/repositories/kubernetes_config/repository" "$GHM_DRILL/work"
   ```

3. Compare the clone's HEAD with GitHub's current default-branch HEAD.

   ```sh
   printf '%s\n' 'GH_TOKEN=op://VPS/GitHub/PAT' > "$GHM_DRILL/gh.env.tpl"
   GHM_BRANCH=$(op run --env-file="$GHM_DRILL/gh.env.tpl" -- gh api repos/mnbf9rca/kubernetes_config --jq .default_branch)
   GHM_REMOTE_HEAD=$(op run --env-file="$GHM_DRILL/gh.env.tpl" -- gh api "repos/mnbf9rca/kubernetes_config/commits/$GHM_BRANCH" --jq .sha)
   test "$(git -C "$GHM_DRILL/work" rev-parse HEAD)" = "$GHM_REMOTE_HEAD"
   ```

   A newer GitHub commit can cause a legitimate mismatch with the last nightly snapshot.
   Record that difference before judging the drill.
   No scratch push is required.

4. Parse one restored issue and one restored release JSON file.

   ```sh
   python3 -m json.tool "$GHM_DRILL/restore/data/repositories/kubernetes_config/issues/<number>.json" >/dev/null
   python3 -m json.tool "$GHM_DRILL/restore/data/repositories/kubernetes_config/releases/<tag>.json" >/dev/null
   ```

   Replace the two placeholders with files present in the restore.
   Record the drill date, repository and results below.

## Runbook: cutover

Create an empty destination repository.
Set `GHM_MIRROR` to its restored bare mirror directory.
Set `GHM_NEW_REMOTE` to the destination's plain Git URL.
Push the mirror to the new remote.

```sh
git -C "$GHM_MIRROR" push --mirror "$GHM_NEW_REMOTE"
```

GitHub rejects writes to `refs/pull/*`.
For a GitHub destination, push only branches and tags instead.

```sh
git -C "$GHM_MIRROR" push "$GHM_NEW_REMOTE" 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
```

Repeat for each repository.
Keep the JSON exports for reference; Git pushes do not recreate issues, pull requests or release records.

## Runbook: unhide after a mass hide

The master key is required to revoke the compromised job key; the operator key lacks `deleteKeys`.
Use the protected laptop for all recovery steps.

1. Authorize the B2 CLI with the master key at its interactive prompts.

   ```sh
   set +x
   unset B2_APPLICATION_KEY_ID B2_APPLICATION_KEY
   b2 account authorize
   ```

2. Revoke the compromised job key.

   ```sh
   GHM_OLD_JOB_KEY_ID=$(env -u OP_SERVICE_ACCOUNT_TOKEN op read op://VPS/GitHub/b2-github-mirror-job-key-id)
   b2 key delete "$GHM_OLD_JOB_KEY_ID"
   ```

3. Create a replacement job key with the same four capabilities.

   ```sh
   GHM_BUCKET=$(env -u OP_SERVICE_ACCOUNT_TOKEN op read op://VPS/GitHub/b2-github-mirror-bucket)
   GHM_NEW_JOB_KEY=$(b2 key create --bucket "$GHM_BUCKET" github-mirror-job listBuckets,listFiles,readFiles,writeFiles)
   ```

   The command captures the new ID and secret without printing them.

4. Copy the replacement ID to the clipboard.

   ```sh
   printf '%s\n' "$GHM_NEW_JOB_KEY" | awk '{print $1}' | pbcopy
   ```

5. Paste the ID into `op://VPS/GitHub/b2-github-mirror-job-key-id` using the 1Password desktop app.
6. Copy the replacement secret to the clipboard.

   ```sh
   printf '%s\n' "$GHM_NEW_JOB_KEY" | awk '{print $2}' | pbcopy
   ```

7. Paste the secret into `op://VPS/GitHub/b2-github-mirror-job-secret` using the 1Password desktop app.
8. Clear the clipboard.

   ```sh
   pbcopy </dev/null
   unset GHM_NEW_JOB_KEY
   ```

9. Run `make apply-vps` from the deployed branch to update the Secret.

   ```sh
   make apply-vps
   ```

Use the operator key from the laptop, never from a cluster.
Resolve the operator secret with interactive 1Password access, without the service-account token.
The operator secret reference is intentionally outside the VPS vault.
Substitute your own reference for `op://<private vault>/<item>/<field>`.
`env -u OP_SERVICE_ACCOUNT_TOKEN op read` uses the desktop-app session instead of the restricted service account.

10. Set `GHM_OPERATOR_SECRET_REF` to your private-vault reference.
11. Read credentials into a subshell environment.
12. Unhide each listed path.

    ```bash
    (
      set +x
      set -euo pipefail
      B2_APPLICATION_KEY_ID=$(env -u OP_SERVICE_ACCOUNT_TOKEN op read op://VPS/GitHub/b2-github-mirror-operator-key-id)
      B2_APPLICATION_KEY=$(env -u OP_SERVICE_ACCOUNT_TOKEN op read "$GHM_OPERATOR_SECRET_REF")
      export B2_APPLICATION_KEY_ID B2_APPLICATION_KEY
      GHM_BUCKET=$(env -u OP_SERVICE_ACCOUNT_TOKEN op read op://VPS/GitHub/b2-github-mirror-bucket)
      b2 ls --versions --recursive --json "b2://$GHM_BUCKET" \
        | jq -r '[.[] | select(.action == "hide") | .fileName] | unique[]' \
        | while IFS= read -r GHM_PATH; do
            b2 file unhide "b2://$GHM_BUCKET/$GHM_PATH" || echo "unhide failed: $GHM_PATH" >&2
          done
    )
    ```

    B2 CLI caches authentication locally, even when keys arrive through environment variables ([CLI reference](https://b2-command-line-tool.readthedocs.io/en/stable/)).
    Use only the operator's protected laptop for this procedure.
    `b2 file unhide` removes hide markers and requires `deleteFiles`; it does not restore versions already deleted by lifecycle retention.

13. Repeat the loop until it prints nothing.
14. Check the recovered repository with the replacement job key.

    ```sh
    AWS_ACCESS_KEY_ID=op://VPS/GitHub/b2-github-mirror-job-key-id \
    AWS_SECRET_ACCESS_KEY=op://VPS/GitHub/b2-github-mirror-job-secret \
    RESTIC_REPOSITORY=op://VPS/GitHub/restic-github-mirror-repository \
    RESTIC_PASSWORD=op://VPS/GitHub/restic-github-mirror-password \
      op run -- restic check
    ```

    Unhiding can restore legitimately pruned packs, forgotten snapshots and stale locks.
    The next nightly `forget --prune` hides obsolete objects again.

15. Run the restore drill with the job key again.
16. Run `b2 account clear`.

    ```sh
    b2 account clear
    ```

Never run `b2 rm --versions` during recovery.
That command deletes the versions recovery needs.

## Residual threats

- **B2 master key, or any `writeKeys` or `writeBuckets` key, compromised.**
  Either can mint a delete key or shorten the lifecycle rule.
  Those keys live only in 1Password vaults the service account cannot read.
- **Hide, then wait.**
  An attacker holding the job key hides everything on day 0 and it is gone around day 366.
  Detection within the year is load-bearing; because the runner never initialises, the next morning's `restic cat config` fails and the heartbeat goes `down`.
- **Overwrite.**
  `writeFiles` allows uploading a corrupt `config` or index under the same name; the old version is implicitly hidden and recoverable for the same window.
- **Repository password disclosure.**
  The design protects availability, not confidentiality; the password sits in the same Secret as the job key.
- **Export completeness.**
  `github-backup` swallows some failures and exits 0, for example a failed release-asset download.
  The git mirrors are strict; the JSON export is best-effort, a counted export failure produces the `export-failed` verdict, and the pod log is the record of anything it swallowed.
- **Cost attack.**
  The job key can upload but not delete, so a flood bills for a year.
  B2 spending alerts are outside this repo.

## Record

| Check | Record |
|---|---|
| Verification date | 2026-09-20 |
| Repository initialization date | 2026-09-20 |
| First successful run | 2026-09-20, `github-mirror-second`, completed at 16:46:44 UTC |
| First successful run counts | `repos=98 gists=4 mirror_failed=0 export_failed=0 export_gone=0` |
| Default-branch change counts | `forced_default=0 deleted_default=0` |
| Fresh owner-repository API count | 98 on 2026-09-20; matches the successful run |
| Restic snapshots | 2 on 2026-09-20; the failed first attempt also saved a snapshot |
| PVC token scan | `CLEAN` on 2026-09-20 |
| Restore drill date | not yet done |
| Restore drill repository and HEAD comparison | not yet done |
| Issue and release JSON validation | not yet done |

The successful run reported `verdict=ok`, and the restic runner reported `rc=0` with no push-delivery failure.
The failed first attempt counted 115 repositories; the later count of 98 is consistent with the operator's deletions that day.
There is no difference between the successful run's count and the fresh API count.

Compare the first run's `repos=` count with a fresh owner-repository count.
Use the reference-only template created during the drill.

```sh
op run --env-file="$GHM_DRILL/gh.env.tpl" -- gh api --paginate --slurp \
  'user/repos?affiliation=owner&per_page=100' --jq 'map(length) | add'
```

Record the observed counts rather than the design-time inventory.
