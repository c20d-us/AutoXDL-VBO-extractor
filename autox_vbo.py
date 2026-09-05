#!/usr/bin/env python3
"""Generate full-length VBO files from AutoXDataLogger session data.

The app's own .vbo export begins at the start-line crossing and throws away
everything before it.  This script reads the untrimmed per-run logs in
<session>/runs/run_NNN.json and writes one .vbo per run covering the entire
recording -- the approach, the run, and the shutdown after the finish.

Usage:
    python3 autox_vbo.py                     # every session folder under ./
    python3 autox_vbo.py "BSCC 20260823"     # one or more session folders
    python3 autox_vbo.py <session> --out-dir /somewhere --time-base elapsed

A session folder is any directory containing a runs/ subdirectory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

# --- VBO conventions -------------------------------------------------------
#
# Latitude and longitude are written in minutes (degrees * 60) with WEST
# longitude POSITIVE -- the inverse of the usual sign, and the reason
# -122.7539 becomes +7365.2343.
#
# Columns, widths and formats below were reverse-engineered from an app-
# generated file so the output drops straight into Circuit Tools / RaceRender.

# --- channel configuration -------------------------------------------------
#
# Every output column is described by one entry here.  Each has a "name" for
# [header], a "short" name for [column names], and a "format" (a Python format
# spec).  Where the values come from is one of:
#
#   "key":      read run[key][i] straight from the log, optionally through
#               "scale" and "offset" (value * scale + offset)
#   "derived":  computed by the script -- one of the DERIVED names below
#   "constant": the same value on every row
#
# Optional per-channel keys:
#   "unit"      emitted in [channel units]; the section is written only if at
#               least one channel declares one
#   "required"  default true.  A channel with "required": false is dropped from
#               the file when its key is absent from that particular log, so a
#               session where only some runs carry OBD data still converts.
#
# Adding a logged channel is a config edit, not a code change: give it a name,
# a short name, the JSON key and a format.  Override the whole table with
# --channels FILE, or drop a file named CHANNELS_FILENAME beside this script.
#
# Derived values the script can supply:
#   time             time column, honouring --time-base (a preformatted string)
#   sats             12 when the sample has a GNSS fix, 0 when it does not
#   latitude_min     latitude in VBO minutes
#   longitude_min    longitude in VBO minutes, west positive
#   heading          bearing from the previous fix to this one
#   run_index        the run number, same on every row
#   avisynctime      milliseconds from the first logged sample
#   elapsed          seconds from the first logged sample

CHANNELS_FILENAME = "autox_vbo_channels.json"

DEFAULT_CHANNELS = [
    {"name": "satellites",    "short": "sats",         "derived": "sats",          "format": "03d"},
    {"name": "time",          "short": "time",         "derived": "time",          "format": ">10"},
    {"name": "latitude",      "short": "lat",          "derived": "latitude_min",  "format": "+.8f"},
    {"name": "longitude",     "short": "long",         "derived": "longitude_min", "format": "+.8f"},
    {"name": "velocity kmh",  "short": "velocity",     "key": "speed", "scale": 3.6, "format": "07.3f"},
    {"name": "heading",       "short": "heading",      "derived": "heading",       "format": "06.2f"},
    {"name": "height",        "short": "height",       "constant": 0.0,            "format": "+09.2f"},
    {"name": "LongitudinalG", "short": "longG",        "key": "gForceX",           "format": "+.2f"},
    {"name": "LateralG",      "short": "latG",         "key": "gForceY",           "format": "+.2f"},
    {"name": "avifileindex",  "short": "avifileindex", "derived": "run_index",     "format": "04d"},
    {"name": "avisynctime",   "short": "avitime",      "derived": "avisynctime",   "format": "09d"},
]

DT_RE = re.compile(r"^(\d+)-(\d+)-(\d+)\s+(\d+):(\d+):([\d.]+)$")


def parse_timestamp(s: str) -> dt.datetime:
    """Parse the logger's loose timestamps.

    Seconds arrive with anywhere from one to eighteen decimal places, and
    occasionally with float noise ('...559589420000002').  The app's own
    exporter silently drops those rows; we keep them.
    """
    m = DT_RE.match(s.strip())
    if not m:
        raise ValueError(f"unparseable timestamp: {s!r}")
    year, month, day, hour, minute, sec = m.groups()
    base = dt.datetime(int(year), int(month), int(day), int(hour), int(minute))
    return base + dt.timedelta(seconds=float(sec))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def vbo_minutes(degrees: float) -> float:
    return degrees * 60.0


def fmt_time_utc(t: dt.datetime) -> str:
    """UTC time of day as HHMMSS.sss, zero-padded to 10 characters."""
    secs = t.second + t.microsecond / 1e6
    return f"{t.hour:02d}{t.minute:02d}{secs:06.3f}"


def fmt_time_elapsed(seconds: float) -> str:
    """Elapsed seconds dressed as HHMMSS.sss -- the app's own convention."""
    hours, rem = divmod(seconds, 3600.0)
    minutes, secs = divmod(rem, 60.0)
    return f"{int(hours):02d}{int(minutes):02d}{secs:06.3f}"


# --- channel loading and evaluation ----------------------------------------

def load_channels(path: Path | None):
    """Load the channel table, falling back to the built-in default.

    An explicit --channels path must exist; the conventional file beside the
    script is used when present and ignored when not.
    """
    if path is None:
        beside = Path(__file__).resolve().parent / CHANNELS_FILENAME
        if not beside.exists():
            return DEFAULT_CHANNELS
        path = beside
    try:
        loaded = json.loads(path.read_text())
    except ValueError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from None
    channels = loaded.get("columns") if isinstance(loaded, dict) else loaded
    if not isinstance(channels, list) or not channels:
        raise ValueError(f"{path}: expected a non-empty list of columns, "
                         f'or an object with a "columns" list')
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict):
            raise ValueError(f"{path}: column {i} is not an object")
        for field in ("name", "short", "format"):
            if field not in ch:
                raise ValueError(f'{path}: column {i} is missing "{field}"')
        sources = [k for k in ("key", "derived", "constant") if k in ch]
        if len(sources) != 1:
            raise ValueError(f'{path}: column "{ch["name"]}" needs exactly one of '
                             f'"key", "derived" or "constant" (found {len(sources)})')
        if "derived" in ch and ch["derived"] not in DERIVED_NAMES:
            raise ValueError(f'{path}: column "{ch["name"]}" has unknown derived '
                             f'source "{ch["derived"]}"; known: '
                             f'{", ".join(sorted(DERIVED_NAMES))}')
    return channels


DERIVED_NAMES = {"time", "sats", "latitude_min", "longitude_min", "heading",
                 "run_index", "avisynctime", "elapsed"}


def format_value(value, spec: str) -> str:
    """Apply a format spec, rounding to int for integer specs."""
    if spec.endswith("d") and not isinstance(value, int):
        value = round(value)
    return format(value, spec)


def build_columns(run: dict, channels, derived: dict, n: int):
    """Evaluate every channel into a column of formatted strings.

    Returns (kept_channels, columns).  A channel marked "required": false whose
    key is missing from this log is dropped rather than failing the run.
    """
    kept, columns = [], []
    for ch in channels:
        if "derived" in ch:
            values = derived[ch["derived"]]
        elif "constant" in ch:
            values = [ch["constant"]] * n
        else:
            key = ch["key"]
            if key not in run:
                if ch.get("required", True):
                    raise ValueError(f'missing channel(s): {key} (for column "{ch["name"]}")')
                continue
            series = run[key]
            if len(series) != n:
                raise ValueError(f'channel "{key}" has {len(series)} samples, expected {n}')
            scale, offset = ch.get("scale", 1.0), ch.get("offset", 0.0)
            values = ([v * scale + offset for v in series]
                      if (scale != 1.0 or offset) else series)
        spec = ch["format"]
        kept.append(ch)
        columns.append([format_value(v, spec) for v in values])
    if not kept:
        raise ValueError("no columns left to write")
    return kept, columns


# --- session inputs --------------------------------------------------------

def read_start_finish(path: Path):
    """start-finish.csv: startLatA,startLonA,startLatB,startLonB,
    finishLatA,finishLonA,finishLatB,finishLonB[,?]

    A ninth field turns up in some sessions and not others; its meaning is
    unidentified (it matches neither gate length nor the distance between the
    gates), so it is carried along unlabelled rather than guessed at.
    """
    if not path.exists():
        return None
    fields = [f.strip() for f in path.read_text().strip().split(",")]
    if len(fields) < 8:
        return None
    v = [float(f) for f in fields[:8]]
    return {
        "start": (v[0], v[1], v[2], v[3]),
        "finish": (v[4], v[5], v[6], v[7]),
        "extra": fields[8] if len(fields) > 8 and fields[8] else None,
    }


def laptiming_line(label: str, gate) -> str:
    lat_a, lon_a, lat_b, lon_b = gate
    return (
        f"{label:<13}"
        f"{vbo_minutes(-lon_a):+.8f} {vbo_minutes(lat_a):+.8f} "
        f"{vbo_minutes(-lon_b):+.8f} {vbo_minutes(lat_b):+.8f} "
        f"¬  {label}"
    )


# --- gate crossings --------------------------------------------------------
#
# Gates are a few metres long and the track is sampled at 25 Hz, so a crossing
# almost never lands on a sample.  Treat lat/lon as planar -- the error over a
# 13 m gate at this latitude is far below GPS noise -- and interpolate the
# fraction of the sample interval at which the path cuts the gate.

def segment_intersection(p1, p2, q1, q2) -> float | None:
    """Fraction along p1 -> p2 at which it crosses q1 -> q2, or None."""
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1, d2 = cross(q1, q2, p1), cross(q1, q2, p2)
    d3, d4 = cross(p1, p2, q1), cross(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return d1 / (d1 - d2)
    return None


# Direction of travel through a gate, measured over this many samples either
# side of the crossing.  One 0.04 s step is too short a baseline to be steady
# against GPS jitter; ~0.2 s is long enough to settle and short enough that
# the car has not begun to turn.
HEADING_WINDOW = 5


def gate_crossings(lat, lon, elapsed, gate) -> list[tuple[float, float]]:
    """(elapsed seconds, heading) for each crossing of the gate.

    The heading is the car's direction of travel through the gate, which is
    what RaceRender's lap-timing setup wants -- not the bearing of the gate
    line itself, and not necessarily perpendicular to it.
    """
    lat_a, lon_a, lat_b, lon_b = gate
    q1, q2 = (lat_a, lon_a), (lat_b, lon_b)
    hits = []
    for i in range(len(lat) - 1):
        f = segment_intersection((lat[i], lon[i]), (lat[i + 1], lon[i + 1]), q1, q2)
        if f is None:
            continue
        a = max(0, i - HEADING_WINDOW)
        b = min(len(lat) - 1, i + HEADING_WINDOW + 1)
        hits.append((elapsed[i] + f * (elapsed[i + 1] - elapsed[i]),
                     bearing(lat[a], lon[a], lat[b], lon[b])))
    return hits


def circular_mean(degrees) -> tuple[float, float]:
    """Mean of a set of bearings, plus the largest deviation from that mean."""
    x = sum(math.cos(math.radians(d)) for d in degrees)
    y = sum(math.sin(math.radians(d)) for d in degrees)
    mean = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    spread = max(abs(((d - mean + 180) % 360) - 180) for d in degrees)
    return mean, spread


def gate_geometry_comments(gates, session_headings, run_headings) -> list[str]:
    """Coordinates and crossing direction for the two gates.

    RaceRender's lap-timing setup takes a position plus the heading the car
    travels when it crosses.  `session_headings` averages that heading over
    every run in the session, which is the figure to type in; this run's own
    value is shown beside it so an outlier is visible.
    """
    lines = ["Gate coordinates (WGS84 decimal degrees, for RaceRender lap timing)"]
    for label in ("start", "finish"):
        lat_a, lon_a, lat_b, lon_b = gates[label]
        mid_lat, mid_lon = (lat_a + lat_b) / 2, (lon_a + lon_b) / 2
        lines += [
            f"  {label.capitalize()} line",
            f"    position  {mid_lat:.7f}, {mid_lon:.7f}   (gate midpoint)",
        ]
        avg = session_headings.get(label)
        if avg:
            mean, spread, n = avg
            lines.append(f"    heading   {mean:.1f} deg   (direction of travel when"
                         f" crossing; mean of {n} run{'s' if n != 1 else ''},"
                         f" max deviation {spread:.1f} deg)")
        mine = run_headings.get(label)
        if mine is not None:
            lines.append(f"    this run crossed at {mine:.1f} deg")
        lines += [
            f"    endpoints {lat_a:.7f}, {lon_a:.7f}  and  {lat_b:.7f}, {lon_b:.7f}",
            f"    line is {haversine(lat_a, lon_a, lat_b, lon_b):.2f} m long,"
            f" bearing A to B {bearing(lat_a, lon_a, lat_b, lon_b):.1f} deg",
        ]
    return lines


def crossing_comments(starts, finishes, t0) -> tuple[list[str], float | None]:
    """Comment lines describing the gate crossings, plus the lap time if any."""
    lines = []
    for label, hits in (("Start", starts), ("Finish", finishes)):
        if not hits:
            lines.append(f"{label} line : not crossed in this recording")
            continue
        for n, (secs, _) in enumerate(hits, 1):
            tag = f"{label} line" if len(hits) == 1 else f"{label} line #{n}"
            wall = t0 + dt.timedelta(seconds=secs)
            lines.append(
                f"{tag} : {secs:.3f} s into the log"
                f"   avisynctime {round(secs * 1000)} ms"
                f"   UTC {wall:%H:%M:%S}.{wall.microsecond // 1000:03d}"
            )

    # With more than one crossing of either gate, pair the first start with the
    # first finish that follows it rather than guessing at the driver's intent.
    lap = None
    if starts and finishes:
        after = [f for f, _ in finishes if f > starts[0][0]]
        if after:
            lap = after[0] - starts[0][0]
            note = "" if len(starts) == len(finishes) == 1 else "  (first start to next finish)"
            lines.append(f"Lap time : {lap:.3f} s{note}")
    if lap is None:
        lines.append("Lap time : not available")
    return lines, lap


# --- per-run conversion ----------------------------------------------------

def build_vbo(run: dict, session_name: str, gates, video_name: str | None,
              time_base: str, run_index: int, session_headings: dict,
              channels) -> tuple[str, dict]:
    # Named explicitly so a channel the app renames is reported as a skipped
    # run rather than a KeyError that takes the rest of the batch with it.
    # Channels beyond these are checked per-column in build_columns().
    absent = [c for c in ("latitude", "longitude", "dateTime") if c not in run]
    if absent:
        raise ValueError(f"missing channel(s): {', '.join(absent)}")

    lat = run["latitude"]
    lon = run["longitude"]
    speed = run["speed"]            # metres/second
    gx = run["gForceX"]             # longitudinal
    gy = run["gForceY"]             # lateral
    fix = run.get("gnssFix") or [1] * len(lat)
    times = [parse_timestamp(s) for s in run["dateTime"]]

    n = len(lat)
    if not (len(lon) == len(speed) == len(gx) == len(gy) == len(times) == n):
        raise ValueError("channel lengths disagree")
    if n < 2:
        raise ValueError("run has fewer than two samples")

    t0 = times[0]
    elapsed = [(t - t0).total_seconds() for t in times]
    span = elapsed[-1]
    log_rate = (n - 1) / span if span > 0 else 0.0

    # Heading is not logged; derive it from the previous fix.  Sample 0 has no
    # predecessor, so it borrows the 0 -> 1 bearing.  avisynctime is
    # milliseconds from the first logged sample: the video is assumed to start
    # with the log, and any real offset is dialled in by hand in RaceRender /
    # Circuit Tools.
    derived = {
        "time": [fmt_time_utc(times[i]) if time_base == "utc"
                 else fmt_time_elapsed(elapsed[i]) for i in range(n)],
        "sats": [12 if fix[i] else 0 for i in range(n)],
        "latitude_min": [vbo_minutes(v) for v in lat],
        "longitude_min": [vbo_minutes(-v) for v in lon],
        "heading": [bearing(lat[max(i - 1, 0)], lon[max(i - 1, 0)],
                            lat[max(i, 1)], lon[max(i, 1)]) for i in range(n)],
        "run_index": [run_index] * n,
        "avisynctime": [round(e * 1000) for e in elapsed],
        "elapsed": elapsed,
    }
    channels, columns = build_columns(run, channels, derived, n)
    rows = [" ".join(col[i] for col in columns) for i in range(n)]

    starts, finishes, lap = [], [], None
    if gates:
        starts = gate_crossings(lat, lon, elapsed, gates["start"])
        finishes = gate_crossings(lat, lon, elapsed, gates["finish"])
        gate_lines, lap = crossing_comments(starts, finishes, t0)
    else:
        # No start-finish.csv: say so, rather than reporting the gates as
        # present but never crossed.
        gate_lines = ["No start-finish.csv for this session:"
                      " gate timing and coordinates unavailable."]
    run_headings = {k: v[0][1] for k, v in (("start", starts), ("finish", finishes)) if v}

    created = dt.datetime.now()
    out = [
        f"File created on {created:%d/%m/%Y} at {created:%H:%M:%S}",
        "",
        "[header]",
        *(ch["name"] for ch in channels),
        "",
        "[comments]",
        f"Log Rate (Hz) : {log_rate:.2f}",
        f"name {session_name}",
        f"Run {run.get('runIdx', '?')} - full recording (pre-start and post-finish included)",
        f"Samples {n}   Duration {span:.2f} s",
        f"Log start (UTC) {t0:%Y-%m-%d %H:%M:%S.%f}",
        "Time column is UTC time of day." if time_base == "utc"
        else "Time column is elapsed seconds since log start, formatted HHMMSS.sss.",
        "avisynctime is milliseconds from the first logged sample; the video is",
        "assumed to begin with the log, so align it in your overlay tool.",
        "",
        *gate_lines,
        "",
        *(gate_geometry_comments(gates, session_headings, run_headings) + [""]
          if gates else []),
    ]
    for key, label in (("driver", "Driver"), ("car", "Car"), ("tires", "Tires")):
        if run.get(key):
            out.append(f"{label} : {run[key]}")

    # The video file resolves as <[AVI] prefix><avifileindex>.<[AVI] extension>,
    # so indexing by run number gives every run of a session a distinct clip
    # name -- <session>_0001.mp4, _0002.mp4 and so on -- letting them all sit in
    # one folder instead of colliding on a single _0001.mp4.
    out += ["", "[AVI]", f"{video_name or session_name}_", "mp4"]

    if gates:
        out += [
            "",
            "[laptiming]",
            laptiming_line("Start", gates["start"]),
            laptiming_line("Finish", gates["finish"]),
        ]

    units = [ch for ch in channels if ch.get("unit")]
    if units:
        out += ["", "[channel units]",
                " ".join(f'{ch["short"]}={ch["unit"]}' for ch in units)]

    out += ["", "[column names]", " ".join(ch["short"] for ch in channels),
            "", "[data]", *rows, ""]

    stats = {"samples": n, "duration": span, "rate": log_rate, "start_utc": t0,
             "starts": starts, "finishes": finishes, "lap": lap}
    return "\n".join(out), stats


# --- driver ----------------------------------------------------------------

def session_crossing_headings(files, gates) -> dict:
    """Average the crossing heading of each gate over every run in the session.

    A single run gives the heading to within GPS noise; averaging across the
    session is what makes it worth typing into RaceRender once and reusing.
    """
    if not gates:
        return {}
    collected = {"start": [], "finish": []}
    for path in files:
        try:
            run = json.loads(path.read_text())
            lat, lon = run["latitude"], run["longitude"]
        except (ValueError, KeyError):
            continue
        stub = [0.0] * len(lat)          # heading needs no timebase
        for label in collected:
            for _, hdg in gate_crossings(lat, lon, stub, gates[label]):
                collected[label].append(hdg)
    out = {}
    for label, hdgs in collected.items():
        if hdgs:
            mean, spread = circular_mean(hdgs)
            out[label] = (mean, spread, len(hdgs))
    return out


def run_files(session: Path):
    return sorted(
        p for p in (session / "runs").glob("run_*.json")
        if not p.stem.endswith("_parsed")
    )


def process_session(session: Path, out_dir: Path | None, time_base: str,
                    dry_run: bool, channels) -> int:
    files = run_files(session)
    if not files:
        print(f"  no run_*.json under {session / 'runs'}", file=sys.stderr)
        return 0

    gates = read_start_finish(session / "start-finish.csv")
    if gates is None:
        print("  start-finish.csv missing or short; omitting [laptiming]",
              file=sys.stderr)

    session_headings = session_crossing_headings(files, gates)

    session_name = session.name
    target = out_dir or (session / "vbo")
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)

    written = 0
    for path in files:
        run = json.loads(path.read_text())
        idx = run.get("runIdx")
        if idx is None:
            m = re.search(r"(\d+)", path.stem)
            idx = int(m.group(1)) if m else written + 1

        try:
            text, stats = build_vbo(run, session_name, gates, session_name,
                                    time_base, idx, session_headings,
                                    channels)
        except ValueError as exc:
            print(f"  {path.name}: skipped ({exc})", file=sys.stderr)
            continue

        dest = target / f"{session_name}-{idx:02d}.vbo"
        lap = (f"lap {stats['lap']:.3f} s" if stats["lap"] is not None
               else "lap not timed")
        summary = (f"{dest.name}: {stats['samples']} samples, "
                   f"{stats['duration']:.2f} s, {stats['rate']:.2f} Hz, {lap}")
        if dry_run:
            print(f"  would write {summary}")
        else:
            dest.write_text(text, encoding="utf-8")
            print(f"  {summary}")
        written += 1
    return written


def find_sessions(root: Path):
    if (root / "runs").is_dir():
        return [root]
    if root.name == "runs" and root.is_dir():
        return [root.parent]
    return sorted(p for p in root.iterdir() if (p / "runs").is_dir())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="*", type=Path, default=None,
                    help="session folder(s), or a folder containing them "
                         "(default: current directory)")
    ap.add_argument("--out-dir", type=Path,
                    help="write all .vbo files here instead of <session>/vbo/")
    ap.add_argument("--channels", type=Path,
                    help=f"JSON channel table defining the output columns "
                         f"(default: {CHANNELS_FILENAME} beside this script, "
                         f"else the built-in table)")
    ap.add_argument("--list-channels", action="store_true",
                    help="print the channel table that would be used, and exit")
    ap.add_argument("--time-base", choices=("utc", "elapsed"), default="utc",
                    help="time column: UTC time of day (default) or elapsed "
                         "seconds since log start, as the app writes it")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written without writing it")
    args = ap.parse_args()

    try:
        channels = load_channels(args.channels)
    except (ValueError, OSError) as exc:
        print(f"channel table: {exc}", file=sys.stderr)
        return 1

    if args.list_channels:
        for ch in channels:
            src = (f'key {ch["key"]}' if "key" in ch else
                   f'derived {ch["derived"]}' if "derived" in ch else
                   f'constant {ch["constant"]}')
            scale = f'  x{ch["scale"]}' if ch.get("scale", 1.0) != 1.0 else ""
            unit = f'  [{ch["unit"]}]' if ch.get("unit") else ""
            opt = "" if ch.get("required", True) else "  (optional)"
            print(f'{ch["short"]:<14}{ch["name"]:<16}{src}{scale}{unit}{opt}')
        return 0

    roots = args.sessions or [Path.cwd()]
    sessions = []
    for root in roots:
        if not root.exists():
            print(f"{root}: no such directory", file=sys.stderr)
            return 1
        sessions.extend(find_sessions(root))

    if not sessions:
        print("no session folders found (need a runs/ subdirectory)", file=sys.stderr)
        return 1

    total = 0
    for session in sessions:
        print(f"{session.name}:")
        total += process_session(session, args.out_dir, args.time_base,
                                 args.dry_run, channels)
    print(f"\n{total} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
