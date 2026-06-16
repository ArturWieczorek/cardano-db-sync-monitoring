"""Shared helpers for db-sync-monitoring scripts.

Imported by both monitor scripts (db-sync-resource-monitor, node-resource-monitor) and both plot
scripts (db-sync-plot, node-plot). Keep it focused on pure, side-effect-light
helpers - no domain-specific data fetching (those belong in script-specific
modules like `_db_sync_queries`).

Naming convention for users:
    from _common import (
        format_size, format_bytes, format_duration, format_duration_compact,
        warn, utc_timestamp, era_for, ERA_BY_PROTOCOL_MAJOR,
        find_process, find_processes, get_memory_details, get_cpu_details,
        has_table, has_column, init_sqlite_schema, report_existing_history,
        short, load_versions_from_sqlite, resolve_versions, insert_gap_breaks,
    )
"""

from __future__ import annotations

import sqlite3
import sys
import warnings
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import psutil
from pandas import DataFrame
from psutil import Process

# --- stderr-aware logging --------------------------------------------------


def warn(msg: str) -> None:
    """Emit a warning/error message on stderr (line-flushed).

    The monitors stream sample lines on stdout. Errors and warnings must not
    interleave there, or they'll pollute log-analyzer consumption. Use this
    helper for anything that isn't a regular sample/info line.
    """
    print(msg, file=sys.stderr, flush=True)


# --- formatters ------------------------------------------------------------


def format_size(mib: float | None) -> str:
    """Format a mebibyte value as 'X.X MiB' or 'X.X GiB'.

    Strict binary units: the input is already in MiB (bytes / 1024^2), so the
    output is labelled MiB / GiB rather than MB / GB. Calling a 1024-based value
    "GB" overstates it by ~7.4% (a GiB is 1.074 GB), which is misleading when
    comparing on-disk / RAM footprints.
    """
    if mib is None:
        return "N/A"
    if mib >= 1024:
        return f"{mib / 1024:.1f} GiB"
    return f"{mib:.1f} MiB"


def format_bytes(n: int | None) -> str:
    """Format a raw byte count as 'X.XX [TiB|GiB|MiB|KiB|B]'.

    Binary units (1024-based) with binary labels, so the suffix matches the
    divisor exactly - no GiB-labelled-as-GB ambiguity.
    """
    if n is None:
        return "N/A"
    for unit, div in (("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def format_duration(seconds: float | None) -> str:
    """Long-form duration: 'Xh YYm ZZs (S sec)'. Used by reports."""
    if seconds is None:
        return "N/A"
    seconds = float(seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}h {m:02d}m {s:02d}s ({seconds:.0f} sec)"


def format_duration_compact(seconds: float | None) -> str:
    """Compact duration for status-line use: 's' / 'm' / 'h' / 'd' bucket."""
    if seconds is None:
        return "N/A"
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# --- time helpers ----------------------------------------------------------


def utc_timestamp(dt: datetime) -> float:
    """POSIX seconds.

    psycopg2 returns NAIVE datetimes for postgres `timestamp` columns (no time
    zone). cardano-db-sync stores UTC values in those columns. Python's
    `.timestamp()` would interpret a naive datetime as LOCAL time, producing
    a constant offset equal to the host's UTC offset (we hit this exact bug
    earlier - `TipLag 2h` that was just the user's timezone). We pin tz=UTC
    on naive datetimes before converting; aware datetimes pass through.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).timestamp()
    return dt.timestamp()


# --- Cardano era mapping ---------------------------------------------------

ERA_BY_PROTOCOL_MAJOR: dict[int, str] = {
    0: "Byron",
    1: "Byron",
    2: "Shelley",
    3: "Allegra",
    4: "Mary",
    5: "Alonzo",
    6: "Alonzo",
    7: "Babbage",
    8: "Babbage",
    9: "Conway",
    10: "Conway",
}


def era_for(proto_major: int | None) -> str:
    """Map Cardano protocol major version to era name.

    Unknown / future versions land in a descriptive bucket rather than the
    bare 'Unknown' label, so plots and reports show the proto number directly.
    """
    if proto_major is None:
        return "Unknown"
    return ERA_BY_PROTOCOL_MAJOR.get(proto_major, f"Unknown (proto {proto_major})")


# Canonical chronological order of Cardano eras. Used to sort era tables in
# reports so 'Byron' always comes first and 'Conway' last regardless of the
# join order produced by SQL.
ERA_ORDER: list[str] = list(dict.fromkeys(ERA_BY_PROTOCOL_MAJOR.values()))


def era_sort_key(era_name: str) -> tuple[int, int | str]:
    """Stable sort key for era names.

    Known eras sort by chronological position; unknowns (`'Unknown'`,
    `'Unknown (proto 11)'`, etc.) sort after all known eras, in alphabetical
    order among themselves.
    """
    if era_name in ERA_ORDER:
        return (0, ERA_ORDER.index(era_name))
    return (1, era_name)


def step(i: int, n: int, msg: str) -> None:
    """Stage-by-stage progress line - useful for long-running reports so a
    hang is locatable."""
    print(f"[{i}/{n}] {msg}...", flush=True)


def compute_epoch_durations(df: pd.DataFrame) -> pd.DataFrame:
    """Per-(version, epoch_no) wall-clock duration computed from sample timestamps.

    `df` must have at least: ts (datetime), version, epoch_no, era. For each
    (version, epoch_no) group we return:
        - ts_min   first sample seen in that epoch
        - ts_max   last sample seen in that epoch
        - era      era label of the first sample (constant within an epoch on
                   Cardano - hard forks happen at epoch boundaries)
        - duration_sec   max(ts) minus min(ts) in seconds

    Used by the node-plot ingest mode to produce per-epoch and per-era charts.
    Note that for the very first / last epochs of a sync, the duration is
    *partial* - only as long as the monitor was observing. Full-epoch durations
    only become available for epochs that started and ended while the monitor
    was running.
    """
    if df.empty:
        return pd.DataFrame(columns=["version", "epoch_no", "era", "duration_sec"])
    g = df.groupby(["version", "epoch_no"], as_index=False).agg(
        ts_min=("ts", "min"),
        ts_max=("ts", "max"),
        era=("era", "first"),
    )
    g["duration_sec"] = (g["ts_max"] - g["ts_min"]).dt.total_seconds()
    return g[["version", "epoch_no", "era", "duration_sec"]]


# --- psutil sampling -------------------------------------------------------


def find_processes(predicate: Callable[[Process], bool]) -> list[Process]:
    """All running Processes for which predicate(p) is True.

    Wraps psutil.process_iter and swallows the standard transient-process
    exceptions (NoSuchProcess / AccessDenied / ZombieProcess) so callers don't
    have to. `predicate` receives the live Process; access proc.info.get(...)
    or proc.cmdline() / proc.name() from inside.
    """
    matches: list[Process] = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if predicate(proc):
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):  # noqa: PERF203
            # Per-process try/except is structurally required: psutil exceptions
            # are raised lazily by predicate() per-process, not by process_iter
            # itself. Catching at the loop level would let one disappearing
            # process abort the whole scan.
            continue
    return matches


def find_process(predicate: Callable[[Process], bool]) -> Process | None:
    """First matching Process, or None."""
    ms = find_processes(predicate)
    return ms[0] if ms else None


def get_memory_details(process: Process) -> dict[str, float] | None:
    """Memory snapshot in MiB. Returns None on transient psutil errors."""
    try:
        mi = process.memory_info()
        mfi = process.memory_full_info()
        return {
            "rss": mi.rss / 1024**2,
            "vms": mi.vms / 1024**2,
            "uss": getattr(mfi, "uss", 0) / 1024**2,
            "pss": getattr(mfi, "pss", 0) / 1024**2,
            "swap": getattr(mfi, "swap", 0) / 1024**2,
            "shared": getattr(mi, "shared", 0) / 1024**2,
        }
    except Exception:
        return None


def get_cpu_details(process: Process) -> dict[str, Any] | None:
    """CPU snapshot. Caller must have called process.cpu_percent(interval=None)
    once before sampling so the first reading isn't ~0%."""
    try:
        times = process.cpu_times()
        percent = process.cpu_percent(interval=None)
        with process.oneshot():
            ctx = process.num_ctx_switches()
        return {
            "cpu_percent": percent,
            "user_time": times.user,
            "system_time": times.system,
            "children_user": getattr(times, "children_user", 0.0),
            "children_system": getattr(times, "children_system", 0.0),
            "iowait": getattr(times, "iowait", 0.0),
            "ctx_switches": ctx.voluntary + ctx.involuntary,
            "interrupts": None,
        }
    except Exception:
        return None


# --- SQLite helpers --------------------------------------------------------

# Writer connections to a per-env SQLite DB need a generous busy timeout.
# Several collectors write to the SAME file (node-resource-monitor + node-rts-monitor +
# node-db-size-monitor for `node`; db-sync-resource-monitor + ledger-size for `db-sync`),
# and WAL serializes writers - only one connection may write at a time. Python's
# sqlite3 default busy timeout is 5s, so a brief two-writer collision, or a
# maintenance pass (rename-version, backup-stats) holding the write lock on a
# multi-hundred-MB DB, raises "database is locked". The collectors don't catch
# it, so a single lost race kills a run meant to last for days. 30s comfortably
# outlasts normal contention; a stall longer than that is left for the caller to
# handle (warn + skip the sample) rather than crash.
WRITE_TIMEOUT_SEC = 30.0


def connect_writer(db_file: str, timeout: float = WRITE_TIMEOUT_SEC) -> sqlite3.Connection:
    """sqlite3 connection for code that INSERTs/CREATEs on a shared per-env DB.

    The `timeout` arg installs SQLite's busy handler, so a writer blocked behind
    another writer waits up to `timeout` seconds for the lock instead of failing
    immediately. Readers (plot/report/stats) can keep using plain
    sqlite3.connect - under WAL, reads never block on a writer.
    """
    return sqlite3.connect(db_file, timeout=timeout)


def has_table(sqlite_file: str, table: str) -> bool:
    with sqlite3.connect(sqlite_file) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
            (table,),
        )
        return cur.fetchone() is not None


def has_column(sqlite_file: str, table: str, col: str) -> bool:
    with sqlite3.connect(sqlite_file) as conn:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return col in cols


def init_sqlite_schema(db_file: str, version_table: str) -> None:
    """Idempotent schema setup for monitor SQLite DBs.

    Creates the three tables both monitors share:
      - memory_metrics (ts, slot_no, rss, vms, uss, pss, swap, shared, version)
      - cpu_metrics (ts, slot_no, cpu_percent, user_time, system_time, ...)
      - <version_table> (timestamp, version)

    Enables SQLite WAL mode so the plot/report scripts can read concurrently
    while the monitor writes - under the default rollback journal, long reads
    can block writes (and vice versa) on a busy DB.

    Migrates older DBs that pre-date the `ts` column on memory_metrics/cpu_metrics.

    Callers that need additional tables (e.g. db-sync-resource-monitor's `ingest_metrics`
    and `table_rowcounts`) should create them after calling this.
    """
    with connect_writer(db_file) as conn:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute(
            """CREATE TABLE IF NOT EXISTS memory_metrics
               (ts TEXT, slot_no INTEGER, rss REAL, vms REAL, uss REAL,
                pss REAL, swap REAL, shared REAL, version TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS cpu_metrics
               (ts TEXT, slot_no INTEGER, cpu_percent REAL, user_time REAL,
                system_time REAL, children_user REAL, children_system REAL,
                iowait REAL, ctx_switches INTEGER, interrupts INTEGER,
                version TEXT)"""
        )
        c.execute(f"CREATE TABLE IF NOT EXISTS {version_table} (timestamp TEXT, version TEXT)")
        for tbl in ("memory_metrics", "cpu_metrics"):
            cols = {row[1] for row in c.execute(f"PRAGMA table_info({tbl})")}
            if "ts" not in cols:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN ts TEXT")
        conn.commit()


def report_existing_history(db_file: str, version_table: str, run_label: str) -> None:
    """Tell the operator at startup whether `run_label` already has samples.

    Append-only collectors silently extend the same series across restarts
    (Prometheus/InfluxDB model). Without this notice, multi-session runs
    produce surprising "cliff" artifacts in plots; see insert_gap_breaks()
    for the visualization-side fix.
    """
    try:
        with sqlite3.connect(db_file) as conn:
            row = conn.execute(
                f"SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM {version_table} WHERE version = ?",
                (run_label,),
            ).fetchone()
    except Exception as e:
        warn(f"Startup history check failed: {e}")
        return
    count = (row or [0])[0] or 0
    if count == 0:
        print(f"This version label has no existing samples in {db_file}.")
        return
    first_ts, last_ts = row[1], row[2]
    print(
        f"Note: this version label already has {count:,} samples in {db_file} "
        f"(first {first_ts}, last {last_ts}). New samples will be appended."
    )


# --- plot helpers ----------------------------------------------------------


def short(v: str) -> str:
    """Extract a short token from a version label.

    Labels look like 'cardano-db-sync 13.6.0.5 preprod' or
    'cardano-node 11.0.1 preprod'. Returns the middle whitespace-separated
    field ('13.6.0.5' / '11.0.1') for use in filenames and plot legends.
    Falls back to the label with spaces replaced by underscores if there's
    no second field (unusual).
    """
    parts = v.split()
    return parts[1] if len(parts) >= 2 else v.replace(" ", "_")


# Single source of truth for every table that carries a `version` column, by
# role. Consumed by rename-version.py (which must rewrite all of them) and by
# the plot pickers (which enumerate versions across all of them). Tables are
# CREATEd across several files (_common.py, node-resource-monitor.py, db-sync-resource-monitor.py,
# _disk_size.py, node-rts-monitor.py); keeping the list here - and enforcing it
# with tests/test_version_tables_registry.py - prevents the drift that twice
# left a new collector's table (disk_metrics, rts_metrics) unregistered, with
# silently-empty plots and stranded data as the result.
VERSION_KEYED_TABLES: dict[str, tuple[str, ...]] = {
    "node": (
        "memory_metrics",
        "cpu_metrics",
        "node_version",
        "node_ingest_metrics",
        "disk_metrics",
        "rts_metrics",
    ),
    "db-sync": (
        "memory_metrics",
        "cpu_metrics",
        "db_sync_version",
        "ingest_metrics",
        "table_rowcounts",
    ),
}


def attach_slot_by_ts(df: DataFrame, sqlite_file: str, versions: list[str]) -> DataFrame:
    """Add a `slot_no` column to a timestamped df that has none of its own (e.g.
    `disk_metrics`), by nearest-timestamp lookup against the concurrently-written
    `memory_metrics` (which carries both `ts` and `slot_no`), per version.

    The disk collector runs alongside the resource monitor on the same DB but
    doesn't record a slot; its samples fall between resource samples in
    wall-clock time, so `merge_asof(..., direction="nearest")` assigns each disk
    sample the closest resource slot. A version with no resource rows gets
    `slot_no = NaN`, so the caller can fall back to the time axis.
    """
    if df.empty or "ts" not in df.columns or not has_table(sqlite_file, "memory_metrics"):
        return df.assign(slot_no=pd.NA)  # type: ignore[arg-type]
    placeholders = ",".join("?" for _ in versions)
    with sqlite3.connect(sqlite_file) as conn:
        ref = pd.read_sql_query(
            f"SELECT version, ts, slot_no FROM memory_metrics "
            f"WHERE version IN ({placeholders}) AND ts IS NOT NULL AND slot_no IS NOT NULL",
            conn,
            params=versions,  # type: ignore[arg-type]
        )
    if ref.empty:
        return df.assign(slot_no=pd.NA)  # type: ignore[arg-type]
    ref["ts"] = pd.to_datetime(ref["ts"], errors="coerce")
    left = df.copy()
    left["ts"] = pd.to_datetime(left["ts"], errors="coerce")
    # merge_asof needs both inputs globally sorted on the `on` key.
    left = left.sort_values("ts")
    ref = ref.dropna(subset=["ts"]).sort_values("ts")
    return pd.merge_asof(left, ref[["ts", "version", "slot_no"]], on="ts", by="version", direction="nearest")


def load_versions_from_sqlite(sqlite_file: str, version_table: str) -> list[str]:
    """Distinct versions present in `version_table`, latest first."""
    with sqlite3.connect(sqlite_file) as conn:
        df = pd.read_sql_query(
            f"SELECT DISTINCT version FROM {version_table} ORDER BY timestamp DESC",
            conn,
        )
    return [str(v) for v in df["version"].tolist()]


def load_all_versions(sqlite_file: str, tables: list[str]) -> list[str]:
    """Union of distinct `version` labels across every existing table in
    `tables`, most-recent-first.

    Unlike `load_versions_from_sqlite` (which reads a single version table),
    this surfaces labels that only the optional collectors wrote - the disk
    (`disk_metrics`) and RTS (`rts_metrics`) monitors never write the
    `node_version` table, so a run collected *only* by one of them - or one
    saved under a mistyped `--node-ver` - would otherwise be invisible and
    unselectable in the plot picker. Each table is ordered by its own time
    column (`ts` on metric tables, `timestamp` on the *_version tables);
    missing tables are skipped.
    """
    last_seen: dict[str, str] = {}
    with sqlite3.connect(sqlite_file) as conn:
        for table in tables:
            if not has_table(sqlite_file, table):
                continue
            time_col = "ts" if has_column(sqlite_file, table, "ts") else "timestamp"
            rows = conn.execute(f"SELECT version, MAX({time_col}) FROM {table} GROUP BY version").fetchall()
            for version, last_ts in rows:
                if version is None:
                    continue
                key = str(version)
                # Keep the most recent sighting of each label across all tables.
                if key not in last_seen or (last_ts is not None and str(last_ts) > last_seen[key]):
                    last_seen[key] = str(last_ts) if last_ts is not None else ""
    return [v for v, _ in sorted(last_seen.items(), key=lambda kv: kv[1], reverse=True)]


def subplot_dims(rows: int, panel_px: int = 300, gap_px: int = 40, margin_px: int = 160) -> tuple[int, float]:
    """Figure height (px) and `vertical_spacing` (fraction) for an `rows`-row
    stacked subplot, sizing each panel and the inter-panel gap in fixed pixels.

    Plotly's `vertical_spacing` is a fraction of the figure height applied
    between every pair of rows, so a fixed fraction makes the gaps balloon to
    most of the height once there are many rows (tiny panels, huge whitespace).
    Deriving the fraction from a fixed pixel gap keeps every panel its intended
    height regardless of row count, and stays under Plotly's 1/(rows-1) cap.
    Returns ``(height_px, vertical_spacing)``; spacing is 0.0 for a single row.
    """
    total = rows * panel_px + max(0, rows - 1) * gap_px + margin_px
    return total, (gap_px / total if rows > 1 else 0.0)


def resolve_versions(requested: list[str], available: list[str]) -> list[str]:
    """Map user input (`--versions`) to full version labels.

    Each item in `requested` may be a full label
    ('cardano-db-sync 13.6.0.5 preprod') or a short token ('13.6.0.5'). On
    ambiguous or missing entries the function raises SystemExit with a clear
    diagnostic, so the caller doesn't silently drop versions.
    """
    resolved: list[str] = []
    for item in requested:
        if item in available:
            resolved.append(item)
            continue
        matches = [v for v in available if len(v.split()) >= 2 and v.split()[1] == item]
        if len(matches) == 1:
            resolved.append(matches[0])
        elif not matches:
            raise SystemExit(f"No version matches '{item}'. Available: {available}")
        else:
            raise SystemExit(f"Ambiguous '{item}'; matches: {matches}")
    return resolved


# Fallback gap threshold (seconds) when a series' cadence can't be measured -
# ~5x the standard 10-second sample interval.
_DEFAULT_GAP_SEC = 50.0


def insert_gap_breaks(df: DataFrame, group_keys: list[str], gap_sec: float | None = None) -> DataFrame:
    """Insert NaN marker rows where consecutive samples within each group have
    a wall-clock `ts` gap larger than the gap threshold.

    The monitor appends samples to the same series across restarts (correct
    industry-standard model). Without this, plotly connects the last sample
    of session 1 directly to the first of session 2, producing misleading
    near-vertical "cliffs" across periods when the monitor wasn't running.
    Inserting a NaN row at the gap midpoint forces plotly to break the line
    there (Scatter has connectgaps=False by default).

    The threshold adapts to each series' own cadence: when `gap_sec` is None
    (the default), it is computed per group as 5x the median inter-sample
    interval, falling back to ``_DEFAULT_GAP_SEC`` when that can't be measured
    (fewer than two samples, or a zero/NaN median). This matters because
    collectors sample at different rates - node-resource-monitor.py every ~10s, but
    node-db-size-monitor.py every 60s; a fixed 50s threshold would treat
    *every* normal 60s disk sample as a gap and break the line at every point,
    rendering an empty plot. Pass an explicit `gap_sec` to override.

    Large enough to ignore the occasional slow sample; small enough to catch
    real gaps (restarts, ssh disconnects, etc.).
    """
    if df.empty or "ts" not in df.columns:
        return df
    pieces: list[DataFrame] = []
    for keys, sub in df.groupby(group_keys, sort=False, dropna=False):
        sub = sub.sort_values("ts").reset_index(drop=True)
        if len(sub) < 2 or sub["ts"].isna().all():
            pieces.append(sub)
            continue
        diffs = sub["ts"].diff().dt.total_seconds()  # type: ignore[arg-type]
        if gap_sec is None:
            median_diff = diffs.median()
            threshold = 5 * median_diff if median_diff and median_diff > 0 else _DEFAULT_GAP_SEC
        else:
            threshold = gap_sec
        gap_positions = sub.index[diffs > threshold].tolist()
        if not gap_positions:
            pieces.append(sub)
            continue
        breaks_rows: list[dict[str, Any]] = []
        keys_tuple = keys if isinstance(keys, tuple) else (keys,)
        for i in gap_positions:
            mid_ts = sub["ts"].iloc[i - 1] + (sub["ts"].iloc[i] - sub["ts"].iloc[i - 1]) / 2
            blank: dict[str, Any] = {col: None for col in sub.columns}
            blank["ts"] = mid_ts
            blank.update(dict(zip(group_keys, keys_tuple)))
            breaks_rows.append(blank)
        # The all-None columns trip a pandas FutureWarning about future
        # dtype-inference behavior; the warning is benign for plotly's
        # gap-detection (NaN in y is what we want) so we silence it locally.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=FutureWarning,
                message="The behavior of DataFrame concatenation",
            )
            pieces.append(
                pd.concat([sub, pd.DataFrame(breaks_rows)], ignore_index=True).sort_values("ts").reset_index(drop=True)
            )
    return pd.concat(pieces, ignore_index=True)
