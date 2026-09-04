# The Hermes VM

VM 103 (`hermes.cynexia.net`) runs the Hermes agent, its four always-on gateway profiles, the dashboard and the WebUI the Hermex iOS app talks to.
It is not a Kubernetes cluster, so none of this repository's cluster machinery reaches it.
What it has instead lives in `hermes-vm/`: a daily liveness check, a weekly docker-sandbox refresh, a hand-run helper that finishes a new profile's docker setup, the managed-scope Hermes config and the `unattended-upgrades` configuration.

This page covers that machinery — how to read it, how to install it — and the VM facts that keep being rediscovered.
Updating the application stack is a separate procedure: [hermes-vm-updates.md](hermes-vm-updates.md).

The canonical copy of every file named here is in `hermes-vm/`.
The VM holds installed copies.
Edit the repository, then reinstall.

**Only three things run on a schedule here, and only one of them is systemd's.**
`unattended-upgrades` patches Debian security-only overnight and reboots at 04:45 UTC; the `hermes-app-alive` **cron job inside the default gateway** pushes a liveness verdict at 05:45 UTC; and `hermes-sandbox-refresh`, a cron job in the same place, replaces stale docker sandbox containers at 05:15 UTC on Sundays.
Nothing under `hermes-vm/` is scheduled by systemd any more — the `systemd/` directory and its two units were deleted on August 27, 2026.
Application updates never run on a schedule at all.

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
| `import-failed` | The shared venv cannot import `run_agent`. **This is the silent failure the whole design exists for**: every unit is still active and `/health` still answers `status: ok`, while the iOS app answers `AIAgent not available` | Read [homelab.md, "Do not give it a venv of its own"](homelab.md#hermes-webui-on-the-vm), then follow [the update runbook's rollback](hermes-vm-updates.md#rollback). The Python traceback in the journal names which import failed |
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
All three scripts live at `~/.hermes/scripts/`, so they ride that zip too: scripts and cron store are backed up and restored together.
That coupling is the right one, because the store names each script by bare filename, so a restore bringing back one without the other would leave a job pointing at nothing.
The four apt files and `/etc/hermes/config.yaml` sit outside the zip, and they are rebuild territory.
**A rebuild that skips `/etc/hermes/config.yaml` runs every profile's terminal tool on the host instead of in a container, and nothing reports it.**
**Do not trust the restored cron store without looking**: `hermes cron list` after a rebuild, because a check that quietly failed to come back looks exactly like a healthy day until the heartbeat lapses.

### 1. Lint first

```sh
make check-vm-scripts
```

This is the **only** thing that ever lints these files: it runs in no preflight, the repository's one workflow builds the InfluxDB MCP image from `homelab/health/mcp/` and covers nothing under `hermes-vm/`, and nothing runs it on a schedule.
This procedure is its only caller; the update runbook never invokes it.
It covers all three scripts under `hermes-vm/scripts/` — `hermes-app-alive.sh` from step 2, `hermes-sandbox-refresh.sh` from step 6, and `hermes-profile-docker-setup.sh` from [step 7](#7-pin-the-docker-terminal-backend).

### 2. Install the daily check's script

One file; the other two this install copies to the VM are the refresh script in [step 6](#6-install-the-sandbox-refresh-job) and the profile helper in [step 7](#7-pin-the-docker-terminal-backend).

```sh
scp hermes-vm/scripts/hermes-app-alive.sh \
  hermes@hermes.cynexia.net:/home/hermes/.hermes/scripts/hermes-app-alive.sh
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
  The `emh` and `hal` gateways gain nothing from this.
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
Its own death is caught by the update runbook's precondition on its last run ([Preconditions](hermes-vm-updates.md#preconditions)), at that runbook's cadence.

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

**The managed scope wins, and that is the point.**
`/etc/hermes/config.yaml` pins `terminal.backend`, `terminal.docker_image`, `terminal.docker_run_as_host_user` and `terminal.cwd` for every profile on this VM, including profiles that do not exist yet.
Its values beat a per-profile `config.yaml`, and `hermes config set` refuses to write any key it holds, so changing one means editing that file with `sudo` and restarting the gateways.
A profile created tomorrow therefore runs its terminal tool in a container without anybody remembering to configure it — which is why the pin is here rather than repeated per profile.

**The repository copy is the only durable record.**
The nightly `hermes backup` zip covers `~/.hermes` and nothing under `/etc`, and `hermes update` never touches `/etc` either, so this file survives a rebuild only by being copied back from `hermes-vm/etc/hermes/config.yaml`.
It follows the same pattern as the apt configuration in [step 9](#9-install-unattended-upgrades): repository is the source, the VM holds an installed copy.

**`terminal.docker_volumes` is deliberately not pinned**, because it names per-profile paths and because only some profiles get the shared attachments mount.
The helper writes those for one profile, and [Creating a profile](#creating-a-profile) is where it is used; the three entries it writes are in [The docker terminal sandboxes](#the-docker-terminal-sandboxes).

### 8. Create the `hermes-update` check

Create a healthchecks.io check named `hermes-update` by hand in the UI — period **10 days**, grace **4 days** — and store its ping UUID at `op://Homelab/hermes-update/healthcheck-uuid`, typed `[text]`.
Nothing in this estate creates that check, that item or that field, and the update runbook's [Report step](hermes-vm-updates.md#report) reads the reference directly, so an absent field fails the `op read` at the end of an otherwise successful update.

**The vault is `Homelab`, not `hermes`.**
Nothing on the VM ever needs this reference: the ping is sent from the operator's laptop, whose credential reads either vault, so it sits beside `estate-update` in `Homelab` rather than with the VM's own secrets.
The cadence allows one skipped week against a roughly weekly runbook before it alarms.
A ping UUID is a tier-2 spam-target identifier rather than a secret, which is why the field is `[text]` — but it belongs in 1Password and never in this repository.

### 9. Install `unattended-upgrades`

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

### 10. Verify the install

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
   If it does not, the update runbook's stamp-age precondition starts stopping sessions a fortnight later with nothing else wrong:

   ```sh
   stat -c '%y' /var/lib/apt/periodic/unattended-upgrades-stamp
   sudo systemctl start apt-daily-upgrade.service
   stat -c '%y' /var/lib/apt/periodic/unattended-upgrades-stamp
   ```

   **Never reach for `unattended-upgrade --dry-run` to check on the stamp, because a dry run writes it.**
   Verified August 27, 2026: a dry run completes through the same `write_stamp_file()` as a real one.
   The diagnostic an operator reaches for when the stamp looks stale is therefore the one thing that makes it look fresh, and it hides a dead timer from the update runbook's 14-day precondition for another fortnight.

   **The check also resists being run twice in a day**, which is not a fault.
   `apt.systemd.daily` guards its `unattended-upgrade` call with a **day-granular** interval — midnight today against midnight of the stamp's day — so once the stamp carries today's date a forced run is skipped and the mtime stays put.
   On a machine whose stamp is already fresh, confirm the mechanism tomorrow instead, by checking that its date has advanced.

5. **The VM's timezone is still UTC.**
   Every schedule on this page is UTC, and a drift moves the 05:45 daily check into the 04:45 reboot window:

   ```sh
   timedatectl | grep 'Time zone'
   ```

6. **Both cron jobs exist, and their next runs are 05:45 daily and 05:15 on Sunday.**
   The jobs live in the agent's own store, so nothing in this repository proves they are there:

   ```sh
   hermes cron list
   ```

7. **The token actually reaches the script.**
   This is the one step that catches a sanitiser name collision, a typo in the `op://` reference and a gateway that was never restarted, and none of the three is visible any other way.
   Run the job once with `hermes cron run <job_id>`, taking the id from `hermes cron list`, and confirm the `hermes-app-alive` monitor turns green in uptime-kuma.

   **A passing `cron run` does not prove the scheduled run will pass**, and on the day you create the vault item it usually does not.
   `cron run` executes in the CLI's process, and the CLI resolves `secrets.onepassword.env` at its own startup — so it picks up a token created moments ago.
   The **gateway** resolved its copy when it last started, and holds whatever existed then.
   Until the gateway is restarted the two disagree, the manual run succeeds, and the 05:45 scheduled run still fails its first line.
   Confirm the gateway's own view instead: `journalctl --user -u hermes-gateway` should show `applied N secrets` with **no** `op read failed` line beside it, and N should have risen by one.
   This trap was walked into on August 27, 2026.

   **A hand-triggered run does not reach the gateway's journal.**
   `cron run` executes the script in the CLI's own process — the run record calls it `source=direct` — so its exit code and stderr land in `hermes cron runs <job_id>` and nowhere else.
   Read that when triaging a manual trigger.
   A *scheduled* run is the gateway's subprocess and does go to the journal, which is what the table at the top of this page assumes.

   Note too that the scheduler runs a `.sh` file through **bash**, while `make check-vm-scripts` lints it as POSIX `sh`.
   The lint is the stricter of the two, which is the safe direction, but it means one failure reports two exit codes: 2 by hand under dash, 1 under the scheduler.

   A **scheduled** run that fails on its first line prints `HERMES_APP_ALIVE_PUSH_TOKEN: not injected …` to the gateway's journal, which is the tell for all three; the same failure from `cron run` prints it into the run record instead.

8. **Every profile resolves its terminal backend to `docker`.**
   A missing or misplaced `/etc/hermes/config.yaml` leaves the backend at its default and every terminal call then runs on the host, which nothing else on this page detects.
   Ask each profile what it resolved, rather than reading the file back:

   ```sh
   /home/hermes/.local/bin/hermes config get terminal.backend
   /home/hermes/.local/bin/hermes -p emh config get terminal.backend
   ```

**Expect a reboot the first night after installing.**
Before the first install the VM has a pending kernel reboot — an uptime over four days and a kernel image in the reboot-required list — and nothing has been arming an automatic reboot, because the drop-in that arms it is part of this install.
So the first 04:45 window clears that backlog — correct behaviour, not a fault.

## Creating a profile

Adding a Hermes profile to this VM, in order.
Worked end to end on `web_watcher` on August 31, 2026; every hazard below is one that run met or would have met.
Only one ordering constraint is real, and it is step 3: **disable the platforms the profile will not use before its gateway starts for the first time.**

Steps 4 to 8 are conditional, and a profile that only answers kanban tasks or WebUI chats needs none of them.

### 1. Create the profile

Create it in the WebUI, choosing a **clean** profile rather than one inheriting the default's configuration.
`hermes profile create <name>` works too, and seeds every bundled skill pack into `<profile home>/skills/` — 14 of them on August 31, 2026, so read the directory rather than that number.
Delete the directories the profile does not need, and **keep `<profile home>/skills/.bundled_manifest`** — that file is how `skills_sync` remembers a deletion, so a pack removed with the manifest intact stays removed instead of being restored on the next sync.

### 2. The terminal backend needs nothing

`/etc/hermes/config.yaml` already pins `terminal.backend`, `terminal.docker_image`, `terminal.docker_run_as_host_user` and `terminal.cwd` for every profile, this one included ([step 7 of the install](#7-pin-the-docker-terminal-backend)).
There is no per-profile terminal setting to copy, and `hermes config set` would refuse it anyway.

### 3. Disable the platforms this profile will not use

**Before the first gateway start:**

```sh
/home/hermes/.local/bin/hermes -p <name> config set platforms.telegram.enabled false
/home/hermes/.local/bin/hermes -p <name> config set platforms.api_server.enabled false
```

**A profile gateway inherits the base `~/.hermes/.env`**, so a profile that has configured nothing still starts with the default profile's platform credentials.
Left enabled, it connects Telegram with the default profile's token, which the API rejects as `token already in use`, and it binds the default `api_server` port 8642, which fails as `address already in use`.
Disabling them afterwards fixes it, but the gateway loops on those collisions until you do.

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
An email profile's map is one entry: `EMAIL_PASSWORD` against `op://hermes/purelymail-<name>/password` from step 4.
The **operator** adds `OP_SERVICE_ACCOUNT_TOKEN` to the profile's own `.env` by hand; that value goes nowhere else, and no agent handles it.

### 6. The email platform, if wanted

The email adapter reads **`.env` only**.
Put `EMAIL_ADDRESS`, `EMAIL_IMAP_HOST`, `EMAIL_SMTP_HOST` and `EMAIL_ALLOWED_USERS` in the profile's `.env`, let `EMAIL_PASSWORD` arrive through the 1Password map from step 5, and then:

```sh
/home/hermes/.local/bin/hermes -p <name> config set platforms.email.enabled true
```

**Skip `platforms.email.extra`.**
The adapter does not read it, so setting the address and hosts there is harmless and does nothing — while looking exactly like the configuration that matters.

### 7. The standard docker mounts, for a trusted profile only

```sh
/home/hermes/.hermes/scripts/hermes-profile-docker-setup.sh <name>
```

This gives the profile the shared WebUI attachments inbox along with its workspace, so run it only where every profile's uploads may be read — see [The docker terminal sandboxes](#the-docker-terminal-sandboxes) for the three entries and for the [#6939](https://github.com/nesquena/hermes-webui/issues/6939) stopgap that will remove one of them.
Skip it for an isolated worker, which is the `safer_web_reader` pattern ([safer-web-reader.md](safer-web-reader.md)): that profile deliberately has no `docker_volumes` of its own and takes the platform's per-profile sandbox workspace instead.

**Launching a profile from the WebUI writes those same three entries itself**, into the profile's `config.yaml` and as `TERMINAL_DOCKER_VOLUMES` in its `.env`.
The helper then finds all three present and writes nothing, which is the intended outcome rather than a sign it failed.

### 8. An always-on gateway, for any messaging platform

A profile with a messaging platform needs a systemd user unit; without one, a gateway started by hand dies at the next reboot, and this VM reboots at 04:45 whenever `unattended-upgrades` installs a kernel.

**The hermes CLI owns these unit files.**
`refresh_systemd_unit_if_needed` regenerates the unit whenever the installed text differs from what the current install would write, and it runs on `gateway start`, on `gateway restart` and on **every gateway boot** (`hermes_cli/gateway.py`, read on August 31, 2026).
So a hand edit does not stick, and none should be attempted: `TimeoutStopSec` in particular is derived, not written — `max(60, max(restart_drain_timeout, cron_drain_timeout + 10) + 30)` — so gateways differ from each other because their profiles' drain settings differ, and the CLI's value is the correct one.
`hermes-gateway-web_watcher.service` carries `TimeoutStopSec=70` against the other three gateways' `210` for exactly that reason, and it stays as the CLI wrote it (operator ruling, August 31, 2026).

Check whether the CLI has already created the unit, because starting a gateway once is enough to make one:

```sh
ls ~/.config/systemd/user/hermes-gateway-<name>.service
```

If it exists, there is nothing to write — go straight to enabling it:

```sh
systemctl --user enable --now hermes-gateway-<name>
```

If it does not, seed one from an existing gateway's unit, and let the CLI correct it on the first start:

```sh
sed 's/emh/<name>/g' ~/.config/systemd/user/hermes-gateway-emh.service \
  > ~/.config/systemd/user/hermes-gateway-<name>.service
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway-<name>
```

**A new gateway unit is a new thing to watch, and nothing notices that on its own.**
Add its name to the `UNITS` list in `hermes-vm/scripts/hermes-app-alive.sh`, then re-run `make check-vm-scripts` and reinstall the script ([step 2](#2-install-the-daily-checks-script)) — the expected count is derived from that list, so a unit missing from it is a gateway whose death the daily check reports as healthy.
The [update runbook](hermes-vm-updates.md) enumerates the units it restarts, so a new gateway belongs in that list too, or `hermes update` leaves it running the old code.

A kanban or WebUI-only worker needs no unit at all: its work runs inside a gateway that already exists.

### 9. Verify

```sh
tail ~/.hermes/profiles/<name>/logs/gateway.log
/home/hermes/.local/bin/hermes -p <name> config get terminal.backend
```

The log shows `✓ <platform> connected` for each platform the profile enabled, or `No messaging platforms enabled` for a worker; `terminal.backend` reads `docker`.

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
| 05:15, Sundays only | `hermes-sandbox-refresh`, as a cron job inside the default gateway | **Between** the reboot window and the daily check: the reboot has already left the sandboxes `exited`, which is the cheapest state to replace them in, and the removals are done before anything reports on the VM. Weekly, because the pinned image moves only when Microsoft rebuilds the tag |
| 05:45 | `hermes-app-alive`, as a cron job inside the default gateway | **One hour** after the reboot, so the reboot has finished and the lingering user units have come back before anything looks at them |

The two cron rows are the only ones with no jitter: they are cron expressions in the agent's scheduler rather than systemd timers, and neither `RandomizedDelaySec` nor `Persistent=` applies to them.
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
It writes it on a `--dry-run` too, so a dry run is never a way to inspect the stamp — see step 10 of the install.
The other files in that directory belong to `apt.systemd.daily` and are not this.

So the stamp proves the timer fired, not that anything was patched.
The alarm built on it is the update runbook's 14-day stamp-age precondition, which stops a session rather than reporting on its own, so **detection latency is the runbook's cadence — about a week — against a 14-day threshold**.
That is accepted.
The VM has no MTA, so `unattended-upgrades` mail would be a silent failure; the journal is the record.

## What the daily check does not watch

- **Nothing continuous.**
  Detection latency for an application fault is up to about a day, and a missing heartbeat alarms about 30 hours after the last good one.
  **Accepted by the operator on August 26, 2026 — "homelab not NASA."**
  The `hermes` uptime-kuma monitor still catches a VM that is off or unreachable, faster, through the dashboard on `hermes.cynexia.com`.
- **No chat turn is monitored at all.**
  The daily check makes none, by design.
  The only chat turn this estate performs is the update runbook's verification step, and update sessions are unscheduled, so a fault that lets the agent import, serve `/health` and keep its units up while failing every chat turn is caught at the next session.
- **The daily import runs in a fresh interpreter, not the WebUI's process.**
  So a venv repaired without restarting `hermes-webui` reports `verdict=ok` while the live process still cannot serve — the very failure that check exists to catch.
  After any venv repair, restart the unit.
- **Three of the five units are only counted, never exercised.**
  The daily check asserts something real about `hermes-webui` through its `/health`, about the shared venv through the import, and — since the check became a cron job inside it — about **`hermes-gateway`**, the default gateway, by running at all: a beat arriving means its scheduler executed a subprocess, so the old wedged-but-running-gateway blind spot is closed for that one unit.
  `hermes-gateway-emh`, `hermes-gateway-hal` and `hermes-dashboard` still contribute nothing but a `systemctl --user is-active` result.
  The gap is smaller for the dashboard: the `hermes` uptime-kuma monitor probes it externally on `hermes.cynexia.com/api/health`.
  **Nothing external probes the two remaining gateways.**
- **A gateway unit that is not in the check's `UNITS` list.**
  The expected count is derived from that list, so a unit missing from it is not half-watched but unwatched, and the check reports `ok` while it is dead.
  `hermes-gateway-web_watcher` sat in that position for the hours between its creation and the list being extended on August 31, 2026, and the list now names all six units.
  Adding a gateway means adding its name, which is [step 8 of Creating a profile](#8-an-always-on-gateway-for-any-messaging-platform).
- **Per-profile state.**
  The check exercises the shared venv and the default profile.
  A fault confined to `emh` or `hal` profile state is invisible to it.
- **Memory writes.**
  Nothing here detects a Hindsight write that fails: the write is on a background path a chat response does not wait for, so a profile can retain nothing while every check on this page passes.
  That is not hypothetical — from August 23 to August 27, 2026 the `default` profile's writes all returned `401 Invalid API key` and no check in this estate noticed ([hindsight.md](hindsight.md#monitoring)).
  The journal grep that would have caught it is a step of [the update runbook's Verify](hermes-vm-updates.md#verify).

  **Read that grep as a failure detector only, because a healthy retain is silent.**
  The provider logs `Hindsight retain succeeded` at DEBUG, which this gateway does not emit, while a failure is a WARNING carrying the full traceback.
  So an empty grep after a turn means "no failure", never "a write landed" — and an empty grep after *no* turn means nothing at all.
  Retention is per turn (`retain_every_n_turns` defaults to 1) and `auto_retain` defaults to on, so one chat turn is enough to produce the WARNING if the write path is broken.
  That asymmetry is what made the August 2026 401s findable at all.
- **A dead apt timer.**
  The 14-day gate that catches it is a precondition of the update runbook, so it surfaces at the next update session rather than on its own.
- **Its own cron job going missing.**
  The job is agent state rather than a file this repository installs, so a rebuild, a restore or a hand edit can drop it.
  Nothing notices until the monitor's heartbeat lapses about 30 hours later, and the report then reads as "no beat at all" — indistinguishable from a VM that is off.
  `hermes cron list` is the only positive proof it is still there.
- **The docker sandboxes, and the image they are running.**
  The check runs on the host and says nothing about the containers any profile's terminal works inside, including whether their image is still the one the pinned tag now resolves to.
  `hermes-sandbox-refresh` is what moves them, it pushes to no monitor, and the thing that notices it has stopped is the update runbook's precondition on its last run ([Preconditions](hermes-vm-updates.md#preconditions)).
  So detection latency for a stale sandbox image is the update cadence, about a week, and that is accepted: a stale sandbox userland is neither lockout nor data loss, and the containment boundary is the host kernel, which `unattended-upgrades` patches nightly.
  A check on the age of the running image was considered and rejected — a healthy refresh job against a quiet upstream reads identically to a dead one.
- **The refresh job going missing.**
  Same shape as the daily check's own job, and the same store, but with no heartbeat behind it at all: nothing lapses, so the only detection is the same update-runbook precondition, reading the job's last run.
  `hermes cron list` remains the only positive proof the job exists.

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
The detail, and `hermes cron list` as the check, is in [what the daily check does not watch](#what-the-daily-check-does-not-watch).

## Facts about this VM

Recorded so nobody rediscovers them.

**`hermes` is not on the non-interactive ssh PATH.**
That PATH is `/usr/local/bin:/usr/bin:/bin:/usr/games`, and `~/.local/bin` is not on it, so `ssh hermes@… 'hermes …'` fails with "command not found".
Call `/home/hermes/.local/bin/hermes` — a three-line shim that unsets `PYTHONPATH` and `PYTHONHOME` and execs the venv's own `hermes`.
A `systemd-run --user` unit gets the same clean environment and needs the same absolute path.

**`uv` is at `$HERMES_HOME/bin/uv` and is resolved by absolute path** (`hermes_cli/managed_uv.py:53-63`, whose docstring says "no PATH probing, no conda guards, no multi-location resolution chains").
It is on no PATH on this VM and does not need to be.
That is why the units set `HERMES_HOME` rather than extending PATH: a wrong or absent `HERMES_HOME`, not a short PATH, is what would stop `hermes update` finding its own tooling.

**`hermes update` restarts the three gateway units itself** — `hermes update --plan` says so — and does **not** restart `hermes-webui` or `hermes-dashboard`, because neither runs the `hermes` entry point.
Without a deliberate restart of those two, the WebUI keeps serving the module already resident in memory, and the break surfaces at the next restart or the 04:45 reboot.

**Update snapshots live in `~/.hermes/backups/`**, not `~/.hermes/pre_update_backups/` — an earlier draft of this design named the second path and it does not exist.
Retention is five snapshots, pruned after each write and floored at one, at roughly 200 MiB each.

**The API server's posture, as found:** `API_SERVER_HOST=0.0.0.0` and `API_SERVER_CORS_ORIGINS=*` on all three profiles, with the gateway logging a network-accessible and unsandboxed warning on every start.
**The host-level bind is not the control, and nothing on the VM is.**
[homelab.md's security posture section](homelab.md#security-posture--read-before-fixing-any-of-it) records what actually holds: the VM runs no host firewall, the LAN is a trusted zone, and the only gate an agent cannot forge is the Cloudflare Access service token on the published hostnames.
Firewalling 8787, 9119 and 8642 down to the cluster egress address is the obvious hardening and is a change of its own.

**`/p/<profile>/` routing exists but is inert.**
The routes are registered unconditionally, and the prefix is **discarded** while `gateway.multiplex_profiles` is off — which it is.
So `/p/anything/…` reaches the default profile, and anything probing a gateway should use the unprefixed path.

**`/var/lib/docker` is a disk of its own, and nothing backs it up.**
A second 125 GiB thin-provisioned virtio-scsi disk (SCSI 1, discard and SSD emulation on) was added to VM 103 on August 29, 2026, formatted `mkfs.ext4 -m 0 -L dockerdata` and mounted at `/var/lib/docker` from `/etc/fstab` by UUID, with `defaults,nofail,x-systemd.device-timeout=30s 0 2`.
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

Verified live on August 29, 2026; the managed scope and the attachments mount were added on August 31, 2026.

**All four profiles run their terminal tool inside docker containers rather than on the host**, and a profile created tomorrow will too.
`default` and `emh` switched on August 29; `hal` followed the same day once its long-running task finished.
`safer_web_reader` was still on the default backend until the managed scope pinned it on August 31; all four were read back as `docker` that day, so the refresh job's next run reports `profiles=4` rather than the `profiles=3` it used to.

Four settings come from the managed scope at `/etc/hermes/config.yaml` and apply to every profile; the fifth is written per profile:

| Setting | Value | Set where |
|---|---|---|
| `terminal.backend` | `docker` | Managed scope |
| `terminal.docker_image` | `mcr.microsoft.com/devcontainers/python:3.14-trixie` | Managed scope |
| `terminal.docker_run_as_host_user` | `true`, so the exec runs as uid 1000, the host `hermes` user | Managed scope |
| `terminal.cwd` | `/workspace` | Managed scope |
| `terminal.docker_volumes` | Three entries on `default`, `emh` and `hal`; unset on `safer_web_reader` | Per profile, by `hermes -p <profile> config set` or [step 7](#7-pin-the-docker-terminal-backend)'s helper |

A managed key beats the same key in a profile's own `config.yaml`, and `hermes config set` refuses to write one, so those four move only through a `sudo` edit of `/etc/hermes/config.yaml`.
A **bulk** write — the setup wizard, or the dashboard's raw YAML editor — is not refused: `save_config` strips every managed leaf out of it first and prints a `managed setting(s) were not saved` note on stderr, so the rest of the document lands and the pinned keys do not (read in `hermes_cli/config.py` on August 31, 2026).
The file is root-owned `0644` in a `0755` directory, and it is outside the nightly backup — see [step 7](#7-pin-the-docker-terminal-backend).

The default profile's workspace is `/home/hermes/.hermes/workspace`, created for this; every other profile's is `workspace` inside its own profile home.
A settings change takes effect when that profile's gateway is restarted.

**`default`, `emh` and `hal` mount three volumes each, and two of them are identity mounts** — the same host path inside the container as outside:

| Entry | Why |
|---|---|
| `<workspace>:/workspace` | The sandbox's working directory, matching `terminal.cwd` |
| `/home/hermes/.hermes/webui/attachments:/home/hermes/.hermes/webui/attachments:ro` | Chat uploads, read-only, at the host path the WebUI pastes into the prompt |
| `<workspace>:<workspace>` | The workspace again, at the host path the WebUI advertises in its `[Workspace::v1: …]` label |

The identity mounts exist because the WebUI hands the agent **host** paths as text, and a path that does not resolve inside the container is a file the agent cannot read.

**The attachments mount is a stopgap and has an end date.**
hermes-webui saves a chat upload to `~/.hermes/webui/attachments/<session>/` — one directory shared by every profile, not scoped per profile — and pastes that host path into the prompt, so before this mount a sandboxed agent could not read anything a person uploaded.
That is upstream issue [#6939](https://github.com/nesquena/hermes-webui/issues/6939), confirmed by the maintainer, and open pull request [#7022](https://github.com/nesquena/hermes-webui/pull/7022) moves uploads into the profile's own `cache/documents/webui-attachments/<session>/`, which hermes already mounts.
The mount's cost is that every profile's uploads are readable from every sandbox that has it, which is why `safer_web_reader` does not, so it comes out as soon as a hermes-webui update carries the fix.
Removing it is a step of [the update runbook](hermes-vm-updates.md#the-webui-attachments-mount-comes-out-when-6939-lands).

**The WebUI's "workspace" is a label, not a mount.**
Each profile has a file-browser root recorded in `{profile home}/webui_state/last_workspace.txt` and `workspaces.json` — `~/.hermes/webui/` for the default profile.
It scopes the WebUI's own file browser, editor and git panel, and it prepends a `[Workspace::v1: <host path>]` line to the messages that profile sends.
Nothing mounts it into a container: the label is why the third volume entry above exists, and switching workspaces in the WebUI does not change what a sandbox can see.

**Check that a `${VAR}` placeholder survived a configuration edit made through the WebUI.**
On August 31, 2026 something resolved the `${EMAIL_PASSWORD}` placeholder behind `mcp_servers.mail.env.MCP_EMAIL_SERVER_PASSWORD` in the `emh` profile's `config.yaml` into the literal password on disk.
The cause was not pinned down; `hermes config set` was tested and exonerated, which leaves the WebUI's profile-settings save and the `emh` agent itself as the suspects.
The credential was rotated and the placeholder restored the same day.
After any configuration change made outside the CLI, `grep -c '\${' <profile home>/config.yaml` and confirm the placeholders are still placeholders.

**Containers are created lazily, per (profile, task-id), so one profile can own several at once.**
Each is named `hermes-<hex>` and labelled `hermes-profile:<name>` and `hermes-task-id:<id>`.
The label is what tooling selects on; the name carries nothing.
Hermes mounts the profile workspace at `/workspace`, along with skills, caches and attachments — and a **host-backed home**: `/home/hermes/.hermes/sandboxes/docker/<task-id>/home` is mounted as `/root` in the container and is writable at host uid 1000.

**`/workspace` and that home are the only durable paths.**
Anything written elsewhere in the container filesystem is lost when the container is recreated, so a user-level package install that lands under the mounted home survives a recreation and one that lands in `/usr` does not.
Both durable paths sit under `~/.hermes`, so both ride the nightly `hermes backup` zip.

**`docker rm -f` on a sandbox is safe while its profile is idle**, verified by removing one and letting it come back.
The next terminal call builds a new container from the local image, with no gateway restart involved.
That is also the whole of the image-update mechanism: a `docker pull` never touches an existing container, so **removal and recreation is the update**, which is what `hermes-sandbox-refresh` exists to do.

**The pin is a tag rather than a digest, deliberately.**
Microsoft rebuilds `3.14-trixie` with security patches, and the weekly pull-and-replace is how those arrive.
The sandbox userland is hygiene, not the containment boundary — the boundary is the host kernel and runc, patched nightly by `unattended-upgrades`.

**`no_agent` cron scripts are unaffected by any of this.**
The scheduler runs them as plain host subprocesses (`subprocess.Popen` in `cron/scheduler.py`), so `hermes-app-alive` still runs on the host and reports on the host whatever a profile's terminal backend is set to.

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
