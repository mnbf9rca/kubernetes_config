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
   Current failures can include older retained records; recovered and unknown errors stay in stdout diagnostics.
   A checkable error can also appear after three consecutive mornings with a fresh occurrence.
   Active ignore and snooze rules remove matching items; expired rules are dropped.
   Requested action results since the last accepted cutoff appear under Done for you or, on failure, Needs you.
   Already-assessed updates get a dated reminder, while new or unsent advice says “assessment pending”.
   After the board work in step 3, the script freezes the morning draft, sends it, stores the acceptance receipt and pushes the Kuma heartbeat.
3. Before sending, the script updates Hal's native Kanban board.
   It creates one batch root for new unassessed package/installed/available pairs outside existing open work.
   It creates or reuses one look-over root per unhealthy device situation, which can group related devices.
   A saved look-over keeps that episode root, without a new worker or separate email; a complete check archives it after recovery.
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
   A batch with an assessed pair produces one email, including pending package reasons and earlier-assessed reminders.
   A batch with no assessed pair closes silently from its first attempt.
   A look-over saves its note against the episode root for the next morning email; it sends no separate email.
   Concurrent publishers wait for the exclusive lock for up to the existing 180-second mail timeout.
6. The morning receipt suppresses another accepted morning email on the same London date; card work can continue later that day.
   Refused publications remain scheduled, and the next 09:00 run wakes scheduled and blocked Hal cards.
   A recovered device's episode is archived quietly when the device check is complete.
   Updates waiting to be installed get the next morning's reminder; pending-only assessments close silently.
   A quiet day needs only the morning email.

## Files and ownership

Hal's profile home is `/home/hermes/.hermes/profiles/hal`.
The shared gateway owns scheduling and dispatch; Hal needs no separate gateway unit.

| Purpose | Installed location | Repository source |
|---|---|---|
| Morning entrypoint | `<profile>/scripts/hal-daily-check-morning.py` | `hermes-vm/scripts/hal-daily-check-morning.py` |
| Collection, findings and morning rendering | `<profile>/scripts/hal-daily-check/hal_daily_check.py` | Same path below `hermes-vm/` |
| Native cards, validation and publication | `<profile>/scripts/hal-daily-check/hal_daily_cards.py` | Same path below `hermes-vm/` |
| Worker tools, reply rules and write-log hooks | `<profile>/plugins/hal-daily-check/` | `hermes-vm/plugins/hal-daily-check/` |
| Reply rules and action records | `<profile>/scripts/hal-daily-check/hal_reply_actions.py` | Same path below `hermes-vm/` |
| Saved timed-restart entry | `<profile>/scripts/hal-reply-actions.py` | `hermes-vm/scripts/hal-reply-actions.py` |
| Native email adapter extension | `<profile>/plugins/hal-email-verdict/` | `hermes-vm/plugins/hal-email-verdict/` |
| Hal guide and persona | `<profile>/workspace/HA-WATCH.md`, `<profile>/SOUL.md` | `hermes-vm/profiles/hal/` |
| Native cron store | `<profile>/cron/jobs.json` | Agent state; no committed job definition |
| Native execution records | `<profile>/cron/executions.db` | Agent state |
| Board and worker logs | `/home/hermes/.hermes/kanban/boards/hal/kanban.db`, adjacent `logs/<task-id>.log` | Native agent state |

The no-agent job is named `ha-daily-health-email`, uses `0 9 * * *` in Europe/London and delivers scheduler output locally.
It runs at 08:00 UTC during British Summer Time and 09:00 UTC in winter.
The old 08:15 producer `9f6d4108c6a6`, its scripts and the daily pipeline's staging and rollback copies were deleted on 24 October 2026.
Other legacy observer jobs and timers are separate from this runner.

### Private state

The private JSON stores live directly under Hal's profile home, outside its terminal sandbox mounts.
Each update is atomic; the morning runner and publisher use separate directory locks.

| File | Contents |
|---|---|
| `daily-check-state.json` | London date, frozen morning subject/body, collection cutoff, last accepted cutoff, receipt, acknowledged development notices and daily error fingerprints |
| `daily-check-assessments.json` | Exact-pair assessments or permanent `no_public_notes` outcomes, frozen batches, pending notices and send receipts |
| `daily-check-device-notes.json` | Saved look-over notes and observation times keyed by episode root; older receipt fields remain readable |
| `daily-check-rules.json` | Device ignores, integration-error ignores and update snoozes; stable identities, optional expiry and applied reply IDs |
| `daily-check-actions.json` | Admitted reply context, native MCP write calls/results and saved timed-restart intents |

A pair key represents the package, installed version and available version.
Batch roots use `hal-daily:batch:<London-date>`; assembly roots refer to the batch root ID.
Device situation keys derive from members and conditions; notes use the episode root ID so a later recurrence can receive new advice.
The planner uses native create commands with idempotency keys and initial dependencies, never database writes.
The native concurrent-create race is accepted: it can cost one duplicate card.

### Home Assistant access and mounts

The operator switched Hal's MCP address to read/write on 22 October 2026.
Hal's HA-MCP URL comes from 1Password through the profile secret map and native substitution.
Hermes does not redact 1Password-resolved values automatically, so Hal uses a LAN-only direct-access URL.
Hal uses native HA-MCP write tools directly on explicit authenticated email replies; the usual install, reload and restart services are examples, not a whitelist.
Cards and audits remain read-only by instruction, and no new pre-tool block restricts the MCP surface.
The native Home Assistant plugin is disabled; `HASS_TOKEN` and HA credentials are absent from agent/worker mappings.
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
The shared section order is **Needs you / Done for you / Updates / Could not finish / Already assessed**, omitting empty sections.
The morning email has no separate Already assessed section: its dated reminders are actionable Needs-you items.
Assessment mail uses Already assessed for saved verdicts and their original email date/time; those reminders do not increase its subject count.
Done for you and pending Updates do not count either.
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
Saved-but-unsent assessments and missing look-over notes still participate in the three-day morning board reminder.
The dispatcher can run ready work later the same day; it does not automatically wake a scheduled publication.
SMTP acceptance is not inbox delivery, and a crash after acceptance but before receipt storage can repeat the same email.

Existing card bodies retain their original instructions.
The installed current guide overrides older requests for a separate look-over email; the host publisher saves the note silently.
A normal morning wakes scheduled and blocked Hal cards without manual replay or reset.

## Operator email replies — Tier A, 22 October 2026

Reply to a Hal email using the normal mail client.
The native `email` platform receives replies; `hal_email_verdict` sends the composed reports only.
The installed sender allowlist contains only the operator's direct address and SimpleLogin alias, with receiving-MTA authentication required.
No agent address is allowed.
In-Reply-To and Message-ID bind a reply to its new instruction; quoted original mail is context only.
Addresses and mail credentials live in the private profile configuration, not this repository.

Hal's native email home is the operator's direct address.
Native email startup/restart/shutdown notifications are disabled.
Email display settings suppress tool progress, reasoning, intermediate replies, busy acknowledgements, long-running status and warning notifications.
Hal's profile also disables memory-update, background-process and credits notices, the optional turn-completion explanation and failed-file-mutation footer.
These are native configuration settings; Hermes source is unchanged.
Two native notices can still appear: a suspended-session reset means Hermes could not continue the old conversation, and a compression-exhaustion notice means its context could not be shortened enough and a new session was opened.
Neither notice reports a Home Assistant action or fault.

A home channel also supplies the destination for a bare email `send_message` target, including messages sent from another platform.
Cron can use it for bare-platform delivery, broadcast delivery, or `origin` delivery without a saved origin.
All Hal cron records specify delivery: script jobs use `local`, and weekly audit `ea2df3246202` uses its explicit WebUI origin.
None gains email delivery from this home setting; the audit's existing WebUI delivery failure remains recorded in [the VM runbook](hermes-vm.md#hals-own-script-cron-jobs).
Reply-created timed-restart jobs also specify `deliver=local`.

Hal understands requests to ignore a device, ignore an integration's error, snooze/ignore an update, remove a rule, list current rules, install an update, reload an integration or restart HA now or at a stated time.
It answers questions about the estate from its reads.
For example, “Ignore Monstera through Friday” records a dated device rule, and “What are you ignoring?” lists active rules.
Ambiguous targets or times get one clarification question.
A completed or recorded request gets one truthful confirmation line.
An unavailable write connection gets one line saying Hal cannot act yet.
An accepted update request is not evidence that it loaded, and a reload does not prove a fix.

The single `hal_reply_rules` tool adds, removes and lists rules.
Device targets are registry IDs; error targets are integration domains; update targets are update entity IDs.
Rules without dates last until removed.
An `until` date is inclusive in Europe/London; the morning script removes expired rules.
Rules suppress both a device's offline line and its low-battery line, or the matching integration error/update, without a suppression notice.
Devices are grouped before suppression: a partly ignored hub group keeps its key and shared-connection finding; an entirely ignored group is hidden.
Saved look-over prose includes only lines named for current, non-ignored members; otherwise the deterministic finding remains.
Suppressed devices are not mistaken for recovered episodes.

Hal calls native HA-MCP write tools directly.
A post-tool hook records every write's time, native instruction Message-ID, actual instruction, tool, arguments and result in `daily-check-actions.json`.
Read actions of write-capable tools (`list`, `get`, `view`, `diff`, `diagnostics`, `network_status`, `ping`, `get_engine_theme`) are excluded from the write log.
Hal does not have to remember a separate logging call.
Rules changes are recorded there too.
The next morning puts successful requested actions under Done for you and failed actions under Needs you, since its previous accepted cutoff.
Install lines use the current update state before claiming installation; other accepted requests remain described as requests.
An uncertain result is not automatically replayed.

For a future restart, `hal_reply_restart_at` saves the explicit reply and creates a native no-agent one-shot for `hal-reply-actions.py`.
The script reads only saved due intents, marks each started before the HA-MCP call and records its result.
A crash or uncertain call does not replay that intent.
An interrupted scheduling attempt remains visible in the private action records rather than creating duplicate jobs.

The operator's 22 October ruling releases explicit reply actions (Tier A).
Autonomous fixes (Tier B) and automatic update installation (Tier C) remain deferred.
Hal never edits configuration files or deletes devices, entities, integrations or files.
The native HA plugin remains disabled; the deterministic morning runner retains its read-only command set and runner-only credential reference.

## Diagnostics and the Kuma heartbeat

Production prints detailed diagnostics to **stdout**, including run-history coverage and every retained integration error, its current state and timing.
Hermes saves that output under `<profile>/cron/output/e6a2fb6484b8/<run-time>.md` with `deliver=local`.
The scheduler does not email this file; the script sends the separate morning email itself.
The execution database records start, completion and result; a scheduler `delivery_outcome: suppressed` does not prove mail failed.
Successful no-agent runs discard stderr, so stderr alone is not a durable diagnostic surface.
A recovered log with unreadable timing is currently omitted from email and reported only in stdout.

The `hal-daily-check` push monitor uses a 24-hour interval and one two-hour retry.
A missing daily beat therefore alarms about 26 hours after the last accepted beat, near 11:00 London time on an ordinary day.
It monitors morning-mail acceptance, including an email that says the check could not finish; it does not monitor later Hal mail.
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

## Code rollback

Rollback reinstalls the selected files from git history on `master`.
The VM's daily-pipeline candidates and rollback preimages were deleted after operator acceptance on 24 October 2026.
They are no longer a recovery source.
Commit `bf743d2` is the first `master` revision containing this pipeline; earlier revisions do not contain its files.

For an approved code rollback:

1. Fetch `origin/master` in an isolated checkout.
2. Select a revision on `master` that contains the required daily-check files.
3. Check those files for compatibility with the current private state and card schemas.
4. Extract only the required files from that revision with `git show` or `git archive`.
5. Wait for active Hal workers and morning runs to finish.
6. Reinstall the extracted files at the locations in [Files and ownership](#files-and-ownership).
7. Compare installed hashes with the selected git files.
8. Run the installed tests without invoking the production or trial runner.
9. Preserve current state, receipts, card claims, schedules and profile credentials.

Changed plugin code can require a gateway reload; replacing tests or documentation does not.
Restoring the native HA plugin, token mappings or deleted producer is not an ordinary code rollback.
Neither private profile configuration nor old agent-authored pipeline code is recoverable from this repository's history.
Runner-only credentials must remain outside agent and worker environments.

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
There is no Resolved overnight section.
Unknown or uncheckable integration errors never reach the email.
Setup retry/error or all entities unavailable means still failing and reaches Needs you.
A loaded integration with some unavailable entities is diagnostic detail, unless the same error has a fresh occurrence on three consecutive mornings.
The saved history records error fingerprints by London date; merely retaining an old log record does not create another occurrence.
Unavailable entity names or a count remain in stdout diagnostics.
Ignored repairs stay ignored, and an empty repair list does not prove configuration validity.

A quiet morning does not prove future reader/model access, correct assessment prose, inbox delivery or later publication.
The quarantined reader's [limits](safer-web-reader.md#what-nothing-watches) still apply.
VM service/import checks and their separate blind spots remain in [What the daily check does not watch](hermes-vm.md#what-the-daily-check-does-not-watch).
No assessment price, spending cap, daily counter or prose-length limit is imposed by this pipeline.
