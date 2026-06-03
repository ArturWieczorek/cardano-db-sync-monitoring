#!/usr/bin/env python3
"""Generate comparison graphs from a cardano-node SQLite stats DB.

Read-only — never modifies the stats DB. Mirrors db-sync-plot.py's shape.

Metric sets:
  cpu_ram  (default): RSS + CPU% over slot or wall-clock.
  ingest:             Sync time by era (bar) + sync duration per epoch (line).
  disk:               On-disk db-directory size over time (total + lsm subdir),
                      from the separate node-db-size-monitor.py collector.
  all:                Produces one HTML per kind (disk skipped if not collected).
"""

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import plotly.graph_objs as go
from _common import (
    compute_epoch_durations,
    era_sort_key,
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
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "cardano-node"
DEFAULT_PLOTS_DIR = PROJECT_ROOT / "plots" / "cardano-node"


@dataclass
class Args:
    env: str
    sqlite_db: str
    outdir: str
    versions: list[str] | None
    list_only: bool
    x_axis: str
    metrics: str


# --- output path -----------------------------------------------------------

def out_path(outdir: str, env: str, versions: list[str], kind: str, x_axis: str) -> str:
    """Compose canonical output HTML path. `kind` is the plot type tag.

    Filename format: ``<env>_<versions>_<kind>_by_<axis>.html`` — same scheme
    used by db-sync-plot's out_path; kept identical on purpose so a user
    browsing plots/cardano-node/ vs plots/cardano-db-sync/ sees the same
    conventions on both sides, and a file moved out of its parent dir
    still carries its env in the name.
    """
    shorts = [short(v) for v in versions]
    p = Path(outdir) / env
    p.mkdir(parents=True, exist_ok=True)
    return str(p / f"{env}_{'_vs_'.join(shorts)}_{kind}_by_{x_axis}.html")


# --- loaders ---------------------------------------------------------------

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
            "monitor). Either re-run the updated node-monitor.py briefly to migrate the schema, "
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
    mem_df["ts"] = pd.to_datetime(mem_df["ts"], errors="coerce")
    cpu_df["ts"] = pd.to_datetime(cpu_df["ts"], errors="coerce")
    mem_df = insert_gap_breaks(mem_df, ["version"])
    cpu_df = insert_gap_breaks(cpu_df, ["version"])
    return mem_df, cpu_df


def load_node_ingest(sqlite_file: str, versions: list[str]) -> DataFrame:
    """Load `node_ingest_metrics` for the selected versions.

    Skips rows whose ts or epoch_no is NULL — without both the duration math
    is undefined. The per-epoch / per-era aggregation happens downstream in
    compute_epoch_durations().
    """
    placeholders = ",".join("?" for _ in versions)
    sql = (
        "SELECT ts, slot_no, epoch_no, era, sync_progress, version "
        f"FROM node_ingest_metrics WHERE version IN ({placeholders}) "
        "AND ts IS NOT NULL AND epoch_no IS NOT NULL"
    )
    with sqlite3.connect(sqlite_file) as conn:
        df = pd.read_sql_query(sql, conn, params=versions)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    return df


def load_disk(sqlite_file: str, versions: list[str]) -> DataFrame:
    """Load `disk_metrics` for the selected versions.

    Written by the separate node-db-size-monitor.py collector. `slot_no` is
    NULL there (the disk collector deliberately doesn't query the node for a
    slot), so the disk series is always plotted against wall-clock `ts`
    regardless of --x-axis. Byte counts are converted to GiB for readability
    and labelled GiB (strict binary unit — not GB, which would overstate by ~7%).
    """
    placeholders = ",".join("?" for _ in versions)
    sql = (
        "SELECT ts, total_bytes, lsm_bytes, version "
        f"FROM disk_metrics WHERE version IN ({placeholders}) AND ts IS NOT NULL"
    )
    with sqlite3.connect(sqlite_file) as conn:
        df = pd.read_sql_query(sql, conn, params=versions)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["total_gb"] = df["total_bytes"] / 1024**3
    df["lsm_gb"] = df["lsm_bytes"] / 1024**3
    df = insert_gap_breaks(df, ["version"])
    return df


# --- plotters --------------------------------------------------------------

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
        title=dict(text=f"{env} cardano-node - Memory & CPU Comparison", x=0.5, xanchor="center"),
        xaxis_title=x_label, yaxis_title="RSS (MiB)",
        xaxis2_title=x_label, yaxis2_title="CPU (%)",
        legend_title="Version",
    )
    path = out_path(outdir, env, versions, "cpu_ram", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


def plot_ingest(per_epoch: DataFrame, versions: list[str], outdir: str, env: str, x_axis: str) -> None:
    """Two-row figure: era bar chart on top, per-epoch duration line below.

    Wall-clock seconds the monitor observed per epoch and per era. During
    catch-up sync this is meaningfully shorter than 432000 (preprod epoch
    length in slots); at tip it equals real time.
    """
    is_compare = len(versions) > 1

    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=[
            "Sync Time by Era (total seconds)",
            "Sync Duration per Epoch (seconds)",
        ],
        # Generous gap between the bar chart and the line below it. The
        # default ~0.05 makes them touch.
        vertical_spacing=0.18,
        row_heights=[0.45, 0.55],
    )

    # Top: era bar chart, aggregated from per_epoch.
    per_era = (
        per_epoch.groupby(["version", "era"], as_index=False)
        .agg(total_duration_sec=("duration_sec", "sum"), epochs=("epoch_no", "count"))
    )
    per_era["sort_key"] = per_era["era"].map(era_sort_key)
    per_era = per_era.sort_values("sort_key").drop(columns="sort_key")

    for v in versions:
        d = per_era[per_era["version"] == v]
        fig.add_trace(
            go.Bar(
                x=d["era"], y=d["total_duration_sec"],
                name=v if is_compare else "era_total",
                text=None if is_compare else
                     [f"{val:.0f}s ({eps} epochs)" for val, eps in
                      zip(d["total_duration_sec"], d["epochs"])],
                textposition="auto",
            ),
            row=1, col=1,
        )

    # Bottom: per-epoch duration line.
    for v in versions:
        d = per_epoch[per_epoch["version"] == v].sort_values("epoch_no")
        fig.add_trace(
            go.Scatter(x=d["epoch_no"], y=d["duration_sec"], mode="lines",
                       name=f"{v}" if is_compare else "duration_sec"),
            row=2, col=1,
        )

    # Caveat lives in the main figure title as a styled sub-line. Plotly's
    # title HTML supports <br>, <b>, <i>, <sub>, <sup>, <span style=...>, <a> —
    # NOT <code>, so we use <b> to highlight the flag name. The subtitle is
    # wrapped in <span> with a smaller font and gray color so it reads as a
    # footnote rather than competing with the main title.
    title_text = (
        f"<b>{env} cardano-node — Sync Time by Era / per Epoch</b>"
        "<br>"
        "<span style='font-size:12px;color:#666'>"
        "Epoch counts reflect observed samples. Short early epochs (esp. testnet "
        "Byron) can pass faster than the default <b>--interval=10s</b> and may be "
        "missed. Lower <b>--interval</b> to capture them."
        "</span>"
    )
    fig.update_layout(
        title=dict(
            text=title_text,
            x=0.5,
            xanchor="center",
            y=0.97,
            yanchor="top",
            # title.pad.b adds vertical space between the title block (incl.
            # subtitle) and the first chart. Without this, the bar chart sits
            # right under the gray caveat line.
            pad=dict(b=30),
        ),
        legend_title="Version" if is_compare else None,
        barmode="group" if is_compare else "relative",
        showlegend=is_compare,
        # Sized for two stacked panels with the line chart taking the larger
        # share. Bigger top margin to host the two-line title without the bar
        # chart crowding it. The previous height=900 left a large empty band
        # below the bottom chart; 800 + tight bottom margin renders snug.
        height=800,
        margin=dict(t=150, b=60, l=80, r=40),
    )
    fig.update_xaxes(title_text="Era", row=1, col=1)
    fig.update_yaxes(title_text="seconds (total)", row=1, col=1)
    fig.update_xaxes(title_text="Epoch", row=2, col=1)
    fig.update_yaxes(title_text="seconds", row=2, col=1)

    path = out_path(outdir, env, versions, "ingest", x_axis)
    fig.write_html(path)
    print(f"Saved plot to {path}")


def plot_disk(df: DataFrame, versions: list[str], outdir: str, env: str) -> None:
    """On-disk db-directory size over wall-clock time.

    Row 1 is always the total directory size. Row 2 (the `lsm/` subdir) is
    added only when at least one selected version actually has an lsm subdir —
    stock/in-memory builds have none, so `lsm_bytes` is 0 and the second row is
    omitted entirely rather than drawn as a flat zero line. In a mixed LSM-vs-
    in-memory comparison the row is shown (the in-memory line sits at zero,
    which is itself the point).

    Always plotted against `ts`: disk_metrics has no slot_no, and disk growth
    reads naturally against wall-clock anyway. The filename is tagged `_by_time`
    accordingly.
    """
    # to_numeric coerces the all-None gap-break marker rows to NaN (NaN > 0 is
    # False) without a fillna downcast warning on the object-dtype column.
    has_lsm = bool((pd.to_numeric(df["lsm_bytes"], errors="coerce") > 0).any())
    rows = 2 if has_lsm else 1
    titles = ["Total DB Directory Size by Time"]
    if has_lsm:
        titles.append("LSM Subdir Size by Time")

    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True,
                        vertical_spacing=0.1, subplot_titles=titles)
    for v in versions:
        d = df[df["version"] == v].sort_values("ts")
        fig.add_trace(go.Scatter(x=d["ts"], y=d["total_gb"], mode="lines", name=f"Total - {v}"),
                      row=1, col=1)
    if has_lsm:
        for v in versions:
            d = df[df["version"] == v].sort_values("ts")
            fig.add_trace(go.Scatter(x=d["ts"], y=d["lsm_gb"], mode="lines", name=f"LSM - {v}"),
                          row=2, col=1)

    fig.update_layout(
        title=dict(text=f"{env} cardano-node - On-disk DB Size", x=0.5, xanchor="center"),
        legend_title="Version",
        height=260 * rows + 120,
    )
    fig.update_xaxes(title_text="Time", row=rows, col=1)
    fig.update_yaxes(title_text="GiB", row=1, col=1)
    if has_lsm:
        fig.update_yaxes(title_text="GiB", row=2, col=1)

    path = out_path(outdir, env, versions, "disk", "time")
    fig.write_html(path)
    print(f"Saved plot to {path}")


# --- CLI -------------------------------------------------------------------

def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description="Generate comparison graphs from an existing cardano-node SQLite file."
    )
    parser.add_argument("--env", required=True,
                        choices=["mainnet", "preprod", "preview"],
                        help="Environment name")
    parser.add_argument("--sqlite-db",
                        help="Override the SQLite DB path (defaults to data/cardano-node/<env>.db)")
    parser.add_argument("--outdir", default=str(DEFAULT_PLOTS_DIR),
                        help="Base directory for HTML graphs (env subdir is appended)")
    parser.add_argument("--versions",
                        help="Comma-separated version labels to plot (skips the interactive prompt). "
                             "Each item may be the full label ('cardano-node 11.0.1 preprod') "
                             "or just the short token ('11.0.1').")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="List available versions in the DB and exit")
    parser.add_argument("--x-axis", choices=["slot", "time"], default="slot",
                        help="X-axis for cpu_ram mode: 'slot' (slot_no, default) or 'time' "
                             "(wall-clock ts). Ingest mode always uses epoch on x.")
    parser.add_argument("--metrics", choices=["cpu_ram", "ingest", "disk", "all"], default="cpu_ram",
                        help="Which plot to produce. 'cpu_ram' (default) plots memory and CPU. "
                             "'ingest' plots sync time by era (bar) + sync duration per epoch "
                             "(line) — needs the post-refactor monitor's node_ingest_metrics table. "
                             "'disk' plots on-disk db-directory size over time (total + lsm subdir) "
                             "from node-db-size-monitor.py's disk_metrics table. "
                             "'all' produces one per kind (disk skipped if not collected).")
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


def _filter_versions_with_data(df: DataFrame, chosen: list[str], table: str) -> list[str]:
    """Drop any requested version that has no rows in `df`, warning loudly.

    `node_ingest_metrics` was added later than memory/cpu metrics, so older
    monitor runs have no data for it. Without this filter, the plot would
    silently render an empty trace for the missing version.
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


def render_cpu_ram(args: Args, chosen: list[str]) -> None:
    mem_df, cpu_df = load_cpu_ram(args.sqlite_db, chosen, args.x_axis)
    if args.x_axis == "time" and (mem_df.empty or cpu_df.empty):
        raise SystemExit(
            "No timestamped rows found for the selected versions. "
            "This DB was likely written before the ts column existed — try --x-axis slot."
        )
    plot_cpu_ram(mem_df, cpu_df, chosen, args.outdir, args.env, args.x_axis)


def render_ingest(args: Args, chosen: list[str]) -> None:
    if not has_table(args.sqlite_db, "node_ingest_metrics"):
        raise SystemExit(
            "node_ingest_metrics table not present — collector must run on the "
            "updated node-monitor.py first."
        )
    df = load_node_ingest(args.sqlite_db, chosen)
    if df.empty:
        raise SystemExit("No node_ingest_metrics rows found for the selected versions.")
    plottable = _filter_versions_with_data(df, chosen, "node_ingest_metrics")
    if not plottable:
        raise SystemExit("None of the selected versions have node_ingest_metrics rows.")
    per_epoch = compute_epoch_durations(df[df["version"].isin(plottable)])
    if per_epoch.empty:
        raise SystemExit("Could not compute per-epoch durations (no usable samples).")
    plot_ingest(per_epoch, plottable, args.outdir, args.env, args.x_axis)


def render_disk(args: Args, chosen: list[str]) -> None:
    """Plot on-disk db-directory size.

    Deliberately a graceful no-op (prints and returns, never raises) when the
    disk_metrics table or its rows are absent — disk size is collected by a
    separate, optional collector that most DBs won't have run. That way
    `--metrics all` keeps producing the cpu_ram/ingest plots for DBs that were
    never run through node-db-size-monitor.py, instead of aborting the batch.
    """
    if not has_table(args.sqlite_db, "disk_metrics"):
        print("Skipping disk plot: no disk_metrics table in this DB "
              "(run node-db-size-monitor.py to collect on-disk sizes).")
        return
    df = load_disk(args.sqlite_db, chosen)
    if df.empty:
        print("Skipping disk plot: no disk_metrics rows for the selected versions.")
        return
    plottable = _filter_versions_with_data(df, chosen, "disk_metrics")
    if not plottable:
        print("Skipping disk plot: none of the selected versions have disk_metrics rows.")
        return
    plot_disk(df[df["version"].isin(plottable)], plottable, args.outdir, args.env)


def main() -> None:
    args = parse_args()

    versions = load_versions_from_sqlite(args.sqlite_db, "node_version")
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

    kinds = ["cpu_ram", "ingest", "disk"] if args.metrics == "all" else [args.metrics]
    for kind in kinds:
        if kind == "cpu_ram":
            render_cpu_ram(args, chosen)
        elif kind == "ingest":
            render_ingest(args, chosen)
        elif kind == "disk":
            render_disk(args, chosen)


if __name__ == "__main__":
    main()
