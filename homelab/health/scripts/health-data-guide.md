# Reading the health data in InfluxDB

Wide Withings schema, September 4, 2026. Field names come from `TYPES` in `homelab/health/scripts/withings-ingest.py`, which is the source of record.

Read this before writing any Flux against this instance.

## Connection

Organization `cynexia`. Five buckets:

| Bucket | Holds |
|---|---|
| `withings` | Body composition, height and blood pressure from two Withings scales and a cuff |
| `apple_metrics` | Everything Health Auto Export sends from Apple Health, one measurement per metric-and-unit |
| `apple_workouts` | Apple workout records |
| `garmin` | Garmin device data, including a wide `BodyComposition` measurement |
| `cloudflare` | Per-hostname Cloudflare request analytics |

**A mixed-field-type group is an error, not an empty result, and that holds for the whole organization.** Grouping tables whose `_value` types differ answers `schema collision: cannot group string and float types together`. That body reads as "no data" if you check only for rows. Check for an error object before you conclude a bucket is empty.

## The `withings` bucket

One measurement, `withings_measure_group`. One point per measure group — one weigh-in, one typed height, or one cuff reading.

Tags, all group-level:

| Tag | Meaning |
|---|---|
| `person` | Always `rob` |
| `grpid` | The Withings group id: the per-reading entity, one value per weigh-in |
| `deviceid` | The device that took the group, or `unknown` for a manual entry |
| `model` | The device model name, when the API sent one. Absent on groups from before 2022 |

One float field per measure, already scaled to the unit in the table below. **There is no string field**, so no field filter is needed and ungrouped aggregates work.

### How a field is named

```
name(code)      = the TYPES name              if the code is known, else "type_<code>"
suffix(pos)     = the POSITIONS name          if the position is known
                  "position_<pos>"            if it is not
                  "position_none"             if the measure carries no position
segmental(code) = name(code) ends in "_segments"
repeated(code)  = this group holds more than one measure with this code
field           = name(code)                                      if neither
                = name(code) minus "_segments" + "_" + suffix(pos) if either
```

A whole-body code carries no suffix even when the API sends a `position`, because that position is an electrode path and not an anatomy: extracellular and intracellular water arrive at position 7, `whole_body`, and are still written as `extracellular_water` and `intracellular_water`.

### Sparse rows are normal

A group with no composition carries `weight` alone, and a few groups carry composition with no weight. **Never anchor a latest-reading query on `weight`** — you will skip those.

### Ask for the vocabulary

`schema.fieldKeys(bucket: "withings")` returns all of it. That is the authoritative list; the table below carries the units.

### The field table

Neither the unit nor the name's meaning is in the data. Both are here.

<!-- field-table -->
| Field | Unit | Code |
|---|---|---|
| `weight` | kg | 1 |
| `height` | m | 4 |
| `fat_free_mass` | kg | 5 |
| `fat_ratio` | % | 6 |
| `fat_mass_weight` | kg | 8 |
| `diastolic_blood_pressure` | mmHg | 9 |
| `systolic_blood_pressure` | mmHg | 10 |
| `heart_pulse` | bpm | 11 |
| `muscle_mass` | kg | 76 |
| `hydration` | kg | 77 |
| `bone_mass` | kg | 88 |
| `pulse_wave_velocity` | m/s | 91 |
| `vascular_age` | — | 155 |
| `extracellular_water` | kg | 168 |
| `intracellular_water` | kg | 169 |
| `visceral_fat` | index | 170 |
| `basal_metabolic_rate` | kcal | 226 |
| `metabolic_age` | years | 227 |
| `fat_free_mass_<position>` | kg | 173 |
| `fat_mass_<position>` | kg | 174 |
| `muscle_mass_<position>` | kg | 175 |
<!-- /field-table -->

`—` means the source states no unit, and none is guessed.

`<position>` is one of these five:

<!-- segment-positions -->
`right_arm`, `left_arm`, `torso`, `left_leg`, `right_leg`
<!-- /segment-positions -->

**Whole-body fat mass is `fat_mass_weight`, while its segments are `fat_mass_*`.** That asymmetry is deliberate: `fat_mass_weight` is Withings' own name for the whole-body code.

### Residue

A field named `type_<code>`, or a suffix `_position_<n>`, means a code or a position that `TYPES` and `POSITIONS` do not name — newer firmware, most likely. **Report it. Do not guess what it holds.**

## Flux idioms for `withings`

Every block below carries its measurement filter, so each one is complete on its own.

### 1. Latest weigh-in, whole

```flux
from(bucket: "withings")
  |> range(start: -90d)
  |> filter(fn: (r) => r._measurement == "withings_measure_group")
  |> keep(columns: ["_time", "grpid", "deviceid", "_field", "_value"])
  |> group()
  |> pivot(rowKey: ["_time", "grpid", "deviceid"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 1)
```

One row, sparse columns. It anchors on no field, so a weightless group is visible rather than skipped.

### 2. Weight over time, per device

```flux
from(bucket: "withings")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "withings_measure_group" and r._field == "weight")
  |> group(columns: ["deviceid"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
```

### 3. Composition stack

```flux
from(bucket: "withings")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "withings_measure_group")
  |> filter(fn: (r) => r._field == "fat_mass_weight" or r._field == "muscle_mass" or r._field == "bone_mass")
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
```

### 4. Segmental muscle by limb

```flux
from(bucket: "withings")
  |> range(start: -90d)
  |> filter(fn: (r) => r._measurement == "withings_measure_group")
  |> filter(fn: (r) => contains(value: r._field, set: [
       "muscle_mass_right_arm", "muscle_mass_left_arm", "muscle_mass_torso",
       "muscle_mass_left_leg", "muscle_mass_right_leg"]))
  |> group(columns: ["_field"])
  |> last()
  |> group()
```

No pivot and no composite key. Anatomical ordering is a display concern.

### 5. Left-right symmetry, with a real time axis

```flux
from(bucket: "withings")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "withings_measure_group")
  |> filter(fn: (r) => r._field == "muscle_mass_left_arm" or r._field == "muscle_mass_right_arm")
  |> keep(columns: ["_time", "_field", "_value"])
  |> group()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({_time: r._time, _value: r.muscle_mass_left_arm - r.muscle_mass_right_arm}))
```

A reading missing one side yields null. That is true of this shape and of every other.

### 6. Readings per scale

```flux
from(bucket: "withings")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "withings_measure_group")
  |> group(columns: ["deviceid", "grpid"])
  |> first()
  |> group(columns: ["deviceid"])
  |> count()
```

No field filter is needed. The row key is `deviceid` alone: grouping on `model` as well would split the device whose older groups carry none.

### 7. One day's body composition

```flux
from(bucket: "withings")
  |> range(start: 2026-09-03T00:00:00Z, stop: 2026-09-04T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "withings_measure_group")
  |> keep(columns: ["_time", "grpid", "deviceid", "_field", "_value"])
  |> group()
  |> pivot(rowKey: ["_time", "grpid", "deviceid"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"], desc: true)
```

Pattern 1 without the limit: one row per weigh-in in the window, so the reading boundary is structural rather than inferred.

### 8. A derived metric: extracellular share of total body water

```flux
from(bucket: "withings")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "withings_measure_group")
  |> filter(fn: (r) => r._field == "extracellular_water" or r._field == "intracellular_water")
  |> keep(columns: ["_time", "_field", "_value"])
  |> group()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({_time: r._time,
       _value: r.extracellular_water / (r.extracellular_water + r.intracellular_water) * 100.0}))
```

The same six lines serve every pair. Only the two field names and the arithmetic change.

### 9. Dedupe check

```flux
from(bucket: "withings")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "withings_measure_group")
  |> group(columns: ["grpid", "_field"])
  |> count()
  |> filter(fn: (r) => r._value > 1)
  |> group()
```

Empty is correct. A hit means one weigh-in was written under two tag sets — in practice, `model` present on one write and absent on another.

## Differences from the sibling buckets

The same quantity has three shapes here. Do not carry a query pattern across.

| Bucket | Shape | Weight, as an example |
|---|---|---|
| `withings` | One measurement, one point per reading, one float field per measure, **already scaled to kg** | `withings_measure_group`, field `weight`, `76.925` |
| `garmin` | Wide like withings — `BodyComposition` with one field per measure — but the masses are in **grams** | `BodyComposition`, field `weight`, `76919`. Divide by 1000 |
| `apple_metrics` | One measurement per metric-and-unit, a numeric `qty` field plus three string fields | measurement named for the metric, field `qty` |

`garmin` and `apple_metrics` are not reshaped and will not be: `apple_metrics` is written by an upstream image whose own dashboard would need a hand rewrite at every bump.

## Two scales, both live

| `deviceid` | Role |
|---|---|
| `b85992ff4770833b2b0300b4a4f61a62fe4ce448` | `BodyFit`, the current scale, which produces the segmental and water fields. First reading 2026-09-03 |
| `bbe9fb71807031b3f5c4a9748b75005e44b4d7eb` | `Body+`, the older scale, and the continuity source for the long weight history: readings since 2017, with no `model` tag on groups before 2022-01-19 |

**Never combine them.** They disagree on body composition by more than the change you are usually looking for. Filter or group on `deviceid` in any query that spans both.

## Validation checks

- Segmental masses of one family sum to about the whole-body total for the same reading.
- `extracellular_water + intracellular_water` is about `hydration`.
- One point per `grpid`: pattern 9 is empty.
- Deduplicate to one reading per local day before a rolling average, or a day with two weigh-ins counts twice.

## Timezone

Daily points elsewhere in this instance are stamped at local midnight expressed as UTC. Before any `aggregateWindow(every: 1d)`, set the location:

```flux
import "timezone"
option location = timezone.location(name: "Europe/London")
```

Without it, a British Summer Time day is bucketed an hour off and the first and last days of a range are wrong.

## What not to do

- **Do not pivot on a type name and read a segment as the whole body.** That was the old schema's failure and it was silent. Under this schema the segments have their own field names.
- **Do not filter on `_field == "value"`.** No such field exists here any more; the filter returns an empty result rather than an error.
- **Do not anchor a latest-reading query on `weight`.** Sparse rows are normal.
- **Do not combine the two scales.**
- **Do not guess what a `type_<n>` field holds.** Report it.
- **Do not quote a record count from this file.** Measure it with `count()`, `first()` and `last()`.
