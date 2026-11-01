# The Hermes VM

VM 103 (`hermes.cynexia.net`) runs the Hermes agent, one multiplexed gateway serving four messaging profiles, the dashboard and the WebUI the Hermex iOS app talks to.
It is not a Kubernetes cluster, so none of this repository's cluster machinery reaches it.
What it has instead lives in `hermes-vm/`: a daily liveness check, a weekly docker-sandbox refresh, a hand-run helper that finishes a new profile's docker setup, the Hal morning runner and publication plugins, the reviewer persona/plugin and closed-report sweep, the managed-scope Hermes config and the `unattended-upgrades` configuration.

This page covers that machinery — how to read it, how to install it — and the VM facts that keep being rediscovered.
Update core, its bundled dashboard, external hermes-webui and plugins together using [Application updates](#application-updates).
There is no update timer or update-estate step.

The canonical copy of every file named here is in `hermes-vm/`.
The VM holds installed copies.
Edit the repository, then reinstall.

**Scheduled work spans VM maintenance, reviewer cleanup and the Hal morning check.**
`unattended-upgrades` patches Debian security-only overnight and reboots at 04:45 UTC; the `hermes-app-alive` **cron job inside the default gateway** pushes a liveness verdict at 05:45 UTC; and `hermes-sandbox-refresh`, a cron job in the same place, replaces stale docker sandbox containers at 05:15 UTC on Sundays. The security_reviewer profile runs its closed-report sweep at 06:15 UTC daily.
Hal’s no-agent house check runs at 09:00 Europe/London; its [runbook](hal-daily-check.md) owns the detailed flow.
Nothing under `hermes-vm/` is scheduled by systemd any more — the `systemd/` directory and its two units were deleted on August 27, 2026.
Application updates never run on a schedule at all.
The `hal` profile also carries four agent-authored systemd timers that this repository does not own — see [Topology: what runs where](#topology-what-runs-where).

## Application updates

“Update Hermes” includes **core, the bundled dashboard, external hermes-webui and plugins**.
Use the upstream `hermes update` route for core; keep its official-install **implicit stable** default.
An official install ahead of stable waits for a later release instead of rolling back.
Do not set an explicit stable channel or advance core with `git pull`.
Update external WebUI on **stable**; its **Update Now** is WebUI-only and `ignore_agent_updates: true` hides agent offers.
There is no scheduled application updater.

Read every intervening stable release note for core and WebUI before opening the window.
Compare changed defaults, migrations, dependencies and deprecated options with this VM's saved and effective configuration.
Escalate configuration, credential, mount or profile-behaviour changes before applying them.
[Core v0.22.3](https://github.com/NousResearch/hermes-agent/releases/tag/v0.22.3) changed `sessions.auto_prune` from true to false by default, so a missing key changes effective behaviour.
The October 30 maintenance explicitly retained `sessions.auto_prune: true` with 90-day retention on every existing profile.
The October 15 record below describes the earlier enabled default.

1. Prepare the window.
   1. Record both source commits, the actual install stamp and each non-bundled plugin's version/source pin in every profile where it is installed.
   2. Record PM state, pending state and unit enablement.
   3. Set `HERMES_DISABLE_LAZY_INSTALLS=1` for preparatory Hermes commands.
   An ordinary worker command can complete a pending update without the operator.
2. Hold unattended reboot.
   1. Install a temporary final-sorting apt drop-in containing `Unattended-Upgrade::Automatic-Reboot "false";`.
   2. Verify the effective setting with `apt-config dump`.
   3. Retain the hold through verification and any controlled reboot.
3. Quiesce writers.
   1. Stop and disable the shared gateway, dashboard and WebUI.
   2. Stop the four hal observer timers without changing enablement.
   3. Require their observer services to be inactive.
   4. Inspect user/system `hermes-worker-*` scopes with root visibility.
   5. Stop an orphan only after proving it has no Hermes/Python work or open store handles.
   6. Check sandbox background tasks separately.
4. Create recovery.
   1. Stream the stopped-state archive to a private off-host 0700 directory.
   2. Include `~/.hermes`, `~/hermes-webui`, units/drop-ins, launchers, interpreter targets, `/etc/hermes` and every unit EnvironmentFile.
   3. Require archive mode 0600.
   4. Verify gzip, tar and copied SQLite stores.
   5. Remove the disposable plaintext extraction.
   6. Retain the credential-bearing archive until the operator explicitly deletes it.
5. Prepare the next source advance.
   1. Compare each `untrusted-web-content-analysis` copy with its [recorded profile revision](safer-web-reader.md#shared-and-hal-skill-revisions).
   2. Establish its ownership and recoverable home.
   3. Move `hermes_cli/.web_dist-build-vZVavf/` to private recovery storage.
   Preserve both paths until this work is complete; never `git clean` them.
6. Update the application.
   1. Review core's proposed source target.
   2. Update external WebUI on stable.
   3. Update core through its upstream updater.
   4. Preserve profile `op://` references, mail pins, client selections and bank identities.
   For a stopped WebUI source update, require a clean tracked tree and verified tag before fast-forwarding.
   Keep it stopped until the runtime gates pass.
   Do not reinstall the Hindsight backend.
   5. After core and WebUI, run `hermes plugins list --plain` for default and each profile (`hermes -p <profile> plugins list --plain`).
   Bundled plugins move with core; inspect every non-bundled plugin's installed and offered version/source pin.
   Read its upstream changes against the affected profiles' configuration before proceeding; escalate configuration, credentials, mounts or profile-behaviour changes first.
   6. Update every non-bundled plugin through `hermes plugins update <name>` in each home where it is separately installed, using `-p <profile>` for named profiles.
   Accept only the native scanner's **safe** verdict; stop and escalate **caution** or **dangerous**, without `--force`, confirmation or a scan bypass.
   Catalogue updates can silently accept caution on reviewed pins: first stage the exact offered pin with the updater's user-file carry rules and run `tools.plugin_guard.scan_plugin` in the selected PM runtime, requiring safe before invoking the updater.
   Record the scan verdict and resulting version/pin for every copy; do not treat the updater's exit status alone as scan evidence.
   7. Follow upstream's plugin activation/restart guidance, then verify every affected profile and the feature each plugin serves after the coherent runtime starts.
   For Hindsight this means the actual dashboard panels/hubs, existing expected-bank reads and scheduled backend canary in step 11.
7. Gate completion.
   1. Require a matching built install stamp without `adoptedAt`.
   2. Require the upstream `completion_pending_path` to be absent.
   3. Require safe selected SQLite and a current PM environment.
   4. Run `hermes pm status` and `hermes pm doctor` under the guard.
   5. Distinguish actual update receipts from PM-sync receipts.
   6. Check kernel-lock ownership and upstream's owed-restart predicates.
   Leave marker files to upstream.
   A failed historical receipt alone is not a reason to run completion again.
8. Persist terminal policy.
   1. Run the setup helper with `--policy-only` for default and every profile directory.
   2. Require its actual `build_profile_terminal_scope` assertions to pass.
   3. Preserve profile mounts and secret references.
   The helper copies three managed policy keys and the selected profile image into YAML and `.env`.
   The `.env` values survive bulk saves stripping managed YAML keys; `config get` alone misses this gap.
9. Start the coherent runtime.
   1. Let upstream maintain the PM-aware gateway launcher and multiplex topology.
   2. Configure WebUI's direct `server.py` start as described below.
   3. Restart the shared gateway when the updater has not already done so.
   4. Restart the dashboard explicitly.
   5. Restart external WebUI explicitly.
   6. Require the serving WebUI process on store Python 3.14.
   7. Exercise one authenticated read-only WebUI gateway-status action.
   Keep WebUI authentication, ports and unrelated settings.
   Remove `VIRTUAL_ENV`, persisted `HERMES_WEBUI_PYTHON` and the legacy venv PATH prefix.
   Use `ExecStart=/usr/bin/python3 /home/hermes/hermes-webui/server.py`.
   Use base PATH `/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin`.
   Set `HERMES_WEBUI_AGENT_DIR` and `HERMES_SUPERVISED_CHILD=1`.
   Upstream `server.py` re-enters PM itself.
10. Check deep imports.
    1. Run `/usr/bin/python3 /home/hermes/.hermes/scripts/hermes-webui-runtime.py --check-import` from `/`.
    2. Require successful WebUI YAML/profile API and `run_agent` imports.
    The helper guards lazy completion and uses disposable WebUI state.
    It serves nothing.
    Stable WebUI's ruamel fallback needs no PyYAML supplement when this gate passes.
11. Verify profiles.
    1. Authenticate to the actual dashboard using `op://hermes/hermes-dashboard/password` inside the verification process.
    2. Require ready/available/configured Hindsight on default, emh and hal Memory panels.
    3. Require enabled runtime status at `/api/dashboard/plugins/hub` for each profile.
    4. Read existing expected-bank memory without printing content.
    5. Check the existing scheduled backend canary.
    6. Exercise used HA calls read-only.
    7. Preserve Hal’s disabled native HA plugin, runner-only HA credentials, operator-managed MCP address and emh’s settings.
    Never print passwords, cookies or memory text.
    Do not repeat diagnostic memory writes or create backend Jobs.
12. Verify serving topology.
    1. Require three enabled/active units.
    2. Confirm four messaging profiles connected once using profile connection logs.
    3. Require safer_web_reader and security_reviewer to remain worker-only.
    4. Check WebUI health.
    5. Exercise one normal authenticated emh WebUI diagnostic chat using the read-only health-data help tool.
    6. Run authenticated dashboard MCP tests for Health-data and HA-MCP.
    7. Require the health-data help tool and `ha_get_system_health` in their returned lists.
    8. Restore observer timers to their previous active/enabled states.
    9. After startup finishes, check `/home/hermes/.hermes/logs/errors.log` and each `/home/hermes/.hermes/profiles/<profile>/logs/errors.log` for new `Failed to load plugin` entries since the restart.
    Resolve any plugin-load failure before accepting the update.
    Successful live tool execution and dashboard discovery are the completion evidence; registration lines can support them.
    Do not change configuration or core to manufacture a registration line.
    The shared runtime record omits secondary mailbox connections.
    Existing observer failures remain outside this procedure.
13. Verify sandbox and jobs.
    1. Pull desktop and the configured reviewer image.
    2. Refresh only stale containers proven idle by platform and Docker process checks.
    3. Prove a new terminal sees its existing durable home/workspace.
    4. Preserve cron IDs, schedules, model pins and delivery.
    5. Require the alive job to be enabled and no_agent.
    6. Queue it with `cron.jobs.trigger_job('36f76a55200c')`.
    7. Require its gateway-owned run and uptime-kuma's `verdict=ok units=3/3 webui_http=200`.
    Keep shared attachments until the deployed per-profile-upload fix is independently proven.
    CLI `cron run` can use a different secret environment.
14. Close the window.
    1. Verify the [reader tool surface](safer-web-reader.md#verification-record) with a dispatched public diagnostic.
    2. Check stores with selected store SQLite.
    3. Check reboot-required while the hold remains installed.
    4. Perform an owed reboot under supervision.
    5. Recheck services, panels and chat after any reboot.
    6. Remove the temporary apt hold as the **last VM mutation**.
    7. Require Automatic-Reboot true, WithUsers true and time 04:45.
    Keep the hold if a required gate fails.
    Do not patch core or weaken the broker.

If a future release fails specifically for missing PyYAML, record the failure first.
Ruling 4 permits ordinary `uv pip install` of `pyyaml==6.0.3` into the current PM generation when PM tolerates it.
Use a WebUI-owned target directory only when PM polices installed contents.
Use no hand-rolled extraction or core lock edits.
Recheck the existing import-failed gate after every generation change.

### Completion record: October 15, 2026

Core HEAD is `5575a0859d6278bfcef79c28189a24594952efd3`; external WebUI stable **v0.52.458** is `fd0141fa196fe832171c9c0551bbb8205b6412d4`.
The interrupted October 14 update receipt ended **failed** at 22:56Z; successful 23:26–23:28 receipts were PM syncs.
An ordinary emh cron worker apparently triggered completion at 02:01Z (timing evidence; caller attribution is not proven), writing the built source stamp and clearing pending state without a new update receipt.
The old serving units still had September 30 code loaded. The maintenance archived completed source/config 51/stores 30 first and **did not run a second completion**.
After controlled migration/start all five configs remain schema 51 and all five stores are schema 31, passing quick_check with PM Python 3.14.7 / SQLite 3.53.1.
System SQLite 3.46.1 can falsely report the newer FTS5 trigram index malformed; do not use it to diagnose these stores.

The private recovery archive is `/Users/rob/Downloads/hermes-recovery-20261015/pre-completion.tar.gz`, 6,265,279,065 bytes, SHA256 `d4d974289b1f6fa367532241e761a56835473e604bfe9ae5005c2639b2ad5795`.
Full extraction verified 214,826 entries and five profile plus 34 other SQLite stores; the 9.9 GiB plaintext verification copy was removed.
The archive retains both untracked paths and the external legacy interpreter for recovery.
Root grew once from 25 GiB to 50 GiB online; Docker remained 125 GiB on `/dev/sdb`. Proxmox uses thick LVM, with the pre-existing pending SSD flag retained and its pending size corrected to 50 GiB.

Authenticated live Memory panels changed from inactive/unavailable to ready on default/emh/hal after the runtime restart; installed Plugin hubs show enabled Hindsight 1.2.2 at catalog commit `f843f173bb41f001e322cc83c917371639e47fbf`.
Existing-memory readbacks returned 23/53/136 results respectively, without another write. The earlier repair's round-trip remains the write/read evidence.
The new process/runtime fixed availability; the exact exception hidden by the old long-lived process was not established.
All three serving processes run store Python 3.14.7; WebUI health, its read-only gateway-status action and a fresh normal chat passed.
The actual uptime-kuma heartbeat at 07:43:48Z is `verdict=ok units=3/3 webui_http=200`.
Mail pins are `EMAIL_AUTHSERV_ID=purelymail.com` in default/emh/hal/web_watcher; the first three had header evidence. web_watcher's inbox was still empty: confirm its first permitted message's topmost Authentication-Results before calling that mailbox pin verified.
The three `sessions.auto_prune:false` overrides were removed while retention 90 was preserved; no manual purge or VACUUM ran.

Stable WebUI [native PM startup](https://github.com/nesquena/hermes-webui/blob/fd0141fa196fe832171c9c0551bbb8205b6412d4/server.py#L112) and [YAML fallback](https://github.com/nesquena/hermes-webui/blob/fd0141fa196fe832171c9c0551bbb8205b6412d4/api/yaml_compat.py) admit existing ruamel/cryptography. No PyYAML supplement or separate venv was installed.
[Issue #8113](https://github.com/nesquena/hermes-webui/issues/8113) concerns the Docker Python 3.12 dependency-marker failure; this VM's source deployment uses PM 3.14 and the shipped source fix. It does not establish that the Docker issue was fixed.

### Reader managed-worker defect and local repair: issue 146

At 5575a08, `agent/managed_worker.py` passes only `WORKER_DISABLED_TOOLSETS=('delegation',)` to the agent and loses the profile's `agent.disabled_toolsets:[kanban]`.
Public diagnostic task `t_d80f1ad7` exposed thirteen unexpected Kanban tools; the unchanged broker refused both lifecycle operations and the task blocked without a result.
A two-line constructor patch was briefly applied under ruling 7 and its fake-factory regression test went red then green.
The operator then overruled ruling 7: the patch was reversed, its repository patch/test files removed, the follow-up public diagnostic archived, and the broker left unchanged.
Core's tracked tree again matches 5575a08; only the two pre-existing untracked directories remain. There is **no local core patch or reapply step**.
The later reader-fix ruling approved broker-local profile-scoped overrides, with an explicit `tools.override` grant only in the reader.
Broker 1.1.0 enumerates every runtime kanban registration, hides its schema and refuses guessed calls without invoking the original handler.
The strict four-tool guard and envelope validation remain unchanged.
The deployment and five-part live verification are recorded in [safer-web-reader.md](safer-web-reader.md#verification-record); [issue 146](https://github.com/mnbf9rca/kubernetes_config/issues/146) retains the upstream defect history.
After an update or restore, verify the dispatched surface before relying on this plugin workaround.

### Dashboard Migrate config

At this commit, [the System action](https://github.com/NousResearch/hermes-agent/blob/5575a0859d6278bfcef79c28189a24594952efd3/hermes_cli/web_routers/status.py#L793) spawns `hermes config migrate` for the selected profile.
Startup/update maintenance already ran the schema migration; all five profiles are at the current version 51.
Pressing it adds no schema upgrade, but the implementation still checks `.env` formatting and suspicious MCP entries. It is not a button needed to finish this maintenance.

### Repair record: September 11, 2026

The stale marker was preserved as `~/.hermes/fleet_restart_pending.saved-20260911T135304Z`.
The incompatible Superpowers manifests were moved from `~/.hermes/plugins/superpowers` to `~/.hermes-disabled-plugins/superpowers-20260911T135304Z`.
No plugin files were deleted.
Credential checks found no Teams or Google Chat credentials in any profile, managed configuration, running service environment or Google credential files.
The native CLI disabled `platforms/teams` and `platforms/google_chat` in all five profiles.
The remaining user plugin, `safer-reader-broker`, passed `hermes plugins doctor --ci` with runtime discovery, parsing, import and registration clean.
All four gateways were restarted; all six user units were active and the WebUI `/health` returned HTTP 200.

### Attachments stopgap removal condition

The `default`, `emh`, `hal` and `web_watcher` sandboxes retain the read-only `/home/hermes/.hermes/webui/attachments` mount until the installed WebUI fixes [#6939](https://github.com/nesquena/hermes-webui/issues/6939), proposed in [#7022](https://github.com/nesquena/hermes-webui/pull/7022).
The removal condition is that uploads use the profile's own `cache/documents/webui-attachments/<session>/` directory and work through its existing mount.
Remove the shared mount from those profiles, the sandbox table below, and `hermes-vm/scripts/hermes-profile-docker-setup.sh` when that condition holds.
Keep every other profile mount.
Restart the shared gateway and WebUI after removing the shared mount.

## Rollback

Restore a matching stopped-state archive: source, PM generations/interpreter targets, launchers, configs, clients, units/drop-ins, stores and WALs together.
Stop all serving units, observer timers/services, worker scopes and sandbox writers first; retain the reboot hold.
Create private failed-state parents outside every archived tree and outside the archive location, on the destination filesystems.
Move each non-overlapping archived top-level destination into them; require the original destinations absent before extraction.
**Never overlay a newer tree**: leftover PM state, migration manifests, drop-ins or WALs can make the restored combination incoherent.
Restore original ownership/modes; run user daemon-reload. Keep failed-state trees intact and services disabled until the recovery start is chosen.
Keep the live false reboot hold unchanged: exclude its archived reference leaf from extraction and never move the whole apt configuration directory.
The October 15 archive is completed source/config 51/stores 30, not the older pending installation or a known healthy serving stack; restarting the original six units blindly is unsafe.
Forward-only schema changes and pruning require the matching stores, and restoring loses writes since the recovery point.
Do not erase ledgers, restore only Git, shrink disks, or roll back remote Hindsight banks.

## Topology: what runs where

Verified live on October 15, 2026. Profiles own their personas, secrets, mailboxes, memories and sessions; the **single gateway** serves all six profile contexts.
Four profiles receive messages: default, emh, hal and web_watcher. safer_web_reader and security_reviewer are worker-only. The reader retains its broker boundary; the reviewer is advisory and has its own image and named board.

### The processes

| Unit | Role | Port |
|---|---|---|
| `hermes-gateway` | Multiplexed messaging, cron, dispatcher and profile owners |8642 API, where configured|
| `hermes-dashboard` | Profile administration and Memory/Plugins panels |9119|
| `hermes-webui` | External browser/Hermex chat runtime, serving all profiles |8787|

The homelab cloudflared tunnel publishes dashboard `hermes.cynexia.com` and WebUI `hermes-app.cynexia.com`, behind Cloudflare Access.
All three ports also answer on the trusted LAN; the VM has no host firewall. The published Access gate is separate from each application's authentication.
The three enabled user units run under lingering and use upstream PM Python 3.14. Their former three secondary gateway unit files were removed by upstream migration.
The four `hal-ha-observer-{analyze,health,inventory,logbook}.timer` units are agent-authored, outside repository management, and were restored active/enabled. Their pre-existing failures were not repaired; their legacy interpreter needs separate investigation before any change that could write profile stores.

### The state tree

```
~/.hermes/                       the DEFAULT profile's home, and the shared root
  hermes-agent/                  the shared checkout; PM selects dependency environments
  config.yaml  .env  auth.json   default profile: settings, environment, provider tokens
  shared/nous_auth.json          provider tokens, shared by every profile on purpose
  scripts/                       cron scripts and the guarded deep-import helper
  webui/attachments/             WebUI chat uploads: ONE directory for all profiles
  sandboxes/docker/<task-id>/home   each sandbox's writable /root
  workspace/                     the default profile's sandbox /workspace
  profiles/
    emh/               config.yaml  .env  auth.json  workspace/  logs/  state.db  ...
    hal/               the same three files, the same shape
    web_watcher/       the same
    safer_web_reader/  the same; worker-only, no separate gateway unit
    security_reviewer/ the same; worker-only, reports in workspace and five enabled Trail of Bits skills
```

### The three roles

The **gateway** is the shared always-on owner for the profiles: it holds the connections that arrive unprompted.
It runs the platform adapters (a Telegram bot connection, an email adapter polling IMAP), it runs that profile's cron jobs, and it starts that profile's MCP servers as child processes.
The shared gateway runs the default profile’s daily alive check and the weekly sandbox refresh ([step 5](#5-create-the-daily-checks-cron-job), [step 6](#6-install-the-sandbox-refresh-job)).

The **dashboard** is an administration panel for the default profile: configuration, credentials, sessions, logs and cron.
It is reached at `https://hermes.cynexia.com`, and it starts a mail MCP server of its own.

For secondary email profiles such as `hal` and `emh`, the dashboard can keep `fatal` after the receiver reconnects.
The email adapter records the failure but does not publish `connected` after recovery, so this flag shows the last error rather than the current receiver state.
A later `IMAP reconnect test passed` line in `/home/hermes/.hermes/profiles/<profile>/logs/gateway.log` confirms mailbox reconnection; the subsequent `email reconnected` line confirms the platform has reconnected.
The known recurring IMAP `UNEXPECTED_MESSAGE` error usually recovers in about a second and needs no action.

The **WebUI is a second runtime, not a front end for a gateway.**
This is the piece that surprises people.
`hermes-webui` is a separate program from a separate repository, but upstream activates the agent’s selected PM environment, imports hermes-agent, and calls `run_agent` inside its own process.
So a chat in the browser makes its own model calls and starts its own MCP servers, and the gateway for that profile is not involved at all.
It serves every profile from that one process, switching `HERMES_HOME` per request, which is why there is one unit and one port rather than five.
Restarting `hermes-gateway` therefore changes nothing about a browser conversation — the fact recorded under [Facts about this VM](#facts-about-this-vm) about MCP tool discovery is a consequence of exactly this.

### emh fitness and nutrition skill

Only `emh` has the optional upstream [fitness-nutrition skill](https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/health/health-fitness-nutrition).
It supplies deterministic Python calculators, wger exercise lookup and USDA FoodData Central lookup.
Foodnoms remains emh's nutrition logger; installing this skill does not log meals.
The installed version is `1.0.0`, copied from `optional-skills/health/fitness-nutrition` at core commit `5575a0859d6278bfcef79c28189a24594952efd3`.
All four installed files match that source byte for byte.

Install it for emh only:

```sh
/home/hermes/.local/bin/hermes -p emh skills install official/health/fitness-nutrition --yes
```

The destination is `/home/hermes/.hermes/profiles/emh/skills/health/fitness-nutrition/`.
The calculator is `scripts/body_calc.py` there and uses only Python's standard library in the desktop sandbox.
The skill needs `curl` and `python`; its network calls go to `wger.de` and `api.nal.usda.gov`.

The emh config adds this entry to its existing `secrets.onepassword.env` map:

```yaml
secrets:
  onepassword:
    env:
      USDA_API_KEY: op://hermes/usda-fooddata/api-key
```

The key is resolved on the host and is not stored literally in profile YAML or this repository.
The upstream skill declares optional `USDA_API_KEY` in `required_environment_variables`.
Loading it through `skill_view` registers that one name for profile-scoped terminal passthrough.
No `docker_env`, `docker_forward_env`, managed-scope change or egress proxy is needed.
The skill uses `DEMO_KEY` when the variable is absent; a missing reference leaves successful existing 1Password lookups intact.
A live gateway keeps its secret-source snapshot.
After changing the map or rotating the key, run `systemctl --user restart hermes-gateway` on the VM to refresh the shared gateway owner; this interrupts all gateway profiles.
The classic CLI's `/reload` only refreshes that client's `.env`; it does not refresh the gateway's 1Password snapshot.
Never print the key or enable shell tracing around USDA requests.

Verification on October 15 used a fresh emh chat through the shared gateway, not an independent CLI agent.
Its desktop sandbox had Python `3.13.16`, `curl` and the real USDA key; no credential value was printed.
The calculator returned BMI `22.2` for synthetic inputs `72 kg, 180 cm`, matching the independent calculation.
USDA returned a banana result with FDC ID `173945`; wger returned exercise ID `9`, “2 Handed Kettlebell Swing”.

**The skill's wger name-search example is stale.**
[wger 2.5 removed `/api/v2/exercise/search/`](https://github.com/wger-project/wger/releases/tag/2.5); that example returns HTTP 404.
Use `/api/v2/exerciseinfo/` for exercise lookup; the gateway chat verified its JSON list with HTTP 200.
**Pass `M` or `F` to `tdee` and `bodyfat`.**
Any other sex value, including `male`, silently selects the female formula; `bodyfat` can also fail if the hip measurement is missing.
The installed skill remains byte-identical to upstream; no local patch was added.

### What is shared and what is per profile

Shared: the checkout and PM tool store, the host, the docker daemon and its sandboxes, the WebUI attachments directory, and the provider credentials.
Per profile: `config.yaml`, `.env`, `auth.json`, workspace, logs, sessions, memories, skills and cron store.
The rule for reading any path is that a profile is just a different `HERMES_HOME`, which is the contract set out in [homelab.md](homelab.md#hermes-vm-configuration-layout-and-secrets).

Two consequences have already cost time, and both come from sharing.

**Secret names collide inside the WebUI.**
One process serves six profiles, so a `${VAR}` placeholder that more than one profile declares can resolve to another profile's value.
On September 6, 2026 an `emh` WebUI chat was handed the default profile's mailbox password.
The fix — a per-profile prefix on every name — is [step 5 of Creating a profile](#5-secrets-if-the-profile-needs-any), and the defect is [hermes-agent issue 2](https://github.com/mnbf9rca/hermes-agent/issues/2).

**Profiles invalidate each other's model login.**
Profiles that select `model.provider: openai-codex` share the grant in `~/.hermes/auth.json`: each profile's own `auth.json` is empty of providers and falls back to the root file.
That provider rotates its refresh token on every refresh, so two processes refreshing the same grant leave one of them holding a revoked token (`_write_through_provider_state_to_global_root` in `agent/credential_pool.py` explains it, and its write-back is best effort).
The failure reads `refresh_token_reused` in `~/.hermes/auth.json`, and re-authenticating is the only repair.

## Lingering is a precondition

```sh
loginctl enable-linger hermes
loginctl show-user hermes -p Linger        # must print Linger=yes
```

Without lingering, the `hermes` user manager stops when the last session ends.
Every hermes user unit dies at the next reboot — **and the daily check dies with them**, because it runs as a cron job inside `hermes-gateway` rather than on a timer of its own.

That is the nastiest failure mode here: **the monitor that would report the outage is itself down.**
Nothing pushes, so uptime-kuma sees silence rather than a `down`, and `hermes-app-alive` stays green until its 24-hour heartbeat and 6-hour retry expire, about 30 hours after the last good beat.
Moving the check inside the gateway did not create that coupling — a user timer died with lingering too — but it tightened it: one process's death now stops both the thing being watched and the watching.

One thing catches it sooner: the `hermes` HTTP monitor probes the dashboard on `hermes.cynexia.com`, and `hermes-dashboard` is one of the units that died, so that monitor goes red at its own interval.
Read a red `hermes` beside a still-green `hermes-app-alive` as this failure until proven otherwise.

The reboot is automatic and can happen any night at 04:45 UTC, so an un-lingered rebuild is not a latent problem.
It is a problem the first night.

## Reading a DOWN hermes-app-alive

The daily check runs at **05:45 UTC**, one hour after the 04:45 automatic-reboot window, as a `no_agent` cron job inside the default gateway.
The scheduler's ticker picks up a past-due job, so a run missed while the VM was rebooting is caught up rather than skipped.
The monitor's heartbeat is 24 hours with one retry at 6 hours, so **a missing heartbeat alarms about 30 hours after the last good one**.

| `verdict=` | What it means | First thing to do |
|---|---|---|
| `units-down` | Not every hermes user unit is active. `units=<active>/<expected>` carries both counts, and the expected one is derived from the `UNITS` list in the script, so it follows the list rather than a number written down here | `systemctl --user status` each unit in that list. If none are up, check `loginctl show-user hermes -p Linger` first |
| `import-failed` | The selected PM runtime cannot import WebUI YAML/profile APIs and `run_agent`. **This is the silent failure the whole design exists for**: every unit is still active and `/health` still answers `status: ok`, while the iOS app answers `AIAgent not available` | Read [homelab.md, "Do not give it a venv of its own"](homelab.md#hermes-webui-on-the-vm), then use [Rollback](#rollback). The Python traceback in the journal names which import failed |
| `webui-unreachable` | The WebUI's own `/health` did not answer 2xx. `webui_http=` carries the status; `000` means no connection | `journalctl --user -u hermes-webui`. Note the unit's `StartLimitIntervalSec=60`/`StartLimitBurst=5` parking behaviour — a repeatedly failing start parks in `failed` rather than looping invisibly |
| **No beat at all** | The VM is off, the user manager is not running, lingering was lost, `hermes-gateway` is down, the cron job was deleted from the agent's store, the push token stopped being injected, or the Cloudflare Access bypass on `/api/push/*` was removed | `journalctl --user -u hermes-gateway` carries the run and its stderr — every one of those fails loudly there. Then `hermes cron list`, then the bypass. **A bypass regression turns every push monitor in the estate red at once, which is the tell** |

**The push token is injected, never stored on this VM.** hermes's own 1Password secrets provider resolves `HERMES_APP_ALIVE_PUSH_TOKEN` from the default profile's `secrets.onepassword.env` at gateway start, the cron subprocess inherits it, and the script assembles the push URL from it in memory.
A missing or empty variable fails the run at its first line — loudly, on stderr, before anything else happens.
**That cannot push a false `down`**: with no token there is nowhere to push at all, so uptime-kuma sees silence and reports it on its own heartbeat.

**A daily line of Python import noise in the journal is normal, not a fault.**
The check deliberately does not discard the interpreter's stderr, because the traceback names which import failed and that is the whole answer to "what broke".
The traceback goes to the journal and never near the pushed message, which carries only `verdict=`, `units=` and `webui_http=`.

## Installing or reinstalling

A VM rebuild must repeat all of this.
The nightly `hermes backup` zip covers `~/.hermes`, so it carries the profile config — the token injection in step 4 — and the cron store holding both jobs, from steps 5 and 6.
Default scripts live at `~/.hermes/scripts/`; reviewer cleanup lives in its own profile scripts directory. Both scripts and their owning cron stores ride that zip together.
That coupling is the right one, because the store names each script by bare filename, so a restore bringing back one without the other would leave a job pointing at nothing.
The four apt files and `/etc/hermes/config.yaml` sit outside the zip, and they are rebuild territory.
**Restore `/etc/hermes/config.yaml` and verify every profile's YAML/env terminal fallbacks. Missing both the managed policy and usable fallbacks can run terminal tools on the host without an alert.**
**Do not trust the restored cron store without looking**: `hermes cron list` after a rebuild, because a check that quietly failed to come back looks exactly like a healthy day until the heartbeat lapses.

### 1. Lint first

```sh
make check-vm-scripts
```

This VM-script guard runs in no preflight or schedule. The two image workflows cover InfluxDB MCP and reviewer-image inputs; neither runs this guard for installed VM scripts.
Run this guard before installing repository-owned VM scripts.
It shellchecks all shell scripts under `hermes-vm/scripts/` — `hermes-app-alive.sh` from step 2, `hermes-sandbox-refresh.sh` from step 6, and `hermes-profile-docker-setup.sh` from [step 7](#7-pin-the-docker-terminal-backend).

### 2. Install the daily check's script

Install the shell script and its Python deep-import helper; the other copies are the refresh script in [step 6](#6-install-the-sandbox-refresh-job) and the profile helper in [step 7](#7-pin-the-docker-terminal-backend).

```sh
scp hermes-vm/scripts/hermes-app-alive.sh hermes-vm/scripts/hermes-webui-runtime.py \
  hermes@hermes.cynexia.net:/home/hermes/.hermes/scripts/
ssh hermes@hermes.cynexia.net 'chmod 0755 /home/hermes/.hermes/scripts/hermes-app-alive.sh'
```

**The directory is not a preference.**
`hermes cron create --script` takes a bare filename resolved under `$HERMES_HOME/scripts/` and rejects an absolute path outright, so a copy installed anywhere else can be run by hand but cannot be scheduled at all.
Steps 5 and 6 both depend on this.
**Keep exactly one copy.**
An earlier draft of this page installed to `/home/hermes/bin`; two copies is a parity trap in which the file the scheduler runs and the file an operator edits are different files, and nothing says so.

There is no systemd unit and no environment file.
Lingering still has to be on, because the gateway that runs the check is a user unit:

```sh
loginctl enable-linger hermes
```

### 3. Create the monitor and store its token

Create the `hermes-app-alive` push monitor in uptime-kuma by hand — a 24-hour heartbeat with one 6-hour retry — and store its push **token** at `op://hermes/hermes-app-alive/kuma-push-token`, typed `[text]`.
The monitor and the vault field are both created by hand; no manifest in this estate assembles the URL.
See [uptime-kuma.md](uptime-kuma.md#push-monitors).

**The vault is `hermes`, and that is what makes step 4 work.**
The VM's 1Password service account can see only that vault, so any reference the VM itself resolves has to live there; a reference the operator's laptop resolves may live anywhere.
The placement was chosen for this on August 26, 2026, before anything on the VM read it.

### 4. Inject the token into the default profile

Add **one** entry to the default profile's `secrets.onepassword.env`:

```yaml
secrets:
  onepassword:
    env:
      HERMES_APP_ALIVE_PUSH_TOKEN: op://hermes/hermes-app-alive/kuma-push-token
```

Edit `~/.hermes/config.yaml` directly, or drive it through the `hermes secrets onepassword` command group — its help line reads `onepassword (op, 1password)`, so all three spellings of the group work, but **check `hermes secrets onepassword --help` for the subcommand that sets an entry**; no session has run one.
Then restart the gateway, because the provider resolves these references at start:

```sh
systemctl --user restart hermes-gateway
```

This is the same mechanism `HINDSIGHT_API_KEY` already uses on all three homes.
`HERMES_APP_ALIVE_PUSH_TOKEN` is declared by the default profile alone, which is what [step 5 of Creating a profile](#5-secrets-if-the-profile-needs-any) requires of every entry in every profile's map.

**The variable name is load-bearing and must not be "tidied".**
The cron subprocess sanitiser in `tools/environments/local.py` (`_sanitize_subprocess_env`) strips by **name**: every provider-registry name, anything matching `AUXILIARY_*` or `GATEWAY_RELAY_*`, and a fixed always-strip list.
`HERMES_APP_ALIVE_PUSH_TOKEN` belongs to no registry, so it passes through to the script.
Renaming it into any of those shapes removes it silently, and the check then fails its first line every day.

### 5. Create the daily check's cron job

The check runs as a **`no_agent`** cron job: the scheduler runs the script on schedule and delivers its stdout directly, skipping the agent entirely, so it costs **zero model tokens**.

```sh
/home/hermes/.local/bin/hermes cron create --name hermes-app-alive \
  --no-agent \
  --script hermes-app-alive.sh \
  --deliver local \
  '45 5 * * *'
```

Run against the live CLI on August 27, 2026, so this is a transcript rather than a shape.
Four details are easy to get wrong, and only the last of them fails quietly:

- **The schedule is positional.**
  There is no `--schedule`.
- **`--script` takes a bare filename**, resolved under `~/.hermes/scripts/`.
  An absolute path is rejected at the API boundary by `_validate_cron_script_path` (`tools/cronjob_tools.py`), which exists to stop prompt injection aiming a job at an arbitrary file.
  There is no `--command`.
- **`--no-agent`** is spelled as it looks.
- **`--deliver local` is set deliberately, not left to the default.**
  `local` resolves to **zero delivery targets** (`_resolve_delivery_targets` in `cron/scheduler.py` returns `[]` for it), so the script's stdout is recorded against the job and sent to no messaging platform.
  That is what this job wants: it pushes its own verdict to uptime-kuma, so any delivery target would put a duplicate into a chat every morning.
  `local` is already the default, so the flag changes no behaviour — it is written down so the intent survives a change of default, and because an omitted `deliver` makes the CLI print a "will NOT be delivered back into this session" notice that reads like a fault and is not one.

Two things follow from the job living inside the agent rather than in systemd:

- **The push now proves the default gateway executes.**
  A wedged-but-running `hermes-gateway` used to be invisible to every check on this page; a beat arriving at all means its scheduler ran a subprocess.
  This proves scheduler execution in the shared gateway, not each profile’s mailbox or worker health.
- **The cron store is agent state, not a file this repository owns.**
  A job lost to a rebuild, a restore or a hand edit produces silence, and silence is not reported until the monitor's 24-hour heartbeat and 6-hour retry lapse.
  `hermes cron list` is the check; run it after any restore.

### 6. Install the sandbox refresh job

The weekly job that replaces stale docker sandbox containers, so the pinned devcontainer image a live sandbox is running does not drift behind the tag it was pulled from.
It is a second `no_agent` cron job in the same store as step 5, and it needs no monitor, no token and no vault item.

```sh
scp hermes-vm/scripts/hermes-sandbox-refresh.sh \
  hermes@hermes.cynexia.net:/home/hermes/.hermes/scripts/hermes-sandbox-refresh.sh
ssh hermes@hermes.cynexia.net 'chmod 0755 /home/hermes/.hermes/scripts/hermes-sandbox-refresh.sh'
```

```sh
/home/hermes/.local/bin/hermes cron create --name hermes-sandbox-refresh \
  --no-agent \
  --script hermes-sandbox-refresh.sh \
  --deliver local \
  '15 5 * * 0'
```

The four details under step 5 apply unchanged — the schedule is positional, `--script` takes a bare filename, `--no-agent` is spelled as it looks, and `--deliver local` is written out rather than left to the default.
`local` carries more weight here than it does there: this job pushes to nothing at all, so its stdout summary is only ever read out of the run record, and any delivery target would put that summary into a chat every Sunday.

Then run it once and read the result, because the run record is the only place a `no_agent` run reports:

```sh
/home/hermes/.local/bin/hermes cron run <job_id>
/home/hermes/.local/bin/hermes cron runs <job_id>
```

A run that finds nothing stale is the normal result and a pass, not a no-op to investigate.

**What the job does, and what it refuses to do.**
It reads the docker-backend profiles out of the live config, so no profile is named in it — which is how `hal` joined on the day it migrated, and `safer_web_reader` on the day the managed scope pinned it, with no edit here either time.
It `docker pull`s each distinct pinned image on **every** run: there is deliberately no global "the digest has not moved, exit early", because comparing per container is what lets a container skipped one Sunday be replaced the next.
Then, per container found by its `hermes-profile` label, it compares the container's image id with the pulled tag's.
A container whose id matches is left alone.
A stale container that is **not running** is removed outright — the 04:45 reboot leaves sandboxes `exited` exactly when the Sunday run fires, and an exited container is not busy.
A stale container that **is** running is removed only when its profile is idle.
It finishes with a dangling-image prune, `until=168h`.
**Any read that fails anywhere means skip, never remove.**

**Idle is a conjunction, and `active_agents` alone is not it.**
The dashboard API at `http://127.0.0.1:9119/api/status?profile=<p>` is read as busy unless `gateway_running` is true, `gateway_state` is `running`, `gateway_busy` is false, and `active_agents` **and** `active_sessions` are both zero.
`active_agents` is a per-turn flag: on a profile in the middle of a long task it reads 0 between turns, and a 55-second zero window was measured on a busy profile on August 29, 2026.
That verdict is then ANDed with `docker top` showing nothing beyond the container's own init, because background terminal processes run inside the sandbox and are deliberately not counted in `active_agents`.

**The job does not see itself as busy.**
A `no_agent` run creates no session row and does not bump the persisted `active_agents` — read in the source on August 29, 2026 — but that is an upstream implementation detail rather than a contract, and a chat turn ending on the wrong side of the read can still leave a stale +1.
Both of those fail in the same direction — a false *busy* — so the job skips and tries again next Sunday.

**Nothing watches this job, by design.**
It pushes to no monitor: a stale sandbox image is neither lockout nor data loss, and the containment boundary is the host kernel rather than the sandbox userland.
Nothing monitors the refresh job's own execution; failures remain visible in its run history.

### 7. Pin the docker terminal backend

Two files: the managed-scope config, which is root-owned and pins the backend for every profile, and the helper that finishes a new profile's own configuration.
What the settings do, and what a sandbox looks like once they are live, is [The docker terminal sandboxes](#the-docker-terminal-sandboxes).

```sh
ssh hermes@hermes.cynexia.net 'sudo mkdir -p -m 0755 /etc/hermes'
scp hermes-vm/etc/hermes/config.yaml hermes@hermes.cynexia.net:/tmp/hermes-managed.yaml
ssh hermes@hermes.cynexia.net 'sudo install -m 0644 -o root -g root /tmp/hermes-managed.yaml /etc/hermes/config.yaml'
scp hermes-vm/scripts/hermes-profile-docker-setup.sh \
  hermes@hermes.cynexia.net:/home/hermes/.hermes/scripts/hermes-profile-docker-setup.sh
ssh hermes@hermes.cynexia.net 'chmod 0755 /home/hermes/.hermes/scripts/hermes-profile-docker-setup.sh'
```

### Managed-scope gap at 5575a08

Managed scope is **still an upstream feature**, not deprecated in favour of per-profile config.
The installed [user guide](https://github.com/NousResearch/hermes-agent/blob/5575a0859d6278bfcef79c28189a24594952efd3/website/docs/user-guide/managed-scope.md) documents `/etc/hermes/config.yaml`, leaf precedence and CLI write refusal; [managed_scope.py](https://github.com/NousResearch/hermes-agent/blob/5575a0859d6278bfcef79c28189a24594952efd3/hermes_cli/managed_scope.py) implements it.
The reviewed v0.21.x/v0.22.0 release notes contain no managed-scope deprecation notice.
However, [build_profile_terminal_scope](https://github.com/NousResearch/hermes-agent/blob/5575a0859d6278bfcef79c28189a24594952efd3/tools/terminal_scope.py#L114) reads defaults, profile env and raw profile YAML without the managed layer.
We observed a fresh emh container using the old image while `hermes config get` correctly reported desktop.

`/etc/hermes/config.yaml` remains the source of record. Until upstream's actual terminal scope honors it, mirror its backend, run-as-host-user and cwd into each profile’s YAML and `.env` through upstream writers. Preserve an explicit profile image (the reviewer's derived image); use the managed image only when no YAML or env image is selected.
The existing setup helper now does that and asserts actual scope parity, preserving unrelated config/comments and mounts.
After a managed policy change, run it with `--policy-only` for **default and every directory under profiles/**; isolated workers must use that mode to avoid shared attachment mounts.
A new trusted profile can use the ordinary helper to copy policy and add its standard mounts together.
Bulk config saves strip managed leaves from profile YAML. The corresponding `TERMINAL_ENV`, `TERMINAL_DOCKER_IMAGE`, `TERMINAL_DOCKER_RUN_AS_HOST_USER` and `TERMINAL_CWD` values in each `.env` survive, keeping the selected Docker image and backend.
The helper preserves unrelated env entries and asserts actual scope parity.
The PM regression fixture applies upstream’s real managed-key stripping and proves the selected image survives, including a YAML image winning a stale env image.
These copies are a temporary duplicate; remove copying only after an updated upstream `build_profile_terminal_scope` demonstrably reads managed scope for each profile. Before removing image fallbacks, unpin `terminal.docker_image` in managed scope and prove the reviewer's explicit image still wins; otherwise managed precedence would replace it with desktop.
Do not file or draft an upstream bug report for this gap (operator ruling).

**The repository copy is the only durable record.**
The nightly `hermes backup` zip covers `~/.hermes` and nothing under `/etc`, and `hermes update` never touches `/etc` either, so this file survives a rebuild only by being copied back from `hermes-vm/etc/hermes/config.yaml`.
It follows the same pattern as the apt configuration in [step 8](#8-install-unattended-upgrades): repository is the source, the VM holds an installed copy.

**`terminal.docker_volumes` is deliberately not pinned**, because it names per-profile paths and because only some profiles get the shared attachments mount.
The helper writes those for one profile, and [Creating a profile](#creating-a-profile) is where it is used; the three entries it writes are in [The docker terminal sandboxes](#the-docker-terminal-sandboxes).


### 8. Install `unattended-upgrades`

Four files, and **two of them do not install where they live in the repository**:

| Repository path | Installs to |
|---|---|
| `hermes-vm/etc/apt.conf.d/20auto-upgrades` | `/etc/apt/apt.conf.d/20auto-upgrades` |
| `hermes-vm/etc/apt.conf.d/52unattended-upgrades-local` | `/etc/apt/apt.conf.d/52unattended-upgrades-local` |
| `hermes-vm/etc/systemd/apt-daily.timer.d/override.conf` | `/etc/systemd/system/apt-daily.timer.d/override.conf` |
| `hermes-vm/etc/systemd/apt-daily-upgrade.timer.d/override.conf` | `/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf` |

**A naive recursive copy of `hermes-vm/etc/` lands the two drop-ins under `/etc/systemd/apt-daily.timer.d/`, where systemd never looks and they silently do nothing.**
Copy them one at a time, or the schedule stays at Debian's default and nothing says so.

```sh
sudo systemctl daemon-reload
```

### 9. Verify the install

Check each of these once.
Every one of them fails silently if it is wrong.

1. **The package sources resolve to security-only entries.**
   The `#clear` directive is load-bearing, not future-proofing: Debian's shipped `50unattended-upgrades` leaves three patterns uncommented and the first matches `label=Debian`, which is every package in stable.
   Confirm exactly two patterns come back, both `label=Debian-Security`:

   ```sh
   apt-config dump Unattended-Upgrade::Origins-Pattern
   ```

2. **Each apt timer shows exactly ONE calendar entry**, not two.
   The empty `OnCalendar=` line in each drop-in is what replaces the vendor schedule instead of adding to it:

   ```sh
   systemctl show apt-daily.timer -p TimersCalendar
   systemctl show apt-daily-upgrade.timer -p TimersCalendar
   systemctl list-timers apt-daily.timer apt-daily-upgrade.timer
   ```

3. **The reboot hook is armed.**
   Confirm `Automatic-Reboot` is `true` and `Automatic-Reboot-Time` is `04:45`, and that `/sbin/shutdown` exists — `unattended-upgrade` does not reboot directly, it hands that literal time to `shutdown`:

   ```sh
   apt-config dump Unattended-Upgrade::Automatic-Reboot
   apt-config dump Unattended-Upgrade::Automatic-Reboot-Time
   ```

4. **The stamp file refreshes after a real run.**
   Note its mtime, force a run, and confirm the mtime moved.
   A stale stamp can indicate that the timer did not run:

   ```sh
   stat -c '%y' /var/lib/apt/periodic/unattended-upgrades-stamp
   sudo systemctl start apt-daily-upgrade.service
   stat -c '%y' /var/lib/apt/periodic/unattended-upgrades-stamp
   ```

   **Never reach for `unattended-upgrade --dry-run` to check on the stamp, because a dry run writes it.**
   Verified August 27, 2026: a dry run completes through the same `write_stamp_file()` as a real one.
   A dry run can make a dead timer's stamp look current.

   **The check also resists being run twice in a day**, which is not a fault.
   `apt.systemd.daily` guards its `unattended-upgrade` call with a **day-granular** interval — midnight today against midnight of the stamp's day — so once the stamp carries today's date a forced run is skipped and the mtime stays put.
   On a machine whose stamp is already fresh, confirm the mechanism tomorrow instead, by checking that its date has advanced.

5. **The VM's timezone is still UTC.**
   The VM maintenance schedules use UTC; Hal’s house check uses Europe/London.
   A timezone drift can move maintenance into the reboot window:

   ```sh
   timedatectl | grep 'Time zone'
   ```

6. **Both cron jobs exist, and their next runs are 05:45 daily and 05:15 on Sunday.**
   The jobs live in the agent's own store, so nothing in this repository proves they are there:

   ```sh
   hermes cron list
   ```

7. **The token actually reaches the gateway-owned script.**
   Check the existing job is enabled and no_agent, then call selected-runtime `cron.jobs.trigger_job('36f76a55200c')`.
   Read its gateway-owned result and the existing uptime-kuma heartbeat; require `verdict=ok units=3/3 webui_http=200`.
   Do not substitute CLI `cron run`: it can resolve a fresh secret environment while the scheduled gateway still holds the old one.
   The scheduler uses bash for `.sh` files; lint remains POSIX sh.

8. **Every profile retains its actual Docker policy and selected image.**
   Run the helper's scope assertion for default and every profile, rather than `config get`:

   ```sh
   helper=/home/hermes/.hermes/scripts/hermes-profile-docker-setup.sh
   "$helper" default --policy-only
   for profile_home in /home/hermes/.hermes/profiles/*; do
     [ -d "$profile_home" ] || continue
     "$helper" "${profile_home##*/}" --policy-only
   done
   ```

   This persists the four env fallbacks and verifies `build_profile_terminal_scope` for each profile.
   The disposable regression test is `hermes-vm/scripts/test_hermes_profile_policy.py`; run it under the selected PM runtime.

**Expect a reboot the first night after installing.**
Before the first install the VM has a pending kernel reboot — an uptime over four days and a kernel image in the reboot-required list — and nothing has been arming an automatic reboot, because the drop-in that arms it is part of this install.
So the first 04:45 window clears that backlog — correct behaviour, not a fault.

## Creating a profile

Adding a Hermes profile to this VM, in order.
Worked end to end on `web_watcher` on August 31, 2026; every hazard below is one that run met or would have met.
**Choose admission before creation:** either stop the shared gateway for the ordinary in-place flow, or use the fully staged native hot-add described in [Security reviewer operation](#security-reviewer-operation). Never expose a half-configured profile to live discovery. Configure terminal policy, platform disables and credentials before admission.

Mailbox, secret and email steps are conditional. Isolated workers still need explicit mounts, adapter disables, admission and verification; do not run the ordinary shared-attachments helper on them.

### 1. Create the profile

Create it in the WebUI, choosing a **clean** profile rather than one inheriting the default's configuration.
`hermes profile create <name>` works too, and seeds every bundled skill pack into `<profile home>/skills/` — 14 of them on August 31, 2026, so read the directory rather than that number.
Delete the directories the profile does not need, and **keep `<profile home>/skills/.bundled_manifest`** — that file is how `skills_sync` remembers a deletion, so a pack removed with the manifest intact stays removed instead of being restored on the next sync.

### 2. Copy the terminal policy

Run `/home/hermes/.hermes/scripts/hermes-profile-docker-setup.sh <name> --policy-only` before any work is admitted.
This mirrors three managed policy keys and the selected profile image without adding any shared mount; it is required for isolated workers too.
See [the managed-scope gap](#managed-scope-gap-at-5575a08).
The live multiplexer discovers new profiles on create/delete and periodic rescan, so establish platform disables and credentials before letting a new profile receive work.

### 3. Disable the platforms this profile will not use

**Before restarting the shared gateway:**

```sh
/home/hermes/.local/bin/hermes -p <name> config set platforms.telegram.enabled false
/home/hermes/.local/bin/hermes -p <name> config set platforms.api_server.enabled false
```

**A new profile can inherit base platform credentials from `~/.hermes/.env`**, so a profile that has configured nothing still starts with the default profile's platform credentials.
Left enabled, it connects Telegram with the default profile's token, which the API rejects as `token already in use`, and it binds the default `api_server` port 8642, which fails as `address already in use`.
Disabling them afterwards fixes it, but the shared gateway may encounter those collisions until you do.

### 4. The mailbox, if the profile will use email

The mailbox has to exist before step 6 can connect to it.

Create `<name>@cynexia.io` at Purelymail.
That is a manual step in the Purelymail admin today; Purelymail also has an API, and the key and a pointer to its specification are stored at `op://Homelab/purelymail/api_key` and `op://Homelab/purelymail/api_spec`.
Nothing in this estate automates mailbox creation, and this step is not the place to start: the API is named so a future automation knows where to look.

Then record the credential as a 1Password item, and give the mailbox the password from it:

```sh
op item create --category login --vault hermes --title purelymail-<name> \
  --generate-password='letters,digits,symbols,32' \
  'username[text]=<name>@cynexia.io'
```

**Type the fields explicitly**, as AGENTS.md requires of every `op item create`: a bare `field=value` is stored **concealed**, and the address is an ordinary identifier rather than a secret, so it is `[text]`.
Read the generated password back with `op read` when Purelymail asks for it, and never echo it.

**The two vaults differ deliberately.**
The API key is estate administration and belongs in `Homelab` with the operator's other administrative credentials.
The per-agent mailbox credential belongs in `hermes`, because that is the only vault the VM's own service account can read — the same reasoning as [step 3 of the install](#3-create-the-monitor-and-store-its-token).

### 5. Secrets, if the profile needs any

```sh
/home/hermes/.local/bin/hermes -p <name> config set secrets.onepassword.enabled true
```

Then set `secrets.onepassword.env` to the map of environment-variable name to `op://vault/item/field` reference the profile needs, as [step 4 of the install](#4-inject-the-token-into-the-default-profile) does for the default profile.
The **operator** adds `OP_SERVICE_ACCOUNT_TOKEN` to the profile's own `.env` by hand; that value goes nowhere else, and no agent handles it.

**Give every variable in that map a name no other profile uses.**
Prefix each name with the profile's own name, and use the prefixed name in every `${...}` placeholder that consumes it.
Every profile on this VM carries its prefix as of September 6, 2026: `DEFAULT_`, `EMH_`, `HAL_` and `WEB_WATCHER_`.
The prefix is the **profile** name, not the mailbox name — the default profile reads `kairos@cynexia.io` and its prefix is still `DEFAULT_`.

`hermes-webui` serves all six profiles from one process, which is what makes a shared name dangerous.
`_interpolate_env_vars` in `tools/mcp_tool.py` resolves a `${VAR}` placeholder through `agent.secret_scope.get_secret`, which reads the secret scope installed on the current context and can fall back to the process environment on a miss. Multiplex mode is now enabled; keep the unique secret names rather than assuming it repairs every caller.
Either of those can be holding another profile's value for a name that more than one profile declares, and nothing in the resolution reports the mismatch.
A name only one profile declares cannot resolve to the wrong account: it resolves correctly or it stays a literal placeholder, which fails loudly at the first call.
This is a workaround for a hermes-agent defect, filed upstream as [hermes-agent issue 2](https://github.com/mnbf9rca/hermes-agent/issues/2); the prefixes stay until that is fixed.

**A name several profiles share is only safe when every profile points it at the same item.**
`HERMES_DASHBOARD_SESSION_TOKEN`, `HINDSIGHT_API_KEY` and `API_SERVER_KEY` are shared on purpose and must not be prefixed: each resolves to one `op://` reference estate-wide, so resolving it through another profile's scope returns the same value.
The rule bites on a name that means a *different* secret in each profile, which is what a per-mailbox credential is.

**The 2026-09-06 incident is what this rule is for.**
`emh` and the default profile (mailbox `kairos`) both declared `EMAIL_PASSWORD`, pointing at different Purelymail items, and the `mcp-email-server` that `hermes-webui` spawned for `emh` was started with the `kairos` password.
Every authenticated IMAP and SMTP call from an `emh` WebUI chat then failed as `provider_failure`, while account discovery kept succeeding, because discovery needs no login.
The per-profile gateways then in use were unaffected: each served one profile, so its scope and environment agreed.

**The `EMAIL_*` names are read verbatim and cannot be prefixed.**
The email adapter and gateway config reader use these fixed names through profile-aware secret getters:

```
EMAIL_ADDRESS          EMAIL_IMAP_HOST   EMAIL_SMTP_HOST   EMAIL_ALLOWED_USERS       EMAIL_HOME_ADDRESS
EMAIL_PASSWORD         EMAIL_IMAP_PORT   EMAIL_SMTP_PORT   EMAIL_ALLOW_ALL_USERS     EMAIL_HOME_ADDRESS_NAME
EMAIL_POLL_INTERVAL    EMAIL_AUTHSERV_ID EMAIL_TRUST_FROM_HEADER                     EMAIL_HOME_ADDRESS_THREAD_ID
```

Sources: `plugins/platforms/email/adapter.py` and `gateway/config.py` in the installed hermes-agent checkout.
Some non-secret settings also have YAML equivalents; `EMAIL_PASSWORD` itself has no configurable name or password-key alternative.
**Only `EMAIL_PASSWORD` is affected by the prefix rule**, because it is the only one of them that comes from the 1Password map; the rest are non-secret settings the profile's `.env` carries, and `.env` is per-profile and never shared.
So a profile running `platforms.email` keeps a verbatim `EMAIL_PASSWORD` entry **as well as** its prefixed one, both against the same `op://` reference.
The shared gateway constructs and connects each secondary email adapter inside that profile's `_profile_runtime_scope`.
The adapter reads `EMAIL_PASSWORD` through `gateway.platforms._shared.get_scoped_secret` and saves it on that adapter instance.
A missing value in an active secondary profile scope does not fall back to another profile's process environment.
The unscoped default adapter uses the default profile's environment.
This profile scoping, rather than a separate gateway process, keeps the fixed credential name local to its mailbox.
The prefixed name remains for the mail MCP server used by WebUI.

**The pattern a new email profile copies**, with `<PREFIX>` the profile name upper-cased and `<name>` the mailbox:

```yaml
secrets:
  onepassword:
    env:
      <PREFIX>_MAIL_USERNAME: op://hermes/purelymail-<name>/username
      <PREFIX>_EMAIL_PASSWORD: op://hermes/purelymail-<name>/password
      EMAIL_PASSWORD: op://hermes/purelymail-<name>/password    # verbatim, for the platform adapter
mcp_servers:
  mail:
    env:
      MCP_EMAIL_SERVER_EMAIL_ADDRESS: ${<PREFIX>_MAIL_USERNAME}
      MCP_EMAIL_SERVER_PASSWORD: ${<PREFIX>_EMAIL_PASSWORD}
```

The four deployed profiles also carry a `<PREFIX>_MAIL_PASSWORD` entry against the same password reference.
Nothing consumes it — it is the renamed remains of the pre-fix `MAIL_PASSWORD` — so a new profile does not need it.
`web_watcher` carries the prefixed names with no `mcp_servers` block at all, for uniformity: it runs the email platform but no mail MCP server.

**A restart is what applies any of this**, because the 1Password provider resolves the map at start.
Restart the shared `hermes-gateway`, then the dashboard and WebUI when their cached configuration needs refreshing.
Read the profile’s `1Password: applied N secrets` log; compare N with that profile’s configured secret map.

Mail profiles also need an explicit `EMAIL_AUTHSERV_ID` pin established from the topmost Authentication-Results header using read-only IMAP sampling. The current Purelymail profiles use `purelymail.com`; an empty mailbox remains unverified until its first permitted message.

### 6. The email platform, if wanted

The deployed profiles keep email settings in `.env`; the adapter also accepts several non-secret settings through `platforms.email.extra`.
Put `EMAIL_ADDRESS`, `EMAIL_IMAP_HOST`, `EMAIL_SMTP_HOST` and `EMAIL_ALLOWED_USERS` in the profile's `.env`, let `EMAIL_PASSWORD` arrive through the 1Password map from step 5, and then:

```sh
/home/hermes/.local/bin/hermes -p <name> config set platforms.email.enabled true
```

Keep each setting in one place.
The native adapter gives scoped environment values precedence over the corresponding YAML address and host settings.
`EMAIL_PASSWORD` still comes from the profile secret map, not `platforms.email.extra`.

### 7. The standard docker mounts, for a trusted profile only

```sh
/home/hermes/.hermes/scripts/hermes-profile-docker-setup.sh <name>
```

This gives the profile the shared WebUI attachments inbox along with its workspace, so run it only where every profile's uploads may be read — see [The docker terminal sandboxes](#the-docker-terminal-sandboxes) for the three entries and for the [#6939](https://github.com/nesquena/hermes-webui/issues/6939) stopgap that will remove one of them.
Skip it for an isolated worker, which is the `safer_web_reader` pattern ([safer-web-reader.md](safer-web-reader.md)): that profile deliberately has no `docker_volumes` of its own and takes the platform's per-profile sandbox workspace instead.

**Launching a profile from the WebUI writes those same three entries itself**, into the profile's `config.yaml` and as `TERMINAL_DOCKER_VOLUMES` in its `.env`.
The WebUI's `.env` line is double-quoted, which its own parser cannot read.
Run the helper afterward: it preserves existing YAML mounts and writes matching single-quoted JSON to `.env`, even when all three standard entries are already present.
A repeat run leaves both files untouched once they agree.
The helper uses upstream's locked atomic environment writer and preserves the file mode.
After a mount change, restart the WebUI and recreate only idle containers whose mounts are stale; verify through a fresh WebUI chat.
See [The docker terminal sandboxes](#the-docker-terminal-sandboxes).

#### Hal: read-only tool-result spillover

Hermes saves large tool results under `$HERMES_HOME/cache/spillover` and can return that host path to a card worker.
The native spillover directory is fixed, so Hal adds one read-only identity bind rather than changing native storage.
This exposes only Hal's own tool results; the reader profile and other profiles get no additional mount.

1. Preserve Hal’s existing YAML `terminal.docker_volumes` entries.
2. Append `/home/hermes/.hermes/profiles/hal/cache/spillover:/home/hermes/.hermes/profiles/hal/cache/spillover:ro`.
3. Run `/home/hermes/.hermes/scripts/hermes-profile-docker-setup.sh hal --sync-mounts` to mirror the list into `.env`.
4. Compare the core and WebUI runtime mount lists.
5. Let fresh card workers use the new scope.
6. Recreate only idle stale Hal containers, preserving their host-backed home and workspace.
7. Restart the WebUI only if its cached scope needs clearing.
8. Check the exact bind has `RW=false`.
9. Read a known spillover file through Hal’s sandbox.
10. Confirm a write through that bind fails.

The native `/root/.hermes/cache/spillover` alias alone did not cover the host path handed to Hal on 19 October.
No whole-profile home, board database, other profile’s cache or credential directory is mounted for this fix.
A spilled `kanban_show` reader result is still untrusted data when Hal reads it with `read_file`.

### 8. Shared gateway admission

Do not create a `hermes-gateway-<name>` unit. Upstream multiplexing discovers profiles automatically; the one `hermes-gateway` unit serves messaging and worker contexts.
Configure only the intended adapters. At 5575a08, a new fully configured profile can be published atomically and admitted through native `gateway/run_profile_reconcile.py` plus `hermes_cli.profiles._notify_multiplexer(name)`, without a shared restart. Stage it outside live discovery first. A worker-only profile has no messaging adapters and needs no separate unit.
Restart the shared gateway if a change needs fresh startup discovery; browser runtime changes also require restarting WebUI.
The alive check continues to track the final three units, not one unit per profile.

### 9. Verify

```sh
tail ~/.hermes/logs/gateway.log
/home/hermes/.hermes/scripts/hermes-profile-docker-setup.sh <name> --policy-only
```

The shared root log names connected profile adapters. The helper asserts the actual `build_profile_terminal_scope` policy, including the selected image. For a worker-only profile, prove admission with its own real ticket and inspect its disabled adapters; absence of a connection line is expected. Compare the WebUI runtime reader too after a mount change.

## unattended-upgrades

Security-only, by the two `Origins-Pattern` entries in `/etc/apt/apt.conf.d/52unattended-upgrades-local`.
The estate's other update surfaces — keel, Renovate, the update session — cover feature upgrades; this one exists for kernel and library CVEs.

**Only the second pattern matches anything on Debian 13.**
Since bullseye the security archive carries its own codename (`n=trixie-security`), so `codename=${distro_codename},label=Debian-Security` matches nothing.
It is the vendor idiom, kept deliberately for archive migration — but it means a typo confined to the second line alone leaves **zero** security patching behind a stamp file that still looks fresh.
Edit the two together and re-run the `apt-config dump` above.

**Why `#clear` precedes `Origins-Pattern`.**
For a scalar the last assignment wins, so a `52` file overrides `50`.
`Origins-Pattern` is a **list**, and a later block *appends* to whatever `50` already declared.
Without the clear, apt reports five patterns including `label=Debian` — the whole stable archive — and `unattended-upgrades` would install all of it and then reboot the machine at 04:45.
The clear is the single thing making this policy security-only.
Do not delete it as redundant.

### The schedule

| Time (UTC) | What runs | Why there |
|---|---|---|
| 02:00 | `hermes-pull` SSHes in for the nightly zip | Cluster-side, done well before 02:30 |
| 03:00 | The restic sweep | Cluster-side; does not touch this VM |
| 03:30 (+0–10 min) | `apt-daily.timer` refreshes the package lists | `unattended-upgrade` installs only from the cache and never refreshes the lists itself |
| 04:00 (+0–10 min) | `apt-daily-upgrade.timer` runs `unattended-upgrade` | Leaves a 35-minute margin before the reboot |
| 04:45 | Automatic reboot | Reboot is **on**, by operator decision, August 26, 2026 |
| 05:15, Sundays only | `hermes-sandbox-refresh`, as a cron job inside the default gateway | **Between** the reboot window and the daily check: the reboot has already left the sandboxes `exited`, which is the cheapest state to replace them in, and the removals are done before anything reports on the VM. Weekly; upstream desktop floats and the reviewer uses its separately configured derived image |
| 05:45 | `hermes-app-alive`, as a cron job inside the default gateway | **One hour** after the reboot, so the reboot has finished and the lingering user units have come back before anything looks at them |
| 06:15 | `security-reviewer-sweep.py`, a no-agent job owned by security_reviewer | Removes only expired final reports for closed reviewer tickets |

Hal’s separate house-check schedule is 09:00 Europe/London, outside this UTC maintenance table.
The three maintenance cron rows have no jitter: they are cron expressions in the agent's scheduler rather than systemd timers, and neither `RandomizedDelaySec` nor `Persistent=` applies to them.
Catch-up after a missed window comes from the scheduler's ticker picking up past-due jobs instead.

Both apt drop-ins set `FixedRandomDelay=true`, so the offset is derived from the machine ID and unit name rather than re-rolled nightly.
The gap between the refresh and the install is then a constant for this machine, and `systemctl list-timers` can be read once and still trusted tomorrow.

**The jitter is 10 minutes, not Debian's hour, and that is a reboot correctness matter rather than a tidiness one.**
`unattended-upgrade` hands the literal `04:45` to `shutdown`, and systemd resolves a time that has already passed to the same time **tomorrow**.
An upgrade still running at 04:45 does not delay the reboot by minutes; it slips it by 24 hours, and the kernel sits installed-but-not-running for a day.

**The two apt jobs share a lock, and the waiter blocks for up to an hour.**
A 03:30 refresh that overruns past 04:00 makes the upgrade **block** rather than fail.
That is the safe behaviour — nothing installs against half-refreshed lists — but the block eats into the 35-minute margin before the reboot.
If `systemctl list-timers` shows the upgrade starting late, look at the refresh run's duration first.

### Why nothing watches apt directly

There is no `-success` file and nothing creates one.
`unattended-upgrade` writes `/var/lib/apt/periodic/unattended-upgrades-stamp` for itself in `write_stamp_file()`, and it writes it on a run that found **nothing to do** just as readily as on one that installed everything.
It writes it on a `--dry-run` too, so a dry run is never a way to inspect the stamp — see step 9 of the install.
The other files in that directory belong to `apt.systemd.daily` and are not this.

So the stamp proves the timer fired, not that anything was patched.
There is no stamp-age alarm after removal of the application update runbook.
That is accepted.
The VM has no MTA, so `unattended-upgrades` mail would be a silent failure; the journal is the record.

## What the daily check does not watch

This section describes `hermes-app-alive`; the [Hal house-check limits](hal-daily-check.md#what-this-check-does-not-watch) are separate.

The alive check verifies the final three units, fresh PM WebUI/agent imports and WebUI `/health`; its arrival proves the shared gateway scheduler executed.
It makes **no chat turn**, memory write, adapter login or reader-containment test.
A secondary profile can fail while the shared process remains active; neither a unit count nor the aggregate runtime record proves each mailbox works.
A fresh import can succeed while a long-lived process has stale modules: restart affected serving processes after repair and use actual authenticated panels.
Hindsight's backend canary checks the backend, not every profile's admission/bank credentials or asynchronous retain path.
A failed reader plugin or changed dispatch surface can leave research unavailable or widen its capabilities with every scheduled monitor green.
Detection is daily; a missing heartbeat alarms only after its 24-hour interval and 6-hour retry. The external dashboard monitor catches a dead VM sooner.
Losing any cron job is agent-state loss: the alive job eventually alarms by silence, while refresh and reviewer cleanup have no heartbeat. Read the default, Hal and reviewer cron stores after restore.
The daily check says nothing about apt timer age, Docker images, busy sandboxes or the weekly refresh's success; its run history remains the diagnostic record.
No extra monitors were added.

## What moving the check inside hermes traded

The check used to be a systemd user timer with a service unit and an environment file holding the push URL.
Three things changed with it, and two of them cost something.

**Gained: the default gateway is now exercised** — the detail is in [step 5](#5-create-the-daily-checks-cron-job) and [what the daily check does not watch](#what-the-daily-check-does-not-watch).

**Gained: nothing holding the token is installed on this VM.**
The old environment file lived under `~/.hermes` and rode the nightly `hermes backup` zip to the `hermes-dumps` PVC and on to B2.
The token now arrives injected at gateway start and exists only in memory.
That is hygiene rather than incident response: the push token is a tier-2 spam-target identifier, not a secret, so its old exposure needs no rotation and no honesty-box row.

**Cost: the hang bound is weaker.**
The service unit set `TimeoutStartSec=120`, so a wedged run failed rather than sitting there.
A no-agent cron subprocess is bounded instead by the scheduler's `script_timeout_seconds`, which defaults to **3600** (`hermes_cli/config_defaults.py`, overridable through `cron.script_timeout_seconds` or `HERMES_CRON_SCRIPT_TIMEOUT`) — an hour rather than two minutes, but bounded, and far inside the monitor's 24-hour heartbeat.
Every command in the script that reaches the network carries its own `-m 15`, so the residual exposure is a wedged `systemctl` or a wedged Python import rather than an unbounded wait.

**Cost: the schedule is agent state** — a systemd timer is a file this repository owns; a cron job is not.
The detail, and `hermes cron list` as the check, is in [Create the daily check's cron job](#5-create-the-daily-checks-cron-job).

## Facts about this VM

Recorded so nobody rediscovers them.

**`hermes` is not on the non-interactive ssh PATH.**
That PATH is `/usr/local/bin:/usr/bin:/bin:/usr/games`, and `~/.local/bin` is not on it, so `ssh hermes@… 'hermes …'` fails with "command not found".
Call `/home/hermes/.local/bin/hermes` — the published PM-aware launcher.
A `systemd-run --user` unit gets the same clean environment and needs the same absolute path.

**PM owns interpreter and dependency selection.**
Do not hardcode a store generation or revive the legacy venv. Resolve the current project interpreter through upstream's PM APIs when running diagnostics; use the guarded canonical helper for deep imports.

**Updater restart coverage is not a substitute for explicit runtime verification.**
After a core update, verify the shared gateway and explicitly restart/check dashboard and external WebUI; a source update can leave old modules resident until a later reboot.

**A Hermes process discovers an MCP server's tools once, when it connects, so a tool added to a server is invisible to every already-running Hermes process until that process restarts.**
Nothing re-lists tools on a live connection, and neither the server's own restart nor a reconnect re-runs discovery, so the stale process keeps serving the tool list it captured — which is how the `how-to-use-health-data` tool added to the health MCP server on September 4, 2026 stayed missing on September 5.
**A WebUI chat does not run in the profile gateway**, it runs in `hermes-webui`, so restarting the shared `hermes-gateway` fixes the profile's cron and messaging turns and changes nothing about the conversation in the browser.
Restart the shared `hermes-gateway`, `hermes-webui` and `hermes-dashboard`.
Verify the WebUI through an authenticated tool call.
Verify the dashboard through its authenticated MCP test action, as above.
A fresh `tools.mcp_tool` line, `MCP server '<name>' (HTTP): registered N tool(s): …`, names the discovered tools and supports that evidence.
Connections can be lazy, so an absent line immediately after restart does not establish a failure.

Log destinations follow the active profile and logging handlers, so the base `HERMES_HOME` does not identify every destination.
On October 30 with core v0.22.5, the WebUI process held the emh profile's `logs/agent.log`; the dashboard held root, emh and hal logs.
Shared profile logs omit PID attribution, so a line in one file alone does not prove which service wrote it.
Use successful live actions instead of changing logging configuration or investigating an absent line after those actions pass.

**Update snapshots live in `~/.hermes/backups/`**, not `~/.hermes/pre_update_backups/` — an earlier draft of this design named the second path and it does not exist.
Retention is five snapshots, pruned after each write and floored at one, at roughly 200 MiB each.

**The API server's posture, as found:** `API_SERVER_HOST=0.0.0.0` and `API_SERVER_CORS_ORIGINS=*` on all three profiles, with the gateway logging a network-accessible and unsandboxed warning on every start.
**The host-level bind is not the control, and nothing on the VM is.**
[homelab.md's security posture section](homelab.md#security-posture--read-before-fixing-any-of-it) records what actually holds: the VM runs no host firewall, the LAN is a trusted zone, and the only gate an agent cannot forge is the Cloudflare Access service token on the published hostnames.
Firewalling 8787, 9119 and 8642 down to the cluster egress address is the obvious hardening and is a change of its own.

**Profile-prefixed webhook routing now follows multiplex mode.**
Only verify `/p/<profile>/` callers where that adapter is actually configured; do not assume the aggregate gateway state lists secondary mailbox connections.

**`/var/lib/docker` is a disk of its own, and nothing backs it up.**
A second 125 GiB virtio-scsi disk on the thick-LVM vmdata pool (SCSI 1, discard and SSD emulation on) was added to VM 103 on August 29, 2026, formatted `mkfs.ext4 -m 0 -L dockerdata` and mounted at `/var/lib/docker` from `/etc/fstab` by UUID, with `defaults,nofail,x-systemd.device-timeout=30s 0 2`.
Three of those choices are deliberate and should survive a rebuild.
`nofail` with the 30-second device timeout keeps a dead data disk from hanging the boot: the VM must come back without docker rather than not come back.
`-m 0` drops the 5% root reserve, which exists to keep a full root filesystem usable and buys nothing on a disk holding only images and containers.
And **Proxmox Backup is off for this disk**, because everything on it is re-pullable — images come from a registry, and a sandbox container is recreated on demand.
The durable sandbox state is not here: it is under `~/.hermes` on the root disk, which the nightly zip covers.

The old `/var/lib/docker` was empty when the new disk was mounted, so nothing was migrated onto it.
It had held one thing: **open-webui**, installed on June 26, 2026 as an undocumented experiment — a generic chat frontend pointed at the default profile's API server on 8642 and exposed on the LAN on port 3000, in no tunnel ingress and in no backup.
Its container, image and `open-webui-data` volume were removed on August 29, 2026, and the volume's contents went with it, deliberately.

The daemon is the rootful system one on the default context, with its root directory at `/var/lib/docker`.
There is no rootless install and no second context.

### The docker terminal sandboxes

Updated October 15, 2026: root 50 GiB, separate Docker disk 125 GiB; desktop image is floating and pulled weekly.

All six profiles resolve the terminal backend to docker after the managed-key mirror. The reader still cannot use terminal because its capability boundary is separate.

| Setting | Value | Source |
|---|---|---|
|`terminal.backend`|`docker`|Managed config plus temporary profile copy|
|`terminal.docker_image`|`nousresearch/hermes-sandbox:desktop`; reviewer uses its signed derived digest below|Profile image, then env fallback, then managed default|
|`terminal.docker_run_as_host_user`|`true`, uid1000|Same|
|`terminal.cwd`|`/workspace`|Same|
|`terminal.docker_volumes`|Profile-specific workspaces/identity/attachments mounts|Profile only|

The managed file is root-owned 0644 in a 0755 directory. Its CLI precedence still works, but multiplex terminal scope bypasses it: [workaround and removal condition](#managed-scope-gap-at-5575a08).
Desktop digest observed October 15: `sha256:c7d8607d076f26a64e0bd6828f74e1764af43fe3281368188459bb693bfa7567`, image 3,050,982,704 bytes.
The existing refresh removed only idle old containers. A fresh hal terminal used desktop with the previous home/workspace mounts and a successful host→container→host diagnostic file round-trip; the diagnostic file was removed.

The default profile's workspace is `/home/hermes/.hermes/workspace`, created for this; every other profile's is `workspace` inside its own profile home.
A settings change takes effect on the next admitted profile turn or after restarting the shared gateway when startup discovery is involved.

**`default`, `emh`, `hal` and `web_watcher` share three base mounts.**
Two are identity mounts: the same host path inside the container as outside.
HAL also has the writable scripts and read-only tool-result spillover identity mounts below.

| Entry | Why |
|---|---|
| `<workspace>:/workspace` | The sandbox's working directory, matching `terminal.cwd` |
| `/home/hermes/.hermes/webui/attachments:/home/hermes/.hermes/webui/attachments:ro` | Chat uploads, read-only, at the host path the WebUI pastes into the prompt |
| `<workspace>:<workspace>` | The workspace again, at the host path the WebUI advertises in its `[Workspace::v1: …]` label |
| HAL only: `<scripts>:<scripts>:rw` | Shared script directory at `/home/hermes/.hermes/profiles/hal/scripts/`; the daily runner is repository-maintained |
| HAL only: `<spillover>:<spillover>:ro` | Oversized tool results at `/home/hermes/.hermes/profiles/hal/cache/spillover/`, readable at the host path returned to the worker |

The identity mounts exist because the WebUI hands the agent **host** paths as text, and a path that does not resolve inside the container is a file the agent cannot read.

**The attachments mount is a stopgap and has an end date.**
hermes-webui saves a chat upload to `~/.hermes/webui/attachments/<session>/` — one directory shared by every profile, not scoped per profile — and pastes that host path into the prompt, so before this mount a sandboxed agent could not read anything a person uploaded.
That is upstream issue [#6939](https://github.com/nesquena/hermes-webui/issues/6939), confirmed by the maintainer, and open pull request [#7022](https://github.com/nesquena/hermes-webui/pull/7022) moves uploads into the profile's own `cache/documents/webui-attachments/<session>/`, which hermes already mounts.
The mount's cost is that every profile's uploads are readable from every sandbox that has it, which is why `safer_web_reader` does not, so it comes out as soon as a hermes-webui update carries the fix.
Use the [attachments stopgap removal condition](#attachments-stopgap-removal-condition) above.

**The WebUI's "workspace" is a label, not a mount.**
Each profile has a file-browser root recorded in `{profile home}/webui_state/last_workspace.txt` and `workspaces.json` — `~/.hermes/webui/` for the default profile.
It scopes the WebUI's own file browser, editor and git panel, and it prepends a `[Workspace::v1: <host path>]` line to the messages that profile sends.
Nothing mounts it into a container: the label is why the third volume entry above exists, and switching workspaces in the WebUI does not change what a sandbox can see.

**Check that a `${VAR}` placeholder survived a configuration edit made through the WebUI.**
On August 31, 2026 something resolved the `${EMAIL_PASSWORD}` placeholder behind `mcp_servers.mail.env.MCP_EMAIL_SERVER_PASSWORD` in the `emh` profile's `config.yaml` into the literal password on disk.
The cause was not pinned down; `hermes config set` was tested and exonerated, which leaves the WebUI's profile-settings save and the `emh` agent itself as the suspects.
The credential was rotated and the placeholder restored the same day.
That placeholder reads `${EMH_EMAIL_PASSWORD}` since September 6, 2026 — see [step 5 of Creating a profile](#5-secrets-if-the-profile-needs-any).
After any configuration change made outside the CLI, `grep -c '\${' <profile home>/config.yaml` and confirm the placeholders are still placeholders.

**`TERMINAL_DOCKER_VOLUMES` must be single-quoted in a profile's `.env`, or the WebUI cannot read it.**
hermes-webui parses that file itself — `v.strip().strip('"').strip("'")` at `api/profiles.py:903`, and again at 1561 on the profile-switch reload — which strips the enclosing quotes without undoing dotenv's `\"` escaping, so the double-quoted form the platform writes reaches the WebUI process as literal backslashes and its terminal cleanup thread logs an `expected valid JSON` error into `~/.hermes/logs/errors.log` once a minute.
Single-quoted JSON parses identically under both readers.
A WebUI profile launch or `hermes config set terminal.docker_volumes` can write the escaped form back; re-check the line after either operation.
The setup helper writes matching single-quoted JSON without dropping extra mounts. Ordinary mode adds the standard trusted-profile mounts; `--policy-only` preserves the selected image and mirrors policy without adding mounts; `--sync-mounts` copies only the saved YAML list to env, supports default and isolated profiles, and adds no attachments or policy. Use sync mode for custom reviewer/report binds.
On October 15, default, emh and web_watcher already matched their three YAML mounts; HAL's four-entry list was repaired separately, and safer_web_reader had no override or explicit volumes.

**Containers are created lazily, per (profile, task-id), so one profile can own several at once.**
Each is named `hermes-<hex>` and labelled `hermes-profile:<name>` and `hermes-task-id:<id>`.
The label is what tooling selects on; the name carries nothing.
Hermes mounts the profile workspace at `/workspace`, along with skills, caches and attachments — and a **host-backed home**: `/home/hermes/.hermes/sandboxes/docker/<task-id>/home` is mounted as `/root` in the container and is writable at host uid 1000.

**The workspace, home and explicitly mounted writable directories are durable.**
Anything written elsewhere in the container filesystem is lost when the container is recreated, so a user-level package install that lands under the mounted home survives a recreation and one that lands in `/usr` does not.
The workspace, home and HAL scripts sit under `~/.hermes`, so they ride the nightly `hermes backup` zip.

**`docker rm -f` on a sandbox is safe while its profile is idle**, verified by removing one and letting it come back.
The next terminal call builds a new container from the local image, with no gateway restart involved.
That is also the whole of the image-update mechanism: a `docker pull` never touches an existing container, so **removal and recreation is the update**, which is what `hermes-sandbox-refresh` exists to do.

**The upstream desktop reference is a floating tag, deliberately.**
Upstream rebuilds the floating `desktop` image, and the weekly pull-and-replace is how those arrive.
The sandbox userland is hygiene, not the containment boundary — the boundary is the host kernel and runc, patched nightly by `unattended-upgrades`.
For HAL, the writable scripts bind lets sandbox-written code run on the host as `hermes`, with access to every profile's secrets, so the boundary is HAL's own judgement over what it reads.

**`no_agent` cron scripts are unaffected by any of this.**
The scheduler runs them as plain host subprocesses (`subprocess.Popen` in `cron/scheduler.py`), so `hermes-app-alive` still runs on the host and reports on the host whatever a profile's terminal backend is set to.

### HAL's own script cron jobs

Hal’s active daily job is `ha-daily-health-email` (`e6a2fb6484b8`), at 09:00 Europe/London.
It runs the repository-maintained `hal-daily-check-morning.py` without an agent.
The old 08:15 producer `9f6d4108c6a6` and its retired pipeline files were deleted on 24 October 2026.
`ha-observer-weekly-audit` (`ea2df3246202`) remains a separate weekly agent job.
Its recorded delivery to the old `webui` origin fails with `unknown platform 'webui'`.
The [Hal persona](../../hermes-vm/profiles/hal/SOUL.md#weekly-audit--ea2df3246202) retains that audit’s checklist and report format.
Agent-authored observer timers are also separate from the daily pipeline.
Profile-owned cron list, edit and run operations use Hal’s store under the shared gateway.
The default profile owns the alive and sandbox-refresh jobs above.

Hal’s script directory is `/home/hermes/.hermes/profiles/hal/scripts/`.
It contains the installed repository files and can also contain legacy agent-authored scripts.
Its config adds one writable identity bind:

```yaml
terminal:
  docker_volumes:
    # Keep the existing entries and append this HAL-only bind.
    - /home/hermes/.hermes/profiles/hal/scripts:/home/hermes/.hermes/profiles/hal/scripts:rw
```

HAL writes, reads and edits a script at that identical absolute path inside Docker and on the host.
A no-agent script job names only the relative filename, such as `hal-daily-check-morning.py`.
The host gateway resolves it beneath HAL's own `scripts/` and executes it as the host `hermes` user, without a model turn.
The workspace's `/workspace` alias and host-path identity mount do not cover this directory.
A script written to the sandbox's `/root/.hermes/scripts/` instead lands in its persistent container home, not the scheduler's script directory.
The tool rejects absolute script arguments and paths escaping `scripts/`; changing `workdir` does not change lookup.

The gateway's profile terminal scope gives explicit YAML `terminal.docker_volumes` precedence over `.env`.
External WebUI at `fd0141fa` instead loads YAML first and overlays `.env` afterward (`api/profiles.py:get_profile_runtime_env`).
Keep HAL's `TERMINAL_DOCKER_VOLUMES` equal to its approved YAML list; follow the single-quoted JSON rule above and re-check after raw config writes.

After changing HAL's mounts, restart `hermes-webui` to clear its cached sandbox configuration.
Recreate only idle HAL containers; preserve their host-backed home and workspace.
An existing container retains its old bind layout until recreation.
Verify the scripts path through an authenticated HAL WebUI chat as well as the gateway path.
The [canonical HAL persona](../../hermes-vm/profiles/hal/SOUL.md) records this convention.
On rebuild, copy it to `/home/hermes/.hermes/profiles/hal/SOUL.md`.
The current Hal guide lives at `/home/hermes/.hermes/profiles/hal/workspace/HA-WATCH.md`.
`deliver=local` keeps stdout in the host cron run record; that output directory is not part of the scripts bind.
No repository checkout or cron-store mount is needed.

### Hal daily house check

The [Hal daily-check runbook](hal-daily-check.md) owns the end-to-end flow, files, state, retries, diagnostics, monitor verdicts, trial command and code rollback from git.
The [canonical Hal guide](../../hermes-vm/profiles/hal/HA-WATCH.md) and [persona](../../hermes-vm/profiles/hal/SOUL.md) describe Hal’s own task and prose rules.
The morning script collects facts and sends without an agent; later native cards provide update assessments and device look-overs.

### The former Home Assistant event feed on `hal`

The native Home Assistant plugin was disabled and privately archived on October 18, 2026.
Its private rollback archive was deleted after operator acceptance on 24 October 2026.
`HASS_TOKEN` and HA credentials were removed from agent/worker mappings.
Hal uses the operator’s read/write MCP address for explicit authenticated email-reply instructions, under the 22 October 2026 ruling.
The host morning runner alone resolves the separate runner credential reference.
The old producer and its host-only credential adaptation were removed on 24 October 2026.

Before removal, an empty native watch list limited unsolicited HA event turns.
A nested `platforms.homeassistant.enabled: false` alone did not hold when `HASS_TOKEN` could re-enable the adapter.
Those historical settings are not the current restriction; restoring that plugin or token would reopen the gap.
Current reply-action scope is described in [Home Assistant access and mounts](hal-daily-check.md#home-assistant-access-and-mounts).
Code rollback reinstalls [files from git history](hal-daily-check.md#code-rollback).

### `hermes` CLI spellings

Recorded from the August 26, 2026 survey.
Three of these were wrong in earlier drafts and one reported contradiction was a false alarm.

| Spelling | Note |
|---|---|
| `hermes secrets onepassword` / `op` / `1password` | **Aliases.** The help line reads `onepassword (op, 1password)`. Both spellings committed elsewhere in `docs/` are correct; there is nothing to fix |
| `hermes config show` | **Not** `config list` |
| `hermes -p <profile> memory off` | The first-class way to drop the external memory provider, rather than editing a config key |
| `hermes tools disable NAME... --platform api_server` | Disables a toolset. **Its default platform is `cli`**, so omitting `--platform` disables it somewhere other than where you meant |
| `hermes update --check` / `hermes update --plan` | The read-only pair. **`--check` exits 0 whether or not an update is available** — read its output, not its status. Neither names the ref it would install |


### Security reviewer sandbox image

The deployed `security_reviewer` uses its own `terminal.docker_image` and matching `.env` fallback:

```text
ghcr.io/mnbf9rca/hermes-security-reviewer:stable
```

The signed candidate was deployed before merge by operator ruling. After promotion, the profile switched to `stable` on October 16; it resolved to the same verified digest `sha256:5a34087dbcdd4dd91315f0546db8d9125691dcc04c4210dfc306a4fdf304ac03`, image config `sha256:e2b75dd66b889c671dab4e616ebb49102793084c99e07bdea5a99577776a73d3`. Both YAML and env select stable; all mounts are preserved. Managed scope and the five other profiles keep `nousresearch/hermes-sandbox:desktop`. The reviewer base deliberately trails their floating image by at least seven days.

`hermes-vm/sandbox-image/` builds `ghcr.io/mnbf9rca/hermes-security-reviewer` for linux/amd64 from a versioned `vX.Y.Z-desktop@sha256:...` base. It preserves the desktop runtime and adds ShellCheck, Bandit, Gitleaks, Semgrep, ruff, yamllint and CodeQL. Native release archives, including the CodeQL bundle, have committed SHA-256 checksums; `pyproject.toml` declares the four direct Python tools and `uv.lock` records their resolved closure and artifact hashes. A tag-and-digest-pinned uv image supplies `uv sync --locked --no-dev --no-build` into the tools venv; installation consumes the lock without resolving and must pass `uv pip check`. There are no runtime tool installs. Trivy and pip-audit are not included.

The image carries MCP 1.29.0 under Semgrep’s exact pin: [GHSA-84m7-p3x7-pcfv](https://github.com/advisories/GHSA-84m7-p3x7-pcfv) remains outstanding and unreachable because the reviewer never starts an MCP server; lock maintenance can pick up the fix once Semgrep permits it.

CodeQL uses the checksum-pinned Linux x64 bundle from `github/codeql-action`, retaining the CLI, Python security queries, shared dependencies and notices while removing other language packs/extractors. Python is exercised by the image gate: it creates a tiny database with `--language=python --build-mode=none`, runs `codeql/python-queries:Security/CWE-078/CommandInjection.ql`, and checks a command-injection finding plus a clean control. CodeQL does not cover shell. Use baked queries, a writable workspace copy and `--threads=1 --ram=1024`; never target builds, target-controlled queries/configs, custom extractors or runtime pack downloads. The paired Renovate attachment manager updates `CODEQL_VERSION` and `CODEQL_SHA256` together under the existing seven-day rule.

Commands are linked into `/usr/local/bin`, because Hermes's login-shell environment resets PATH. `/etc/hermes-reviewer-tools.txt` records installed versions. Semgrep metrics and version checks are off; its two locally authored rules live under `/opt/hermes-reviewer/rules/`. Use that explicit rules file, `--metrics off --disable-version-check`, and the explicit baked Ruff/yamllint configs. Never use target-controlled configs, Semgrep registry/auto rules, autofix or target builds. Static tools provide limited checks; missing coverage belongs in the report's `Tooling gaps:` line, not an ad hoc package install.

Renovate groups base/uv versions and digests, native versions/checksums, the four direct Python tools and workflow actions as `security-reviewer image inputs`; its pep621 manager regenerates `uv.lock`, and scoped lockfile maintenance refreshes transitives through the resolver. Version and digest updates require seven-day-old source timestamps; uv also enforces the cooldown during lock refreshes. A mutable `desktop` tag cannot evidence an older release; default Docker versioning follows the versioned `-desktop` suffix instead. Missing timestamps or a checksum/closure failure stop the update. The cooldown deliberately delays fixes by seven days. Image-input automerge is **off**. After the image workflow is merged, the operator-visible settings step adds `sandbox-changes` and `sandbox-build` to the existing master ruleset without removing other checks; only a subsequent verified change enables automerge. Do not add required contexts while the workflow is absent from master: with no bypass actor, that blocks other PRs.

The manual bootstrap and exceptional delivery procedure is below. Routine Renovate image updates use the same checksum, cooldown, non-root fixture and signed-digest promotion gates in CI; they do not require repeating the manual VM candidate gate. The profile now tracks `stable`; the existing weekly refresh pulls that configured tag and replaces only idle stale-image containers. Promotion changes the tag, not an already-running container.

1. Change image inputs on an isolated branch, rebase onto freshly fetched master, and open a PR.
2. Read all PR checks, including the existing `changes`/`lint`/`build` and new `sandbox-changes`/`sandbox-build`. The build tests all seven positive fixtures and clean controls as UID/GID 1000 through `bash -lc` with a temporary writable HOME. An altered-checksum build must fail before archive extraction.
3. Resolve `sha-<PR head SHA first 12 characters>` once to its registry digest.
4. Verify that digest with Cosign, GitHub issuer `https://token.actions.githubusercontent.com`, and certificate identity `https://github.com/mnbf9rca/kubernetes_config/.github/workflows/hermes-security-reviewer-image.yml@refs/pull/<PR>/merge`.
5. Confirm the operator's GHCR publication/pull authorization; do not add VM registry credentials or change package visibility implicitly.
6. Pull that exact digest on the VM and run the disposable candidate without profile, workspace or Docker-socket mounts:

   ```sh
   docker run --rm --user 1000:1000 --entrypoint /bin/sh "$IMAGE" \
     -c 'tooling_home=$(mktemp -d /tmp/hermes-reviewer.XXXXXX); export HOME="$tooling_home"; exec bash -lc /opt/hermes-reviewer/test-tools.sh'
   ```

7. Obtain the independent review and report the candidate deployed with its PR number; the operator merges.
8. Check `sandbox-promote` succeeded and registry `stable` equals the verified digest. The merge job resolves the PR tag once, verifies the anchored PR signature, then retags that same digest; it never rebuilds. Fork builds cannot push/sign and must be reopened on a repository branch before merge.

The installed weekly refresh derives each profile's backend/image from `build_profile_terminal_scope` through the current PM runtime with lazy installs disabled. It pulls desktop and the reviewer's configured stable tag once each; it never promotes or rewrites a profile ref. Both idle checks still protect stale-container removal. Worker-only status with an unknown gateway state is conservatively kept.

For an urgent reviewer image rollback, use upstream `atomic_roundtrip_yaml_update` under the selected PM runtime to set only `/home/hermes/.hermes/profiles/security_reviewer/config.yaml`'s `terminal.docker_image` to `nousresearch/hermes-sandbox:desktop`, then run the setup helper `security_reviewer --policy-only` to mirror the new image into `.env`. The helper preserves an existing image; it cannot select a replacement by itself, and `config set` refuses the managed-pinned leaf. Pull the selected image, recreate only proven idle reviewer containers, and record missing scanner coverage. Do not change registry stable or managed scope to roll back one profile.


### Security reviewer operation

`R` below means `/home/hermes/.hermes/profiles/security_reviewer`. The deployed model is `z-ai/glm-5.3` through `nous`, shared provider login, with no copied credentials. All messaging/API adapters are disabled, `mcp_servers` is empty, external memory is unconfigured and 1Password is disabled. The CLI/managed worker surface is `[web, file, terminal, skills, kanban]`, plus the additive request tool below. No HA, mail, MCP, cronjob toolset or host-shell bridge is provided. The [canonical persona](../../hermes-vm/profiles/security_reviewer/SOUL.md) treats targets as untrusted data: static inspection only, no target scripts/tests/hooks/builds, installs, automatic fixes or publication.

File tickets on explicit board `security_reviewer`, assigned to `security_reviewer`, with absolute input paths, snapshot and scope. Ordinary tickets inherit its model; do not set provider/model overrides. Native board creation leaves the global current-board pointer unchanged. The board is routing, not an ACL. The operator accepted ordinary `kanban_create` authority, including its ability to dispatch another profile with a reviewer-written body; the persona forbids doing so. This is not a review gate on HAL or another agent.

The reviewer mounts existing other profiles' workspace/scripts/skills directories at identical absolute paths read-only; missing directories are omitted. Its own workspace is writable at `/workspace` and its host path. There is no custom writable scripts, shared attachments, whole-profile-home, board database or Docker socket bind. Upstream still provides its normal writable sandbox home and read-only own-profile media/cache/skills mounts. Read-only inputs prevent ordinary edits, not network disclosure: the reviewer retains web and terminal network access.

Default, emh and web_watcher have this read-only report bind in saved YAML and matching single-quoted JSON `.env`:

```text
/home/hermes/.hermes/profiles/security_reviewer/workspace:/home/hermes/.hermes/profiles/security_reviewer/workspace:ro
```

**HAL's bind is owed at its next idle window.** Operator ruling forbade editing HAL YAML/env or recreating its container during admission. Append the exact line above to HAL's YAML `terminal.docker_volumes`, preserving its scripts bind; run `hermes-profile-docker-setup.sh hal --sync-mounts`; compare both runtime readers; recreate only a proven idle stale HAL container. Do not restart shared services for this owed step without a separate need/authorization. `safer_web_reader` is excluded from requester/report mounts and its config/env remain unchanged.

Admission at 5575a08 used native clean creation outside live discovery, complete offline configuration, atomic rename into `profiles/security_reviewer`, then native reconcile notification. No shared gateway, WebUI or dashboard restart occurred. Default's actual Docker terminal read the completed review by hash and refused a neighboring write; the reviewer's input write was refused and its own workspace write reached the host. HAL remained untouched.

Each review/question atomically finalizes `R/workspace/reports/<task-id>/report.md` and completes its own card with that absolute path, verdict/answer, input hash/revision, evidence/limitations and **Tooling gaps:**. Missing or failed checks are unassessed, never clean; repeat non-empty gaps in the completion summary. Question `t_2cd8fa76`, static review `t_63805e4d` and controlled missing-CodeQL check `t_32c26f43` exercised the live route on October 15. Actual managed sessions billed Nous using GLM-5.3; all six tools passed positive/clean fixtures inside the live non-root reviewer container.

The [reviewer-tooling-request plugin](../../hermes-vm/plugins/reviewer-tooling-request/) adds only `reviewer_tooling_request(tool, reason, report_path)` to kanban, with no override grant. Native reviewer task/run/board/claim and a live worker lease/PID must match; delegated/unbound calls are refused. Finalize the invoking task's regular report first. Its fixed root-home board path is `~/.hermes/kanban/boards/security_reviewer/kanban.db`; the handler creates/reuses a native `Tooling request` with NULL assignee, blocked status and `security-reviewer-tooling:<normalized-tool>` idempotency key. Keep dispatcher `kanban.default_assignee` unset; the narrow plugin bypasses the dispatcher assignment wrapper deliberately. Operator alone installs tools or closes requests. Acceptance called the tool twice and left one acceptance-only CodeQL card `t_97eafaad`, unassigned/blocked with no worker. Keep it blocked until the CodeQL image merges and stable is adopted; then complete it with a summary that CodeQL is baked in, and recreate only an idle reviewer container.

An open tooling request does not retain its linked completed review report. Put a self-contained check/reason in the card. A done, non-archived request absorbs later requests for that tool; archive it to allow a new request. Neither limit makes a missing check assessed.

Five complete Trail of Bits directories at `1f68e03536f7980d4c0e6869596f708043a19443` are enabled under `R/skills/`: differential-review, sharp-edges, vulnerability-triage-brocards, fp-check and variant-analysis. The operator enabled them on October 16 after content-review ticket `t_d1612857`. All 55 original skill/support files and the root LICENSE were preserved byte-for-byte; PROVENANCE.md retains attribution and records activation. Installed `skills_list` lists all five without a restart. All five are equally discoverable; matching-ticket selection and unsupported named-agent/deep routes are persona guidance, not loader enforcement.

The host-installed [sweep](../../hermes-vm/scripts/security-reviewer-sweep.py) lives at `R/scripts/security-reviewer-sweep.py`; it is not a writable sandbox scripts mount. Native profile-owned job `4727a4af2e24` runs `15 6 * * *`, UTC, `no_agent`, `deliver=local`. It only deletes final report directories older than 30 days whose own task is still done/archived, reads the named board without initialization/writes, and keeps unknown/open/error/symlink cases. It never closes tickets or sweeps skills. Before enabling or troubleshooting it:

```sh
/usr/bin/python3 /home/hermes/.hermes/profiles/security_reviewer/scripts/security-reviewer-sweep.py --dry-run
/home/hermes/.local/bin/hermes -p security_reviewer cron list
```

Pause that job to roll back cleanup. The private deployment-time backup is for diagnosis only; do not restore requester `config.yaml` or `.env` over current files, because that would undo later unrelated changes, including the shared root configuration.

To remove report access, for each requester that currently carries the bind (`default`, `emh`, `web_watcher`, and HAL only if its owed bind was later installed):

1. Delete only the exact reviewer-report entry above from saved YAML `terminal.docker_volumes`, preserving every other mount and setting.
2. Run `/home/hermes/.hermes/scripts/hermes-profile-docker-setup.sh <profile> --sync-mounts` for that requester, including `default`.
3. Compare core `build_profile_terminal_scope` and WebUI `get_profile_runtime_env`; require the same remaining mounts and no reviewer-report bind.
4. Recreate only proven-idle containers whose mounts are stale.

For full removal:

1. Park ready reviewer tickets or remove its board from dispatch through the native board lifecycle.
2. Wait for running reviewer work to finish.
3. Pause sweep job `4727a4af2e24`.
4. Disable the profile-local tooling-request plugin.
5. Preserve wanted reports, persona, configuration and enabled skills privately.
6. Remove report access with the four steps above.
7. Remove `security_reviewer` through upstream's profile-removal flow.
8. Archive the dedicated board, retaining task history.

Remove no input directory or other profile's state. Profile files, scripts and job store ride the existing nightly `~/.hermes` backup; no new unit or monitor was added, and the alive check still watches the same three serving units.
