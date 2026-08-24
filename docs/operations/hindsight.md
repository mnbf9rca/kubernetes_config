# Hindsight: the memory backend for the Hermes profiles

Hindsight is a self-hosted memory store. The Hermes agent profiles on VM 103 send it
what they learn and ask it what they know; it extracts memories with a cheap API model
and retrieves them with embeddings and a reranker that run locally, inside the cluster.
It lives in its own `hindsight` namespace on the homelab cluster, reachable only from
the LAN and Tailscale.

Everything here assumes the `cynexia-homelab` kubectl context. The manifests are
`homelab/hindsight/`; the secrets are `homelab/secrets/hindsight.yaml`.

## What is running

| Piece | Where | Notes |
|---|---|---|
| API, with the worker in-process | `deploy/hindsight`, container `api`, port 8888 | The **full** image, not `-slim`: it bundles the embedding and reranking models, so recall never leaves the cluster |
| Control plane (admin UI) | the same Pod, container `control-plane`, port 9999 | Optional by design. It talks to the API over `localhost`, so the hop never crosses the pod network |
| PostgreSQL with pgvector | `deploy/hindsight-postgres`, port 5432 | Version-pinned. Its data directory is on a `local-path` PVC |
| Nightly dump | `cronjob/hindsight-pg-dump`, 02:15 UTC | The recovery artifact |
| Canary | `cronjob/hindsight-canary`, every 15 minutes | The only thing that notices a broken write path |

Two hostnames, both resolving to the LAN address `10.100.0.100` in Route53:
`hindsight.cynexia.net` is the API — what Hermes talks to — and
`hindsight-ui.cynexia.net` is the admin console. Neither is exposed publicly, and
neither should become so through Traefik: if that is ever wanted, the precedent in this
estate is a Cloudflare Access app on a dedicated tunnel.

## Why it is built this way

**Recall works when the internet does not.** The full image runs
`BAAI/bge-small-en-v1.5` and `ms-marco-MiniLM-L-6-v2` on the CPU, so searching memory is
pure local work. Only *writing* a memory touches the paid model. A revoked API key or a
provider outage therefore degrades this to a read-only memory rather than to no memory
at all — which is why the extra several gigabytes of image are worth carrying.

**Authentication is on, from the first apply.** Hindsight's default is no
authentication at all and it has no per-bank access control, so an unauthenticated
server would let any device on the LAN or the tailnet read and rewrite every memory.
The API validates a tenant API key; the control plane presents the same key to the API
and is itself behind its own access key, because it is a full read/write admin console —
retain, delete, bank delete, import and export — and not a viewer.

**Isolation between profiles is logical, not cryptographic.** Every operation is scoped
to a bank, and banks never share memories, so the `emh` profile writing to
`hermes-emh` cannot reach another profile's `hermes-<name>`. But one tenant key spans
every bank. That is accepted here because every profile belongs to the same operator. If
that ever stops being true, the escalation is a custom tenant extension keyed per bank,
not a Traefik middleware.

**Nothing auto-updates.** Images are pinned and the namespace carries no keel
annotations; Renovate opens a grouped "hindsight stack" pull request instead. An
unattended migration at 3 a.m. against the store holding an agent's memory is the
failure this design exists to make impossible, and Hindsight's migrations are
**forward-only** — so the pre-upgrade dump is the rollback, and there is no other one.

## Upgrading

```sh
make hindsight-upgrade
```

That is the whole automated half. It asserts the context and the CronJob, refuses if a
dump is already running, creates a Job from the dump CronJob, waits up to 15 minutes,
and prints what to do next. It never edits a pin, never merges and never applies —
chaining into `make apply-homelab` would apply every pending change in the tree,
unreviewed.

The Job's completion **is** the verification. The dump script publishes an artifact only
after asserting a `CREATE TABLE` count of at least one and a byte-size floor, because
`pg_dump` exits 0 against an empty database and the exit code alone is a lie. The target
adds no second, weaker copy of that assertion.

Then, by hand:

1. Merge the Renovate "hindsight stack" pull request, then `git pull`.
2. `make diff-homelab` — **read it**, and confirm only the image lines moved.
3. `make apply-homelab`.
4. `kubectl -n hindsight rollout status deploy/hindsight --timeout=600s`.
5. Watch the startup probe settle, then run `hermes memory status` on VM 103.

Keep the API and control-plane images on the **same** version tag: Renovate groups them,
and a skewed pair is a combination nobody has tested. Keep the API pin at or above
**0.9.1** forever — the liveness probe uses `/health/live`, which does not exist below
it, and 0.5.0+ is what makes the canary's fixed sentinel deduplicate instead of growing
the bank.

**PostgreSQL major versions are not a tag edit.** Renovate is configured to refuse them
for this tree, because a grouped pull request quietly carrying one would be a data-loss
trap that `make hindsight-upgrade` could not catch: the dump would succeed, and the new
major would refuse the old data directory. A major is a dump, a fresh volume and a
restore, planned deliberately.

### If the upgrade goes wrong

The dump is the rollback. Restore it (below) and pin the image back to the previous tag
in the same commit.

## Restoring

```sh
kubectl -n hindsight scale deploy/hindsight --replicas=0
```

Take a fresh belt-and-braces dump first if the database is still up — `make
hindsight-upgrade` is exactly that, and it costs a minute. Then pick the artifact:

```sh
kubectl -n hindsight exec deploy/hindsight-postgres -- ls -1 /dumps
```

Dumps are named `hindsight-<UTC timestamp to seconds>.sql.gz`, so the newest name is the
newest dump and lexical order is chronological order. The seven newest **artifacts** are
kept, not the seven newest days: a day with three runs consumes three slots, which is
correct, because each is a distinct restore point.

If the dump is no longer on the PVC, `restic restore` it from B2 first — the gate's path
for it is `/data/pvc-*_hindsight_hindsight-dumps/hindsight-*.sql.gz`.

```sh
gzip -dc hindsight-<timestamp>.sql.gz \
  | kubectl exec -i -n hindsight deploy/hindsight-postgres \
      -- psql -U hindsight -d hindsight -v ON_ERROR_STOP=1
```

The dump was taken with `--clean --if-exists`, so it drops and recreates its own objects
and needs no separate drop step. `ON_ERROR_STOP=1` is not optional: without it a
mid-restore error scrolls past and leaves a silently partial database that looks
restored.

```sh
kubectl -n hindsight scale deploy/hindsight --replicas=1
```

Migrations reconcile on startup. Then verify, in this order:

1. `kubectl -n hindsight get pods` — both containers Ready.
2. The next canary run pings green (or force one: `kubectl -n hindsight create job
   --from=cronjob/hindsight-canary now-$(date -u +%s)`).
3. `hermes memory status` on VM 103.
4. Spot-check a recall in the control plane at `hindsight-ui.cynexia.net`.

### The restore drill

**Nothing in the nightly checks proves a dump restores.** The restic gate proves it
exists, is fresh, is above a size floor and contains at least one `CREATE TABLE`. Those
are shape assertions. The only thing that proves restorability is restoring, so do it on
purpose, roughly quarterly, into a scratch database rather than the live one:

```sh
kubectl -n hindsight exec deploy/hindsight-postgres -- \
  psql -U hindsight -d postgres -c 'CREATE DATABASE restore_drill'
kubectl -n hindsight exec deploy/hindsight-postgres -- \
  sh -c 'gzip -dc /dumps/$(ls -1 /dumps | grep "^hindsight-" | tail -n 1)' \
  | kubectl exec -i -n hindsight deploy/hindsight-postgres -- \
      psql -U hindsight -d restore_drill -v ON_ERROR_STOP=1
kubectl -n hindsight exec deploy/hindsight-postgres -- \
  psql -U hindsight -d restore_drill -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
kubectl -n hindsight exec deploy/hindsight-postgres -- \
  psql -U hindsight -d postgres -c 'DROP DATABASE restore_drill'
```

A table count in double figures is the pass. Zero, or a `psql` that stopped on an error,
means the artifact the gate has been calling healthy is not a recovery point. Record the
date of each drill in the pull request that notes it, so "when did we last check" has an
answer.

## Rotating the tenant API key

The tenant key is one value with four consumers: the API validates against it, the
control-plane container presents it to the API, the canary authenticates with it, and
the Hermes profiles on VM 103 send it on every request. Rotating it means:

1. Generate a new value and update `op://Homelab/hindsight/tenant-api-key`.
2. `make apply-homelab`, then `kubectl -n hindsight rollout restart deploy/hindsight` —
   a Secret change does not restart a Pod on its own.
3. Update `HINDSIGHT_API_KEY` in **every** profile's `.env` on VM 103.
4. **Re-run the smoke test.** Tell a profile a fact, start a new session, ask for it
   back.

Step 4 is not optional and is the whole reason this section exists. Hermes fails open:
with a stale key it keeps working, injects no memories, and drops every retain with a
log warning nobody reads. The canary will catch a server that stopped accepting the new
key within about 25 minutes — but the canary uses the key from the cluster Secret, so it
cannot see a VM that is still sending the old one. Only the smoke test can.

The same applies to the control-plane access key, minus the VM: rotate
`op://Homelab/hindsight/cp-access-key`, apply, restart, log in again.

## Wiring a Hermes profile

Per profile, using the **native** provider — the standalone `hindsight-hermes` pip
plugin is deprecated, so install nothing. For `emh`, with
`HERMES_HOME=~/.hermes/profiles/emh`:

```json
// ~/.hermes/profiles/emh/hindsight/config.json
{
  "mode": "local_external",
  "api_url": "https://hindsight.cynexia.net",
  "bank_id_template": "hermes-{profile}",
  "recall_budget": "mid",
  "timeout": 30
}
```

```sh
# ~/.hermes/profiles/emh/.env
HINDSIGHT_API_KEY=<value of op://Homelab/hindsight/tenant-api-key>
```

Then `hermes config set memory.provider hindsight` and `hermes tools disable memory` —
with both providers active the model may prefer the built-in one — and check with
`hermes memory status`.

The 30-second timeout is deliberate, against the 120-second cloud default: the server is
on the LAN, so a longer value would only delay noticing that it is gone.

**The extraction key never lands on VM 103.** In `local_external` mode the plugin makes
plain HTTP calls and the `llm_*` client settings are dead; extraction happens
server-side, from the key in the cluster Secret.

**Adding a second profile** is those two files in `~/.hermes/profiles/<name>/` and
nothing else. `bank_id_template` yields `hermes-<name>`, banks auto-create on first
write, and no server-side work is needed. Before onboarding one, run the two-bank
isolation test once — retain into `probe-a`, recall from `probe-b`, expect nothing —
then delete both probe banks from the control plane.

## Monitoring

Two healthchecks.io checks, both pinging on start and on exit code:
`hindsight-pg-dump` (1 day / 2 hours) and `hindsight-canary` (15 minutes / 10 minutes).
The full table, and the reasoning behind the canary, is in
[monitoring.md](monitoring.md#healthchecksio-checks).

Read `verdict=` in a canary failure body first:

| `verdict=` | What broke |
|---|---|
| `retain-failed` | The API rejected the write. A rotated or mistyped tenant key, a dead API, an unreachable database, or a contract change after a bump |
| `recall-failed` | The write landed but the search call errored |
| `recall-miss` | Both calls succeeded and the search returned nothing. The retrieval side — embeddings, the reranker, or an emptied bank |

And in a dump failure body:

| `verdict=` | What broke |
|---|---|
| `dump-failed` | `pg_dump` itself. Usually the database being down or the password being wrong |
| `empty-dump` | The dump ran and produced nothing worth keeping — no `CREATE TABLE`, or below the size floor. It was **not** published, deliberately |

Silence on either check is a failure too, and that is the point of the dead-man's-switch:
a cluster that is down never sends a start ping.

**What none of this catches:** whether the extraction model is producing *good*
memories. The canary proves writes are accepted, not that they were understood. If
memories start coming back subtly wrong, no automated check here will say so.

## Removing it

Nothing else in the estate references hindsight, which was a design goal.

1. Point Hermes back: `hermes config set memory.provider <previous>` and re-enable the
   built-in memory tool, on every profile.
2. Optionally export the banks from the control plane, if the memories are worth keeping
   in a portable format.
3. Take a final dump and let that night's restic run capture it.
4. Delete, in one commit: `- hindsight` from `homelab/kustomization.yaml`; the
   `homelab/hindsight/` tree; `homelab/secrets/hindsight.yaml` and its line in
   `homelab/secrets/kustomization.yaml`; the namespace block in
   `homelab/bootstrap/namespaces.yaml`; the keel-exception clause naming `hindsight` in
   `AGENTS.md`; the two `REQUIRED_TARGETS` lines in `scripts/check-ping-bodies.py`; the
   six variables from `.env.tpl` and from **both** Makefile lists; the
   `hindsight-upgrade` target and its help line; the hindsight pattern and package rules
   in `renovate.json`; and the gate entries plus the monitoring.md rows.
5. `make apply-homelab`, then `kubectl delete namespace hindsight`.
6. Retire both healthchecks.io checks and delete both Route53 records.
7. `local-path` uses `reclaimPolicy: Retain`, so the PV directories survive on the node.
   Delete them by hand once the final restic snapshot is confirmed.
8. Keep the 1Password item until the dumps have aged out of restic retention, then
   delete it.

Leave the `df` disk-usage check in the restic gate. It was added alongside hindsight but
it is not about hindsight: it watches the node SSD every workload shares.
