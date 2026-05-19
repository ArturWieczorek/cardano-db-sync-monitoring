#!/usr/bin/env python3
"""Generate comparison graphs from a cardano-db-sync SQLite stats DB.

Read-only — never modifies the stats DB. Picks one or more `--versions` to plot
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
    has_column,
    has_table,
    insert_gap_breaks,
    load_versions_from_sqlite,
    resolve_versions,
    short,
)
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

    Filename format: ``<env>_<versions>_<kind>_by_<axis>.html`` — every plot
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
            "monitor). Either re-run the updated db-sync-monitor.py briefly to migrate the schema, "
            "or use --x-axis slot."
        )

    placeholders = ",".join("?" for _ in versions)
    ts_col = "ts" if have_ts else "NULL AS ts"
    ts_filter = " AND ts IS NOT NULL" if (x_axis == "time" and have_ts) else ""
    qm = f"SELECT slot_no, {ts_col}, rss, version FROM memory_metrics WHERE version IN ({placeholders}){ts_filter}"
    qc = f"SELECT slot_no, {ts_col}, cpu_percent, version FROM cpu_metrics WHERE version IN ({placeholders}){ts_filter}"
    with sqlite3.connect(sqlite_file) as conn:
        mem_df = pd.read_sql_query(qm, conn, params=versions)
        cpu_df = pd.read_sql_query(qc, conn, params=versions)
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
        df = pd.read_sql_query(sql, conn, params=versions)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    # Compute per-version rates (blocks/sec, tx/sec) against wall-clock ts.
    df = df.sort_values(["version", "ts"])
    dts = df.groupby("version")["ts"].diff().dt.total_seconds()
    df["block_rate"] = df.groupby("version")["max_block_no"].diff() / dts
    df["tx_rate"] = df.groupby("version")["max_tx_id"].diff() / dts
    df["db_size_mb"] = df["db_size_bytes"] / (1024 * 1024)
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
        df = pd.read_sql_query(sql, conn, params=versions)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df = insert_gap_breaks(df, ["version", "table_name"])
    return df


# --- Plotters ----------------------------------------------------------------

def plot_cpu_ram(mem_df: DataFrame, cpu_df: DataFrame, versions: list[str],
                 outdir: str, env: str, x_axis: str) -> None:
    x_col = "ts" if x_axis == "time" else "slot_no"
    x_label = "Time" if x_axis == "time" else "Slot Number"

    fig: Figure = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.1,
        subplot_titles=[f"Memory (RSS) by {x_label}", f"CPU % by {x_label}"],
        row_heights=[0.5, 0.5],
    )
    for v in versions:
        d = mem_df[mem_df["version"] == v].sort_values(x_col)
        fig.add_trace(go.Scatter(x=d[x_col], y=d["rss"], mode="lines", name=f"Mem - {v}"),
                      row=1, col=1)
    for v in versions:
        d = cpu_df[cpu_df["version"] == v].sort_values(x_col)
        fig.add_trace(go.Scatter(x=d[x_col], y=d["cpu_percent"], mode="lines", name=f"CPU - {v}"),
                      row=2, col=1)

    fig.update_layout(
        title=dict(text=f"{env} cardano-db-sync - Memory & CPU Comparison", x=0.5, xanchor="center"),
        xaxis_title=x_label, yaxis_title="RSS (MB)",
        xaxis2_title=x_label, yaxis2_title="CPU (%)",
        legend_title="Version",
    )
    path = out_path(outdir, env, versions, "cpu_ram", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


def plot_ingest(df: DataFrame, versions: list[str], outdir: str, env: str, x_axis: str) -> None:
    x_col = "ts" if x_axis == "time" else "slot_no"
    x_label = "Time" if x_axis == "time" else "Slot Number"
    has_utxo = df["utxo_count"].notna().any()
    rows = 5 if has_utxo else 4
    titles = [
        f"Tip Lag (sec) by {x_label}",
        f"DB Size (MB) by {x_label}",
        f"Block Insert Rate (blocks/sec) by {x_label}",
        f"Tx Insert Rate (tx/sec) by {x_label}",
    ]
    if has_utxo:
        titles.append(f"UTXO Set Size by {x_label}")

    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.06, subplot_titles=titles)

    def add(col: str, row: int, prefix: str) -> None:
        for v in versions:
            d = df[df["version"] == v].sort_values(x_col).dropna(subset=[col, x_col])
            fig.add_trace(go.Scatter(x=d[x_col], y=d[col], mode="lines", name=f"{prefix} - {v}"),
                          row=row, col=1)

    add("tip_lag_sec", 1, "TipLag")
    add("db_size_mb",  2, "DB")
    add("block_rate",  3, "Blocks/s")
    add("tx_rate",     4, "Tx/s")
    if has_utxo:
        add("utxo_count", 5, "UTXO")

    fig.update_layout(
        title=dict(text=f"{env} cardano-db-sync - Ingest Metrics", x=0.5, xanchor="center"),
        legend_title="Version",
        height=260 * rows,
    )
    fig.update_xaxes(title_text=x_label, row=rows, col=1)
    fig.update_yaxes(title_text="seconds",    row=1, col=1)
    fig.update_yaxes(title_text="MB",         row=2, col=1)
    fig.update_yaxes(title_text="blocks/sec", row=3, col=1)
    fig.update_yaxes(title_text="tx/sec",     row=4, col=1)
    if has_utxo:
        fig.update_yaxes(title_text="rows", row=5, col=1)

    path = out_path(outdir, env, versions, "ingest", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


def plot_rowcounts(df: DataFrame, versions: list[str], outdir: str, env: str, x_axis: str) -> None:
    x_col = "ts" if x_axis == "time" else "slot_no"
    x_label = "Time" if x_axis == "time" else "Slot Number"
    fig = go.Figure()
    # One trace per (version, table) combination. Log y handles the wide range
    # between block (~10^5) and tx_out (~10^8). Version always goes in the
    # trace name so a single-version plot still tells the reader which run it
    # came from — the filename and title are easy to lose when the HTML is
    # opened standalone or pasted as a screenshot.
    for v in versions:
        for tbl in sorted(df[df["version"] == v]["table_name"].unique()):
            d = df[(df["version"] == v) & (df["table_name"] == tbl)].sort_values(x_col)
            label = f"{tbl} - {short(v)}"
            fig.add_trace(go.Scatter(x=d[x_col], y=d["row_count"], mode="lines", name=label))
    version_suffix = " - " + " vs ".join(short(v) for v in versions)
    fig.update_layout(
        title=dict(
            text=f"{env} cardano-db-sync - Table Row Counts (approx.){version_suffix}",
            x=0.5, xanchor="center",
        ),
        xaxis_title=x_label, yaxis_title="rows (log scale)", yaxis_type="log",
        legend_title="Table / Version",
    )
    path = out_path(outdir, env, versions, "tables", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


# --- CLI ---------------------------------------------------------------------

def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description="Generate comparison graphs from an existing cardano-db-sync SQLite file."
    )
    parser.add_argument("--env", required=True,
                        choices=["mainnet", "preprod", "preview"],
                        help="Environment name")
    parser.add_argument("--sqlite-db",
                        help="Override the SQLite DB path (defaults to data/cardano-db-sync/<env>.db)")
    parser.add_argument("--outdir", default=str(DEFAULT_PLOTS_DIR),
                        help="Base directory for HTML graphs (env subdir is appended)")
    parser.add_argument("--versions",
                        help="Comma-separated version labels to plot (skips the interactive prompt). "
                             "Each item may be the full label ('cardano-db-sync 13.6.0.5 preprod') "
                             "or just the short token ('13.6.0.5').")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="List available versions in the DB and exit")
    parser.add_argument("--x-axis", choices=["slot", "time"], default="slot",
                        help="X-axis: 'slot' (slot_no, default) or 'time' (wall-clock ts). "
                             "Time mode skips rows whose ts is NULL (collected before the ts column existed).")
    parser.add_argument("--metrics", choices=["cpu_ram", "ingest", "tables", "all"], default="cpu_ram",
                        help="Which plot to produce. 'cpu_ram' (default) plots memory and CPU. "
                             "'ingest' plots tip lag, DB size, block/tx rate, UTXO count. "
                             "'tables' plots approximate row counts per hot table. "
                             "'all' produces one HTML per kind.")
    parsed = parser.parse_args()
    sqlite_db = parsed.sqlite_db or str(DEFAULT_DATA_DIR / f"{parsed.env}.db")
    versions = (
        [v.strip() for v in parsed.versions.split(",") if v.strip()]
        if parsed.versions else None
    )
    return Args(
        env=parsed.env, sqlite_db=sqlite_db, outdir=parsed.outdir,
        versions=versions, list_only=parsed.list_only,
        x_axis=parsed.x_axis, metrics=parsed.metrics,
    )


def render_cpu_ram(args: Args, chosen: list[str]) -> None:
    mem_df, cpu_df = load_cpu_ram(args.sqlite_db, chosen, args.x_axis)
    if args.x_axis == "time" and (mem_df.empty or cpu_df.empty):
        raise SystemExit(
            "No timestamped CPU/RAM rows for the selected versions. "
            "This DB was likely written before the ts column existed — try --x-axis slot."
        )
    plot_cpu_ram(mem_df, cpu_df, chosen, args.outdir, args.env, args.x_axis)


def _filter_versions_with_data(df: DataFrame, chosen: list[str], table: str) -> list[str]:
    """Drop any requested version that has no rows in `df`, warning loudly.

    `ingest_metrics` and `table_rowcounts` were added later than memory/cpu
    metrics, so older monitor runs have no data for them. Without this filter,
    the plot would silently render an empty trace for the missing version —
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
        raise SystemExit("ingest_metrics table not present — collector must run on the updated db-sync-monitor.py first.")
    df = load_ingest(args.sqlite_db, chosen, args.x_axis)
    if df.empty:
        raise SystemExit("No ingest rows found for the selected versions.")
    plottable = _filter_versions_with_data(df, chosen, "ingest_metrics")
    if not plottable:
        raise SystemExit("None of the selected versions have ingest_metrics rows.")
    plot_ingest(df, plottable, args.outdir, args.env, args.x_axis)


def render_tables(args: Args, chosen: list[str]) -> None:
    if not has_table(args.sqlite_db, "table_rowcounts"):
        raise SystemExit("table_rowcounts table not present — collector must run on the updated db-sync-monitor.py first.")
    df = load_rowcounts(args.sqlite_db, chosen, args.x_axis)
    if df.empty:
        raise SystemExit("No table_rowcounts rows found for the selected versions.")
    plottable = _filter_versions_with_data(df, chosen, "table_rowcounts")
    if not plottable:
        raise SystemExit("None of the selected versions have table_rowcounts rows.")
    plot_rowcounts(df, plottable, args.outdir, args.env, args.x_axis)


def main() -> None:
    args = parse_args()

    versions = load_versions_from_sqlite(args.sqlite_db, "db_sync_version")
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

    kinds = ["cpu_ram", "ingest", "tables"] if args.metrics == "all" else [args.metrics]
    for kind in kinds:
        if kind == "cpu_ram":
            render_cpu_ram(args, chosen)
        elif kind == "ingest":
            render_ingest(args, chosen)
        elif kind == "tables":
            render_tables(args, chosen)


if __name__ == "__main__":
    main()
