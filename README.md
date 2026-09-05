# AutoXDL VBO Extractor

Generate **full-length** `.vbo` video-overlay files from AutoXDataLogger
(AutoXDL) session data.

The app's own `.vbo` export starts at the moment you cross the start line and
throws away everything before it. For a typical run that is 20–30 seconds of
staging and launch — gone. This tool reads the app's untrimmed per-run JSON
logs and writes one `.vbo` per run covering the **entire** recording: the
approach, the run, and the rollout after the finish.

It also fixes a few things the app's exporter gets wrong along the way — see
[Accuracy](#accuracy).

---

## Requirements

Python 3.9 or newer. **No dependencies** — standard library only. Tested on
CPython 3.9.6 and 3.14.7 on macOS.

## Install

```bash
git clone https://github.com/c20d-us/AutoXDL-VBO-extractor.git
```

Run it in place, or copy `autox_vbo.py` and `autox_vbo_channels.json` next to
your session folders.

---

## Quick start

Point it at the directory holding your sessions:

```bash
python3 autox_vbo.py
```

It finds every session folder below the current directory — a *session folder*
is any directory containing a `runs/` subdirectory — and writes
`<session>/vbo/<session>-<NN>.vbo`, one per run.

```
Event 20260628:
  Event 20260628-01.vbo: 1387 samples, 59.32 s, 23.36 Hz, lap 42.881 s
  Event 20260628-02.vbo: 1379 samples, 59.08 s, 23.32 Hz, lap 40.255 s
  ...
```

Or name sessions explicitly, and look before you leap:

```bash
python3 autox_vbo.py "Event 20260823" --dry-run
```

### Expected input

Exactly what the app produces — the script reads the full logs and ignores the
app's pre-trimmed `_parsed.json` files:

```
Event 20260823/
  runs/
    run_001.json
    run_001_parsed.json     (ignored)
    ...
  start-finish.csv          (optional; enables lap timing)
```

Without `start-finish.csv` you still get complete telemetry. The `[laptiming]`
section is omitted and the comments say so explicitly:

```
No start-finish.csv for this session: gate timing and coordinates unavailable.
```

---

## Where you run it from

**There is no working-directory requirement.** The current directory is only
the default place to look when you don't name a session. Output always goes to
`<session>/vbo/`, resolved from the session folder — never from where you
happen to be standing.

Any of these work. Substitute your own session path.

**From the folder holding all your sessions** — converts every session below it:

```bash
cd "$HOME/Library/Mobile Documents/iCloud~me~muellerklein~florian~AutoXDataLogger/Documents"
python3 ~/GitHub/AutoXDL-VBO-extractor/autox_vbo.py
```

**From inside a single session folder**, or even from its `runs/` subfolder —
both convert just that session:

```bash
cd "$HOME/.../Documents/Event 20260823"
python3 ~/GitHub/AutoXDL-VBO-extractor/autox_vbo.py
```

**From anywhere at all**, naming the session:

```bash
python3 ~/GitHub/AutoXDL-VBO-extractor/autox_vbo.py \
  "$HOME/Library/Mobile Documents/iCloud~me~muellerklein~florian~AutoXDataLogger/Documents/Event 20260823"
```

**Relative paths work too** — this is the same thing run from your home
directory:

```bash
cd ~
python3 ~/GitHub/AutoXDL-VBO-extractor/autox_vbo.py \
  "Library/Mobile Documents/iCloud~me~muellerklein~florian~AutoXDataLogger/Documents/Event 20260823"
```

### Quoting paths with spaces

`Mobile Documents` and `Event 20260823` both contain spaces. Quote the path
**or** backslash-escape the spaces — never both:

```bash
# correct: quotes, no backslashes
python3 ~/GitHub/AutoXDL-VBO-extractor/autox_vbo.py "Library/Mobile Documents/.../Event 20260823"

# correct: backslashes, no quotes
python3 ~/GitHub/AutoXDL-VBO-extractor/autox_vbo.py Library/Mobile\ Documents/.../Event\ 20260823

# WRONG: inside quotes a backslash is literal, so the path gains backslashes
python3 ~/GitHub/AutoXDL-VBO-extractor/autox_vbo.py "Library/Mobile\ Documents/.../Event\ 20260823"
#   -> Library/Mobile\ Documents/.../Event\ 20260823: no such directory
```

A **leading** `~` is not expanded inside quotes either, so `"~/Library/..."`
fails the same way. Use `"$HOME/..."`, which does expand in double quotes, or
leave the tilde outside them: `~/"Library/Mobile Documents/..."`.

The interior tildes in `iCloud~me~muellerklein~florian~AutoXDataLogger` are
harmless — a shell only expands a tilde at the start of a word.

### Which config file gets used

`autox_vbo_channels.json` is looked for **next to the script**, not next to
your data or in the current directory. Running the copy in your clone always
uses that clone's config, wherever you invoke it from. Point elsewhere with
`--channels FILE`.

A relative `--out-dir`, by contrast, is relative to the current directory, as
you would expect of an output path.

---

## Options

| Flag | Effect |
|---|---|
| `--dry-run` | Report what would be written, write nothing |
| `--out-dir DIR` | Write everywhere into `DIR` instead of `<session>/vbo/` |
| `--time-base {utc,elapsed}` | `utc` (default) writes real UTC time of day; `elapsed` mimics the app's seconds-since-log-start |
| `--channels FILE` | Use a different column table |
| `--list-channels` | Print the active column table and exit |

---

## What you get

Standard VBO telemetry — position, speed, heading, lateral and longitudinal g
— plus a `[comments]` block with everything needed to set up lap timing in
Circuit Tools or RaceRender:

```
Start line : 22.251 s into the log   avisynctime 22251 ms   UTC 17:20:17.930
Finish line : 80.339 s into the log   avisynctime 80339 ms   UTC 17:21:16.018
Lap time : 58.088 s

Gate coordinates (WGS84 decimal degrees, for RaceRender lap timing)
  Start line
    position  47.4941626, -122.7539300   (gate midpoint)
    heading   3.6 deg   (direction of travel when crossing; mean of 4 runs, max deviation 0.2 deg)
    this run crossed at 3.7 deg
    endpoints 47.4941629, -122.7538998  and  47.4941623, -122.7539602
    line is 4.54 m long, bearing A to B 269.2 deg
```

Gate crossings are **interpolated**, not snapped to the nearest sample — at
25 Hz a sample is about a metre of travel.

The heading is the direction the car actually **travels through** the gate,
averaged across every run in the session, which is what RaceRender's lap-timing
setup wants. It is not the gate line's own bearing, and it is not always
perpendicular to it: one gate in the sample data is crossed 15.7° oblique.

### Video sync

`avisynctime` counts milliseconds from the first logged sample, i.e. the video
is assumed to start when the log does. Nudge the offset in your overlay tool
and everything before the start line comes with it.

Each run's video resolves to `<session>_<run number>.mp4` — `Event 20260823_0004.mp4`
for run 4 — so all of a session's clips can live in one folder.

---

## Adding telemetry channels

Output columns are configuration, not code. `autox_vbo_channels.json` defines
them; delete it and the script falls back to a built-in table that produces
identical output.

A column needs a `name` (for `[header]`), a `short` name (for
`[column names]`), a `format` (a Python format spec), and exactly one source —
`key` to read a channel straight from the log, `derived` for something the
script computes, or `constant`:

```json
{"name": "velocity kmh", "short": "velocity", "key": "speed", "scale": 3.6, "format": "07.3f"}
```

Add `"unit": "rpm"` to get a `[channel units]` entry.

Set **`"required": false`** and the column is dropped from any run whose log
lacks that key. `rpm`, `throttle` and `coolant` ship pre-declared this way:
inert today, they appear on their own the moment the app starts logging them.

> Their key names are **guesses** and must be checked against a real log. A
> wrong key name fails silently — the column just never shows up. Verify with
> `--list-channels` and a test conversion.

```bash
python3 autox_vbo.py --list-channels
```

---

## Accuracy

Output was validated against the app's own `.vbo` files, row by row. In the
overlapping region every row matches byte-for-byte except where this tool is
deliberately correct and the app is not:

- **Dropped samples.** The app's exporter trips over floating-point noise in
  its own timestamps and silently skips those rows — 147 samples across one
  five-run session, in gaps of up to 0.64 s. This tool keeps every sample.
- **Stale heading.** The app derives heading from the previous *emitted* row,
  so after a dropped sample the bearing is measured across the gap. This tool
  uses the previous *logged* sample.
- **Log rate.** The app divides its row count by the full recording duration
  while emitting only the post-start portion, reporting 15.5–18.6 Hz for data
  logged at 22–23 Hz.

Lap times computed from the generated files agree with the app's own
`run_times.csv` to within **0.033 s** across all eight timed runs in the sample
data — and produce a time for one run the app failed to time at all.

---

## Documentation

[`DESIGN.md`](DESIGN.md) is the reference for anyone modifying this: the input
format and its traps, the reverse-engineered VBO conventions, every design
decision and why it was made, what remains unknown, and the procedure for
verifying that a change did not alter output.

## License

MIT — see [`LICENSE`](LICENSE).
