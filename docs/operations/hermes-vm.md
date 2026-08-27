# The Hermes VM

VM 103 (`hermes.cynexia.net`) runs the Hermes agent, its three gateway profiles, the dashboard and the WebUI the Hermex iOS app talks to.
It is not a Kubernetes cluster, so none of this repository's cluster machinery reaches it.
What it has instead lives in `hermes-vm/`: a daily liveness check and the `unattended-upgrades` configuration.

This page documents that machinery — how to read it, how to install it, and the facts about the VM that keep being rediscovered.
Updating the application stack is a separate procedure: [hermes-vm-updates.md](hermes-vm-updates.md).

The canonical copy of every file named here is in `hermes-vm/`.
The VM holds installed copies.
Edit the repository, then reinstall.

**Only two things run on a schedule here.** `unattended-upgrades` patches Debian security-only overnight and reboots at 04:45 UTC; `hermes-app-alive.timer` pushes a liveness verdict at 05:45 UTC.
Application updates never run on a timer.

## Lingering is a precondition

```sh
loginctl enable-linger hermes
loginctl show-user hermes -p Linger        # must print Linger=yes
```

Without lingering, the `hermes` user manager stops when the last session ends.
All five hermes user units die at the next reboot — **and so does `hermes-app-alive.timer`.**

That is the nastiest failure mode here: **the monitor that would report the outage is itself down.** Nothing pushes, so uptime-kuma sees silence rather than a `down`, and `hermes-app-alive` stays green until its 24-hour heartbeat and 6-hour retry expire, about 30 hours after the last good beat.

One thing catches it sooner.
The `hermes` HTTP monitor probes the dashboard on `hermes.cynexia.com`, and `hermes-dashboard` is one of the units that died, so that monitor goes red at its own interval.
Read a red `hermes` beside a still-green `hermes-app-alive` as this failure until proven otherwise.

The reboot is automatic and can happen any night at 04:45 UTC, so an un-lingered rebuild is not a latent problem.
It is a problem the first night.

## Reading a DOWN hermes-app-alive

The daily check runs at **05:45 UTC**, one hour after the 04:45 automatic-reboot window, with `Persistent=true` so a missed window is caught up rather than skipped.
The monitor's heartbeat is 24 hours with one retry at 6 hours, so **a missing heartbeat alarms about 30 hours after the last good one**.

| `verdict=` | What it means | First thing to do |
|---|---|---|
| `units-down` | Not every hermes user unit is active. `units=<active>/<expected>` carries both counts, and the expected one is derived from the `UNITS` list in the script, so it follows the list rather than a number written down here | `systemctl --user status` each unit in that list. If none are up, check `loginctl show-user hermes -p Linger` first |
| `import-failed` | The shared venv cannot import `run_agent`. **This is the silent failure the whole design exists for**: every unit is still active and `/health` still answers `status: ok`, while the iOS app answers `AIAgent not available` | Read [homelab.md, "Do not give it a venv of its own"](homelab.md#hermes-webui-on-the-vm), then follow [the update runbook's rollback](hermes-vm-updates.md#rollback). The Python traceback in the journal names which import failed |
| `webui-unreachable` | The WebUI's own `/health` did not answer 2xx. `webui_http=` carries the status; `000` means no connection | `journalctl --user -u hermes-webui`. Note the unit's `StartLimitIntervalSec=60`/`StartLimitBurst=5` parking behaviour — a repeatedly failing start parks in `failed` rather than looping invisibly |
| **No beat at all** | The VM is off, the user manager is not running, lingering was lost, the timer was disabled, or the Cloudflare Access bypass on `/api/push/*` was removed | Check the VM is up, then check the bypass. **A bypass regression turns every push monitor in the estate red at once, which is the tell** |

**A daily line of Python import noise in the journal is normal, not a fault.** The check deliberately does not discard the interpreter's stderr, because the traceback names which import failed and that is the whole answer to "what broke".
The traceback goes to the journal and never near the pushed message, which carries only `verdict=`, `units=` and `webui_http=`.

## Installing or reinstalling

A VM rebuild must repeat all of this.
The nightly `hermes backup` zip covers `~/.hermes`, so it carries the environment file and nothing else installed here.
Everything under `/home/hermes/bin` and `~/.config/systemd/user` is rebuild territory.

### 1. Lint first

```sh
make check-vm-scripts
```

This is the **only** thing that ever lints these files.
It runs in no preflight, this repository has no CI, and nothing runs it on a schedule.
This procedure is its only caller; the update runbook never invokes it.

### 2. Install the script and its units

```sh
scp hermes-vm/scripts/hermes-app-alive.sh hermes@hermes.cynexia.net:/home/hermes/bin/
ssh hermes@hermes.cynexia.net 'chmod 0755 /home/hermes/bin/hermes-app-alive.sh'
scp hermes-vm/systemd/hermes-app-alive.service hermes-vm/systemd/hermes-app-alive.timer \
  hermes@hermes.cynexia.net:/home/hermes/.config/systemd/user/
```

### 3. Create the monitor, then its environment file

Create the `hermes-app-alive` push monitor in uptime-kuma by hand — a 24-hour heartbeat with one 6-hour retry — and store its push URL in 1Password.
The monitor, the vault field and this file are all created by hand; no manifest in this estate assembles the URL.
See [uptime-kuma.md](uptime-kuma.md#push-monitors).

| File | Holds | Source |
|---|---|---|
| `/home/hermes/.hermes/hermes-app-alive.env` | `PUSH_URL`, whose last path segment is the monitor's push token | `op://hermes/hermes-app-alive/kuma-push-token` |

Write it mode 0600, by piping `op read` over ssh from the operator's laptop.
**Placement rule, from the August 26, 2026 ruling:** any reference the VM itself resolves must live in the `hermes` vault, because the VM's 1Password service account can see only that vault; a reference the operator's laptop resolves and pipes over ssh may live anywhere.
The VM does resolve `op://` references — hermes's own 1Password secrets provider resolves each home's references at gateway startup — so the vault choice here is load-bearing.

The service uses no leading `-` on its `EnvironmentFile=`.
A check that cannot report is worse than one that refuses to start.

```sh
loginctl enable-linger hermes
systemctl --user daemon-reload
systemctl --user enable --now hermes-app-alive.timer
```

### 4. Create the `hermes-update` check

Create a healthchecks.io check named `hermes-update` by hand in the UI — period **10 days**, grace **4 days** — and store its ping UUID at `op://Homelab/hermes-update/healthcheck-uuid`, typed `[text]`.
Nothing in this estate creates that check, that item or that field, and the update runbook's [Report step](hermes-vm-updates.md#report) reads the reference directly, so an absent field fails the `op read` at the end of an otherwise successful update.

**The vault is `Homelab`, not `hermes`.** Nothing on the VM ever needs this reference: the ping is sent from the operator's laptop, whose credential reads either vault, so it sits beside `estate-update` in `Homelab` rather than with the VM's own secrets.
The cadence allows one skipped week against a roughly weekly runbook before it alarms.
A ping UUID is a tier-2 spam-target identifier rather than a secret, which is why the field is `[text]` — but it belongs in 1Password and never in this repository.

### 5. Install `unattended-upgrades`

Four files, and **two of them do not install where they live in the repository**:

| Repository path | Installs to |
|---|---|
| `hermes-vm/etc/apt.conf.d/20auto-upgrades` | `/etc/apt/apt.conf.d/20auto-upgrades` |
| `hermes-vm/etc/apt.conf.d/52unattended-upgrades-local` | `/etc/apt/apt.conf.d/52unattended-upgrades-local` |
| `hermes-vm/etc/systemd/apt-daily.timer.d/override.conf` | `/etc/systemd/system/apt-daily.timer.d/override.conf` |
| `hermes-vm/etc/systemd/apt-daily-upgrade.timer.d/override.conf` | `/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf` |

**A naive recursive copy of `hermes-vm/etc/` lands the two drop-ins under `/etc/systemd/apt-daily.timer.d/`, where systemd never looks and they silently do nothing.** Copy them one at a time, or the schedule stays at Debian's default and nothing says so.

```sh
sudo systemctl daemon-reload
```

### 6. Verify the install

Check each of these once.
Every one of them fails silently if it is wrong.

1. **The package sources resolve to security-only entries.** The `#clear` directive is load-bearing, not future-proofing: Debian's shipped `50unattended-upgrades` leaves three patterns uncommented and the first matches `label=Debian`, which is every package in stable. Confirm exactly two patterns come back, both `label=Debian-Security`:

   ```sh
   apt-config dump Unattended-Upgrade::Origins-Pattern
   ```

2. **Each apt timer shows exactly ONE calendar entry**, not two. The empty `OnCalendar=` line in each drop-in is what replaces the vendor schedule instead of adding to it:

   ```sh
   systemctl show apt-daily.timer -p TimersCalendar
   systemctl show apt-daily-upgrade.timer -p TimersCalendar
   systemctl list-timers apt-daily.timer apt-daily-upgrade.timer
   ```

3. **The reboot hook is armed.** Confirm `Automatic-Reboot` is `true` and `Automatic-Reboot-Time` is `04:45`, and that `/sbin/shutdown` exists — `unattended-upgrade` does not reboot directly, it hands that literal time to `shutdown`:

   ```sh
   apt-config dump Unattended-Upgrade::Automatic-Reboot
   apt-config dump Unattended-Upgrade::Automatic-Reboot-Time
   ```

4. **The stamp file refreshes after a real run.** Note its mtime, force a run, and confirm the mtime moved. If it does not, the update runbook's stamp-age precondition starts stopping sessions a fortnight later with nothing else wrong:

   ```sh
   stat -c '%y' /var/lib/apt/periodic/unattended-upgrades-stamp
   sudo systemctl start apt-daily-upgrade.service
   stat -c '%y' /var/lib/apt/periodic/unattended-upgrades-stamp
   ```

5. **The VM's timezone is still UTC.** Every schedule on this page is UTC, and a drift moves the 05:45 daily check into the 04:45 reboot window:

   ```sh
   timedatectl | grep 'Time zone'
   ```

6. **The units verify under real systemd**, and the standard for that is specific. **`systemd-analyze verify` exiting zero proves nothing**: a misspelled directive exits zero with only a warning on stderr. The evidence is the **absence** of an `Unknown key … ignoring` line, **plus** the resolved value read back:

   ```sh
   systemd-analyze --user verify \
     /home/hermes/.config/systemd/user/hermes-app-alive.service
   systemctl --user show hermes-app-alive -p TimeoutStartUSec
   systemctl --user show hermes-app-alive.timer -p TimersCalendar -p Persistent
   ```

**Expect a reboot the first night after installing.** Before the first install the VM has a pending kernel reboot — an uptime over four days and a kernel image in the reboot-required list — and nothing has been arming an automatic reboot, because the drop-in that arms it is part of this install.
So the first 04:45 window clears that backlog.
That is correct behaviour, not a fault.

## unattended-upgrades

Security-only, by the two `Origins-Pattern` entries in `/etc/apt/apt.conf.d/52unattended-upgrades-local`.
The estate's other update surfaces — keel, Renovate, the update session — cover feature upgrades; this one exists for kernel and library CVEs.

**Only the second pattern matches anything on Debian 13.** Since bullseye the security archive carries its own codename (`n=trixie-security`), so `codename=${distro_codename},label=Debian-Security` matches nothing.
It is the vendor idiom, kept deliberately for archive migration — but it means a typo confined to the second line alone leaves **zero** security patching behind a stamp file that still looks fresh.
Edit the two together and re-run the `apt-config dump` above.

**Why `#clear` precedes `Origins-Pattern`.** For a scalar the last assignment wins, so a `52` file overrides `50`.
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
| 05:45 (+0–5 min) | `hermes-app-alive` | **One hour** after the reboot, so the reboot has finished and the lingering user units have come back before anything looks at them |

Both drop-ins set `FixedRandomDelay=true`, so the offset is derived from the machine ID and unit name rather than re-rolled nightly.
The gap between the refresh and the install is then a constant for this machine, and `systemctl list-timers` can be read once and still trusted tomorrow.

**The jitter is 10 minutes, not Debian's hour, and that is a reboot correctness matter rather than a tidiness one.** `unattended-upgrade` hands the literal `04:45` to `shutdown`, and systemd resolves a time that has already passed to the same time **tomorrow**.
An upgrade still running at 04:45 does not delay the reboot by minutes; it slips it by 24 hours, and the kernel sits installed-but-not-running for a day.

**The two apt jobs share a lock, and the waiter blocks for up to an hour.** A 03:30 refresh that overruns past 04:00 makes the upgrade **block** rather than fail.
That is the safe behaviour — nothing installs against half-refreshed lists — but the block eats into the 35-minute margin before the reboot.
If `systemctl list-timers` shows the upgrade starting late, look at the refresh run's duration first.

### Why nothing watches apt directly

There is no `-success` file and nothing creates one.
`unattended-upgrade` writes `/var/lib/apt/periodic/unattended-upgrades-stamp` for itself in `write_stamp_file()`, and it writes it on a run that found **nothing to do** just as readily as on one that installed everything.
The other files in that directory belong to `apt.systemd.daily` and are not this.

So the stamp proves the timer fired, not that anything was patched.
The alarm built on it is the update runbook's 14-day stamp-age precondition, which stops a session rather than reporting on its own, so **detection latency is the runbook's cadence — about a week — against a 14-day threshold**.
That is accepted.
The VM has no MTA, so `unattended-upgrades` mail would be a silent failure; the journal is the record.

## What the daily check does not watch

- **Nothing continuous.** Detection latency for an application fault is up to about a day, and a missing heartbeat alarms about 30 hours after the last good one. **Accepted by the operator on August 26, 2026 — "homelab not NASA."** The `hermes` uptime-kuma monitor still catches a VM that is off or unreachable, faster, through the dashboard on `hermes.cynexia.com`.
- **No chat turn is monitored at all.** The daily check makes none, by design. The only chat turn this estate performs is the update runbook's verification step, and update sessions are unscheduled, so a fault that lets the agent import, serve `/health` and keep its units up while failing every chat turn is caught at the next session.
- **The daily import runs in a fresh interpreter, not the WebUI's process.** So a venv repaired without restarting `hermes-webui` reports `verdict=ok` while the live process still cannot serve — which is the very failure that check exists to catch. After any venv repair, restart the unit.
- **Four of the five units are only counted, never exercised.** The daily check asserts something real about exactly one unit, `hermes-webui`, through its `/health`, and about the shared venv through the import. The three gateways **and `hermes-dashboard`** contribute nothing but a `systemctl --user is-active` result, so a wedged-but-running gateway or dashboard reports healthy. The gap is smaller for the dashboard: the `hermes` uptime-kuma monitor probes it externally on `hermes.cynexia.com/api/health`. **Nothing external probes the three gateways.**
- **Per-profile state.** The check exercises the shared venv and the default profile. A fault confined to `emh` or `hal` profile state is invisible to it.
- **Memory writes.** Nothing here detects a Hindsight write that fails: the write is on a background path a chat response does not wait for, so a profile can retain nothing while every check on this page passes. That is not hypothetical — from August 23 to August 27, 2026 the `default` profile's writes all returned `401 Invalid API key` and no check in this estate noticed ([hindsight.md](hindsight.md#monitoring)). The journal grep that would have caught it is a step of [the update runbook's Verify](hermes-vm-updates.md#verify).
- **A dead apt timer.** The 14-day gate that catches it is a precondition of the update runbook, so it surfaces at the next update session rather than on its own.

## Accepted exposures

`hermes-app-alive.env` lives under `~/.hermes`, so it **rides the nightly `hermes backup` zip** to the `hermes-dumps` PVC and on to B2.
The uptime-kuma push token therefore rests in two more encrypted places than it strictly needs to.

That is accepted, and the reason is the tier.
The token is a **tier-2 spam-target identifier**, not a secret: holding it lets a stranger report a heartbeat and mask a genuine failure, and grants nothing else.
It needs no rotation and earns no honesty-box row.

## Facts about this VM

Recorded so nobody rediscovers them.

**`hermes` is not on the non-interactive ssh PATH.** That PATH is `/usr/local/bin:/usr/bin:/bin:/usr/games`, and `~/.local/bin` is not on it, so `ssh hermes@… 'hermes …'` fails with "command not found".
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
**The host-level bind is not the control, and nothing on the VM is.** [homelab.md's security posture section](homelab.md#security-posture--read-before-fixing-any-of-it) records what actually holds: the VM runs no host firewall, the LAN is a trusted zone, and the only gate an agent cannot forge is the Cloudflare Access service token on the published hostnames.
Firewalling 8787, 9119 and 8642 down to the cluster egress address is the obvious hardening and is a change of its own.

**`/p/<profile>/` routing exists but is inert.** The routes are registered unconditionally, and the prefix is **discarded** while `gateway.multiplex_profiles` is off — which it is.
So `/p/anything/…` reaches the default profile, and anything probing a gateway should use the unprefixed path.

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
