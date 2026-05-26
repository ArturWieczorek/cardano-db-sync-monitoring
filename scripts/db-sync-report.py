#!/usr/bin/env python3
"""Generate per-epoch HTML + size + summary text reports for one or two
cardano-db-sync postgres databases. Postgres-read-only — never modifies the DB.

When called with two comma-separated --pg-dbname values, the report includes a
headline-deltas table comparing the two versions.
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objs as go
from _common import (
    era_sort_key,
    format_bytes,
    format_duration,
    step,
)
from _db_sync_queries import (
    assemble_epoch_df,
    build_summary,
    fetch_db_size,
    fetch_era_sync,
    fetch_index_sizes,
    fetch_table_sizes,
    pg_connect,
)
from plotly.subplots import make_subplots

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLOTS_DIR = PROJECT_ROOT / "plots" / "cardano-db-sync"
STATS_DIR = PROJECT_ROOT / "stats"


def ensure_dir(path: str) -> None:
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


# --- plotting --------------------------------------------------------------

def plot_epoch_stats(per_db: dict[str, tuple[pd.DataFrame, pd.DataFrame]], outdir: str) -> str:
    """Build the per-epoch HTML.

    `per_db` maps dbname → (epoch_df, era_df). Single entry = single-version
    plot. Two entries = comparison: each panel gets one trace per DB (named
    with the DB), era row uses grouped bars, legend is shown.

    Panel composition is data-driven from the first DB's columns; the two
    DBs are expected to share schema (both assembled by the same script).
    """
    dbnames = list(per_db.keys())
    is_compare = len(dbnames) > 1
    first_df, first_era = per_db[dbnames[0]]

    have_era = first_era is not None and not first_era.empty
    have_p95 = "p95_tx_size" in first_df.columns and first_df["p95_tx_size"].notna().any()
    have_plutus = "plutus_ratio" in first_df.columns
    have_cumulative = "cumulative_assets" in first_df.columns
    have_voting = "voting_count" in first_df.columns
    have_drep = "drep_reg_count" in first_df.columns
    have_conway = have_voting or have_drep

    specs: list[list[Any]] = []
    titles: list[str] = []

    def add_row(layout: list[Any], *row_titles: str) -> None:
        specs.append(layout)
        titles.extend(row_titles)

    if have_era:
        add_row([{"colspan": 2}, None], "Sync Time by Era (total seconds)")
    add_row([{"colspan": 2}, None], "Sync Duration (sec) by Epoch")
    add_row([{}, {}], "Block Count", "Transaction Count")
    add_row([{}, {}], "Total Fees (lovelace)", "Total Output (lovelace)")
    avg_title = "Tx Size (bytes) — avg (solid) & p95 (dashed)" if have_p95 else "Avg Tx Size (bytes)"
    add_row([{}, {}], avg_title, "Sum of Tx Size (bytes)")
    if have_plutus:
        add_row([{}, {}], "Plutus Tx Fraction", "MA Mint Count")
    else:
        add_row([{"colspan": 2}, None], "MA Mint Count")
    if have_cumulative:
        add_row([{"colspan": 2}, None], "Cumulative Distinct Multi-Assets")
    add_row([{}, {}], "Reward Count", "Stake Count")
    if have_conway:
        add_row([{"colspan": 2}, None],
                "Conway: Voting Procedures & Drep Registrations")

    fig = make_subplots(
        rows=len(specs), cols=2, specs=specs, subplot_titles=titles,
        vertical_spacing=0.05, horizontal_spacing=0.08,
    )

    def label(base: str, dbname: str) -> str:
        return f"{base} ({dbname})" if is_compare else base

    row = 1
    if have_era:
        for dbname in dbnames:
            era_df = per_db[dbname][1]
            fig.add_trace(
                go.Bar(
                    x=era_df["era"], y=era_df["total_sync_secs"],
                    name=dbname if is_compare else "era_total",
                    text=None if is_compare else
                         [f"{v:.0f}s ({p:.1f}%)" for v, p in
                          zip(era_df["total_sync_secs"], era_df["pct_of_total"])],
                    textposition="auto",
                ),
                row=row, col=1,
            )
        row += 1

    for dbname in dbnames:
        df = per_db[dbname][0]
        e = df.epoch_no
        r = row
        fig.add_trace(go.Scatter(x=e, y=df["sync_secs"], mode="lines", name=label("sync_secs", dbname)),
                      row=r, col=1)
        r += 1
        fig.add_trace(go.Scatter(x=e, y=df["block_count"], mode="lines", name=label("block_count", dbname)),
                      row=r, col=1)
        fig.add_trace(go.Scatter(x=e, y=df["tx_count"], mode="lines", name=label("tx_count", dbname)),
                      row=r, col=2)
        r += 1
        fig.add_trace(go.Scatter(x=e, y=df["total_fees"], mode="lines", name=label("total_fees", dbname)),
                      row=r, col=1)
        fig.add_trace(go.Scatter(x=e, y=df["total_output"], mode="lines", name=label("total_output", dbname)),
                      row=r, col=2)
        r += 1
        fig.add_trace(go.Scatter(x=e, y=df["avg_tx_size"], mode="lines", name=label("avg_tx_size", dbname)),
                      row=r, col=1)
        if have_p95:
            fig.add_trace(go.Scatter(x=e, y=df["p95_tx_size"], mode="lines",
                                     name=label("p95_tx_size", dbname), line=dict(dash="dot")),
                          row=r, col=1)
        fig.add_trace(go.Scatter(x=e, y=df["sum_tx_size"], mode="lines", name=label("sum_tx_size", dbname)),
                      row=r, col=2)
        r += 1
        if have_plutus:
            fig.add_trace(go.Scatter(x=e, y=df["plutus_ratio"], mode="lines",
                                     name=label("plutus_ratio", dbname)), row=r, col=1)
            fig.add_trace(go.Scatter(x=e, y=df["ma_mint_count"], mode="lines",
                                     name=label("ma_mint_count", dbname)), row=r, col=2)
        else:
            fig.add_trace(go.Scatter(x=e, y=df["ma_mint_count"], mode="lines",
                                     name=label("ma_mint_count", dbname)), row=r, col=1)
        r += 1
        if have_cumulative:
            fig.add_trace(go.Scatter(x=e, y=df["cumulative_assets"], mode="lines",
                                     name=label("cumulative_assets", dbname)), row=r, col=1)
            r += 1
        fig.add_trace(go.Scatter(x=e, y=df["reward_count"], mode="lines",
                                 name=label("reward_count", dbname)), row=r, col=1)
        fig.add_trace(go.Scatter(x=e, y=df["stake_count"], mode="lines",
                                 name=label("stake_count", dbname)), row=r, col=2)
        r += 1
        if have_conway:
            if have_voting:
                fig.add_trace(go.Scatter(x=e, y=df["voting_count"], mode="lines",
                                         name=label("voting_procedures", dbname)), row=r, col=1)
            if have_drep:
                fig.add_trace(go.Scatter(x=e, y=df["drep_reg_count"], mode="lines",
                                         name=label("drep_registrations", dbname)), row=r, col=1)

    title_dbs = " vs ".join(dbnames) if is_compare else dbnames[0]
    fig.update_layout(
        height=320 * len(specs), width=2200,
        title_text=f"Per-Epoch Stats: {title_dbs}",
        showlegend=is_compare,
        barmode="group" if is_compare else "relative",
    )

    ensure_dir(outdir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname_base = "_vs_".join(dbnames) if is_compare else dbnames[0]
    filename = os.path.join(outdir, f"{fname_base}_epoch_stats_{timestamp}.html")
    fig.write_html(filename)
    print(f"Saved plot to {filename}")
    return filename


# --- text reports ----------------------------------------------------------

def write_size_report(
    db_size: int,
    tables: list[tuple[str, int, int]],
    indexes: list[tuple[str, str, int]],
    dbname: str,
    outdir: str,
) -> str:
    ensure_dir(outdir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(outdir, f"{dbname}_db_size_report_{timestamp}.txt")
    with open(filename, "w") as f:
        f.write(f"Database: {dbname}\n")
        f.write(f"Total size: {format_bytes(db_size)}\n\n")
        f.write("Table sizes (data + indexes):\n")
        for name, tbl_b, idx_b in tables:
            total = format_bytes(tbl_b + idx_b)
            tbl = format_bytes(tbl_b)
            idx = format_bytes(idx_b)
            f.write(f"  {name:40s} → total {total:>10s}   (table {tbl:>10s}, indexes {idx:>10s})\n")
        f.write("\nAll indexes (largest first):\n")
        for name, table, b in indexes:
            f.write(f"  {name:60s} on {table:40s} → {format_bytes(b):>10s}\n")
    print(f"Wrote size report to {filename}")
    return filename


def _write_summary_body(f: Any, summary: dict[str, Any], dbname: str) -> None:
    """Single-version body. Reused by single-version and comparison writers."""
    f.write(f"Database: {dbname}\n")
    f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n\n")

    f.write("== Sync ==\n")
    f.write(f"  Total sync time:       {format_duration(summary['total_sync_secs'])}\n")
    f.write(f"  Epoch range:           {summary['epoch_min']} → {summary['epoch_max']}\n")
    f.write(f"  Slot range:            {summary['slot_min']} → {summary['slot_max']}\n")
    if summary["total_sync_secs"] and summary["slot_max"] is not None:
        slots = summary["slot_max"] - (summary["slot_min"] or 0)
        sps = slots / max(summary["total_sync_secs"], 1)
        f.write(f"  Mean slots/sec:        {sps:,.1f}\n")
    f.write("\n")

    era_df: pd.DataFrame = summary["era_breakdown"]
    if era_df is not None and not era_df.empty:
        f.write("== Sync time by era ==\n")
        f.write(f"  {'Era':<10}{'Epochs':>8}  {'Range':<14}{'Total':>14}{'Avg/epoch':>14}{'% of total':>12}\n")
        for _, r in era_df.iterrows():
            rng = f"{int(r['epoch_from'])} → {int(r['epoch_to'])}"
            f.write(f"  {r['era']:<10}{int(r['epochs']):>8}  {rng:<14}"
                    f"{format_duration(r['total_sync_secs']):>14}"
                    f"{format_duration(r['avg_sync_secs']):>14}"
                    f"{r['pct_of_total']:>11.1f}%\n")
        f.write("\n")

    f.write("== Chain activity (cumulative) ==\n")
    f.write(f"  Blocks:                {summary['total_blocks']:,}\n")
    f.write(f"  Transactions:          {summary['total_txs']:,}\n")
    f.write(f"  Total fees:            {summary['total_fees']:,} lovelace\n")
    f.write(f"  MA mint events:        {summary['total_mints']:,} (estimate)\n")
    f.write(f"  Distinct MA assets:    {summary['total_distinct_assets']:,} (estimate)\n")
    if summary.get("plutus_ratio_overall") is None:
        f.write("  Plutus tx fraction:    skipped (--skip-slow)\n")
    else:
        f.write(f"  Plutus tx fraction:    {summary['plutus_ratio_overall']:.2%}\n")
    f.write("\n")

    f.write("== Storage ==\n")
    f.write(f"  Total DB size:         {format_bytes(summary['db_size_bytes'])}\n")
    f.write(f"  All tables ({len(summary['all_tables'])}, largest first):\n")
    for name, total in summary["all_tables"]:
        f.write(f"    {name:38s} {format_bytes(total):>10s}\n")
    f.write(f"  All indexes ({len(summary['all_indexes'])}, largest first):\n")
    for name, table, b in summary["all_indexes"]:
        f.write(f"    {name:58s} on {table:30s} {format_bytes(b):>10s}\n")
    f.write("\n")

    f.write("== UTXO ==\n")
    if summary["utxo_tracking"]:
        f.write(f"  UTXO set size:         {summary['utxo_count']:,}\n")
    else:
        f.write("  UTXO set size:         tracking disabled "
                "(set tx-out config in cardano-db-sync to enable consumed_by_tx_id)\n")


def write_summary_report(summary: dict[str, Any], dbname: str, outdir: str) -> str:
    ensure_dir(outdir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(outdir, f"{dbname}_summary_{timestamp}.txt")
    with open(filename, "w") as f:
        _write_summary_body(f, summary, dbname)
    print(f"Wrote summary report to {filename}")
    return filename


# --- comparison writers ----------------------------------------------------

def _fmt_pct_delta(a: float | int | None, b: float | int | None) -> str:
    """Percent change from a→b. — when undefined."""
    if a is None or b is None or a == 0:
        return "—"
    delta = (b - a) / a * 100.0
    if abs(delta) < 0.05:
        return "—"
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta:.1f}%"


def _fmt_pp_delta(a: float | None, b: float | None) -> str:
    """Percentage-point delta (for ratios like plutus_ratio)."""
    if a is None or b is None:
        return "—"
    delta = (b - a) * 100.0
    if abs(delta) < 0.05:
        return "—"
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta:.1f}pp"


def write_size_report_comparison(
    per_db: dict[str, dict[str, Any]],
    dbnames: list[str],
    outdir: str,
) -> str:
    ensure_dir(outdir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(outdir, f"{'_vs_'.join(dbnames)}_db_size_report_{timestamp}.txt")
    with open(filename, "w") as f:
        f.write(f"Comparison: {' vs '.join(dbnames)}\n")
        f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n\n")
        for dbname in dbnames:
            d = per_db[dbname]
            f.write(f"=== {dbname} ===\n")
            f.write(f"Total size: {format_bytes(d['db_size'])}\n\n")
            f.write("Table sizes (data + indexes):\n")
            for name, tbl_b, idx_b in d["tables"]:
                total = format_bytes(tbl_b + idx_b)
                tbl = format_bytes(tbl_b)
                idx = format_bytes(idx_b)
                f.write(f"  {name:40s} → total {total:>10s}   (table {tbl:>10s}, indexes {idx:>10s})\n")
            f.write("\nAll indexes (largest first):\n")
            for name, table, b in d["indexes"]:
                f.write(f"  {name:60s} on {table:40s} → {format_bytes(b):>10s}\n")
            f.write("\n")
    print(f"Wrote size report to {filename}")
    return filename


def write_summary_report_comparison(
    per_db: dict[str, dict[str, Any]],
    dbnames: list[str],
    outdir: str,
) -> str:
    ensure_dir(outdir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(outdir, f"{'_vs_'.join(dbnames)}_summary_{timestamp}.txt")
    a, b = dbnames[0], dbnames[1]
    sa = per_db[a]["summary"]
    sb = per_db[b]["summary"]

    col_w = max(20, len(a), len(b))
    metric_w = 26

    def line(label: str, va: str, vb: str, delta: str) -> str:
        return f"{label:<{metric_w}}{va:<{col_w + 2}}{vb:<{col_w + 2}}{delta:<10}\n"

    with open(filename, "w") as f:
        f.write(f"Comparison: {a}  vs  {b}\n")
        f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n\n")

        # Headline deltas
        f.write("== Headline metrics ==\n")
        f.write(line("Metric", a, b, "Δ (b vs a)"))
        f.write(line("Total sync time",
                     format_duration(sa.get("total_sync_secs")),
                     format_duration(sb.get("total_sync_secs")),
                     _fmt_pct_delta(sa.get("total_sync_secs"), sb.get("total_sync_secs"))))
        f.write(line("Final DB size",
                     format_bytes(sa["db_size_bytes"]),
                     format_bytes(sb["db_size_bytes"]),
                     _fmt_pct_delta(sa["db_size_bytes"], sb["db_size_bytes"])))
        f.write(line("Total blocks",
                     f"{sa['total_blocks']:,}", f"{sb['total_blocks']:,}",
                     _fmt_pct_delta(sa["total_blocks"], sb["total_blocks"])))
        f.write(line("Total transactions",
                     f"{sa['total_txs']:,}", f"{sb['total_txs']:,}",
                     _fmt_pct_delta(sa["total_txs"], sb["total_txs"])))
        f.write(line("Total fees (lovelace)",
                     f"{sa['total_fees']:,}", f"{sb['total_fees']:,}",
                     _fmt_pct_delta(sa["total_fees"], sb["total_fees"])))
        f.write(line("MA mint events (est.)",
                     f"{sa['total_mints']:,}", f"{sb['total_mints']:,}",
                     _fmt_pct_delta(sa["total_mints"], sb["total_mints"])))
        f.write(line("Distinct MA assets (est.)",
                     f"{sa['total_distinct_assets']:,}", f"{sb['total_distinct_assets']:,}",
                     _fmt_pct_delta(sa["total_distinct_assets"], sb["total_distinct_assets"])))
        pa = sa.get("plutus_ratio_overall")
        pb = sb.get("plutus_ratio_overall")
        f.write(line("Plutus tx fraction",
                     f"{pa:.2%}" if pa is not None else "skipped",
                     f"{pb:.2%}" if pb is not None else "skipped",
                     _fmt_pp_delta(pa, pb)))
        if sa.get("utxo_tracking") and sb.get("utxo_tracking"):
            f.write(line("UTXO set size",
                         f"{sa['utxo_count']:,}", f"{sb['utxo_count']:,}",
                         _fmt_pct_delta(sa["utxo_count"], sb["utxo_count"])))
        else:
            f.write(line("UTXO set size",
                         f"{sa['utxo_count']:,}" if sa.get("utxo_tracking") else "tracking off",
                         f"{sb['utxo_count']:,}" if sb.get("utxo_tracking") else "tracking off",
                         "—"))
        f.write("\n")

        # Per-era comparison
        era_a: pd.DataFrame = sa["era_breakdown"]
        era_b: pd.DataFrame = sb["era_breakdown"]
        if era_a is not None and not era_a.empty and era_b is not None and not era_b.empty:
            f.write("== Sync time by era ==\n")
            merged = era_a[["era", "total_sync_secs"]].merge(
                era_b[["era", "total_sync_secs"]],
                on="era", how="outer", suffixes=("_a", "_b"),
            )
            merged["sort_key"] = merged["era"].map(era_sort_key)
            merged = merged.sort_values("sort_key").drop(columns="sort_key").fillna(0)
            f.write(line("Era", f"{a} total", f"{b} total", "Δ (b vs a)"))
            for _, r in merged.iterrows():
                va = float(r["total_sync_secs_a"])
                vb = float(r["total_sync_secs_b"])
                f.write(line(r["era"],
                             format_duration(va) if va else "—",
                             format_duration(vb) if vb else "—",
                             _fmt_pct_delta(va, vb)))
            f.write("\n")

        # Per-version details
        f.write("== Per-version details ==\n\n")
        for dbname in dbnames:
            f.write(f"--- {dbname} ---\n")
            _write_summary_body(f, per_db[dbname]["summary"], dbname)
            f.write("\n")

    print(f"Wrote summary report to {filename}")
    return filename


# --- CLI -------------------------------------------------------------------

def collect_one(args: argparse.Namespace, dbname: str) -> dict[str, Any]:
    """Open a connection per DB and run every fetcher. Returns the bundle
    plot/report writers need."""
    with pg_connect(args.pg_host, args.pg_port, args.pg_user, dbname) as conn:
        epoch_df = assemble_epoch_df(conn, skip_slow=args.skip_slow, with_p95=args.with_p95)
        era_df = fetch_era_sync(conn)
        db_size = fetch_db_size(conn)
        tables = fetch_table_sizes(conn)
        indexes = fetch_index_sizes(conn)
        summary = build_summary(conn, db_size, tables, indexes, epoch_df, era_df)
    return {
        "epoch_df": epoch_df, "era_df": era_df,
        "db_size": db_size, "tables": tables, "indexes": indexes,
        "summary": summary,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate plot + size + summary reports for one or two cardano-db-sync postgres DBs."
    )
    parser.add_argument("--pg-host",   default=None,
                        help="Postgres host (default: PGHOST env var, then 'localhost')")
    parser.add_argument("--pg-port",   default=None, type=lambda v: int(v),
                        help="Postgres port (default: PGPORT env var, then 5432)")
    parser.add_argument("--pg-user",   default=None,
                        help="Postgres user (default: PGUSER env var, then current user)")
    parser.add_argument("--pg-dbname", required=True,
                        help="One postgres DB, or two comma-separated for an A/B comparison "
                             "(e.g. '--pg-dbname preprod_13.6.0.5,preprod_13.7.1.0'). When two are "
                             "given the plot overlays both versions, and the size+summary reports "
                             "include a headline-deltas table.")
    parser.add_argument("--outdir",    default=str(DEFAULT_PLOTS_DIR))
    parser.add_argument("--with-p95",  action="store_true",
                        help="Compute p95 tx size per epoch via PERCENTILE_CONT. "
                             "Expensive on mainnet (5-20 min) because PG must sort all tx.size "
                             "per epoch group. Without this flag the avg-only line is plotted.")
    parser.add_argument("--skip-slow", action="store_true",
                        help="Skip the expensive per-epoch fetchers: plutus adoption "
                             "(redeemer scan) and cumulative-distinct-assets (ma_tx_mint scan). "
                             "Their panels are omitted from the plot and summary.")
    args = parser.parse_args()

    dbnames = [d.strip() for d in args.pg_dbname.split(",") if d.strip()]
    if not 1 <= len(dbnames) <= 2:
        raise SystemExit(
            f"--pg-dbname accepts one DB or two comma-separated DBs (got {len(dbnames)}). "
            "Comparison mode is capped at two for readable plots."
        )

    started = time.time()
    is_compare = len(dbnames) == 2
    total_steps = 4 if not is_compare else 5

    per_db: dict[str, dict[str, Any]] = {}
    for i, dbname in enumerate(dbnames, start=1):
        step(i, total_steps, f"Collecting from {dbname}")
        per_db[dbname] = collect_one(args, dbname)

    plot_step = len(dbnames) + 1
    step(plot_step, total_steps, "Rendering per-epoch HTML")
    plot_input = {db: (per_db[db]["epoch_df"], per_db[db]["era_df"]) for db in dbnames}
    plot_epoch_stats(plot_input, args.outdir)

    step(plot_step + 1, total_steps, "Writing size report")
    if is_compare:
        write_size_report_comparison(per_db, dbnames, str(STATS_DIR))
    else:
        d = per_db[dbnames[0]]
        write_size_report(d["db_size"], d["tables"], d["indexes"], dbnames[0], str(STATS_DIR))

    step(plot_step + 2, total_steps, "Writing summary report")
    if is_compare:
        write_summary_report_comparison(per_db, dbnames, str(STATS_DIR))
    else:
        write_summary_report(per_db[dbnames[0]]["summary"], dbnames[0], str(STATS_DIR))

    print(f"Done in {time.time() - started:.1f}s.")
