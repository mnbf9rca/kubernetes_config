# Home Assistant sensor history in Databricks

This one-off export supplies raw learning data in the managed volume `workspace.home_assistant.raw`.
Loading files into tables and modelling them are the operator’s exercises.
There is no schedule, cluster workload, Home Assistant installation or source write.

## Contents and privacy

| Dataset | Contents | Time key |
|---|---|---|
| `states` | Every retained state row for admitted `sensor.*` entities, including unavailable/non-numeric states, source IDs and raw epoch-second timestamps | `last_updated_ts` |
| `statistics` | Hourly long-term statistics, with their units, flags, source and other statistics metadata | `start_ts` |
| `entities` | A current snapshot of entity IDs, friendly names, units, device/state classes, platforms, device names and area names | Snapshot date |

Files use Zstandard-compressed Parquet at `<dataset>/date=YYYY-MM-DD/part.parquet` beneath `/Volumes/workspace/home_assistant/raw`.
Each time-series dataset has one file per UTC day; entities has one snapshot file.
The datasets cover different spans: retained states began in August 2026, while long-term statistics began in November 2022.
The local manifest records the exact range, cutoff, counts, bytes, admission decisions and verification result for each execution.

The export excludes every domain except `sensor`.
It also excludes phone/tablet telemetry, vehicle and EV-charger telemetry, Wi-Fi SSID/BSSID, explicit location/presence signals, door/window/motion devices, solar-position/timing sensors, doubtful keyboard-activity counters and the vacuum mission-start timestamp.
Ambiguous retired sensors are excluded unless their names clearly identify physical measurements.
Whole-word matching keeps ordinary outdoor temperatures, water-allocation measurements, entity counts and private-IP diagnostics.
Some harmless batteries and device diagnostics are deliberately lost with their presence-related families.
The private manifest lists each excluded entity and its reason; these household inventories are not committed to the public repository.

State history contains no arbitrary attributes, user/context IDs or events.
The metadata snapshot selects only the four label/class/unit fields above from recorder attributes, plus the requested registry names.
It does not reconstruct historic names, units or area assignments.
Physical energy/environmental measurements can still support indirect activity inference; this export excludes explicit presence/location telemetry rather than anonymizing the requested household data.

Recorder `last_changed_ts` and `last_reported_ts` can be NULL when equal to `last_updated_ts`.
The files preserve those raw NULLs.
Use `COALESCE(last_changed_ts,last_updated_ts)` and `COALESCE(last_reported_ts,last_updated_ts)` for effective times.

## Run an export

The script is [`scripts/ha-databricks-export.py`](../../scripts/ha-databricks-export.py).
It reads credentials per invocation through `op read`; item URLs are resolved in memory through `op item get`.
The Home Assistant item supplies `mariadb_username`, `mariadb_password`, `access_token` and its URL.
The Databricks item supplies `access_token` and its workspace URL.
All references use `op://Homelab/homeassistant/...` or `op://Homelab/databricks/...`.
Do not print those values or save the workspace URL in files or logs.

1. Start from a repository shell with the inherited 1Password service-account token.
2. Create a disposable environment outside the repository.

```sh
uv venv --python python3 /private/tmp/ha-export-venv
UV_CACHE_DIR=/private/tmp/ha-export-cache uv pip install --python /private/tmp/ha-export-venv/bin/python pymysql pyarrow websocket-client
```

3. Run the focused checks.

```sh
/private/tmp/ha-export-venv/bin/python scripts/test_ha_databricks_export.py
```

4. Export an inclusive-start, exclusive-end UTC date range into an empty directory outside every checkout.

```sh
/private/tmp/ha-export-venv/bin/python scripts/ha-databricks-export.py export \
  --start 2026-11-14 --end 2026-11-15 \
  --staging /private/tmp/ha-export-example
```

Omitting both dates exports all retained history up to command start.
The final current-day file is partial; the manifest records its exact cutoff.
Only the Mac receives these files during `export`.
A SELECT-only connection reads states hourly and statistics daily, using time indexes and bounded batches.
The three registry-list API calls are read-only.
A failed export retains completed files and an incomplete manifest; upload refuses that manifest.

5. Review every admitted entity and every exclusion in the private manifest before uploading household data.
6. Upload the completed staging directory.

```sh
/private/tmp/ha-export-venv/bin/python scripts/ha-databricks-export.py upload /private/tmp/ha-export-example
```

Upload creates the schema and managed volume through REST without warehouse compute.
It uses the existing warehouse for one query that counts uploaded rows per dataset and compares them with the manifest.
That query creates no tables or views.
The manifest records `MATCH` only after all three counts agree.

Re-running `upload` skips remote files with the same byte size and refuses different-sized collisions.
No existing file is silently overwritten.
A same-sized content change can escape this check; Parquet parsing/count verification catches structural or row-count errors, not every content change.
A later export overlapping the partial last day will normally collide because it contains more data.
Keep the original local files before deliberately deleting affected remote files for replacement.
Delete only the affected partition files in Catalog Explorer before uploading their intended replacements.
Do not replace a complete historical file with a newer export after recorder retention has purged its earlier rows.

A 30-second database statement timeout and network/verification timeouts stop hangs and fail visibly.
Stop on credential failures, source distress or paused Databricks compute.
Do not add retries that hide missing data.

## Delete the export

1. Open the SQL editor in the Databricks workspace referenced by the vault item.
2. Drop the managed volume to remove the export from the catalog.

```sql
DROP VOLUME IF EXISTS workspace.home_assistant.raw;
```

Databricks retains deleted managed data for a recovery period before asynchronous storage cleanup; this is not immediate physical erasure.
See [Databricks storage lifecycle](https://docs.databricks.com/aws/en/data-governance/unity-catalog/object-storage-lifecycle).

3. Drop the schema only after confirming that it contains no other learning work.

```sql
DROP SCHEMA IF EXISTS workspace.home_assistant;
```

4. Remove the private staging directory and its manifest when no local copy is required.

Deletion is a manual action; the export script never deletes source or destination data.
