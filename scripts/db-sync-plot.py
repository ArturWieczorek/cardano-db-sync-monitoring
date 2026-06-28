#!/usr/bin/env python3
"""Generate comparison graphs from a cardano-db-sync SQLite stats DB.

Read-only - never modifies the stats DB. Picks one or more `--versions` to plot
and produces HTML in `plots/cardano-db-sync/<env>/`. See README.md for the full
list of metric sets and x-axis options.
"""

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import plotly.graph_objs as go
from _common import (
    VERSION_KEYED_TABLES,
    attach_slot_by_ts,
    has_column,
    has_table,
    insert_gap_breaks,
    load_all_versions,
    resolve_versions,
    short,
    subplot_dims,
)
from _rollback import RollbackSample, derive_events
from pandas import DataFrame
from plotly.graph_objs import Figure
from plotly.subplots import make_subplots

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "cardano-db-sync"
DEFAULT_PLOTS_DIR = PROJECT_ROOT / "plots" / "cardano-db-sync"


@dataclass
class Args:
    env: str
    sqlite_db: str
    outdir: str
    versions: list[str] | None
    list_only: bool
    x_axis: str
    metrics: str


def out_path(outdir: str, env: str, versions: list[str], kind: str, x_axis: str) -> str:
    """Compose canonical output HTML path. `kind` is the plot type tag.

    Filename format: ``<env>_<versions>_<kind>_by_<axis>.html`` - every plot
    self-describes which env, which run(s), which metric set, and which
    x-axis it was rendered with, so a file viewed in isolation (downloaded,
    screenshotted, opened from a directory listing) still tells the reader
    everything they need to identify it. The env is also the parent
    directory, but the dir context disappears the moment the file is moved
    or shared.

    Examples (env=preprod):
      preprod_13.7.1.0_cpu_ram_by_slot.html
      preprod_13.7.1.0_cpu_ram_by_time.html
      preprod_13.7.1.0_ingest_by_time.html
      preprod_13.7.1.0_tables_by_time.html

    Comparison (two versions; env appears once at the front since the
    SQLite DB is per-env):
      preprod_13.6.0.5_vs_13.7.1.0_cpu_ram_by_time.html
    """
    shorts = [short(v) for v in versions]
    p = Path(outdir) / env
    p.mkdir(parents=True, exist_ok=True)
    return str(p / f"{env}_{'_vs_'.join(shorts)}_{kind}_by_{x_axis}.html")


# --- Loaders -----------------------------------------------------------------


def load_cpu_ram(sqlite_file: str, versions: list[str], x_axis: str) -> tuple[DataFrame, DataFrame]:
    """Load memory and CPU metrics for selected versions.

    Handles two legacy DB shapes:
      - No `ts` column at all (DB last written by the pre-refactor monitor).
        We SELECT only the columns that exist; time-axis isn't possible.
      - `ts` column exists but holds NULL for older rows (post-migration).
        For x_axis=='time' we filter those out.
    """
    have_ts = has_column(sqlite_file, "memory_metrics", "ts")
    if x_axis == "time" and not have_ts:
        raise SystemExit(
            "memory_metrics has no ts column in this SQLite DB (last written by a pre-refactor "
            "monitor). Either re-run the updated db-sync-resource-monitor.py briefly to migrate the schema, "
            "or use --x-axis slot."
        )

    placeholders = ",".join("?" for _ in versions)
    ts_col = "ts" if have_ts else "NULL AS ts"
    ts_filter = " AND ts IS NOT NULL" if (x_axis == "time" and have_ts) else ""
    qm = f"SELECT slot_no, {ts_col}, rss, version FROM memory_metrics WHERE version IN ({placeholders}){ts_filter}"
    qc = f"SELECT slot_no, {ts_col}, cpu_percent, version FROM cpu_metrics WHERE version IN ({placeholders}){ts_filter}"
    with sqlite3.connect(sqlite_file) as conn:
        mem_df = pd.read_sql_query(qm, conn, params=versions)  # type: ignore[arg-type]
        cpu_df = pd.read_sql_query(qc, conn, params=versions)  # type: ignore[arg-type]
    # Always parse ts; gap detection needs wall-clock regardless of x-axis choice.
    mem_df["ts"] = pd.to_datetime(mem_df["ts"], errors="coerce")
    cpu_df["ts"] = pd.to_datetime(cpu_df["ts"], errors="coerce")
    mem_df = insert_gap_breaks(mem_df, ["version"])
    cpu_df = insert_gap_breaks(cpu_df, ["version"])
    return mem_df, cpu_df


def load_ingest(sqlite_file: str, versions: list[str], x_axis: str) -> DataFrame:
    placeholders = ",".join("?" for _ in versions)
    ts_filter = " AND ts IS NOT NULL" if x_axis == "time" else ""
    sql = (
        "SELECT ts, slot_no, version, tip_lag_sec, db_size_bytes, "
        "max_block_no, max_tx_id, utxo_count "
        f"FROM ingest_metrics WHERE version IN ({placeholders}){ts_filter}"
    )
    with sqlite3.connect(sqlite_file) as conn:
        df = pd.read_sql_query(sql, conn, params=versions)  # type: ignore[arg-type]
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    # Compute per-version rates (blocks/sec, tx/sec) against wall-clock ts.
    df = df.sort_values(["version", "ts"])
    dts = df.groupby("version")["ts"].diff().dt.total_seconds()
    df["block_rate"] = df.groupby("version")["max_block_no"].diff() / dts
    df["tx_rate"] = df.groupby("version")["max_tx_id"].diff() / dts
    df["db_size_mib"] = df["db_size_bytes"] / (1024 * 1024)
    df = insert_gap_breaks(df, ["version"])
    return df


def load_rowcounts(sqlite_file: str, versions: list[str], x_axis: str) -> DataFrame:
    placeholders = ",".join("?" for _ in versions)
    ts_filter = " AND ts IS NOT NULL" if x_axis == "time" else ""
    sql = (
        "SELECT ts, slot_no, version, table_name, row_count "
        f"FROM table_rowcounts WHERE version IN ({placeholders}){ts_filter}"
    )
    with sqlite3.connect(sqlite_file) as conn:
        df = pd.read_sql_query(sql, conn, params=versions)  # type: ignore[arg-type]
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df = insert_gap_breaks(df, ["version", "table_name"])
    return df


def load_disk(sqlite_file: str, versions: list[str], x_axis: str = "time") -> DataFrame:
    """Load `disk_metrics` for the selected versions.

    Written by the separate db-sync-ledger-size-monitor.py collector, which
    records no slot (`slot_no` is NULL). For `x_axis="slot"` we derive one per
    sample by nearest-timestamp lookup against the concurrently-collected
    `memory_metrics` (see `attach_slot_by_ts`) so disk curves from different runs
    can be aligned by chain position - on the time axis they'd sit in disjoint
    wall-clock windows and never overlap. Byte counts are converted to GiB
    (strict binary unit - not GB, which would overstate by ~7%).
    """
    placeholders = ",".join("?" for _ in versions)
    sql = (
        "SELECT ts, total_bytes, lsm_bytes, version "
        f"FROM disk_metrics WHERE version IN ({placeholders}) AND ts IS NOT NULL"
    )
    with sqlite3.connect(sqlite_file) as conn:
        df = pd.read_sql_query(sql, conn, params=versions)  # type: ignore[arg-type]
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["total_gb"] = df["total_bytes"] / 1024**3
    df["lsm_gb"] = df["lsm_bytes"] / 1024**3
    if x_axis == "slot":
        df = attach_slot_by_ts(df, sqlite_file, versions)
    df = insert_gap_breaks(df, ["version"])
    return df


def load_rollback_samples(sqlite_file: str, versions: list[str], x_axis: str) -> DataFrame:
    """Load the raw `rollback_samples` tip/queue series for the selected versions.

    Adds a `block_gap` column (node tip minus db tip) - the catch-up distance
    that spikes on a rollback and decays back to ~0 as db-sync recovers.
    """
    placeholders = ",".join("?" for _ in versions)
    ts_filter = " AND ts IS NOT NULL" if x_axis == "time" else ""
    sql = (
        "SELECT ts, slot_no, version, db_block_height, db_slot_height, "
        "node_block_height, queue_length "
        f"FROM rollback_samples WHERE version IN ({placeholders}){ts_filter}"
    )
    with sqlite3.connect(sqlite_file) as conn:
        df = pd.read_sql_query(sql, conn, params=versions)  # type: ignore[arg-type]
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["block_gap"] = df["node_block_height"] - df["db_block_height"]
    df = insert_gap_breaks(df, ["version"])
    return df


def load_rollback_events(sqlite_file: str, versions: list[str]) -> DataFrame:
    """Load log-sourced rollback events (deletion phase) for the selected versions.
    Empty frame (with the expected columns) when the table is absent or empty."""
    cols = [
        "version",
        "start_ts",
        "from_slot",
        "depth_blocks",
        "delete_duration_sec",
        "recovery_duration_sec",
        "source",
    ]
    if not has_table(sqlite_file, "rollback_events"):
        return pd.DataFrame(columns=cols)
    placeholders = ",".join("?" for _ in versions)
    sql = (
        "SELECT version, event_start_ts AS start_ts, from_slot, depth_blocks, "
        "delete_duration_sec, recovery_duration_sec, source "
        f"FROM rollback_events WHERE version IN ({placeholders})"
    )
    with sqlite3.connect(sqlite_file) as conn:
        df = pd.read_sql_query(sql, conn, params=versions)  # type: ignore[arg-type]
    df["start_ts"] = pd.to_datetime(df["start_ts"], errors="coerce")
    return df


def derive_events_from_samples(samples_df: DataFrame) -> DataFrame:
    """Derive rollback events (tip went backward) from the raw sample series, per
    version, using the same logic the analysis layer shares with the monitor.

    This is the Prometheus-only path: when no db-sync log was tailed, the tip
    regressions in `rollback_samples` are still enough to recover depth and
    recovery time. Returns columns aligned with `load_rollback_events`
    (source='metrics'); recovery_duration_sec is populated here, delete phase is not.
    """
    cols = [
        "version",
        "start_ts",
        "from_slot",
        "depth_blocks",
        "delete_duration_sec",
        "recovery_duration_sec",
        "source",
    ]
    if samples_df.empty:
        return pd.DataFrame(columns=cols)
    out: list[dict[str, object]] = []
    for version, sub in samples_df.groupby("version", sort=False):
        # Drop the synthetic NaN-height rows insert_gap_breaks adds for plotting,
        # so event derivation works on the real series only (not load-bearing on a
        # plotting concern). Real scrapes always carry a height.
        sub = sub.dropna(subset=["ts", "db_block_height"]).sort_values("ts")
        # .tolist() yields plain Python scalars (typed Any), avoiding the noisy
        # numpy/pandas union that mypy infers from itertuples attribute access.
        ts_vals = sub["ts"].tolist()
        blk = sub["db_block_height"].tolist()
        slot = sub["db_slot_height"].tolist()
        node = sub["node_block_height"].tolist()
        queue = sub["queue_length"].tolist()
        samples = [
            RollbackSample(
                ts=pd.Timestamp(ts_vals[i]).timestamp(),
                db_block_height=None if pd.isna(blk[i]) else int(blk[i]),
                db_slot_height=None if pd.isna(slot[i]) else int(slot[i]),
                node_block_height=None if pd.isna(node[i]) else int(node[i]),
                queue_length=None if pd.isna(queue[i]) else float(queue[i]),
            )
            for i in range(len(ts_vals))
        ]
        out.extend(
            {
                "version": version,
                "start_ts": pd.to_datetime(ev.start_ts, unit="s", utc=True),
                "from_slot": ev.from_slot,
                "depth_blocks": ev.depth_blocks,
                "delete_duration_sec": None,
                "recovery_duration_sec": ev.recovery_duration_sec,
                "source": "metrics",
            }
            for ev in derive_events(samples)
        )
    return pd.DataFrame(out, columns=cols)


# --- Plotters ----------------------------------------------------------------


def build_cpu_ram(mem_df: DataFrame, cpu_df: DataFrame, versions: list[str], env: str, x_axis: str) -> Figure:
    """Build the memory/CPU comparison figure (no I/O). The report tool reuses
    this to render PNG/embed HTML; `plot_cpu_ram` is the thin CLI writer."""
    x_col = "ts" if x_axis == "time" else "slot_no"
    x_label = "Time" if x_axis == "time" else "Slot Number"

    # Only two panels here, so unlike the dense plots (ingest/disk/tables) this
    # one is left at Plotly's responsive auto-height - it fills the browser
    # viewport, which reads better than a pinned height - with the wider 0.1 gap
    # so the row-2 subplot title clears the row-1 x-axis title.
    fig: Figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.1,
        subplot_titles=[f"Memory (RSS) by {x_label}", f"CPU % by {x_label}"],
        row_heights=[0.5, 0.5],
    )
    for v in versions:
        d = mem_df[mem_df["version"] == v].sort_values(x_col)
        fig.add_trace(go.Scatter(x=d[x_col], y=d["rss"], mode="lines", name=f"Mem - {v}"), row=1, col=1)
    for v in versions:
        d = cpu_df[cpu_df["version"] == v].sort_values(x_col)
        fig.add_trace(go.Scatter(x=d[x_col], y=d["cpu_percent"], mode="lines", name=f"CPU - {v}"), row=2, col=1)

    fig.update_layout(
        title=dict(text=f"{env} cardano-db-sync - Memory & CPU Comparison", x=0.5, xanchor="center"),
        xaxis_title=x_label,
        yaxis_title="RSS (MiB)",
        xaxis2_title=x_label,
        yaxis2_title="CPU (%)",
        legend_title="Version",
    )
    return fig


def plot_cpu_ram(mem_df: DataFrame, cpu_df: DataFrame, versions: list[str], outdir: str, env: str, x_axis: str) -> None:
    fig = build_cpu_ram(mem_df, cpu_df, versions, env, x_axis)
    path = out_path(outdir, env, versions, "cpu_ram", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


def build_ingest(df: DataFrame, versions: list[str], env: str, x_axis: str) -> Figure:
    """Build the ingest-metrics figure (no I/O); `plot_ingest` is the CLI writer."""
    x_col = "ts" if x_axis == "time" else "slot_no"
    x_label = "Time" if x_axis == "time" else "Slot Number"
    has_utxo = df["utxo_count"].notna().any()
    rows = 5 if has_utxo else 4
    titles = [
        f"Tip Lag (sec) by {x_label}",
        f"DB Size (MiB) by {x_label}",
        f"Block Insert Rate (blocks/sec) by {x_label}",
        f"Tx Insert Rate (tx/sec) by {x_label}",
    ]
    if has_utxo:
        titles.append(f"UTXO Set Size by {x_label}")

    total_px, vspace = subplot_dims(rows)
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=vspace, subplot_titles=titles)

    def add(col: str, row: int, prefix: str) -> None:
        for v in versions:
            d = df[df["version"] == v].sort_values(x_col).dropna(subset=[col, x_col])
            fig.add_trace(go.Scatter(x=d[x_col], y=d[col], mode="lines", name=f"{prefix} - {v}"), row=row, col=1)

    add("tip_lag_sec", 1, "TipLag")
    add("db_size_mib", 2, "DB")
    add("block_rate", 3, "Blocks/s")
    add("tx_rate", 4, "Tx/s")
    if has_utxo:
        add("utxo_count", 5, "UTXO")

    fig.update_layout(
        title=dict(text=f"{env} cardano-db-sync - Ingest Metrics", x=0.5, xanchor="center"),
        legend_title="Version",
        height=total_px,
    )
    fig.update_xaxes(title_text=x_label, row=rows, col=1)
    fig.update_yaxes(title_text="seconds", row=1, col=1)
    fig.update_yaxes(title_text="MiB", row=2, col=1)
    fig.update_yaxes(title_text="blocks/sec", row=3, col=1)
    fig.update_yaxes(title_text="tx/sec", row=4, col=1)
    if has_utxo:
        fig.update_yaxes(title_text="rows", row=5, col=1)
    return fig


def plot_ingest(df: DataFrame, versions: list[str], outdir: str, env: str, x_axis: str) -> None:
    fig = build_ingest(df, versions, env, x_axis)
    path = out_path(outdir, env, versions, "ingest", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


def build_rowcounts(df: DataFrame, versions: list[str], env: str, x_axis: str) -> Figure:
    """Build the table-row-counts figure (no I/O); `plot_rowcounts` is the writer.

    Mirrors the RTS layout: an **overview** panel overlaying every hot table on
    a log y-axis (the range from `block` ~10^5 to `tx_out` ~10^8 makes log the
    only readable shared scale), then one **detail** panel per table. Version
    always goes in the trace name so a single-version plot still says which run
    it came from when the HTML is opened standalone or pasted as a screenshot.
    """
    x_col = "ts" if x_axis == "time" else "slot_no"
    x_label = "Time" if x_axis == "time" else "Slot Number"
    tables = sorted(df["table_name"].dropna().unique())

    rows = 1 + len(tables)  # overview + one detail panel per table
    titles = ["Overview - all tables (log y)", *tables]
    total_px, vspace = subplot_dims(rows)
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=vspace, subplot_titles=titles)

    # Overview: every (table, version) overlaid on row 1, log y. Version is kept
    # in every trace name (even single-version) so a standalone HTML / screenshot
    # still says which run it came from.
    for v in versions:
        for tbl in tables:
            d = df[(df["version"] == v) & (df["table_name"] == tbl)].sort_values(x_col)
            fig.add_trace(
                go.Scatter(x=d[x_col], y=d["row_count"], mode="lines", name=f"{tbl} - {short(v)}"),
                row=1,
                col=1,
            )
    fig.update_yaxes(title_text="rows", type="log", row=1, col=1)

    # Detail: one panel per table.
    for i, tbl in enumerate(tables, start=2):
        for v in versions:
            d = df[(df["version"] == v) & (df["table_name"] == tbl)].sort_values(x_col)
            fig.add_trace(
                go.Scatter(x=d[x_col], y=d["row_count"], mode="lines", name=f"{tbl} - {short(v)}"),
                row=i,
                col=1,
            )
        fig.update_yaxes(title_text="rows", row=i, col=1)

    version_suffix = " - " + " vs ".join(short(v) for v in versions)
    fig.update_layout(
        title=dict(
            text=f"{env} cardano-db-sync - Table Row Counts (approx.){version_suffix}",
            x=0.5,
            xanchor="center",
        ),
        legend_title="Table / Version",
        height=total_px,
    )
    fig.update_xaxes(title_text=x_label, row=rows, col=1)
    return fig


def plot_rowcounts(df: DataFrame, versions: list[str], outdir: str, env: str, x_axis: str) -> None:
    fig = build_rowcounts(df, versions, env, x_axis)
    path = out_path(outdir, env, versions, "tables", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


def plot_disk(df: DataFrame, versions: list[str], outdir: str, env: str, x_axis: str = "time") -> None:
    """On-disk ledger-state size.

    Row 1 is always the total directory size. Row 2 (the `lsm/` subdir) is
    added only when at least one selected version actually has an lsm subdir -
    stock/in-memory builds have none, so `lsm_bytes` is 0 and the second row is
    omitted entirely rather than drawn as a flat zero line. In a mixed LSM-vs-
    in-memory comparison the row is shown (the in-memory line sits at zero,
    which is itself the point).

    `x_axis="time"` plots wall-clock (good for a single run's growth);
    `x_axis="slot"` plots the slot derived in `load_disk` so two runs align by
    chain position. Falls back to time with a notice if no slot could be derived
    (no concurrent resource samples).
    """
    if x_axis == "slot" and ("slot_no" not in df.columns or df["slot_no"].notna().sum() == 0):
        print(
            "No slot could be derived for the disk samples (run the resource "
            "monitor alongside the disk monitor); falling back to the time axis."
        )
        x_axis = "time"
    x_col = "slot_no" if x_axis == "slot" else "ts"
    by = "Slot" if x_axis == "slot" else "Time"

    # to_numeric coerces the all-None gap-break marker rows to NaN (NaN > 0 is
    # False) without a fillna downcast warning on the object-dtype column.
    has_lsm = bool((pd.to_numeric(df["lsm_bytes"], errors="coerce") > 0).any())
    rows = 2 if has_lsm else 1
    titles = [f"Total Ledger-State Directory Size by {by}"]
    if has_lsm:
        titles.append(f"LSM Subdir Size by {by}")

    # At most two panels (total + optional lsm), so - like plot_cpu_ram - leave
    # this at Plotly's responsive auto-height to fill the viewport rather than
    # pinning a fixed height as the dense plots (ingest/tables) do.
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.1, subplot_titles=titles)
    for v in versions:
        d = df[df["version"] == v].sort_values(x_col)
        fig.add_trace(go.Scatter(x=d[x_col], y=d["total_gb"], mode="lines", name=f"Total - {v}"), row=1, col=1)
    if has_lsm:
        for v in versions:
            d = df[df["version"] == v].sort_values(x_col)
            fig.add_trace(go.Scatter(x=d[x_col], y=d["lsm_gb"], mode="lines", name=f"LSM - {v}"), row=2, col=1)

    fig.update_layout(
        title=dict(text=f"{env} cardano-db-sync - On-disk Ledger-State Size", x=0.5, xanchor="center"),
        legend_title="Version",
    )
    fig.update_xaxes(title_text=("Slot Number" if x_axis == "slot" else "Time"), row=rows, col=1)
    fig.update_yaxes(title_text="GiB", row=1, col=1)
    if has_lsm:
        fig.update_yaxes(title_text="GiB", row=2, col=1)

    path = out_path(outdir, env, versions, "disk", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


def build_rollback(samples_df: DataFrame, events_df: DataFrame, versions: list[str], env: str, x_axis: str) -> Figure:
    """Build the rollback figure (no I/O); `plot_rollback` is the CLI writer.

    Three stacked panels sharing the chosen x-axis:
      1. Queue length - the db-event backlog that spikes during a rollback.
      2. Node-vs-db block-height gap - the catch-up distance; a rollback drives
         it up, recovery brings it back to ~0. Rollback starts are marked.
      3. Recovery time per event - how long the db tip took to climb back to the
         pre-rollback height (the "Rollback Recovery Times" signal).
    """
    x_col = "ts" if x_axis == "time" else "slot_no"
    ev_x = "start_ts" if x_axis == "time" else "from_slot"
    x_label = "Time" if x_axis == "time" else "Slot Number"

    rows = 3
    titles = [
        f"DB Event Queue Length by {x_label}",
        f"Node-DB Block-Height Gap by {x_label}",
        f"Rollback Recovery Time (sec) by {x_label}",
    ]
    total_px, vspace = subplot_dims(rows)
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=vspace, subplot_titles=titles)

    for v in versions:
        d = samples_df[samples_df["version"] == v].sort_values(x_col)
        fig.add_trace(
            go.Scatter(x=d[x_col], y=d["queue_length"], mode="lines", name=f"Queue - {short(v)}"), row=1, col=1
        )
        fig.add_trace(go.Scatter(x=d[x_col], y=d["block_gap"], mode="lines", name=f"Gap - {short(v)}"), row=2, col=1)

    # Recovery markers (metrics-derived events carry recovery time; size encodes depth).
    for v in versions:
        ev = events_df[(events_df["version"] == v) & events_df["recovery_duration_sec"].notna()]
        if ev.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=ev[ev_x],
                y=ev["recovery_duration_sec"],
                mode="markers",
                name=f"Recovery - {short(v)}",
                text=[f"depth {int(d)} blocks" if pd.notna(d) else "" for d in ev["depth_blocks"]],
                hovertemplate="%{y:.0f}s recovery<br>%{text}<extra></extra>",
            ),
            row=3,
            col=1,
        )

    fig.update_layout(
        title=dict(text=f"{env} cardano-db-sync - Rollback Performance", x=0.5, xanchor="center"),
        legend_title="Version",
        height=total_px,
    )
    fig.update_xaxes(title_text=x_label, row=rows, col=1)
    fig.update_yaxes(title_text="events", row=1, col=1)
    fig.update_yaxes(title_text="blocks", row=2, col=1)
    fig.update_yaxes(title_text="seconds", row=3, col=1)
    return fig


def plot_rollback(
    samples_df: DataFrame, events_df: DataFrame, versions: list[str], outdir: str, env: str, x_axis: str
) -> None:
    fig = build_rollback(samples_df, events_df, versions, env, x_axis)
    path = out_path(outdir, env, versions, "rollback", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


# --- CLI ---------------------------------------------------------------------


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description="Generate comparison graphs from an existing cardano-db-sync SQLite file."
    )
    parser.add_argument("--env", required=True, choices=["mainnet", "preprod", "preview"], help="Environment name")
    parser.add_argument("--sqlite-db", help="Override the SQLite DB path (defaults to data/cardano-db-sync/<env>.db)")
    parser.add_argument(
        "--outdir", default=str(DEFAULT_PLOTS_DIR), help="Base directory for HTML graphs (env subdir is appended)"
    )
    parser.add_argument(
        "--versions",
        help="Comma-separated version labels to plot (skips the interactive prompt). "
        "Each item may be the full label ('cardano-db-sync 13.6.0.5 preprod') "
        "or just the short token ('13.6.0.5').",
    )
    parser.add_argument(
        "--list", dest="list_only", action="store_true", help="List available versions in the DB and exit"
    )
    parser.add_argument(
        "--x-axis",
        choices=["slot", "time"],
        default="slot",
        help="X-axis: 'slot' (slot_no, default) or 'time' (wall-clock ts). "
        "Time mode skips rows whose ts is NULL (collected before the ts column existed).",
    )
    parser.add_argument(
        "--metrics",
        choices=["cpu_ram", "ingest", "tables", "disk", "rollback", "all"],
        default="cpu_ram",
        help="Which plot to produce. 'cpu_ram' (default) plots memory and CPU. "
        "'ingest' plots tip lag, DB size, block/tx rate, UTXO count. "
        "'tables' plots approximate row counts per hot table. "
        "'disk' plots on-disk ledger-state size over time (total + lsm subdir) "
        "from db-sync-ledger-size-monitor.py's disk_metrics table. "
        "'rollback' plots queue length, node-db tip gap, and recovery time "
        "from db-sync-rollback-monitor.py's rollback_samples/rollback_events tables. "
        "'all' produces one HTML per kind (disk/rollback skipped if not collected).",
    )
    parsed = parser.parse_args()
    sqlite_db = parsed.sqlite_db or str(DEFAULT_DATA_DIR / f"{parsed.env}.db")
    versions = [v.strip() for v in parsed.versions.split(",") if v.strip()] if parsed.versions else None
    return Args(
        env=parsed.env,
        sqlite_db=sqlite_db,
        outdir=parsed.outdir,
        versions=versions,
        list_only=parsed.list_only,
        x_axis=parsed.x_axis,
        metrics=parsed.metrics,
    )


def render_cpu_ram(args: Args, chosen: list[str]) -> None:
    mem_df, cpu_df = load_cpu_ram(args.sqlite_db, chosen, args.x_axis)
    if args.x_axis == "time" and (mem_df.empty or cpu_df.empty):
        raise SystemExit(
            "No timestamped CPU/RAM rows for the selected versions. "
            "This DB was likely written before the ts column existed - try --x-axis slot."
        )
    plot_cpu_ram(mem_df, cpu_df, chosen, args.outdir, args.env, args.x_axis)


def _filter_versions_with_data(df: DataFrame, chosen: list[str], table: str) -> list[str]:
    """Drop any requested version that has no rows in `df`, warning loudly.

    `ingest_metrics` and `table_rowcounts` were added later than memory/cpu
    metrics, so older monitor runs have no data for them. Without this filter,
    the plot would silently render an empty trace for the missing version -
    making a single-version plot masquerade as a comparison.
    """
    present = set(df["version"].unique())
    missing = [v for v in chosen if v not in present]
    if missing:
        print(f"Warning: no rows in `{table}` for the following requested versions:")
        for v in missing:
            print(f"  - {v}")
        print(
            f"`{table}` was added after those runs were monitored. Their data "
            "can't be retroactively reconstructed. Plot will continue with only "
            "the versions that do have data."
        )
    return [v for v in chosen if v in present]


def render_ingest(args: Args, chosen: list[str]) -> None:
    if not has_table(args.sqlite_db, "ingest_metrics"):
        raise SystemExit(
            "ingest_metrics table not present - collector must run on the updated db-sync-resource-monitor.py first."
        )
    df = load_ingest(args.sqlite_db, chosen, args.x_axis)
    if df.empty:
        raise SystemExit("No ingest rows found for the selected versions.")
    plottable = _filter_versions_with_data(df, chosen, "ingest_metrics")
    if not plottable:
        raise SystemExit("None of the selected versions have ingest_metrics rows.")
    plot_ingest(df, plottable, args.outdir, args.env, args.x_axis)


def render_tables(args: Args, chosen: list[str]) -> None:
    if not has_table(args.sqlite_db, "table_rowcounts"):
        raise SystemExit(
            "table_rowcounts table not present - collector must run on the updated db-sync-resource-monitor.py first."
        )
    df = load_rowcounts(args.sqlite_db, chosen, args.x_axis)
    if df.empty:
        raise SystemExit("No table_rowcounts rows found for the selected versions.")
    plottable = _filter_versions_with_data(df, chosen, "table_rowcounts")
    if not plottable:
        raise SystemExit("None of the selected versions have table_rowcounts rows.")
    plot_rowcounts(df, plottable, args.outdir, args.env, args.x_axis)


def render_disk(args: Args, chosen: list[str]) -> None:
    """Plot on-disk ledger-state size.

    Deliberately a graceful no-op (prints and returns, never raises) when the
    disk_metrics table or its rows are absent - disk size is collected by a
    separate, optional collector that most DBs won't have run. That way
    `--metrics all` keeps producing the cpu_ram/ingest/tables plots for DBs that
    were never run through db-sync-ledger-size-monitor.py, instead of aborting.
    """
    if not has_table(args.sqlite_db, "disk_metrics"):
        print(
            "Skipping disk plot: no disk_metrics table in this DB "
            "(run db-sync-ledger-size-monitor.py to collect on-disk sizes)."
        )
        return
    df = load_disk(args.sqlite_db, chosen, args.x_axis)
    if df.empty:
        print("Skipping disk plot: no disk_metrics rows for the selected versions.")
        return
    plottable = _filter_versions_with_data(df, chosen, "disk_metrics")
    if not plottable:
        print("Skipping disk plot: none of the selected versions have disk_metrics rows.")
        return
    plot_disk(df[df["version"].isin(plottable)], plottable, args.outdir, args.env, args.x_axis)


def render_rollback(args: Args, chosen: list[str]) -> None:
    """Plot rollback performance (queue length, tip gap, recovery time).

    Like render_disk, a graceful no-op when the optional rollback_samples table
    or its rows are absent, so `--metrics all` keeps working on DBs that never
    ran db-sync-rollback-monitor.py. Events are taken from the log-sourced
    rollback_events table when present, plus those derived from the tip series
    (the Prometheus-only path), so recovery markers appear either way.
    """
    if not has_table(args.sqlite_db, "rollback_samples"):
        print(
            "Skipping rollback plot: no rollback_samples table in this DB "
            "(run db-sync-rollback-monitor.py to collect rollback metrics)."
        )
        return
    samples_df = load_rollback_samples(args.sqlite_db, chosen, args.x_axis)
    if samples_df.empty:
        print("Skipping rollback plot: no rollback_samples rows for the selected versions.")
        return
    plottable = _filter_versions_with_data(samples_df, chosen, "rollback_samples")
    if not plottable:
        print("Skipping rollback plot: none of the selected versions have rollback_samples rows.")
        return
    samples_df = samples_df[samples_df["version"].isin(plottable)]
    # Combine log-sourced and metrics-derived events; drop empty frames first so
    # pandas doesn't warn about concatenating all-NA columns (FutureWarning).
    event_frames = [
        df
        for df in (load_rollback_events(args.sqlite_db, plottable), derive_events_from_samples(samples_df))
        if not df.empty
    ]
    events_df = pd.concat(event_frames, ignore_index=True) if event_frames else derive_events_from_samples(samples_df)
    plot_rollback(samples_df, events_df, plottable, args.outdir, args.env, args.x_axis)


def main() -> None:
    args = parse_args()

    # Union across every version-keyed db-sync table, not just db_sync_version,
    # so a run collected only by an optional collector - or saved under a
    # mistyped --db-sync-ver - is still listed and selectable.
    versions = load_all_versions(args.sqlite_db, list(VERSION_KEYED_TABLES["db-sync"]))
    if not versions:
        print("No versions found in SQLite DB. Exiting.")
        return

    if args.list_only:
        for v in versions:
            print(v)
        return

    if args.versions:
        chosen = resolve_versions(args.versions, versions)
    else:
        print("Available versions:")
        for i, v in enumerate(versions, start=1):
            print(f"{i}. {v}")
        sel = input("Select versions to compare (comma-sep indices, e.g. 1,2): ")
        try:
            idxs = [int(x.strip()) - 1 for x in sel.split(",")]
            chosen = [versions[i] for i in idxs]
        except Exception:
            print("Invalid selection. Exiting.")
            return

    kinds = ["cpu_ram", "ingest", "tables", "disk", "rollback"] if args.metrics == "all" else [args.metrics]
    for kind in kinds:
        if kind == "cpu_ram":
            render_cpu_ram(args, chosen)
        elif kind == "ingest":
            render_ingest(args, chosen)
        elif kind == "tables":
            render_tables(args, chosen)
        elif kind == "disk":
            render_disk(args, chosen)
        elif kind == "rollback":
            render_rollback(args, chosen)


if __name__ == "__main__":
    main()
