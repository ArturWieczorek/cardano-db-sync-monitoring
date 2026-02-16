#!/usr/bin/env python3
import argparse
import os
from datetime import datetime

import pandas as pd
import plotly.graph_objs as go
import psycopg2
from plotly.subplots import make_subplots


def ensure_dir(path: str) -> None:
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)

def fetch_db_and_table_sizes(
    pg_host: str,
    pg_port: int,
    pg_user: str,
    pg_dbname: str,
) -> dict[str, str]:
    conn = psycopg2.connect(
        host=pg_host, port=pg_port,
        user=pg_user, dbname=pg_dbname
    )
    cur = conn.cursor()

    # Total database size
    cur.execute("SELECT pg_size_pretty(pg_database_size(%s));", (pg_dbname,))
    db_size = cur.fetchone()[0]

    # Per-table sizes
    cur.execute("""
      SELECT
        pg_namespace.nspname || '.' || pg_class.relname AS table_name,
        pg_size_pretty(pg_total_relation_size(pg_class.oid)) AS size
      FROM pg_class
      JOIN pg_namespace
        ON pg_namespace.oid = pg_class.relnamespace
      WHERE pg_class.relkind = 'r'
        AND pg_namespace.nspname NOT IN ('pg_catalog','information_schema')
      ORDER BY pg_total_relation_size(pg_class.oid) DESC;
    """)
    rows = cur.fetchall()
    conn.close()

    sizes: dict[str, str] = {"__database__": db_size, **{table_name: size for table_name, size in rows}}
    return sizes

def fetch_epoch_stats(
    pg_host: str,
    pg_port: int,
    pg_user: str,
    pg_dbname: str,
) -> pd.DataFrame:
    SQL = """
        SELECT
            epoch_no,
            MAX(sync_secs)    AS sync_secs,
            SUM(tx_count)     AS tx_count,
            SUM(sum_tx_size)  AS sum_tx_size,
            SUM(reward_count) AS reward_count,
            SUM(stake_count)  AS stake_count
        FROM (
            SELECT
                earned_epoch AS epoch_no,
                0             AS sync_secs,
                0             AS tx_count,
                0             AS sum_tx_size,
                COUNT(reward) AS reward_count,
                0             AS stake_count
            FROM
                reward
            GROUP BY
                earned_epoch

            UNION

            SELECT
                epoch_no      AS epoch_no,
                0             AS sync_secs,
                0             AS tx_count,
                0             AS sum_tx_size,
                0             AS reward_count,
                COUNT(epoch_stake) AS stake_count
            FROM
                epoch_stake
            GROUP BY
                epoch_no

            UNION

            SELECT
                epoch_no      AS epoch_no,
                0             AS sync_secs,
                COUNT(tx)     AS tx_count,
                SUM(tx.size)  AS tx_sum_size,
                0             AS reward_count,
                0             AS stake_count
            FROM
                block
                INNER JOIN tx ON tx.block_id = block.id
            WHERE
                epoch_no IS NOT NULL
            GROUP BY
                epoch_no

            UNION

            SELECT
                no            AS epoch_no,
                seconds       AS sync_secs,
                0             AS tx_count,
                0             AS tx_sum_size,
                0             AS reward_count,
                0             AS stake_count
            FROM
                epoch_sync_time
        ) AS derived_table
        GROUP BY
            epoch_no;
    """
    conn = psycopg2.connect(
        host=pg_host, port=pg_port,
        user=pg_user, dbname=pg_dbname
    )
    df = pd.read_sql_query(SQL, conn)
    conn.close()
    return df

def plot_epoch_stats(
    df: pd.DataFrame,
    dbname: str,
    outdir: str,
) -> str:
    fig = make_subplots(
        rows=3, cols=2,
        specs=[[{"colspan": 2}, None],
               [{}, {}],
               [{}, {}]],
        subplot_titles=[
            "Sync Duration (sec)",
            "Transaction Count", "Sum of TX Size",
            "Reward Count",      "Stake Count"
        ],
        vertical_spacing=0.1, horizontal_spacing=0.1
    )

    # Row 1
    fig.add_trace(go.Scatter(x=df.epoch_no, y=df.sync_secs, mode="lines", name="sync_secs"), row=1, col=1)
    # Row 2
    fig.add_trace(go.Scatter(x=df.epoch_no, y=df.tx_count, mode="lines", name="tx_count"), row=2, col=1)
    fig.add_trace(go.Scatter(x=df.epoch_no, y=df.sum_tx_size, mode="lines", name="sum_tx_size"), row=2, col=2)
    # Row 3
    fig.add_trace(go.Scatter(x=df.epoch_no, y=df.reward_count, mode="lines", name="reward_count"), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.epoch_no, y=df.stake_count, mode="lines", name="stake_count"), row=3, col=2)

    fig.update_layout(
        height=1400, width=2200,
        title_text=f"Per-Epoch Stats: {dbname}",
        xaxis_title="Epoch Number", yaxis_title="Seconds",
        xaxis2_title="Epoch Number", yaxis2_title="N [Int]",
        xaxis3_title="Epoch Number", yaxis3_title="ADA",
        xaxis4_title="Epoch Number", yaxis4_title="ADA",
        xaxis5_title="Epoch Number", yaxis5_title="ADA",
        showlegend=False
    )

    ensure_dir(outdir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(outdir, f"{dbname}_epoch_stats_{timestamp}.html")
    fig.write_html(filename)
    print(f"Saved plot to {filename}")
    return filename

def write_size_report(
    sizes: dict[str, str],
    dbname: str,
    outdir: str,
) -> str:
    ensure_dir(outdir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(outdir, f"{dbname}_db_size_report_{timestamp}.txt")
    with open(filename, "w") as f:
        f.write(f"Database: {dbname}\n")
        f.write(f"Total size: {sizes.pop('__database__')}\n\n")
        f.write("Table sizes:\n")
        for tbl, sz in sizes.items():
            f.write(f"  {tbl:40s} → {sz}\n")
    print(f"Wrote size report to {filename}")
    return filename

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-host",   default="localhost")
    parser.add_argument("--pg-port",   default=5432, type=int)
    parser.add_argument("--pg-user",   default="postgres")
    parser.add_argument("--pg-dbname", required=True)
    parser.add_argument("--outdir",    default="plots")
    args = parser.parse_args()

    # 1) Epoch stats plot
    df_epochs = fetch_epoch_stats(
        pg_host=args.pg_host,
        pg_port=args.pg_port,
        pg_user=args.pg_user,
        pg_dbname=args.pg_dbname
    )
    plot_epoch_stats(df_epochs, args.pg_dbname, args.outdir)

    # 2) Size report
    sizes = fetch_db_and_table_sizes(
        pg_host=args.pg_host,
        pg_port=args.pg_port,
        pg_user=args.pg_user,
        pg_dbname=args.pg_dbname
    )
    write_size_report(sizes, args.pg_dbname, "stats")
