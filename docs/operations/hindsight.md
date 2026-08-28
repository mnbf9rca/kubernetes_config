# Hindsight: the memory backend for the Hermes profiles

Hindsight is a self-hosted memory store.
The Hermes agent profiles on VM 103 send it what they learn and ask it what they know; it extracts memories with a cheap API model and retrieves them with embeddings and a reranker that run locally, inside the cluster.
It lives in its own `hindsight` namespace on the homelab cluster, reachable only from the LAN and Tailscale.

Everything here assumes the `cynexia-homelab` kubectl context.
The manifests are `homelab/hindsight/`; the secrets are `homelab/secrets/hindsight.yaml`.

## What is running

| Piece | Where | Notes |
|---|---|---|
| API, with the worker in-process | `deploy/hindsight`, container `api`, port 8888 | The **full** image, not `-slim`: it bundles the embedding and reranking models, so recall never leaves the cluster |
| Control plane (admin UI) | the same Pod, container `control-plane`, port 9999 | Optional by design. It talks to the API over `localhost`, so the hop never crosses the pod network |
| PostgreSQL with pgvector | `deploy/hindsight-postgres`, port 5432 | Version-pinned. Its data directory is on a `local-path` PVC |
| Nightly dump | `cronjob/hindsight-pg-dump`, 02:15 UTC | The recovery artifact |
| Canary | `cronjob/hindsight-canary`, hourly | The only thing that notices a broken write path |

Two hostnames, both resolving to the LAN address `10.100.0.100` in Route53: `hindsight.cynexia.net` is the API — what Hermes talks to — and `hindsight-ui.cynexia.net` is the admin console.
Neither is exposed publicly, and neither should become so through Traefik: if that is ever wanted, the precedent in this estate is a Cloudflare Access app on a dedicated tunnel.

## Why it is built this way

**Recall works when the internet does not.**
The full image runs `BAAI/bge-small-en-v1.5` and `ms-marco-MiniLM-L-6-v2` on the CPU, so searching memory is pure local work.
Only *writing* a memory touches the paid model.
A revoked API key or a provider outage therefore degrades this to a read-only memory rather than to no memory at all — which is why the extra several gigabytes of image are worth carrying.

**Authentication is on, from the first apply.**
Hindsight's default is no authentication at all and it has no per-bank access control, so an unauthenticated server would let any device on the LAN or the tailnet read and rewrite every memory.
The API validates a tenant API key; the control plane presents the same key to the API and is itself behind its own access key, because it is a full read/write admin console — retain, delete, bank delete, import and export — and not a viewer.

**Isolation between profiles is logical, not cryptographic.**
Every operation is scoped to a bank, and banks never share memories, so the `emh` profile writing to `hermes-emh` cannot reach another profile's `hermes-<name>`.
But one tenant key spans every bank.
That is accepted because every profile belongs to the same operator.
If that ever stops being true, the escalation is a custom tenant extension keyed per bank, not a Traefik middleware.

**Nothing auto-updates.**
Images are pinned and the namespace carries no keel annotations; Renovate opens a grouped "hindsight stack" pull request instead.
An unattended migration at 3 a.m. against the store holding an agent's memory is the failure this design exists to make impossible, and Hindsight's migrations are **forward-only** — so the pre-upgrade dump is the rollback, and there is no other one.

## Upgrading

```sh
make hindsight-upgrade
```

That is the whole automated half.
It asserts the context and the CronJob, refuses if a dump is already running, creates a Job from the dump CronJob, waits up to 15 minutes, and prints what to do next.
It never edits a pin, never merges and never applies — chaining into `make apply-homelab` would apply every pending change in the tree, unreviewed.

The Job's completion **is** the verification.
The dump script publishes an artifact only after asserting a `CREATE TABLE` count of at least one and a byte-size floor, because `pg_dump` exits 0 against an empty database, so the exit code alone is a lie.
The target adds no second, weaker copy of that assertion.

Then, by hand:

1. Merge the Renovate "hindsight stack" pull request, then `git pull`.
2. `make diff-homelab` — **read it**, and confirm only the image lines moved.
3. `make apply-homelab`.
4. `kubectl -n hindsight rollout status deploy/hindsight --timeout=600s`.
5. Watch the startup probe settle, then run `hermes memory status` on VM 103.
6. **Prove one Hermes memory write still lands.**
   The canary proves the *server*; it says nothing about the agent's client, which is a different library talking to the same API.
   On VM 103, take one chat turn against the default gateway — the command is in [the update runbook's Verify step](hermes-vm-updates.md#verify) — then read that gateway's journal for the write behind it:

   ```sh
   journalctl --user -u hermes-gateway --since '10 min ago' | grep -i hindsight
   ```

   A `401`, a timeout or a schema complaint here, with the canary green, points at the client rather than the server.
   The write is on a background path the chat response does not wait for, so a 200 from the chat turn proves nothing on its own.

**Server and client move independently, and the skew is accepted.**
The VM's `hindsight-client` is never pinned by this estate — see [The client on the hermes VM](#the-client-on-the-hermes-vm) — so a server bump moves the server alone, and the client moves only when hermes-agent's own pin moves.

Keep the API and control-plane images on the **same** version tag: Renovate groups them, and a skewed pair is a combination nobody has tested.
Keep the API pin at or above **0.9.1** forever — the liveness probe uses `/health/live`, which does not exist below it, and 0.5.0+ is what makes the canary's fixed sentinel deduplicate instead of growing the bank.

**PostgreSQL major versions are not a tag edit.**
Renovate refuses them for this tree, because a grouped pull request quietly carrying one would be a data-loss trap that `make hindsight-upgrade` could not catch: the dump would succeed, and the new major would refuse the old data directory.
A major is a dump, a fresh volume and a restore, planned deliberately.

### If the upgrade goes wrong

The dump is the rollback.
Restore it (below) and pin the image back to the previous tag in the same commit.

## The client on the hermes VM

**Ruling, August 27, 2026: `hindsight-client` on VM 103 is never pinned by this estate.**
The version installed there is whatever hermes-agent's own `pyproject.toml` asks for, and this repository neither records it nor moves it.
Nothing on the VM reads the server's reported version to choose a client, and no file here carries a client pin to keep in step with the server tag.

Four facts support that:

1. **Upstream's `hindsight-client==0.6.1` is a supply-chain policy, not a compatibility statement.**
   NousResearch exact-pin every dependency and age each one 14 days before adopting it.
   The pin says "this version has been vetted", not "this version is what the server needs".
2. **The wire surface is additive across the gap.**
   Comparing 0.6.1 against 0.9.x, all 49 paths 0.6.1 calls are still present and the authentication scheme is unchanged.
   Nothing the old client sends has been removed or renamed.
3. **The plugin's version check is a floor, not an equality.**
   It refuses a client below its minimum and accepts anything at or above it, so a client older than the server is a supported configuration rather than an accident.
4. **The server is the side that must stay current, and it does.**
   The homelab deployment is under Renovate, keeps its API and control-plane images on the same tag, and holds the 0.9.1 floor.
   Client-to-server skew is therefore bounded by the server moving forward, and it is accepted.

**What would overturn this.**
Any one of the four failing: the upstream pin moving for a stated compatibility reason rather than an aging one; a release removing or renaming a path the installed client calls; the authentication scheme changing; or the plugin raising its floor above the client the agent installs.
The change-analysis step of [the update runbook](hermes-vm-updates.md#change-analysis) reads the `pyproject.toml` diff on every run, which is where a pin move shows up first.

**Bumping upstream's pin is the operator's, by hand.**
The pull request goes to NousResearch under the operator's name, so no agent opens it.
This estate carries no local override in the meantime.

## Restoring

```sh
kubectl -n hindsight scale deploy/hindsight --replicas=0
```

Take a fresh belt-and-braces dump first if the database is still up — `make hindsight-upgrade` is exactly that, and it costs a minute.
Then pick the artifact:

```sh
kubectl -n hindsight exec deploy/hindsight-postgres -- ls -1 /dumps
```

Dumps are named `hindsight-<UTC timestamp to seconds>.sql.gz`, so the newest name is the newest dump and lexical order is chronological order.
The seven newest **artifacts** are kept, not the seven newest days: a day with three runs consumes three slots, which is correct, because each is a distinct restore point.

If the dump is no longer on the PVC, `restic restore` it from B2 first — the gate's path for it is `/data/pvc-*_hindsight_hindsight-dumps/hindsight-*.sql.gz`.

```sh
gzip -dc hindsight-<timestamp>.sql.gz \
  | kubectl exec -i -n hindsight deploy/hindsight-postgres \
      -- psql -U hindsight -d hindsight -v ON_ERROR_STOP=1
```

The dump was taken with `--clean --if-exists`, so it drops and recreates its own objects and needs no separate drop step.
`ON_ERROR_STOP=1` is not optional: without it a mid-restore error scrolls past and leaves a silently partial database that looks restored.

```sh
kubectl -n hindsight scale deploy/hindsight --replicas=1
```

Migrations reconcile on startup.
Then verify, in this order:

1. `kubectl -n hindsight get pods` — both containers Ready.
2. The `hindsight-canary` monitor goes UP on the next run (or force one: `kubectl -n hindsight create job --from=cronjob/hindsight-canary now-$(date -u +%s)`; a pod log with no `kuma: push not delivered` line already proves the heartbeat landed).
3. `hermes memory status` on VM 103.
4. Spot-check a recall in the control plane at `hindsight-ui.cynexia.net`.

### The restore drill

**Nothing in the nightly checks proves a dump restores.**
The restic gate proves it exists, is fresh, is above a size floor and contains at least one `CREATE TABLE` — all shape assertions.
The only thing that proves restorability is restoring, so do it on purpose, roughly quarterly, into a scratch database rather than the live one:

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

A table count in double figures is the pass.
Zero, or a `psql` that stopped on an error, means the artifact the gate has been calling healthy is not a recovery point.
Record the date of each drill in the pull request that notes it, so "when was this last checked" has an answer.

## Rotating the tenant API key

The tenant key is one value with four consumers: the API validates against it, the control-plane container presents it to the API, the canary authenticates with it, and the Hermes profiles on VM 103 send it on every request.

**It is deliberately stored twice. The duplication is the vault-visibility split, not an accident.**

| Vault item | Read by | How |
|---|---|---|
| `op://Homelab/hindsight/tenant-api-key` | The cluster apply, from the operator's laptop | `${HINDSIGHT_TENANT_API_KEY}` in `homelab/secrets/hindsight.yaml`, resolved by `op run` at apply time |
| `op://hermes/hindsight/tenant-api-key` | The three gateways on VM 103 | hermes's own 1Password secrets provider, resolved into each gateway process's environment at startup |

The VM's 1Password service account can see the `hermes` vault and nothing else, so it cannot read the `Homelab` copy; the operator's laptop credential reads either.
Both copies and the live cluster Secret held the same value when they were last compared, on August 27, 2026.
**Nothing keeps them in step.**
No guard, job or apply compares them, so a rotation that updates one item and misses the other splits the key silently — which is why step 4 below checks the outcome rather than trusting the edit.

Rotating means:

1. Generate a new value and update **both** vault items in the table above.
   Updating one splits the key, and the side that was missed stops authenticating at its next restart.
2. `make apply-homelab`, then `kubectl -n hindsight rollout restart deploy/hindsight` — a Secret change does not restart a Pod on its own.
3. **Restart the three gateways on VM 103** — `hermes-gateway`, `hermes-gateway-emh` and `hermes-gateway-hal`.
   The provider resolves the reference once, at process start, so a running gateway keeps presenting the old key indefinitely.
   The key is in no file on the VM, so there is nothing to edit.
4. **Confirm the outcome, not the inputs.**
   After the restart, take one chat turn and read the gateway's journal for a Hindsight write — a `401` there means the VM copy did not get the new value:

   ```sh
   journalctl --user -u hermes-gateway --since '10 min ago' | grep -i hindsight
   ```

   A write that lands is proof the whole path agrees, which comparing the two vault items would not give you.
   If you want the inputs anyway, compare **truncated digests** and never the raw values — `op read '<reference>' | tr -d '\n' | shasum -a 256 | cut -c1-12` on each side, the `tr -d '\n'` because `op read` appends a newline and the cluster Secret's YAML scalar does not.
5. **Re-run the smoke test.**
   Tell a profile a fact, start a new session, ask for it back.

Step 5 is not optional and is the whole reason this section exists.
Hermes fails open: with a stale key it keeps working, injects no memories, and drops every retain with a log warning nobody reads.
The canary catches a server that stopped accepting the new key within about 90 minutes — but the canary uses the key from the cluster Secret, so it cannot see a VM still sending the old one.
Only the smoke test can.

The same applies to the control-plane access key, minus the VM: rotate `op://Homelab/hindsight/cp-access-key`, apply, restart, log in again.

## Wiring a Hermes profile

Established against the live install on 2026-08-24, and corrected the same day when the first attempt proved wrong.

**Read [Hermes VM configuration layout and secrets](homelab.md#hermes-vm-configuration-layout-and-secrets) first.**
That section is the single copy of the contract every Hermes plugin and profile on VM 103 obeys: config resolves per `HERMES_HOME` and so per profile, the whole file wins with no merging, the file value beats the environment, secrets go through hermes's 1Password integration and must name the `hermes` vault, the dashboard GUI is not the writer of record, `hermes tools disable memory` strips a provider's tools, and a tool listing is not evidence that a plugin's tools are absent.
Everything below is what hindsight adds to it.

### Where the hindsight plugin config lives

The plugin loads the first of these three that exists:

| Order | Source | Notes |
|---|---|---|
| 1 | `$HERMES_HOME/hindsight/config.json` | The profile's own file. For `emh`, `~/.hermes/profiles/emh/hindsight/config.json` |
| 2 | `~/.hindsight/config.json` | Legacy shared path. Note the leading dot: this is **not** `~/.hermes/hindsight/` |
| 3 | Environment variables | Defaults only, reached when neither file exists |

Two keys additionally accept an environment variable as a **per-key fallback**: `api_key` from `HINDSIGHT_API_KEY`, and `api_url` from `HINDSIGHT_API_URL`.
The fallback applies only when the key is absent from the file, because the file value wins over the environment.
Upstream documentation claims the reverse; the code reads the file first.
Trap 3 depends on this detail.

`bank_id_template` is unchanged by any of the above: `{profile}` expands to the profile name at run time, so `emh` lands in `hermes-emh`.
Every profile can carry a byte-identical config file, which is the point — the file is per profile, but no value inside it differs.

Set the tenant key as a profile-scoped secret:

```sh
hermes -p emh secrets onepassword set HINDSIGHT_API_KEY "op://hermes/hindsight/tenant-api-key"
```

The `hermes` vault is the only one the VM's service account can see, and [Rotating the tenant API key](#rotating-the-tenant-api-key) covers the two-copy design that constraint forces.
**The reference resolves once, at process start**, which is why adding or changing one does nothing until that gateway is restarted.

The per-profile config:

```json
// ~/.hermes/profiles/emh/hindsight/config.json — one per profile, identical contents
{
  "mode": "local_external",
  "api_url": "https://hindsight.cynexia.net",
  "bank_id_template": "hermes-{profile}",
  "recall_budget": "mid",
  "timeout": 30
}
```

Note the absent `api_key`: it belongs in the 1Password-backed environment variable, and trap 3 explains why leaving it out is mandatory rather than tidy.

The 30-second timeout is deliberate, against the 120-second cloud default: the server is on the LAN, so a longer value would only delay noticing that it is gone.

**The extraction key never lands on VM 103.**
In `local_external` mode the plugin makes plain HTTP calls and the `llm_*` client settings are dead; extraction happens server-side, from the key in the cluster Secret.

To activate the provider, run `hermes config set memory.provider hindsight`, then check with `hermes -p emh memory status` (expect `Provider: hindsight`, plugin installed, status available).
Treat that status as weak evidence: upstream issue #80388 records `memory status` reporting available while the runtime path fails, because the two use different predicates.
The canary and trap 2's call test are the real proof.

### Trap 1: never run `hermes tools disable memory`

The hindsight integration guide instructs you to run it.
Do not.
The `memory` toolset gates every memory provider's tools alongside the built-in one, so running the command strips `hindsight_retain`, `hindsight_recall` and `hindsight_reflect` from the agent.

Confirmed live on 2026-08-24: before `hermes -p emh tools enable memory --platform cli` the three tools were absent; after it, `hindsight_recall` executed and returned a memory retained in an earlier session.
This is upstream hermes-agent issues #30979 and #46108, both unfixed — the candidate fix, PR #30991, is open and unmerged.

Suppress the built-in memory tool through the profile's `config.yaml` as the shared contract describes, then confirm the toolset is enabled on every platform you use:

```sh
hermes -p emh tools enable memory --platform cli
hermes -p emh tools --summary
```

### Trap 2: `/tools` cannot prove the tools are missing

Provider tools are appended to the agent at session start and never appear in `/tools` or `hermes tools list`, whether or not they work.
PR #30991 would add that visibility and is unmerged.

Verify by use, not by listing:

```sh
hermes -p emh chat -q "Call the hindsight_recall tool with the query 'test' and show me the raw result."
```

A returned result proves registration.
A reply that no such tool exists is the only meaningful negative.

### Trap 3: the dashboard GUI writes secrets to disk in cleartext

The field the dashboard **fails to save** is the API server URL: it reports saved and silently is not (observed 2026-08-24).
Set `api_url` by editing the file, then restart the gateway (`systemctl --user restart hermes-gateway-<profile>`).

What it **saves too much** of is the API key: the literal value lands in the profile's `hindsight/config.json` as cleartext, which defeats the 1Password integration and puts a live credential into the nightly backup zip.
Delete `api_key` from the file and let `HINDSIGHT_API_KEY` supply it.
Deleting is required, not cosmetic — the file value wins over the environment, so a stored key shadows the 1Password-backed variable for as long as it remains.

Re-check after any GUI session, on every profile:

```sh
grep -c api_key ~/.hermes/profiles/emh/hindsight/config.json   # expect 0
```

A non-zero count means a cleartext credential is on disk: remove it, restart the gateway, and rotate the key if the file has been backed up since.

### Adding a second profile

Adding a profile takes four steps, because the config file is per profile:

1. Copy the config file to `~/.hermes/profiles/<name>/hindsight/config.json`.
   The contents are identical every time — `bank_id_template` resolves `hermes-<name>` at run time, and banks auto-create on first write.
2. Set the profile-scoped secret, from the `hermes` vault: `hermes -p <name> secrets onepassword set HINDSIGHT_API_KEY "op://hermes/hindsight/tenant-api-key"`
3. Enable the provider for that profile, then confirm it with trap 2's call test.
4. **Configure the new bank's missions — do not skip this.**
   A fresh bank inherits memory defense from the server's default bank template, but its three missions (`retain_mission`, `reflect_mission`, `observations_mission`) and dispositions start empty, and upstream's guidance calls misconfigured missions the single biggest cause of low-quality memories.
   Set them in the control plane UI once the bank exists, using `hermes-emh`'s as the pattern: name the fact types to extract AND what to ignore; keep trend conclusions but never raw readings (measurements are queryable live at the health-data MCP server); give reflect the agent's persona plus an accuracy-over-inference rule; give observations the durable-versus-transient distinction and contradiction flagging.
   Tailor the domain content to the new agent's role — EMH's health focus does not transfer to an ops agent.

Before onboarding a profile, run the two-bank isolation test once — retain into `probe-a`, recall from `probe-b`, expect nothing — then delete both probe banks from the control plane.

## Monitoring

Two uptime-kuma **push** monitors, both driven from an EXIT trap — `up` on exit 0, `down` otherwise: `hindsight-pg-dump` (86400s interval, one retry at 7200s) and `hindsight-canary` (3600s interval, one retry at 1800s).
They replaced two healthchecks.io checks of the same names on August 26, 2026; tokens are in the `hindsight` 1Password item.
There is no start signal — the push API has none — so `activeDeadlineSeconds` is the whole of the hang bound and the monitor's interval plus retry is the silence bound.
The roster is in [uptime-kuma.md](uptime-kuma.md#push-monitors) and the reasoning behind the canary is in [monitoring.md](monitoring.md#healthchecksio-checks).

**`/health/live` has no consumer outside the cluster.**
It used to: the deleted update wrapper on VM 103 read its `version` field to choose a `hindsight-client` to install.
That went with the wrapper, and the client is no longer pinned from here at all — see [The client on the hermes VM](#the-client-on-the-hermes-vm).
The remaining callers are the Deployment's own liveness probe and the canary, both in-cluster, which is why the endpoint is unauthenticated.
Keep the API pin at or above 0.9.1 for the probe's sake, not the VM's.

An uptime-kuma **HTTP** monitor could not do the canary's job: kuma runs on the VPS, which has no route to any `*.cynexia.net` address.
A **push** monitor reverses the direction — the canary pod calls outward to `uptime.cynexia.com` through the Access bypass — which is why the reporting side could move to kuma while the probing side could not.

Read `verdict=` in a canary failure heartbeat first:

| `verdict=` | What broke |
|---|---|
| `retain-failed` | The API rejected the write. A rotated or mistyped tenant key, a dead API, an unreachable database, or a contract change after a bump |
| `recall-failed` | The write landed but the search call errored |
| `recall-miss` | Both calls succeeded and the search returned nothing. The retrieval side — embeddings, the reranker, or an emptied bank |

And in a dump failure heartbeat:

| `verdict=` | What broke |
|---|---|
| `dump-failed` | `pg_dump` itself. Usually the database being down or the password being wrong |
| `empty-dump` | The dump ran and produced nothing worth keeping — no `CREATE TABLE`, or below the size floor. It was **not** published, deliberately |

Silence on either monitor is a failure too, and that is the point of the dead-man's-switch: a cluster that is down runs no CronJob, so it pushes nothing at all and the monitor goes DOWN at its interval plus retry.
The full detail behind each verdict — the table counts, the byte sizes, the two HTTP statuses — is on the pod log's `detail:` line; the one-line heartbeat carries the verdict and one or two numbers.

**One profile once wrote nothing for four days, and no check here noticed.**
From August 23 to August 27, 2026 the `default` profile's memory writes all returned `401 Invalid API key`: its gateway had started the day before `~/.hermes/config.yaml` gained the `HINDSIGHT_API_KEY` reference, so the plugin initialised with an empty key and kept it until the process was restarted — `emh` and `hal`, whose gateways started after the reference landed, were unaffected throughout.

Nothing detected it, and that gap is the part worth keeping: the canary authenticates with the cluster's own copy of the tenant key and passed throughout, a chat turn returns 200 because the write is on a background path the response does not wait for, and `/health` checks database connectivity rather than auth validity.
**A profile that has retained nothing looks identical to a healthy one from every check in this estate.**
Only the journal grep would have caught it, and that is now a step of [the update runbook's Verify](hermes-vm-updates.md#verify).

If a 401 appears again, restart that profile's gateway first — a reference the running process never read is the cheapest explanation.
If it survives the restart, work the key the profile presents against the key the server accepts: [Rotating the tenant API key](#rotating-the-tenant-api-key), then [Trap 3](#trap-3-the-dashboard-gui-writes-secrets-to-disk-in-cleartext), because a key the GUI wrote into `config.json` shadows the 1Password-backed variable until it is removed.

**What none of this catches:** whether the extraction model is producing *good* memories.
The canary proves writes are accepted, not that they were understood.
If memories start coming back subtly wrong, no automated check here will say so.

## Removing it

Nothing else in the estate references hindsight, which was a design goal.

1. Point Hermes back: `hermes config set memory.provider <previous>`, and restore the built-in memory tool by removing the `memory.memory_enabled` and `memory.user_profile_enabled` false flags from each profile's `config.yaml`.
2. Optionally export the banks from the control plane, if the memories are worth keeping in a portable format.
3. Take a final dump and let that night's restic run capture it.
4. Delete, in one commit: `- hindsight` from `homelab/kustomization.yaml`; the `homelab/hindsight/` tree; `homelab/secrets/hindsight.yaml` and its line in `homelab/secrets/kustomization.yaml`; the namespace block in `homelab/bootstrap/namespaces.yaml`; the keel-exception clause naming `hindsight` in `AGENTS.md`; the two `REQUIRED_TARGETS` lines in `scripts/check-ping-bodies.py`; every `HINDSIGHT_*` variable from `.env.tpl` and from **both** Makefile lists (eight of them as of August 2026 — grep rather than counting from here); the `hindsight-upgrade` target and its help line; the hindsight pattern and package rules in `renovate.json`; and the gate entries plus the monitoring.md and uptime-kuma.md rows.
5. `make apply-homelab`, then `kubectl delete namespace hindsight`.
6. Delete both uptime-kuma push monitors and both Route53 records.
7. `local-path` uses `reclaimPolicy: Retain`, so the PV directories survive on the node.
   Delete them by hand once the final restic snapshot is confirmed.
8. Keep **both** 1Password items — `op://Homelab/hindsight` and the VM's `op://hermes/hindsight` — until the dumps have aged out of restic retention, then delete them.

Leave the `df` disk-usage check in the restic gate.
It was added alongside hindsight but it is not about hindsight: it watches the node SSD every workload shares.
