# Updating the Hermes VM

VM 103 (`hermes.cynexia.net`) runs the Hermes agent, its three gateway profiles, the
dashboard and the WebUI the Hermex iOS app talks to. It is not a Kubernetes cluster, so
none of this repository's cluster machinery reaches it. Its update and liveness machinery
lives in `hermes-vm/` instead, and this page is its runbook.

The canonical copies of every file named here are in `hermes-vm/`. The VM holds installed
copies; the repository holds the originals. Edit the repository, then reinstall.

## What updates, and how

Three surfaces update, by three different mechanisms:

| Surface | Mechanism | Scheduled? |
|---|---|---|
| The Hermes agent | `hermes update --backup`, run by `hermes-update.sh` | **No** |
| The two venv passengers — the `hermes-webui` checkout and the `hindsight-client` package | The same script, in the same run | **No** |
| Debian itself | `unattended-upgrades`, security-only, with an automatic reboot at 04:45 UTC | Yes |

**Only the operating system and the daily liveness check run on a schedule. The
application updater never does.** `hermes-vm/systemd/` holds one `.timer` file, and it
belongs to the liveness check.

## Read this before you promise anyone a rollback

`hermes update` rewrites `~/.hermes/config.yaml` in place, advances a schema version, and
applies in-place schema changes to a 54 MB `state.db`. Those migrations are
**forward-only**: `hermes_cli/config_migrations.py` defines no downgrade, rollback or
revert function at all.

So **the wrapper's rollback restores the agent's code and cannot restore its state.** A
`git reset --hard` plus a `pip install -e` returns the code to the last revision that
passed the health assertion. The migrated configuration file and the migrated database
stay migrated.

Two things follow, and both matter more than the rollback itself:

- The wrapper passes `--backup` on **every** run, so a restorable snapshot always exists
  before the migration starts. That flag is what makes state recovery possible; it is not
  what performs it.
- **Recovering state is a deliberate human act.** Nothing automatic restores a snapshot.
  See [Recovering the agent's state by hand](#recovering-the-agents-state-by-hand).

This is the same doctrine the estate already applies to its other two forward-only
migrators: the Hindsight store (`make hindsight-upgrade`) and Grafana's `grafana.db`
(`make health-upgrade`). Where migrations are forward-only, the pre-upgrade dump **is**
the rollback.

**`backup=requested` in the report body means this script passed the flag.** It does not
mean a snapshot was written. Upstream's backup path warns and continues on its own
failures rather than raising, so the strongest claim the script can make without parsing
another program's output is that it asked. A warning, if there was one, is in the unit's
journal.

## Why the updater is not scheduled

Operator decision, August 26, 2026. `hermes update` sometimes carries manual steps: a
config schema migration can prompt, and a run that stashes local edits needs a human to
look at what it stashed. Arming a timer would change the failure model, not just the
schedule.

The consequence is deliberate. The `hermes-update` healthchecks.io check runs a **45-day
period with a 7-day grace**, matching the 4-to-6-week update session. So the alarm is a
skipped *session*, not a skipped week, and silence past about 52 days is the signal.

**That ruling covers updates, not monitoring.** `hermes-app-alive.timer` runs a read-only
daily heartbeat that mutates nothing and prompts for nothing. It is not a precedent for
scheduling updates, and it is not a violation of the ruling.

## Running an update

From anywhere with ssh access:

```sh
ssh hermes@hermes.cynexia.net 'hermes-update'
```

That is what the `/update-estate` skill runs. `/usr/local/bin/hermes-update` is a
root-owned wrapper, and `/usr/local/bin` is on the default non-login ssh PATH, which is
why `command -v hermes-update` finds it. The wrapper starts the user unit, prints that
run's journal, and returns the unit's exit code.

Two equivalent forms, on the VM:

```sh
systemctl --user start hermes-update            # same unit, no journal dump
/home/hermes/bin/hermes-update.sh               # direct, needs the env file sourced
```

The log is the unit's journal:

```sh
journalctl --user -u hermes-update -n 500 --no-pager
```

**Two runs never overlap, and `flock` is what guarantees it — not systemd.** A
`systemctl start` issued while a start job is already in flight *merges* into that job and
returns its result, so a second invocation does not start a second run but is handed the
first run's exit status as though it were its own. The direct-over-ssh form does not
involve systemd at all. The lock covers both. A run that finds the lock held exits **75**
(`EX_TEMPFAIL`) with `verdict=already-running` and **deliberately reports nothing**: a
duplicate that pinged would either reset the check's timer or mark it red while the real
run is still working.

### Stopping a run

`systemctl --user stop hermes-update` can take up to **40 minutes** to return in the
pathological case. `TimeoutStopSec=2400` is long on purpose: the script sends its entire
report from an EXIT trap, a POSIX `sh` trap cannot run while a foreground child is
blocked, and the longest such child (`hermes update`) is bounded at 2100 seconds
including its kill grace. A shorter stop bound would let systemd `SIGKILL` the shell
mid-migration and report nothing about it.

In practice the stop returns at once: the default `KillMode` is `control-group`, so the
`SIGTERM` reaches every process in the cgroup and the child normally dies immediately.
If you need the run gone now and accept a `SIGKILL` during a forward-only migration:

```sh
systemctl --user kill -s SIGKILL hermes-update
```

`TimeoutStartSec=10800` bounds the run itself. It is derived, not round: the longest of
the five routes through the script — update succeeds, health fails, the full rollback
runs, health is re-asserted — sums to 9040 seconds of bounded children, and the remainder
is headroom for the local operations that are not bounded because they cannot block on a
network. Do not lower it. Killing a run part way through, quite possibly mid-rollback, is
the worst moment this system has.

**The 04:45 automatic reboot kills an active session.** An operator running
`hermes-update` by hand across 04:45 UTC gets rebooted mid-run. That is intended — the
reboot window is not conditional on who is logged in — and the signal traps turn it into a
reported `rc=143` rather than silence, as long as the signal lands between bounded steps
rather than inside one.

## Lingering is a precondition

```sh
loginctl enable-linger hermes
loginctl show-user hermes -p Linger        # must print Linger=yes
```

Without lingering, the `hermes` user manager stops when the last session ends. All five
hermes user units die at the next reboot — **and so does `hermes-app-alive.timer`.**

That is the nastiest failure mode here: **the monitor that would report the outage is
itself down.** Nothing pushes, so uptime-kuma sees silence rather than a `down`, and
`hermes-app-alive` stays green until its 24-hour heartbeat and 6-hour retry expire — about
30 hours after the last good beat.

One thing does catch it sooner. The existing `hermes` HTTP monitor probes the dashboard on
`hermes.cynexia.com`, and `hermes-dashboard` is one of the units that died, so that monitor
goes red at its own interval. Read a red `hermes` beside a still-green `hermes-app-alive`
as this failure until proven otherwise.

The reboot is automatic and can happen any night at 04:45 UTC, so an un-lingered rebuild is
not a latent problem. It is a problem the first night.

## The passenger design

Two things ride along with the agent update, into the **same venv** the five units
execute from. [homelab.md, "Do not give it a venv of its own"](homelab.md#hermes-webui-on-the-vm)
is why: `api/agent_runtime.py` imports `run_agent.AIAgent` into the WebUI process, so the
WebUI needs hermes-agent's whole dependency tree, not the two packages its own
`requirements.txt` names.

**Passenger one: the `hermes-webui` checkout.** It tracks `origin/master`. The script
fetches, resolves `origin/master`, gates the result through `valid_sha40`, and only then
moves the tree with `git checkout -B master <sha>`.

The spec's newest-tag policy was **retired on August 26, 2026** against observed upstream
practice: upstream had stopped tagging five weeks earlier, and the newest tag was 560
commits behind the deployed `master`. A tag rule's first act would have been to roll back
the origin the Hermex iOS app talks to.

What replaced the tag policy's fail-closed property is three things together:
`valid_sha40` before the tree moves, the health assertion after it, and the last-good
rollback when that assertion fails. **Reinstating a tag rule, or inventing a "newest tag,
or master if master is ahead" hybrid, is a design change and needs the operator.**

Two consequences of tracking a branch:

- `git checkout -B` **force-sets** local `master`, so uncommitted changes in
  `/home/hermes/hermes-webui` are discarded. Do not keep work there.
- Upstream moving between runs is the normal state within hours, not an unusual event.
  That is fine for updates and is a hazard only for [the rollback drill](#the-rollback-drill).

**Passenger two: `hindsight-client`.** The script reads the *deployed* Hindsight server's
version from `https://hindsight.cynexia.net/health/live` and pins the client to it. The
repository pin is intent; the server's own `version` field is state, and the VM holds no
kubeconfig with which to read anything else. That read happens in phase 0, **before
anything moves**, so a Hindsight server that is down or reshaped produces a clean exit
rather than a destructive rollback of a VM nothing ever found unhealthy.

## What the health assertion asserts

Four assertions, in order. The fourth is conditional.

1. **All five user units are active** — `hermes-gateway`, `hermes-gateway-emh`,
   `hermes-gateway-hal`, `hermes-dashboard`, `hermes-webui`.
2. **The shared venv imports `run_agent`**, from a pinned working directory of
   `/home/hermes`. This is the only cheap check that catches the documented silent
   failure: a venv missing `dotenv`, `httpx` or `openai` leaves every unit `active` and
   `/health` answering `status: ok` while the iOS app answers `AIAgent not available`.
   Importing `hermes_cli.main` instead would not catch it — that is the CLI entry point,
   not the WebUI's dependency path.
3. **The WebUI answers its own `/health`** on `http://127.0.0.1:8787/health`.
4. **A real chat turn** against the **default** profile's local API server on
   `http://127.0.0.1:8642/v1/chat/completions`, with `API_SERVER_KEY` read from
   `~/.hermes/.env` — the same file the gateway itself reads, so nothing is copied and
   there is one value to rotate rather than two. The key reaches `curl` through a config
   file written by a shell built-in, so it never appears in a process listing.

The assertion is "an assistant message with non-empty text came back", not "the agent said
pong". Model wording is not a contract; populated `choices[0].message.content` proves the
agent class loaded, the provider answered and the venv is whole.

**Two documented fallbacks skip the turn and still pass:**

- `chat_mode=skipped-api-disabled` — `API_SERVER_ENABLED` is not `true`.
- `chat_mode=skipped-no-literal-key` — `API_SERVER_KEY` is absent, or is not a usable
  literal in `[A-Za-z0-9_-]`. An unresolved `op://…` reference fails that gate on its `:`
  and `/`, which is the right outcome: it degrades visibly instead of sending a
  meaningless bearer token.

**A green run reporting either fallback is genuinely weaker than one reporting
`chat_mode=chat`.** Read `chat_mode=` before you read the exit code.

**The turn runs on the operator's own default profile**, which has memory and the full
toolset, so it may write a memory. At roughly one turn per update session that is
accepted; it was untenable only at monitor frequency.

## Reading a red `hermes-update`

**"Restored" in the table below means code and pinned versions only.** `hermes update`'s
configuration and `state.db` migrations are forward-only, so a `Yes` in the last column
says the agent checkout, the webui checkout and the `hindsight-client` pin went back — not
that the agent's configuration or database did. Recovering those is a separate, manual
step: [Recovering the agent's state by hand](#recovering-the-agents-state-by-hand).

**Read `verdict=` and `rollback_state=` together.** A verdict of `rolled-back` is
reachable alongside a `rollback_state=` naming a step that failed, so reading the verdict
alone is optimistic. `verdict` says what the run concluded; `rollback_state` says whether
the restore actually finished.

**After any rollback, inspect the agent checkout by hand.** `rollback_state=complete` is
truthful about code and silent about the stash. `hermes update` stashes local changes
before it pulls, and the rollback never pops that stash, so a stash entry is work that was
serving before the run and is not serving now:

```sh
git -C /home/hermes/.hermes/hermes-agent status --porcelain
git -C /home/hermes/.hermes/hermes-agent stash list
git -C /home/hermes/.hermes/hermes-agent rev-parse --abbrev-ref HEAD
```

The third command matters because the rollback resets whichever branch the update left the
checkout on, to a revision that came from last-good rather than from upstream. That can
leave the branch in a state the next update refuses to work with.
[The rollback drill](#the-rollback-drill) states the same hazard from the other side.

| `verdict=` | What broke | Was the VM restored? |
|---|---|---|
| `preflight-failed` | The run died before it updated anything — a missing `flock`, `timeout`, `curl`, `git`, `python3` or `systemctl` (exit 70), an unwritable run directory, or a lock file that could not be created (exit 73) | Nothing moved |
| `already-running` | Another run holds the lock; this one did nothing. Exit **75**, and it reports **nothing at all** — you see this in the journal, never in a ping body | Nothing moved |
| `client-failed` with `rollback_source=none` | Phase 0: the deployed Hindsight version was unreadable. Read before anything moved, on purpose | Nothing moved |
| `update-failed` | `hermes update` itself failed. `update_rc=` carries its exit status; **124 means this script's `timeout` killed it; 137 means the same `timeout` fired, the child ignored the `SIGTERM` for its whole kill grace, and it was `SIGKILL`ed instead**. Both are the same case — killed mid-flight, with the `--backup` snapshot the only route back for the migrated state | Only if `agent_changed=` is `yes` or `unknown`. `rollback_source=none` means nothing had moved |
| `webui-failed` with `rollback_source=none` | The `git fetch` failed, or `origin/master` did not resolve to a 40-hex object name, **and** the agent tree had not moved. Failed closed before the checkout moved | Nothing moved |
| `webui-failed` with `rollback_source=last-good` or `pre-run` | Either the same two failures after `hermes update` had already moved the agent, or the checkout or its constrained `pip install` failed | Yes — all three revisions restored and the units restarted |
| `client-failed` with a rollback source | Pinning `hindsight-client` failed. The webui had already moved, so the rollback ran | Yes |
| `restart-failed` | The five units did not come back after the update, or did not become ready within 180 seconds. Reported separately from `health-failed` because "would not come back" and "came back broken" want different first moves | Yes — the rollback ran |
| `health-failed` | **Two different runs report this, and `rc=` tells them apart.** `rc=143` (or 130, 129) is a signalled run: the assertion failed and the run ended before the rollback branch could set its own verdict. **`rc=1` is the opposite — the assertion PASSED and recording last-good failed**, because `write_last_good` is a bare command under errexit and returns non-zero on an unreadable revision, a revision that fails the shape gate, or a failed rename. The tell is `rollback_state=none` with `post_rollback=not-attempted`: no rollback was ever attempted, because nothing needed one. The journal names which step failed | `rc=1`: nothing to restore — the update succeeded and only the rollback target went unrecorded. `rc=143`: partially — read `rollback_state=` and `post_rollback=` |
| `rollback-failed` | The update broke the app and the rollback did not fix it. **Look now** | No |
| `rolled-back` | The update broke the app and the restored state passed the assertion. The app works; the update needs a human | Yes — but confirm `rollback_state=complete` |
| `apt-stale` | Every hermes component is fine, and `unattended-upgrades` has not written its stamp in over 14 days | Not applicable |
| `rc=143` with any verdict | The run was `SIGTERM`'d: `TimeoutStartSec` expired, someone stopped the unit, or the 04:45 reboot landed on it. The signal traps make this visible instead of silent | Read the journal; the run stopped wherever it was |
| Silence past about 52 days | Nobody has run an update session | Not applicable |

A bad argument is the one failure that reports nothing: `hermes-update.sh --nonsense`
prints a usage line and exits **64** before any trap is registered.

### The other fields in the body

| Field | Values | Read it for |
|---|---|---|
| `backup=` | `requested`, `not-attempted` | Whether `--backup` was passed. `requested` is not a promise that a snapshot exists — see the caveat above |
| `update_rc=` | `hermes update`'s own exit status | `124` means this script's `timeout` killed it mid-flight; `137` means the child ignored that `SIGTERM` for its whole kill grace and was `SIGKILL`ed. Go and find the snapshot either way |
| `agent_changed=` | `no`, `yes`, `unknown` | `unknown` means the agent checkout would not answer `git rev-parse` after the update, which is itself a post-mutation fault. It routes into the rollback |
| `rollback_source=` | `none`, `last-good`, `pre-run` | Where the restore target came from. `none` means the failure happened **before anything moved** |
| `rollback_state=` | `none`, `complete`, or `failed-agent-reset`, `failed-agent-install`, `failed-webui-checkout`, `failed-webui-requirements`, `failed-client-pin`, `failed-restart` | Whether the restore finished, and if not, the **first** step that failed. Later failures do not overwrite an earlier one |
| `post_rollback=` | `healthy`, `unhealthy`, `not-attempted` | Whether the restored machine passed the assertion afterwards |
| `units_active=` | an integer, or `not-counted` | `not-counted` means nothing had counted them yet. It is deliberately not `0`, which would read as "all five are down" |
| `webui_sha=`, `client_version=` | shape-gated values, or `unknown` before anything read them, or `unreadable` | What the machine holds **now**. A rollback recomputes these, so they describe the state you are looking at, not the state the run tried to reach |
| `chat_mode=` | see below | Which assertion was actually performed |
| `chat_http=` | a digit-gated status, `000` for no connection or no turn | The turn's HTTP status whenever one was made. Read it with `chat_mode=chat-failed` |
| `apt_age_days=` | an integer, `9999` when unreadable | The `unattended-upgrades` stamp age |
| `run_epoch=` | epoch seconds | A stale value across two alerts means the runner itself went quiet |

### `chat_mode=`

| Value | Means |
|---|---|
| `chat` | A real turn was made and returned assistant content. The strongest result |
| `skipped-api-disabled` | `API_SERVER_ENABLED` is not `true`. The run passed on the three free checks |
| `skipped-no-literal-key` | No usable literal `API_SERVER_KEY`. The run passed on the three free checks |
| `chat-failed` | The turn returned a non-2xx status. `chat_http=` carries it |
| `chat-empty` | The turn returned 2xx with no assistant content |
| `forced-fail` | `HERMES_UPDATE_FORCE_HEALTH_FAIL=1` was set. This is the drill hook and must not appear in an ordinary run |
| `not-attempted` | The run never reached the assertion |

## Reading a DOWN `hermes-app-alive`

The daily check runs at **05:45 UTC**, one hour after the 04:45 automatic-reboot window,
with `Persistent=true` so a missed window is caught up rather than skipped. The monitor's
heartbeat is 24 hours with one retry at 6 hours, so **a missing heartbeat alarms about 30
hours after the last good one**.

| `verdict=` | What it means | First thing to do |
|---|---|---|
| `units-down` | Not every hermes user unit is active. `units=<active>/<expected>` carries both counts, and the expected one is derived from the `UNITS` list in the script, so it follows the list rather than a number written down here | `systemctl --user status` each unit in that list. If none are up, check `loginctl show-user hermes -p Linger` first |
| `import-failed` | The shared venv cannot import `run_agent`. **This is the silent failure the whole design exists for**: every unit is still active and `/health` still answers `status: ok`, while the iOS app answers `AIAgent not available` | Read [homelab.md, "Do not give it a venv of its own"](homelab.md#hermes-webui-on-the-vm), then roll the webui back with `~/.hermes/hermes-update.last-good`. The Python traceback in the journal names which import failed |
| `webui-unreachable` | The WebUI's own `/health` did not answer 2xx. `webui_http=` carries the status; `000` means no connection | `journalctl --user -u hermes-webui`. Note the unit's `StartLimitIntervalSec=60`/`StartLimitBurst=5` parking behaviour — a repeatedly failing start parks in `failed` rather than looping invisibly |
| **No beat at all** | The VM is off, the user manager is not running, lingering was lost, the timer was disabled, or the Cloudflare Access bypass on `/api/push/*` was removed | Check the VM is up, then check the bypass. **A bypass regression turns every push monitor in the estate red at once, which is the tell** |

**A daily line of Python import noise in the journal is normal, not a fault.** The check
deliberately does not discard the interpreter's stderr, because the traceback names which
import failed and that is the whole answer to "what broke". The traceback goes to the
journal and never near the pushed message, which carries only `verdict=`, `units=` and
`webui_http=`.

## Manual rollback

The automatic rollback reads `~/.hermes/hermes-update.last-good`, which is written only
after a green assertion. Its four keys:

```
agent_sha=<40 hex>
webui_sha=<40 hex>
client_version=<X.Y.Z>
stamp=<epoch seconds>
```

`client_version` can hold the literal `none`, which the rollback reads as "pin nothing".
That is what the writer records when the installed version could not be read, and it is
deliberate: an empty value would make the whole record unreadable and silently drop the
rollback to the pre-run state for all three revisions.

The script also keeps `~/.hermes/webui.last-good` in step, because
[homelab.md's manual rollback](homelab.md#rollback-when-an-update-breaks-the-app) reads
that file directly and predates this script. Use that procedure for the webui alone.

To restore all three revisions by hand, read the keys and repeat what `rollback()` does:
`git -C ~/.hermes/hermes-agent reset --hard $agent_sha`, `pip install -e
"$HOME/.hermes/hermes-agent[all]"`, `git -C ~/hermes-webui checkout -f -B master
$webui_sha`, the constrained `pip install -r requirements.txt`, `pip install
"hindsight-client==$client_version"`, then `systemctl --user restart` the five units. Use
the venv's own pip at `~/.hermes/hermes-agent/venv/bin/pip`, never a system one.

### Recovering the agent's state by hand

The code rollback above does not touch `config.yaml` or `state.db`. To recover those,
restore the snapshot `hermes update --backup` took:

```sh
ls -1t ~/.hermes/backups/pre-update-*.zip | head -n1
~/.local/bin/hermes import --force ~/.hermes/backups/pre-update-<stamp>.zip
```

**`--force` is mandatory when this runs unattended.** Without it a prompt fires whenever
the target already has a config file — which on a live VM is always — and the command
exits non-zero on end-of-file.

Three things to know before you rely on this:

- **The path is `~/.hermes/backups/`, not `~/.hermes/pre_update_backups/`.** An earlier
  draft of this design named the second path. It does not exist.
- **Retention is five snapshots**, pruned after each write and floored at one, so the
  run's own snapshot can never be the one deleted. Each is roughly 200 MiB.
- **The quick state snapshot is not this.** `hermes update` can also take a cheap state
  snapshot, and that artifact has **no command-line restore verb at all** — its only
  caller is a slash command inside the interactive agent shell. Treat it as an automatic
  safety net a human recovers by hand, and point recovery at the zip.

**The operator's `updates.pre_update_backup` setting now means something different from
when it was set.** There are three modes rather than two, and an explicit `false` maps to
"off entirely" where it once meant "skip the zip but keep the cheap snapshot" — upstream's
own docstring flags the change. That is why the wrapper forces `--backup` per run instead
of changing the setting: the operator's manual `hermes update` keeps behaving exactly as
they configured it, and this script's runs are covered regardless.

## The rollback drill

The drill proves the rollback path works before you need it.

```sh
ssh hermes@hermes.cynexia.net
set -a; . /home/hermes/.hermes/hermes-update.env; set +a
HERMES_UPDATE_FORCE_HEALTH_FAIL=1 /home/hermes/bin/hermes-update.sh
```

Source the environment file first. A direct invocation does not get the unit's
`EnvironmentFile=`, and the script refuses to start without `HERMES_UPDATE_HC_UUID` — it
exits before its traps are up, because a run that cannot report is worse than one that
does not start.

**A passing drill ends in `verdict=rollback-failed`, and that is the correct result.** The
hook stays set through the post-rollback re-assertion on purpose, so the failing path runs
twice. What tells a passing drill apart from a genuinely broken rollback is
`rollback_state=complete` with `post_rollback=unhealthy`: the restore ran every step and
the only thing still failing is the hook you set. A real failure names a step —
`rollback_state=failed-webui-checkout` and so on.

Three risks, stated because the drill is a live operation on the production VM:

- **It runs a real update first.** If upstream moved since the last run, the drill's
  rollback silently undoes that update. With a `master`-tracking passenger, "upstream
  moved" is the normal state within hours. Check `git -C /home/hermes/hermes-webui
  rev-parse HEAD` against `origin/master` before you start.
- **Its rollback is a live reinstall against the production venv** — the same venv all
  five units execute from. It is not a simulation.
- **It moves the agent checkout as well, and the rollback undoes less than it looks like.**
  `hermes update` stashes the checkout's local changes and switches off a parked branch
  before it pulls. The rollback then hard-resets whichever branch that left it on to the
  last-good revision, and it never pops the stash. Two things follow. What comes back is
  the last-good code *without* any uncommitted modifications that were part of what was
  serving before the drill, so "restored" is not the state you started from. And the
  checkout can be left in a branch state the next update refuses to work with, because the
  revision it now holds came from last-good rather than from upstream. Before you start,
  run `git -C /home/hermes/.hermes/hermes-agent status --porcelain` and commit or discard
  what it lists; afterwards, read `git -C /home/hermes/.hermes/hermes-agent stash list`.

The drill also sends a real failure ping, so the `hermes-update` check goes red and stays
red until the next successful run. Take an ordinary run afterwards, or resolve the check by
hand.

## Installing or reinstalling

A VM rebuild must repeat **all** of this. The nightly `hermes backup` zip covers
`~/.hermes`, so it carries the two environment files and the two last-good files, and
nothing else named here. Everything under `/home/hermes/bin`, `/usr/local/bin` and
`~/.config/systemd/user` is rebuild territory.

### 1. Lint first

```sh
make check-vm-scripts
```

This is the **only** thing that ever lints these files. It runs in no preflight, this
repository has no CI, and nothing runs it on a schedule. Its two callers are this runbook
and the 4-to-6-week update session.

### 2. Install the scripts and the entry point

```sh
scp hermes-vm/scripts/hermes-update.sh hermes-vm/scripts/hermes-app-alive.sh \
  hermes@hermes.cynexia.net:/home/hermes/bin/
ssh hermes@hermes.cynexia.net 'chmod 0755 /home/hermes/bin/hermes-*.sh'
```

`/usr/local/bin/hermes-update` is **root-owned, mode 0755**, and is what puts the command
on the non-login PATH:

```sh
scp hermes-vm/bin/hermes-update hermes@hermes.cynexia.net:/tmp/hermes-update
ssh hermes@hermes.cynexia.net \
  'sudo install -o root -g root -m 0755 /tmp/hermes-update /usr/local/bin/ && \
   rm /tmp/hermes-update'
```

### 3. Install the user units and their environment files

The three unit files go to `/home/hermes/.config/systemd/user/`. Both environment files
are **mode 0600** and hold **one name each**:

| File | Holds | Source |
|---|---|---|
| `/home/hermes/.hermes/hermes-update.env` | `HERMES_UPDATE_HC_UUID` | `op://Homelab/hermes-update/healthcheck-uuid` |
| `/home/hermes/.hermes/hermes-app-alive.env` | `PUSH_URL`, whose last path segment is the monitor's push token | `op://hermes/hermes-app-alive/kuma-push-token` |

Write each by piping `op read` over ssh from the operator's laptop. **Placement rule, from
the August 26, 2026 ruling:** any reference the VM itself resolves must live in the
`hermes` vault, because the VM's 1Password service account can see only that vault; a
reference the operator's laptop resolves and pipes over ssh may live anywhere. Nothing on
the VM resolves an `op://` reference today.

Neither unit uses a leading `-` on its `EnvironmentFile=`. A unit that cannot report is
worse than a unit that refuses to start.

```sh
loginctl enable-linger hermes
systemctl --user daemon-reload
systemctl --user enable --now hermes-app-alive.timer
```

`hermes-update.service` is **not** enabled and has no `[Install]` section. Nothing starts
it but a human.

### 4. Install `unattended-upgrades`

Four files, and **two of them do not install where they live in the repository**:

| Repository path | Installs to |
|---|---|
| `hermes-vm/etc/apt.conf.d/20auto-upgrades` | `/etc/apt/apt.conf.d/20auto-upgrades` |
| `hermes-vm/etc/apt.conf.d/52unattended-upgrades-local` | `/etc/apt/apt.conf.d/52unattended-upgrades-local` |
| `hermes-vm/etc/systemd/apt-daily.timer.d/override.conf` | `/etc/systemd/system/apt-daily.timer.d/override.conf` |
| `hermes-vm/etc/systemd/apt-daily-upgrade.timer.d/override.conf` | `/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf` |

**A naive recursive copy of `hermes-vm/etc/` lands the two drop-ins under
`/etc/systemd/apt-daily.timer.d/`, where systemd never looks and they silently do
nothing.** Copy them one at a time, or the schedule stays at Debian's default and nothing
says so.

```sh
sudo systemctl daemon-reload
```

### 5. Seed last-good, then take the first real run

On the VM, seed last-good. `--seed` asserts health, records last-good, updates nothing and
pings nothing, so it needs no environment file:

```sh
/home/hermes/bin/hermes-update.sh --seed
```

**Before the first real run, leave the agent checkout with nothing to stash.** Check what
it holds:

```sh
git -C /home/hermes/.hermes/hermes-agent status --porcelain
```

Commit or discard every file it lists, and decide whether you still want the branch the
checkout is parked on. `hermes update` is configured to stash local changes and to switch
off a parked branch, and the rollback restores neither — see
[The rollback drill](#the-rollback-drill) for what that costs when a run fails its health
assertion.

Then, from the laptop, take the first real run:

```sh
ssh hermes@hermes.cynexia.net 'hermes-update'
```

Seed **before** the first real run, so that run has a rollback target which has already
passed the assertion. Take the first real run **after** `unattended-upgrades` has written
its stamp at least once, or the run ends in a designed `apt-stale` failure.

**The first run is the most expensive this wrapper will ever take.** It stashes the agent
checkout, switches branch, pulls a long gap of commits, syncs the whole dependency tree,
writes a roughly 200 MiB snapshot, runs the `state.db` integrity check and applies
migrations — all inside one `hermes update`, which this script bounds at **thirty
minutes** (1800 seconds) plus a 300-second kill grace. Watch it rather than walking away:

```sh
ssh hermes@hermes.cynexia.net 'journalctl --user -u hermes-update -f'
```

A run that exceeds the bound is terminated part way through and reported honestly, as
`verdict=update-failed` with `update_rc=124` or `137`. That report is accurate and the
interruption is still self-inflicted on a run that was working, so treat those two exit
statuses on the **first** run as a bound to reconsider rather than as a broken update, and
recover the migrated state from the snapshot before trying again.

### 6. Verify the install

Check each of these once. Every one of them fails silently if it is wrong.

1. **The package sources resolve to security-only entries.** The `#clear` directive is
   load-bearing, not future-proofing: Debian's shipped `50unattended-upgrades` leaves
   three patterns uncommented and the first matches `label=Debian`, which is every package
   in stable. Confirm exactly two patterns come back, both `label=Debian-Security`:

   ```sh
   apt-config dump Unattended-Upgrade::Origins-Pattern
   ```

2. **Each apt timer shows exactly ONE calendar entry**, not two. The empty `OnCalendar=`
   line in each drop-in is what replaces the vendor schedule instead of adding to it:

   ```sh
   systemctl show apt-daily.timer -p TimersCalendar
   systemctl show apt-daily-upgrade.timer -p TimersCalendar
   systemctl list-timers apt-daily.timer apt-daily-upgrade.timer
   ```

3. **The reboot hook is armed.** Confirm `Automatic-Reboot` is `true` and
   `Automatic-Reboot-Time` is `04:45`, and that `/sbin/shutdown` exists — `unattended-upgrade`
   does not reboot directly, it hands that literal time to `shutdown`:

   ```sh
   apt-config dump Unattended-Upgrade::Automatic-Reboot
   apt-config dump Unattended-Upgrade::Automatic-Reboot-Time
   ```

4. **The stamp file actually refreshes after a real run.** Note its mtime, force a run,
   and confirm the mtime moved. If it does not, the wrapper's 14-day staleness gate starts
   failing a fortnight later with nothing else wrong:

   ```sh
   stat -c '%y' /var/lib/apt/periodic/unattended-upgrades-stamp
   sudo systemctl start apt-daily-upgrade.service
   stat -c '%y' /var/lib/apt/periodic/unattended-upgrades-stamp
   ```

5. **The VM's timezone is still UTC.** Every schedule on this page is UTC, and a drift
   moves the 05:45 daily check into the 04:45 reboot window:

   ```sh
   timedatectl | grep 'Time zone'
   ```

6. **The units verify under real systemd**, and the standard for that is specific.
   **`systemd-analyze verify` exiting zero proves nothing**: a misspelled directive exits
   zero with only a warning on stderr. The evidence is the **absence** of an
   `Unknown key … ignoring` line, **plus** the resolved value read back:

   ```sh
   systemd-analyze --user verify /home/hermes/.config/systemd/user/hermes-update.service
   systemctl --user show hermes-update -p TimeoutStartUSec -p TimeoutStopUSec
   systemctl --user show hermes-app-alive.timer -p TimersCalendar -p Persistent
   ```

**Expect a reboot the first night after installing.** Before the first install the VM has
a pending kernel reboot — an uptime over four days and a kernel image in the
reboot-required list — and nothing has been arming an automatic reboot, because the
drop-in that arms it is part of this install. So the first 04:45 window clears that
backlog. That is correct behaviour, not a fault.

## `unattended-upgrades`

Security-only, by the two `Origins-Pattern` entries in
`/etc/apt/apt.conf.d/52unattended-upgrades-local`. The estate's other update surfaces —
keel, Renovate, the update session — cover feature upgrades; this one exists for kernel
and library CVEs.

**Only the second pattern matches anything on Debian 13.** Since bullseye the security
archive carries its own codename (`n=trixie-security`), so
`codename=${distro_codename},label=Debian-Security` matches nothing. It is the vendor
idiom, kept deliberately for archive migration — but it means a typo confined to the
second line alone leaves **zero** security patching behind a stamp file that still looks
fresh. Edit the two together and re-run the `apt-config dump` above.

**Why `#clear` precedes `Origins-Pattern`.** For a scalar the last assignment wins, so a
`52` file overrides `50`. `Origins-Pattern` is a **list**, and a later block *appends* to
whatever `50` already declared. Without the clear, apt reports five patterns including
`label=Debian` — the whole stable archive — and `unattended-upgrades` would install all of
it and then reboot the machine at 04:45. The clear is the single thing making this policy
security-only. Do not delete it as redundant.

### The schedule

| Time (UTC) | What runs | Why there |
|---|---|---|
| 02:00 | `hermes-pull` SSHes in for the nightly zip | Cluster-side, done well before 02:30 |
| 03:00 | The restic sweep | Cluster-side; does not touch this VM |
| 03:30 (+0–10 min) | `apt-daily.timer` refreshes the package lists | `unattended-upgrade` installs only from the cache and never refreshes the lists itself |
| 04:00 (+0–10 min) | `apt-daily-upgrade.timer` runs `unattended-upgrade` | Leaves a 35-minute margin before the reboot |
| 04:45 | Automatic reboot | Reboot is **on**, by operator decision, August 26, 2026 |
| 05:45 (+0–5 min) | `hermes-app-alive` | **One hour** after the reboot, so the reboot has finished and the lingering user units have come back before anything looks at them |

Both drop-ins set `FixedRandomDelay=true`, so the offset is derived from the machine ID
and unit name rather than re-rolled nightly. The gap between the refresh and the install
is then a constant for this machine, and `systemctl list-timers` can be read once and
still trusted tomorrow.

**The jitter is 10 minutes, not Debian's hour, and that is a reboot correctness matter
rather than a tidiness one.** `unattended-upgrade` hands the literal `04:45` to
`shutdown`, and systemd resolves a time that has already passed to the same time
**tomorrow**. An upgrade still running at 04:45 does not delay the reboot by minutes; it
slips it by 24 hours, and the kernel sits installed-but-not-running for a day.

**The two apt jobs share a lock, and the waiter blocks for up to an hour.** A 03:30
refresh that overruns past 04:00 makes the upgrade **block** rather than fail. That is the
safe behaviour — nothing installs against half-refreshed lists — but the block eats into
the 35-minute margin before the reboot. If `systemctl list-timers` shows the upgrade
starting late, look at the refresh run's duration first.

### Why nothing watches apt directly

There is no `-success` file and nothing creates one. `unattended-upgrade` writes
`/var/lib/apt/periodic/unattended-upgrades-stamp` for itself in `write_stamp_file()`, and
it writes it on a run that found **nothing to do** just as readily as on one that installed
everything. The other files in that directory belong to `apt.systemd.daily` and are not
this.

So the stamp proves the timer fired, not that anything was patched. The wrapper's 14-day
gate (`APT_MAX_AGE_DAYS=14`) is built on that and claims no more. The VM has no MTA, so
`unattended-upgrades` mail would be a silent failure; the journal is the record.

## Accepted exposures

Both environment files live under `~/.hermes`, so **both ride the nightly `hermes backup`
zip** to the `hermes-dumps` PVC and on to B2. The healthchecks.io ping UUID and the
uptime-kuma push token therefore rest in two more encrypted places than they strictly need
to.

That is accepted, and the reason is the tier. Both are **tier-2 spam-target identifiers**,
not secrets: holding one lets a stranger report a heartbeat and mask a genuine failure,
and grants nothing else. Neither needs rotation and neither earns an honesty-box row.

Note what is **not** in those files. An earlier design generated an `API_SERVER_KEY` for a
dedicated probe profile and put it in the same file. Deleting that design removed the only
tier-1 value this machinery would have added, so it now creates no concealed 1Password
field at all.

## What this does not watch

Be specific here, because a deleted design covered a different set.

- **Nothing continuous.** Detection latency for an application fault is up to about a day,
  and a missing heartbeat alarms about 30 hours after the last good one. **Accepted by the
  operator on August 26, 2026 — "homelab not NASA."** The existing `hermes` uptime-kuma
  monitor still catches a VM that is off or unreachable, faster, through the dashboard on
  `hermes.cynexia.com`.
- **No chat turn is monitored at all.** The daily check makes none, by design; the update
  wrapper's is the only one, and update runs are unscheduled. A fault that lets the agent
  import, serve `/health` and keep its units up while failing every chat turn is caught at
  the next update session, up to six weeks later.
- **The daily import runs in a fresh interpreter, not the WebUI's process.** So a venv
  repaired without restarting `hermes-webui` reports `verdict=ok` while the live process
  still cannot serve — which is the very failure that check exists to catch. After any venv
  repair, restart the unit.
- **Four of the five units are only counted, never exercised.** The daily check asserts
  something real about exactly one unit, `hermes-webui`, through its `/health`, and about
  the shared venv through the import. The three gateways **and `hermes-dashboard`**
  contribute nothing but a `systemctl --user is-active` result, so a wedged-but-running
  gateway or dashboard reports healthy. The gap is smaller for the dashboard than for the
  gateways: the existing `hermes` uptime-kuma monitor probes it externally on
  `hermes.cynexia.com/api/health`, so a wedged dashboard is caught there. **Nothing
  external probes the three gateways.**
- **Per-profile state.** Both checks exercise the shared venv and the default profile. A
  fault confined to `emh` or `hal` profile state is invisible to both.
- **A dead apt timer.** The 14-day gate that would catch it lives in an unscheduled
  script, so it surfaces at the next update session, up to six weeks later.

## Cost

The daily liveness check costs **zero model tokens** — three local assertions and one
HTTPS push. The update wrapper's single chat turn costs one small model call per run, and
runs are unscheduled. This replaced a design that would have spent about 96 model-backed
turns a day.

## Facts about this VM, recorded so nobody rediscovers them

**`uv` is at `$HERMES_HOME/bin/uv` and is resolved by absolute path**
(`hermes_cli/managed_uv.py:53-63`, whose docstring says "no PATH probing, no conda guards,
no multi-location resolution chains"). It is on no PATH on this VM and does not need to
be. That is why both units set `HERMES_HOME` rather than extending PATH: a wrong or absent
`HERMES_HOME`, not a short PATH, is what would stop `hermes update` finding its own
tooling.

**`hermes update` restarts the three gateway units itself** — `hermes update --plan` says
so — and does **not** restart `hermes-webui` or `hermes-dashboard`, because neither runs
the `hermes` entry point. The wrapper's `restart_units` is therefore a second restart of
the gateways and the **only** restart of the other two. Without it, the WebUI keeps serving
the module already resident in memory, and the break surfaces at the next restart or the
04:45 reboot.

**The API server's posture, as found:** `API_SERVER_HOST=0.0.0.0` and
`API_SERVER_CORS_ORIGINS=*` on all three profiles, with the gateway logging a
network-accessible and unsandboxed warning on every start. **The host-level bind is not
the control, and nothing on the VM is.**
[homelab.md's security posture section](homelab.md#security-posture--read-before-fixing-any-of-it)
records what actually holds: the VM runs no host firewall, the LAN is a trusted zone, and
the only gate an agent cannot forge is the Cloudflare Access service token on the published
hostnames. Firewalling 8787, 9119 and 8642 down to the cluster egress address is the
obvious hardening and is a change of its own. The health assertion's chat turn reaches the
gateway on loopback, so it needs none of that to change.

**`/p/<profile>/` routing exists but is inert.** The routes are registered
unconditionally, and the prefix is **discarded** while `gateway.multiplex_profiles` is
off — which it is. So `/p/anything/…` reaches the default profile. That is why the health
assertion uses the unprefixed path. Nobody should rediscover this the hard way.

**The `hindsight-client` skew, as found:** `0.6.1` installed against a deployed `0.9.1`
server, so the first wrapper run jumps three minor versions in one step.

### `hermes` CLI spellings

Recorded from the August 26, 2026 survey. Three of these were wrong in earlier drafts and
one reported contradiction was a false alarm.

| Spelling | Note |
|---|---|
| `hermes secrets onepassword` / `op` / `1password` | **Aliases.** The help line reads `onepassword (op, 1password)`. Both spellings committed elsewhere in `docs/` are correct; there is nothing to fix |
| `hermes config show` | **Not** `config list` |
| `hermes -p <profile> memory off` | The first-class way to drop the external memory provider, rather than editing a config key |
| `hermes tools disable NAME... --platform api_server` | Disables a toolset. **Its default platform is `cli`**, so omitting `--platform` disables it somewhere other than where you meant |
| `hermes update --check` / `hermes update --plan` | The read-only pair. **`--check` exits 0 whether or not an update is available** — read its output, not its status |

The last three are recorded for future use rather than used by anything today; the tasks
that needed them were deleted.

## A known fault this machinery does not fix

**The agent's memory writes to its Hindsight backend are failing with HTTP 401, "Invalid
API key."** Found on August 26, 2026. Every occurrence in two months of gateway journal is
that same failure, with no successes at all.

Nothing on this page detects it, and nothing on this page caused it. The chat turn returns
success regardless, because the memory write happens on a background path the response does
not wait for — which is the same fail-open behaviour
[monitoring.md](monitoring.md#what-this-does-not-catch) records for the memory backend
generally. The health assertion is therefore green over a VM that has not written a memory
in two months.

This needs investigating from the Hindsight side: the tenant key the profile presents,
against the key the server accepts. Start with
[hindsight.md, "Rotating the tenant API key"](hindsight.md#rotating-the-tenant-api-key)
and the plugin config rules in
[homelab.md](homelab.md#hermes-vm-configuration-layout-and-secrets) — the dashboard GUI
writes secrets to `config.json` in cleartext, and a stored key shadows the 1Password-backed
variable until it is removed.
