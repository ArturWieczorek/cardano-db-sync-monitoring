#!/usr/bin/env python3
import argparse
import os
import sqlite3
from dataclasses import dataclass

import pandas as pd
import plotly.graph_objs as go
from pandas import DataFrame
from plotly.graph_objs import Figure
from plotly.subplots import make_subplots


@dataclass
class Args:
    sqlite_db: str
    output_folder: str
    dbname: str

def load_versions(sqlite_file: str) -> list[str]:
    """Return list of distinct versions in the SQLite DB."""
    with sqlite3.connect(sqlite_file) as conn:
        df = pd.read_sql_query(
            "SELECT DISTINCT version FROM db_sync_version ORDER BY timestamp DESC",
            conn
        )
    return [str(v) for v in df["version"].tolist()]


def load_metrics(sqlite_file: str, versions: list[str]) -> tuple[DataFrame, DataFrame]:
    """Load memory and CPU metrics for selected versions."""
    placeholders = ",".join("?" for _ in versions)
    qm = f"""
      SELECT slot_no, rss, version
      FROM memory_metrics
      WHERE version IN ({placeholders})
      ORDER BY slot_no
    """
    qc = f"""
      SELECT slot_no, cpu_percent, version
      FROM cpu_metrics
      WHERE version IN ({placeholders})
      ORDER BY slot_no
    """
    with sqlite3.connect(sqlite_file) as conn:
        mem_df = pd.read_sql_query(qm, conn, params=versions)
        cpu_df = pd.read_sql_query(qc, conn, params=versions)
    return mem_df, cpu_df


def plot_and_save(mem_df: DataFrame, cpu_df: DataFrame, versions: list[str], output_folder: str, dbname: str) -> None:
    """Build a combined memory+CPU subplot and save as HTML."""
    fig: Figure = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.1,
        subplot_titles=["Memory (RSS) by Slot", "CPU % by Slot"],
        row_heights=[0.5, 0.5]
    )

    # Memory traces
    for v in versions:
        d = mem_df[mem_df["version"] == v]
        fig.add_trace(
            go.Scatter(x=d["slot_no"], y=d["rss"],
                       mode="lines", name=f"Mem - {v}"),
            row=1, col=1
        )

    # CPU traces
    for v in versions:
        d = cpu_df[cpu_df["version"] == v]
        fig.add_trace(
            go.Scatter(x=d["slot_no"], y=d["cpu_percent"],
                       mode="lines", name=f"CPU - {v}"),
            row=2, col=1
        )

    fig.update_layout(
        title_text=f"dbsync_{dbname} - Memory & CPU Comparison",
        xaxis_title="Slot Number",
        yaxis_title="RSS (MB)",
        xaxis2_title="Slot Number",
        yaxis2_title="CPU (%)",
        legend_title="Version"
    )

    os.makedirs(output_folder, exist_ok=True)
    safe = "_".join(v.replace(" ", "").replace("/", "-") for v in versions)
    out_path = os.path.join(output_folder, f"comparison_{dbname}_{safe}.html")
    fig.write_html(out_path)
    print(f"Saved comparison HTML to {out_path}")


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description="Regenerate comparison graphs from an existing dbsync SQLite file."
    )
    parser.add_argument("--sqlite-db", required=True,
                        help="Path to the SQLite file (e.g. dbsync_preprod.db)")
    parser.add_argument("--output-folder", default="plots",
                        help="Directory to write HTML graphs into")
    parser.add_argument("--dbname", required=True,
                        help="The original Postgres DB name (for filename prefix)")
    parsed = parser.parse_args()
    return Args(
        sqlite_db=parsed.sqlite_db,
        output_folder=parsed.output_folder,
        dbname=parsed.dbname
    )


def main() -> None:
    args = parse_args()

    versions = load_versions(args.sqlite_db)
    if not versions:
        print("No versions found in SQLite DB. Exiting.")
        return

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

    mem_df, cpu_df = load_metrics(args.sqlite_db, chosen)
    plot_and_save(mem_df, cpu_df, chosen, args.output_folder, args.dbname)


if __name__ == "__main__":
    main()

