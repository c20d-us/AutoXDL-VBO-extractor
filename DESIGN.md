# DESIGN

Notes for whoever picks this up next, including a future Claude session. This
records not just *what* the code does but *why* each choice was made, which
facts were verified against real data, and which are still unknown. Read this
before changing behaviour.

---

## 1. What this exists to do

The AutoXDataLogger ("AutoXDL") iOS app records an autocross run — GNSS
position, speed, lateral/longitudinal g — and can export a `.vbo` file for
video-overlay tools (RaceLogic Circuit Tools, RaceRender).

**The app's export begins at the start-line crossing and discards everything
before it.** It then runs to the end of the recording. For run 3 of the
BSCC 20260823 session that meant 1,509 of 2,098 logged samples: the entire
approach to the line was gone.

`autox_vbo.py` reads the app's own untrimmed per-run JSON logs and writes one
`.vbo` per run covering the **whole** recording — the approach, the run, and
the rollout after the finish.

This is not a bug report against the app; front-truncation is its design
intent. Genuine app defects are catalogued in §9.

---

## 2. Repository layout

```
autox_vbo.py              the converter
autox_vbo_channels.json   output column definitions (see §7)
DESIGN.md                 this file
LICENSE                   MIT
```

Usage:

```bash
python3 autox_vbo.py                      # every session folder under ./
python3 autox_vbo.py "BSCC 20260823"      # named session folder(s)
python3 autox_vbo.py <session> --dry-run
python3 autox_vbo.py --list-channels      # show the active column table
```

Flags: `--out-dir`, `--time-base {utc,elapsed}`, `--channels FILE`,
`--list-channels`, `--dry-run`.

A *session folder* is any directory containing a `runs/` subdirectory. Output
goes to `<session>/vbo/<session name>-<NN>.vbo`.

---

## 3. Input data

A session folder from the app looks like:

```
BSCC 20260823/
  runs/
    run_001.json          full recording  <-- the script reads ONLY these
    run_001_parsed.json   trimmed to start->finish  <-- deliberately ignored
    ...
  start-finish.csv
  cones.csv
  run_times.csv
  run_times_parsed.csv
```

### `runs/run_NNN.json`

Exactly 15 keys, verified across 18 files in two sessions. Eight are
per-sample arrays of equal length; the rest are scalars.

| key | type | notes |
|---|---|---|
| `latitude`, `longitude` | float[] | WGS84 decimal degrees |
| `speed` | float[] | **metres/second** |
| `gForceX` | float[] | longitudinal |
| `gForceY` | float[] | lateral |
| `gnssFix` | int[] | binary. **`1` for all 16,623 samples in both sessions** |
| `distance` | float[] | cumulative metres; not written to `.vbo` by default |
| `dateTime` | string[] | see the warning below |
| `runIdx` | int | run number, used for filename and `avifileindex` |
| `runtime` | float | full recording duration, seconds |
| `videoStart` | float | **semantics unresolved — see §8** |
| `videoFile` | string | UUID of the phone-camera clip |
| `car`, `driver`, `tires` | string | written into `[comments]` |

**There is no satellite count anywhere.** Not in the JSON, not in the CSVs.
`gnssFix` is a binary validity flag, not a count. This is probably because iOS
CoreLocation does not expose satellite count — `CLLocation` gives
`horizontalAccuracy` but no view of the constellation. See §6.4 for what the
`sats` column therefore contains.

#### `dateTime` is hostile — read this before touching timestamp code

Values are serialised floats rendered as decimal strings, e.g.
`"2026-8-23 17:19:55.679589248"`. Three traps:

1. **Float-representation noise.** Fractional parts run to 15–17 digits:
   `"2026-6-28 18:41:1.9199896980000002"`, `"...40.199962952999996"`.
2. **The opposite.** Sometimes one digit: `"2026-8-23 17:20:18.0"`.
3. **No zero padding** on hour/minute/second: `"2026-6-28 18:41:2.03"`.

`parse_timestamp()` handles all three with a permissive regex plus
`float(seconds)`. Do **not** replace it with `strptime` or any fixed-width
parse — that is precisely the app's own bug (§9.1).

Timestamps are **UTC**. Confirmed to the second: each log file's mtime (written
when the run ends) is exactly 7.000 h behind that run's *last* `dateTime`
sample, across all four BSCC 20260823 runs — matching PDT (UTC−7) for the
recording date.

Sample rate is ~25 Hz for BSCC 20260823 and ~23 Hz with real gaps for
BSCC 20260628. Do not assume a fixed step; always work from the timestamps.

### `start-finish.csv`

One line, comma-separated:

```
startLatA,startLonA,startLatB,startLonB,finishLatA,finishLonA,finishLatB,finishLonB[,?]
```

Each gate is a line segment between two points. A **ninth field** appears in
some sessions (`12.917812843934012` for BSCC 20260823) and is absent in others
(BSCC 20260628 has only eight). **Its meaning is unknown.** It is not the gate
length (those measure 4.54 m and 5.22 m), not the distance between gates
(284 m). It is parsed as `extra` and never used. Do not guess at it.

### The other CSVs

`cones.csv` is lat/lon pairs for course cones — unused. `run_times.csv` maps
run filename to the app's lap time — used only for cross-checking (§10).

### `_parsed.json` — deliberately ignored

These are the trimmed start→finish versions. The whole point of this tool is
to avoid them. `run_files()` filters them out by the `_parsed` stem suffix.

---

## 4. Output format

VBO is a RaceLogic text format. Sections: `[header]`, `[comments]`, `[AVI]`,
`[laptiming]`, `[column names]`, `[data]`. UTF-8, LF endings, trailing newline.

Everything about the wire format was **reverse-engineered by diffing an
app-generated `.vbo` against its source JSON**, then verified by regenerating
the overlapping region and comparing byte-for-byte (§10).

### Coordinate convention — the thing that will bite you

VBO writes latitude and longitude in **minutes** (degrees × 60), with **west
longitude POSITIVE** — inverted from the usual sign.

```
-122.7539051  ->  +7365.23430600      (× 60, then negated)
  47.4941661  ->  +2849.64996600      (× 60)
```

`vbo_minutes()` does the ×60; the sign flip is applied at each call site for
longitude (`vbo_minutes(-lon)`). Get this wrong and the track appears in China.

### Column formats

Field widths were matched to the app's output exactly:

| column | source | format |
|---|---|---|
| `sats` | derived | `03d` |
| `time` | derived | `>10` (string, `HHMMSS.sss`) |
| `lat` / `long` | derived | `+.8f` (minutes) |
| `velocity` | `speed` × 3.6 | `07.3f` (km/h) |
| `heading` | derived | `06.2f` |
| `height` | constant `0.0` | `+09.2f` |
| `longG` / `latG` | `gForceX` / `gForceY` | `+.2f` |
| `avifileindex` | run number | `04d` |
| `avitime` | derived | `09d` (ms) |

### `[laptiming]`

```
Start        +7365.23398800 +2849.64977400 +7365.23761200 +2849.64973800 ¬  Start
```

Label padded to 13 chars, then **longitude first, then latitude** for each of
the two endpoints — the reverse of the data columns' order. The `¬` is
U+00AC. Coordinates come straight from `start-finish.csv`.

---

## 5. Decisions, and why

These were settled with the repo owner. Changing one is a behaviour change,
not a refactor.

### 5.1 Full recording, never truncated

The entire reason the tool exists. Every sample from the log's first to last
is emitted.

### 5.2 `time` column is **UTC time of day**

The app writes elapsed-seconds-since-log-start dressed up in `HHMMSS.sss`
(so `000123.880` means 83.88 s). That is not the VBO convention. This tool
writes real UTC (`171955.680`), which is standards-correct and lets runs be
correlated on a common timeline.

`--time-base elapsed` restores the app's convention if a tool ever needs it.

### 5.3 `avisynctime` is milliseconds from the first logged sample

**The video is assumed to start at the same instant as the log.** The owner
aligns video by hand in RaceRender, so no offset is applied. This is stated
explicitly in every file's `[comments]`.

Do not try to be clever with `videoStart` here — see §8.

### 5.4 `avifileindex` carries the run number

The app hardcodes `0001` in every file, so all runs in a session resolve to
the same video filename `<session>_0001.mp4` and cannot coexist in one folder.

The video path resolves as `<[AVI] prefix><avifileindex>.<[AVI] extension>`.
Keeping the prefix as `<session>_` and setting the index to the run number
yields `<session>_0004.mp4` for run 4. The `[AVI]` prefix line is **not**
suffixed with the run number — doing that would resolve to `_00010001.mp4`.

### 5.5 Heading uses the true previous sample

Heading is not logged; it is the bearing from sample *i−1* to sample *i*.
Sample 0 borrows the 0→1 bearing.

The app computes it from the previous **emitted** row, so after any dropped
sample the bearing is measured across the gap from a stale baseline (§9.3).
This tool uses the previous **logged** sample. This accounts for 29 of the
differences against the app's file, and this tool is correct.

Heading while stationary is GPS noise. Left raw deliberately — the owner may
want the real values. Do not silently damp it.

### 5.6 Gate crossings are interpolated, not snapped

At 25 Hz a sample is 0.04 s ≈ 1 m at autocross speed. Snapping a crossing to
the nearest sample costs more precision than the measurement is worth.
`segment_intersection()` finds where the path segment cuts the gate segment
and returns the fraction along it.

Lat/lon are treated as planar. Over a gate a few metres long at this latitude
the error is far below GPS noise.

### 5.7 Crossing heading is measured over ±5 samples and averaged per session

RaceRender's lap-timing setup wants a position plus **the heading the car
travels when crossing** — not the bearing of the gate line, and **not
necessarily perpendicular to it**. The BSCC 20260628 start gate proves this:
line bearing 87.0°, so perpendicular would be 357.0°, but the car actually
crosses at 341.3° — **15.7° oblique**.

One 0.04 s step is too short a baseline to be steady against jitter.
`HEADING_WINDOW = 5` (≈ ±0.2 s) settles it while staying short enough that
the car has not begun to turn.

Since the gate is fixed, every run measures the same quantity, so
`session_crossing_headings()` averages across all runs via `circular_mean()`
(vector mean — never average bearings arithmetically, 359° and 1° do not
average to 180°). Measured spread: **0.4°** across five June runs, **0.2°**
across four August runs. Each file prints the session mean and its own value
so an outlier is visible.

### 5.8 Honest `Log Rate`

Emitted rows ÷ emitted span. The app divides by the *full* run duration while
emitting only the post-start portion, so it reports 15.5–18.6 Hz for 22–23 Hz
data (§9.4).

---

## 6. Comment block contents

`[comments]` is generated, not copied. It carries:

1. `Log Rate`, session name, run number, sample count, duration, log start UTC
2. A statement of the time base and the `avisynctime` assumption
3. **Gate crossing times** — elapsed, `avisynctime` ms, and UTC, plus lap time
4. **Gate coordinates** — midpoint, crossing heading (session mean + this
   run's), endpoints, line length and bearing
5. Driver / car / tires

Multiple crossings of a gate are listed and numbered; the lap is taken from
the first start to the next finish after it and flagged as such. A gate never
crossed prints `not crossed in this recording`.

### 6.4 The `sats` column is a placeholder

There is no satellite count in the source (§3). The column is required by the
format, so it emits `12` when `gnssFix` is truthy and `0` otherwise — matching
the app, which hardcodes `012` everywhere. **`gnssFix` is `1` in every sample
of every run seen so far, so in practice every row reads `012`.**

Treat it as "the phone reported a valid fix". It is not telemetry. If a tool
shows a satellite plot from these files, it is showing a constant.

### Removed by request

The `To view video in Circuit Tools...` block, and with it the source clip
UUID. The owner does not need the clip mapping — the video comes off the phone
camera at export time.

---

## 7. The channel configuration system

Output columns are **data, not code**. `autox_vbo_channels.json` defines them;
`DEFAULT_CHANNELS` in the script is an identical fallback used when no config
file is present, so the config is optional.

Resolution order: `--channels FILE` (must exist) → `autox_vbo_channels.json`
beside the script → built-in `DEFAULT_CHANNELS`.

Each column needs `name` (for `[header]`), `short` (for `[column names]`),
`format` (a Python format spec), and **exactly one** source:

- `"key"` — read `run[key][i]`, optionally through `scale` / `offset`
- `"derived"` — computed; one of `time`, `sats`, `latitude_min`,
  `longitude_min`, `heading`, `run_index`, `avisynctime`, `elapsed`
- `"constant"` — same value every row

Optional: `unit` (adds a `[channel units]` entry; the section is written only
if something declares one) and `required` (default `true`).

### `required: false` is how new telemetry lands

A column marked `required: false` is **dropped from any run whose log lacks
that key**. `rpm`, `throttle` and `coolant` are pre-declared this way: inert
today, they materialise automatically once the app logs them. Verified with
one run carrying OBD data and one without, in the same session with the same
config — the first grew three columns and a `[channel units]` section, the
second was unchanged.

**The pre-declared OBD key names are guesses** (`rpm`, `throttlePosition`,
`coolantTemp`) and will not match until corrected against a real log. A wrong
key name fails *silently* — the column simply never appears. Check with
`--list-channels` and a test conversion.

`required: false` only governs what happens when a key is **absent**. A key
that *is* present always produces a column. This is why `distance` is not
pre-declared — it exists in every log and would immediately add a column.

Adding a channel to an existing tool chain changes the column count. Confirm
whatever consumes these files tolerates extra columns before shipping one.

### Extending derived sources

Add the name to `DERIVED_NAMES` and produce an array in the `derived` dict
inside `build_vbo()`. Both live near their use sites; the validator lists
valid names in its error message, so keep them in sync.

---

## 8. Unresolved: `videoStart`

Two readings fit the data and **the available evidence cannot separate them**.
Do not build video-sync logic on this field without new evidence.

Verified across all 9 runs in two sessions:

```
parsed.videoStart == raw.videoStart + (start-line offset into the log)
```

e.g. BSCC 20260628 run 1: `14.00 + 13.92 = 27.92`.

The trap: `raw.videoStart` and the start-line offset are *near-identical but
not equal* (14.00 vs 13.92; 18.20 vs 18.16). So either

- **(a)** `raw.videoStart` is the video lead-in, and it coincidentally almost
  equals the start-line offset in all 9 runs, or
- **(b)** it is a second, slightly different estimate of the start-line offset,
  and the app double-counts it when producing `parsed.videoStart`.

`run_004.json` of BSCC 20260823 has `videoStart == 0` while its start line is
crossed 22.2 s in, which argues for (a) — but that run is anomalous in other
ways (the app failed to time it at all).

**Current behaviour sidesteps this entirely**: `avisynctime` counts from the
first logged sample and the owner aligns video by hand (§5.3).

---

## 9. Defects found in the AutoXDL app

Established by diffing app output against its own source logs; a report was
sent to the developer. Recorded here as context, and because a future change
should not accidentally reproduce them.

All figures are over the five distinct runs of BSCC 20260628 (7,310 logged
samples, 5,245 written by the app).

**9.1 Malformed timestamps make the app's exporter drop samples.** The float
noise described in §3 defeats the app's own timestamp parse. **147 samples
dropped**, in 83 gaps; **81 of the 83 begin exactly on a float-noise
timestamp**. Longest run of consecutive drops: 16 samples (0.64 s). Root cause
is upstream — the app *writes* those strings — so ISO-8601 with fixed
millisecond precision would fix both writer and reader.

**9.2 Some rows survive with badly wrong timestamps.** 7 rows off by >5 ms,
worst **919 ms**. Run 1's sample logged at 14.880 s
(`18:24:3.9199617890000003`) was written as `000013.960` / `avisynctime
000013961`, while the very next sample was correct.

**9.3 Heading goes stale after every dropped sample.** 74 occurrences. See
§5.5.

**9.4 `Log Rate (Hz)` is wrong in every file.** See §5.8.

**Deliberately excluded from the report**: front-truncation (design intent,
not a defect); the `avifileindex 0001` collision (each app export is
single-run and internally consistent — a workflow suggestion, not incorrect
output); a 1 ms `avisynctime` rounding difference (immaterial).

**Not an app defect**: an early observation of duplicate run output was a
manual renaming mistake by the owner, not the app.

---

## 10. How this was verified — reproduce before changing behaviour

**Regression against the app's own output.** Take an app-generated `.vbo`,
match its rows to the generated file by the printed lat/long strings, and
compare every other column. Expected result for BSCC 20260823 run 3: all
1,509 app rows present among the 2,098 generated rows, with **zero
unexplained differences**. The only differences are intentional:

- 29 heading values (§5.5 — this tool is correct)
- 62 `avisynctime` values off by 1 ms (the app rounds up)
- `avifileindex`, now the run number (§5.4)

**Lap-time cross-check.** Interpolate the gate crossings from the generated
files and compare to the app's `run_times.csv`. All eight timed runs agree
within **0.033 s**. BSCC 20260823 run 4, which the app could not time at all,
yields 56.808 s.

**Structural checks.** Sample count equals the source array length; time is
monotonic; each file crosses each gate exactly once; comment coordinates match
`start-finish.csv` and agree with `[laptiming]` after converting out of VBO
minutes and un-flipping the longitude sign; each midpoint is the true mean of
its endpoints.

**Snapshot diff for refactors.** Before any change that should not alter
output, copy the `vbo/` directories aside, regenerate, and diff ignoring the
`File created on` line. The channel-config refactor was validated this way —
all nine files byte-identical.

Known-good lap times, for quick sanity checks:

| session | run 1 | run 2 | run 3 | run 4 | run 5 |
|---|---|---|---|---|---|
| BSCC 20260628 | 42.881 | 40.255 | 40.647 | 40.453 | 38.973 |
| BSCC 20260823 | 58.485 | 59.307 | 58.088 | 56.808 | — |

Session crossing headings: BSCC 20260628 start 341.3°, finish 181.8°;
BSCC 20260823 start 3.6°, finish 358.7°.

---

## 11. If you add OBD-II support

The app does not log OBD data yet. When it does:

1. Get one real log and read the actual key names — do not trust §7's guesses.
2. Add or correct entries in `autox_vbo_channels.json`. No code change should
   be needed for a plain scalar channel.
3. **Check the sample rate.** OBD-II typically arrives slower than GNSS —
   RPM at 5–20 Hz against 25 Hz positions. The JSON will carry either gaps or
   a held-forward value, and which one decides whether interpolation or
   last-value-carried-forward is right. `build_columns()` currently requires
   every channel to have exactly the sample count of `latitude`; a
   shorter/sparser channel will raise. That is deliberate — it fails loudly
   rather than silently misaligning telemetry against position.
4. Declare `unit` on each so RaceRender labels axes sensibly.
