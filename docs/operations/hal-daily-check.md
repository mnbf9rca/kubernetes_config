# Hal daily house check

## Daily flow

1. At 09:00 Europe/London, native Hermes job `e6a2fb6484b8` runs `hal-daily-check-morning.py` on the Hermes VM without a model.
   It reads Home Assistant states, registries, failed automation runs, retained errors, integration setup, repairs and pending updates.
   Most reads use WebSocket; the offline monitor's live state and timestamps come from the read-only `/api/template` request.
   The comparison starts at the last accepted morning's saved cutoff, or 24 hours earlier if none exists.
   Previously emailed assessments are read from a separate store.
2. The script composes the morning email from those facts.
   Offline devices and their low batteries share one item; other low batteries remain separate.
   Error lines include the actual problem, first/last occurrence, count and current integration condition.
   Current failures can include older retained records; recovered records appear only after the previous cutoff.
   Already-assessed updates get a dated reminder, while new or unsent advice says “assessment pending”.
   After the board work in step 3, the script freezes the morning draft, sends it, stores the acceptance receipt and pushes the Kuma heartbeat.
3. Before sending, the script updates Hal's native Kanban board.
   It creates one batch root for new unassessed package/installed/available pairs outside existing open work.
   It creates or reuses one look-over root per unhealthy device situation, which can group related devices.
   An accepted look-over keeps that episode root, without a new worker or repeat email; a complete check archives it after recovery.
   Assessed pairs stay outside new batches, and the same package/version pair retains its advice.
4. The shared Kanban dispatcher polls every 60 seconds and starts ready workers when capacity and dependency gates allow.
   Hal plans each batch through `kanban_hal_daily_plan`, locating releases itself without a guessed URL scheme or fixed source list.
   The host creates public-only quarantined reader cards and a dependent Hal assembly card; household inventory stays with Hal.
   Readers return structured summaries of breaking changes, migrations, relevant fixes and required upgrade steps.
   Hal compares those summaries with this installation and writes a verdict and action lines per package.
   Look-over workers read device entities, report times, battery readings and integration evidence, then write one finding per device.
5. An assembly or look-over worker calls `kanban_hal_daily_publish`.
   The host reads the card and complete reader results from the board, validates them and refuses advice for affected invalid pairs.
   It stores the frozen draft before sending, then saves the receipt after SMTP acceptance.
   A batch produces one assessment email, including any pending package reasons and earlier-assessed reminders; a look-over produces one email per situation root.
   Concurrent publishers wait for the exclusive lock for up to the existing 180-second mail timeout.
6. The morning receipt suppresses another accepted morning email on the same London date; card work can continue later that day.
   Refused publications remain scheduled, and the next 09:00 run wakes scheduled and runtime-blocked Hal cards.
   A recovered device's episode is archived quietly when the device check is complete.
   Still-pending installed updates get the next morning's reminder; unchanged pending-only assessments can close silently after their first notice.
   A quiet day needs only the morning email.

## Files and ownership

Hal's profile home is `/home/hermes/.hermes/profiles/hal`.
The shared gateway owns scheduling and dispatch; Hal needs no separate gateway unit.

| Purpose | Installed location | Repository source |
|---|---|---|
| Morning entrypoint | `<profile>/scripts/hal-daily-check-morning.py` | `hermes-vm/scripts/hal-daily-check-morning.py` |
| Collection, findings and morning rendering | `<profile>/scripts/hal-daily-check/hal_daily_check.py` | Same path below `hermes-vm/` |
| Native cards, validation and publication | `<profile>/scripts/hal-daily-check/hal_daily_cards.py` | Same path below `hermes-vm/` |
| Two worker tools | `<profile>/plugins/hal-daily-check/` | `hermes-vm/plugins/hal-daily-check/` |
| Native email adapter extension | `<profile>/plugins/hal-email-verdict/` | `hermes-vm/plugins/hal-email-verdict/` |
| Hal guide and persona | `<profile>/workspace/HA-WATCH.md`, `<profile>/SOUL.md` | `hermes-vm/profiles/hal/` |
| Native cron store | `<profile>/cron/jobs.json` | Agent state; no committed job definition |
| Native execution records | `<profile>/cron/executions.db` | Agent state |
| Board and worker logs | `/home/hermes/.hermes/kanban/boards/hal/kanban.db`, adjacent `logs/<task-id>.log` | Native agent state |

The no-agent job is named `ha-daily-health-email`, uses `0 9 * * *` in Europe/London and delivers scheduler output locally.
It runs at 08:00 UTC during British Summer Time and 09:00 UTC in winter.
The old producer `9f6d4108c6a6`, `hal-assessment-morning.py`, is paused and retained for rollback.
The former watchdog description and 08:15 producer are not the active pipeline.
Other legacy observer jobs and timers are separate from this runner.

### Private state

The three JSON stores live directly under Hal's profile home, outside its terminal sandbox mounts.
Each update is atomic; the producer and publisher use separate directory locks.

| File | Contents |
|---|---|
| `daily-check-state.json` | London date, frozen morning subject/body, collection cutoff, last accepted cutoff, receipt and acknowledged development notices |
| `daily-check-assessments.json` | Exact-pair assessments or permanent `no_public_notes` outcomes, frozen batches, pending notices and send receipts |
| `daily-check-device-notes.json` | Look-over notes and receipts keyed by episode root, plus frozen look-over batches |

A pair key represents the package, installed version and available version.
Batch roots use `hal-daily:batch:<London-date>`; assembly roots refer to the batch root ID.
Device situation keys derive from members and conditions; receipt keys use the episode root ID so a later recurrence can send again.
The planner uses native create commands with idempotency keys and initial dependencies, never database writes.
The native concurrent-create race is accepted: it can cost one duplicate card.

### Home Assistant access and mounts

Hal's MCP address is the operator's read-only endpoint, across chats and dispatched workers.
That restriction belongs to the address, not every other client using the same credential.
The native Home Assistant plugin is disabled and privately archived; `HASS_TOKEN` and HA credentials are absent from agent/worker mappings.
Only the host runner resolves `daily_check.runner_secrets.onepassword.env` into a private credential mapping.
`HAL_DAILY_RUNNER_HASS_URL` and `HAL_DAILY_RUNNER_HASS_TOKEN` do not enter child-process environments.
The runner enforces its fixed read-command set; it never installs updates or changes Home Assistant.

Hal retains a writable scripts identity bind, an existing trust boundary rather than a read-only host sandbox.
Large tool results have a Hal-only read-only identity bind at `/home/hermes/.hermes/profiles/hal/cache/spillover`.
The [mount procedure](hermes-vm.md#hal-read-only-tool-result-spillover) preserves every other mount.
Spilled reader results remain untrusted data when read through the sandbox.

## Email and retry rules

The sender uses Hermes's native SMTP adapter and returns its Message-ID.
Messages contain `multipart/alternative`: structured plain text first, escaped HTML second.
The shared section order is **Needs you / Resolved overnight / Updates / Could not finish / Already assessed**, omitting empty sections.
The morning email has no separate Already assessed section: its dated reminders are actionable Needs-you items.
Assessment mail uses Already assessed for saved verdicts and their original email date/time; those reminders do not increase its subject count.
Resolved overnight and pending Updates do not count either.
The subject count is the number of Needs-you bullets; whole-check blockers change the morning subject to “I could not finish the check”.

Hal supplies verdict-only assessment prose; the host adds package names and installed → available versions.
Pending note lines start with the available version and a colon, so the host can name the package once.
Assessments and pending notes can share a publish call and one batch email.
A real `no_public_notes` finding is permanent for that exact pair; inability to locate notes is only pending.
The [Hal guide](../../hermes-vm/profiles/hal/HA-WATCH.md) defines the prose rules, with understanding and Home Assistant terms taking priority.

A refused mail or failed publication parks its own card through native scheduling, without block-recurrence or triage routing.
`Tickets.ensure` wakes scheduled and blocked Hal cards during the next morning's board phase, before mail delivery.
Any status except done/archived counts as open work, so a parked batch prevents duplicate creation.
A frozen unsent draft retries identically; a saved receipt suppresses repeat publication.
Saved-but-unsent advice still participates in the three-day morning board reminder.
The dispatcher can run ready work later the same day; it does not automatically wake a scheduled publication.
SMTP acceptance is not inbox delivery, and a crash after acceptance but before receipt storage can repeat the same email.

Two existing scheduled look-over cards from 20 October retain older body instructions: Magnolia `t_4f3079fe` and ePaper `t_d288fc75`.
A guide or instruction-source edit does not rewrite existing card bodies.
Their next ordinary wake is 21 October at 09:00 BST; no manual replay or reset is needed.
The emitted instruction still has one old “device or integration name” clause; the guide requires device names and no healthy-integration finding.
This docs-only update does not change that runtime string or the stored cards.

## Diagnostics and the Kuma heartbeat

Production prints detailed diagnostics to **stdout**, including run-history coverage and unreadable recovered-error timing.
Hermes saves that output under `<profile>/cron/output/e6a2fb6484b8/<run-time>.md` with `deliver=local`.
The scheduler does not email this file; the script sends the separate morning email itself.
The execution database records start, completion and result; a scheduler `delivery_outcome: suppressed` does not prove mail failed.
Successful no-agent runs discard stderr, so stderr alone is not a durable diagnostic surface.
A recovered log with unreadable timing is currently omitted from email and reported only in stdout.
That deferred behavior remains unchanged; it is not silently described as a runtime fix here.

The `hal-daily-check` push monitor uses a 24-hour interval and one two-hour retry.
A missing daily beat therefore alarms about 26 hours after the last accepted beat, near 11:00 London time on an ordinary day.
It monitors morning-mail acceptance, including honest incomplete checks; it does not monitor later Hal mail.
The token reference is `op://hermes/hal-daily-check/kuma-push-token`; the profile mapping names `HAL_DAILY_CHECK_PUSH_TOKEN`.
The [monitor inventory](uptime-kuma.md#push-monitors) and [failure triage](monitoring.md#layers-3-and-4-uptime-kuma) describe its place in estate monitoring.

| Heartbeat `verdict=` | Status | Meaning |
|---|---|---|
| `accepted` | up | Morning SMTP acceptance and saved receipt |
| `already-accepted` | up | This London day's morning receipt already exists |
| `send-failed` | down | No acceptable complete SMTP result |
| `state-failed` | down | Draft or receipt could not be saved |
| `credentials-failed` | down, if a push token is available | Required credentials or profile setup failed |
| `timeout` | down, if a push token is available | The outer hang guard fired |
| `internal-failed` | down, if a push token is available | Unhandled runner failure |

No push comes from preview, trial or a production run that loses its non-blocking profile lock.
A credential failure before token resolution leaves silence instead of a down push.
A rejected push prints `verdict=heartbeat-failed`; that line is a local diagnostic, not a heartbeat verdict.
The HA reads, board phase, mail call and outer run have hang guards; none is a spending allowance.
The publish lock reuses the existing 180-second mail wait, with a monotonic deadline.

## Preview and trial

A local fixture preview sends nothing and changes no state:

```sh
python3 hermes-vm/scripts/hal-daily-check/hal_daily_check.py --fixture <snapshot.json>
```

For an authorized trial email, run this command on the VM as `hermes`:

```sh
HERMES_HOME=/home/hermes/.hermes/profiles/hal /home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/.hermes/profiles/hal/scripts/hal-daily-check-morning.py --trial
```

The interpreter path is Hermes's existing runtime entry, not a new virtual environment or pinned store generation.
Trial collects real HA reads and sends once with exactly one `[trial] ` subject prefix.
It uses the runner-only credential references and reads saved cutoffs and advice, but writes no state, creates no cards and pushes no heartbeat.
It takes no production lock, so it does not contend with the scheduled morning runner.
Trial omits board-generated three-day reminders and “Hal looked at this” annotations, because it does not ensure cards.
After today's accepted morning run, it reads automation runs only after that run's collection cutoff, near 09:00.
A refused trial returns exit 1 and does not retry.
No arguments to the cron entrypoint mean production; trial must be explicit.

## Rollback preimages

Candidates and manifests are under `<profile>/workspace/hal-daily-check-next/<change>-<UTC timestamp>/`.
Preimages are under `<profile>/workspace/hal-daily-check-next/rollback-native-ha-20261018T193004Z/<change>-<UTC timestamp>/`.
Each leaf preserves the replaced relative paths; its candidate manifest records preimage and installed hashes, protected files and test results.
These working directories are private runtime evidence, not source or instructions for Hal to execute.

For an approved code rollback:

1. Read the selected candidate manifest.
2. Wait for active Hal publishers to finish.
3. Restore only the listed code files from that change's preimages.
4. Compare their hashes with the recorded preimage hashes.
5. Run the affected local tests before admitting new work.
6. Preserve current state, receipts, card claims, schedules and profile credentials.

Restoring the native HA plugin, token mappings or paused producer is not an ordinary code rollback.
The retained old producer already uses the runner-only credential reference; it must not export that token to agents.
Guide and persona changes in a docs-only commit are canonical edits, not an installation.

## What this check does not watch

This is a morning snapshot, not continuous fault detection, an exhaustive HA configuration test or an update installer.
The trusted `sensor.offline_devices` owns device scope, exclusions and reporting allowances; unmonitored devices are outside the check.
Its every-minute template trigger is judged using the live `last_reported` value, not cached state JSON timestamps.
The installed guard requires a usable state and a report within five minutes; unchanged `last_changed` or `last_updated` alone is not stale.
Missing or incomplete device evidence never clears an episode.

Only retained automation failures are reported; disabled automations are not faults by themselves.
HA normally retains five traces per automation and can lose unsaved traces on restart; coverage diagnostics do not recover missing history.
A trace-retention setting below five defeats the script’s coverage inference.
Recent errors are the structured records still retained by HA, not a complete log archive since yesterday.
Loaded integrations with some unavailable entities belong under non-counting Resolved overnight; setup retry/error or all-unavailable means still failing.
Names are listed for up to five unavailable entities; larger groups use a count.
Ignored repairs stay ignored, and an empty repair list does not prove configuration validity.

A quiet morning does not prove future reader/model access, correct assessment prose, inbox delivery or later publication.
The quarantined reader's [limits](safer-web-reader.md#what-nothing-watches) still apply.
VM service/import checks and their separate blind spots remain in [What the daily check does not watch](hermes-vm.md#what-the-daily-check-does-not-watch).
No assessment price, spending cap, daily counter or prose-length limit is imposed by this pipeline.
