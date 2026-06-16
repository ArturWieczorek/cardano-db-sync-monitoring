"""Postgres queries for the cardano-db-sync report.

Imported by db-sync-epoch-report.py. Stays focused on data acquisition: opens
connections, runs SQL, returns DataFrames / scalars / dicts. No plotting or
file-writing logic here - those belong in db-sync-epoch-report.py.

All query functions take a live psycopg2 connection as their first parameter
so the caller can scope a single connection across multiple fetches per DB
(the report opens one connection per --pg-dbname).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pandas as pd
import psycopg2
from _common import era_for, utc_timestamp  # noqa: F401  (utc_timestamp re-exported for callers)

# Opt in to pandas' future fillna behavior so we don't get FutureWarning noise
# on assemble_epoch_df's downstream fillna(0).
pd.set_option("future.no_silent_downcasting", True)


# --- connection / introspection helpers ------------------------------------


@contextmanager
def pg_connect(
    pg_host: str | None,
    pg_port: int | str | None,
    pg_user: str | None,
    pg_dbname: str,
    **kwargs: Any,
) -> Iterator[psycopg2.extensions.connection]:
    """Context-managed psycopg2 connection that always closes on exit.

    psycopg2's own `with` only commits/rollbacks - it does NOT close. We always
    close because each report invocation opens at most a handful of connections.

    `None` host/port/user fall back to psycopg2's env-var defaults (PGHOST,
    PGPORT, PGUSER, PGPASSWORD), matching the convention used by `psql`.
    """
    conn_kwargs: dict[str, Any] = {"dbname": pg_dbname}
    if pg_host is not None:
        conn_kwargs["host"] = pg_host
    if pg_port is not None:
        conn_kwargs["port"] = pg_port
    if pg_user is not None:
        conn_kwargs["user"] = pg_user
    conn_kwargs.update(kwargs)
    conn = psycopg2.connect(**conn_kwargs)
    try:
        yield conn
    finally:
        conn.close()


def table_exists(conn: psycopg2.extensions.connection, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL;", (name,))
        row = cur.fetchone()
        return bool(row and row[0])


def utxo_tracking_enabled(conn: psycopg2.extensions.connection) -> bool:
    """Same idea + same 5-second guard as the monitor's detect_utxo_tracking.

    Verdict: True iff tx_out.consumed_by_tx_id has any non-NULL value. The
    `consumed_by_tx_id` column is db-sync-config-gated; if disabled, the
    probe would otherwise seq-scan a huge tx_out table looking for a NOT NULL
    that doesn't exist.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5000';")
            cur.execute("SELECT EXISTS (SELECT 1 FROM tx_out WHERE consumed_by_tx_id IS NOT NULL);")
            row = cur.fetchone()
        return bool(row and row[0])
    except psycopg2.errors.QueryCanceled:
        dbname = conn.info.dbname if hasattr(conn, "info") else "(unknown)"
        print(f"UTXO tracking probe timed out (>5s) for '{dbname}' - assuming DISABLED.")
        conn.rollback()
        return False


def query_df(
    conn: psycopg2.extensions.connection,
    sql: str,
    params: tuple | list | None = None,
) -> pd.DataFrame:
    """Execute SQL via psycopg2 and return a DataFrame.

    Bypasses pd.read_sql_query - pandas warns that psycopg2 connections aren't
    in its officially-tested set even though they work. This helper avoids the
    warning without adding a SQLAlchemy dependency.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [desc[0] for desc in cur.description] if cur.description else []
    return pd.DataFrame(rows, columns=cols)


# --- per-epoch fetchers ----------------------------------------------------


def fetch_epoch_stats(conn: psycopg2.extensions.connection, with_p95: bool) -> pd.DataFrame:
    """Per-epoch tx-level stats: block_count, tx_count, fees, output, sizes.

    p95_tx_size is gated behind --with-p95 because PERCENTILE_CONT sorts every
    tx.size in each epoch group - minutes on mainnet (~100M tx rows). When
    not requested, the column is omitted from the SELECT entirely.
    """
    p95_select = (
        ",\n          COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY t.size), 0)::float AS p95_tx_size"
        if with_p95
        else ""
    )
    sql = f"""
      SELECT
          b.epoch_no                                                AS epoch_no,
          COUNT(DISTINCT b.id)                                      AS block_count,
          COUNT(t.id)                                               AS tx_count,
          COALESCE(SUM(t.size), 0)::bigint                          AS sum_tx_size,
          COALESCE(SUM(t.fee), 0)::bigint                           AS total_fees,
          COALESCE(SUM(t.out_sum), 0)::double precision             AS total_output,
          COALESCE(AVG(t.size), 0)::float                           AS avg_tx_size{p95_select}
      FROM block b
      LEFT JOIN tx t ON t.block_id = b.id
      WHERE b.epoch_no IS NOT NULL
      GROUP BY b.epoch_no;
    """
    return query_df(conn, sql)


def fetch_epoch_sync_secs(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    return query_df(
        conn,
        "SELECT no AS epoch_no, MAX(seconds) AS sync_secs FROM epoch_sync_time GROUP BY no;",
    )


def fetch_epoch_plutus(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    """Per-epoch Plutus adoption. Slow on mainnet (full redeemer + tx scans)."""
    sql = """
      SELECT
        b.epoch_no                                                            AS epoch_no,
        COUNT(DISTINCT t.id) FILTER (WHERE r.id IS NOT NULL)                  AS plutus_tx_count,
        COUNT(DISTINCT t.id)                                                  AS tx_total
      FROM block b
      JOIN tx t ON t.block_id = b.id
      LEFT JOIN redeemer r ON r.tx_id = t.id
      WHERE b.epoch_no IS NOT NULL
      GROUP BY b.epoch_no;
    """
    df = query_df(conn, sql)
    df["plutus_ratio"] = (df["plutus_tx_count"] / df["tx_total"]).where(df["tx_total"] > 0, 0.0)
    return df[["epoch_no", "plutus_tx_count", "plutus_ratio"]]


def fetch_epoch_mints(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    sql = """
      SELECT b.epoch_no AS epoch_no, COUNT(m.id) AS ma_mint_count
      FROM ma_tx_mint m
      JOIN tx t ON m.tx_id = t.id
      JOIN block b ON t.block_id = b.id
      WHERE b.epoch_no IS NOT NULL
      GROUP BY b.epoch_no;
    """
    return query_df(conn, sql)


def fetch_epoch_distinct_assets(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    """Per-epoch count of newly-minted distinct multi_assets. Slow on mainnet."""
    sql = """
      SELECT first_epoch AS epoch_no, COUNT(*) AS new_assets
      FROM (
        SELECT m.ident, MIN(b.epoch_no) AS first_epoch
        FROM ma_tx_mint m
        JOIN tx t  ON t.id = m.tx_id
        JOIN block b ON b.id = t.block_id
        WHERE b.epoch_no IS NOT NULL
        GROUP BY m.ident
      ) s
      GROUP BY first_epoch
      ORDER BY first_epoch;
    """
    return query_df(conn, sql)


def fetch_epoch_rewards(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    return query_df(
        conn,
        "SELECT earned_epoch AS epoch_no, COUNT(*) AS reward_count FROM reward GROUP BY earned_epoch;",
    )


def fetch_epoch_stakes(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    return query_df(
        conn,
        "SELECT epoch_no, COUNT(*) AS stake_count FROM epoch_stake GROUP BY epoch_no;",
    )


def fetch_epoch_conway(conn: psycopg2.extensions.connection) -> pd.DataFrame | None:
    have_vp = table_exists(conn, "voting_procedure")
    have_dr = table_exists(conn, "drep_registration")
    if not (have_vp or have_dr):
        return None

    df = pd.DataFrame({"epoch_no": pd.Series(dtype=int)})
    if have_vp:
        df_vp = query_df(
            conn,
            "SELECT b.epoch_no AS epoch_no, COUNT(vp.id) AS voting_count "
            "FROM voting_procedure vp JOIN tx t ON vp.tx_id = t.id "
            "JOIN block b ON t.block_id = b.id "
            "WHERE b.epoch_no IS NOT NULL GROUP BY b.epoch_no;",
        )
        df = df.merge(df_vp, on="epoch_no", how="outer")
    if have_dr:
        df_dr = query_df(
            conn,
            "SELECT b.epoch_no AS epoch_no, COUNT(dr.id) AS drep_reg_count "
            "FROM drep_registration dr JOIN tx t ON dr.tx_id = t.id "
            "JOIN block b ON t.block_id = b.id "
            "WHERE b.epoch_no IS NOT NULL GROUP BY b.epoch_no;",
        )
        df = df.merge(df_dr, on="epoch_no", how="outer")
    return df


def fetch_era_sync(conn: psycopg2.extensions.connection) -> pd.DataFrame:
    """Sync time aggregated by Cardano era.

    Era is derived from `epoch_param.protocol_major` - the protocol version
    that's *actually active* for each epoch (the ledger state) - not from
    `block.proto_major` which is per-block producer signaling and can advertise
    the next-upgrade version many epochs before the hard fork activates.

    The proto-to-era mapping is applied in Python via `era_for()` rather than
    a SQL CASE, so future eras can be added in one place (_common.ERA_BY_…).
    """
    sql = """
      WITH epoch_era AS (
        SELECT epoch_no, MAX(protocol_major) AS proto
        FROM epoch_param
        WHERE epoch_no IS NOT NULL AND protocol_major IS NOT NULL
        GROUP BY epoch_no
      )
      SELECT ee.epoch_no, ee.proto, est.seconds AS sync_secs
      FROM epoch_era ee
      LEFT JOIN epoch_sync_time est ON est.no = ee.epoch_no
      ORDER BY ee.epoch_no;
    """
    raw = query_df(conn, sql)
    if raw.empty:
        return pd.DataFrame(
            columns=[
                "era",
                "epoch_from",
                "epoch_to",
                "epochs",
                "total_sync_secs",
                "avg_sync_secs",
                "pct_of_total",
            ]
        )
    raw["era"] = raw["proto"].apply(era_for)
    raw["sync_secs"] = raw["sync_secs"].fillna(0).astype(float)
    grouped = (
        raw.groupby("era", sort=False)
        .agg(
            epoch_from=("epoch_no", "min"),
            epoch_to=("epoch_no", "max"),
            epochs=("epoch_no", "count"),
            total_sync_secs=("sync_secs", "sum"),
            avg_sync_secs=("sync_secs", "mean"),
        )
        .reset_index()
    )
    total = grouped["total_sync_secs"].sum()
    grouped["pct_of_total"] = (grouped["total_sync_secs"] / total * 100.0) if total else 0.0
    return grouped


def assemble_epoch_df(
    conn: psycopg2.extensions.connection,
    *,
    skip_slow: bool,
    with_p95: bool,
) -> pd.DataFrame:
    """Build the per-epoch DataFrame, optionally skipping expensive fetchers."""
    df = fetch_epoch_stats(conn, with_p95=with_p95)
    fetchers: list[Any] = [fetch_epoch_sync_secs, fetch_epoch_mints, fetch_epoch_rewards, fetch_epoch_stakes]
    if not skip_slow:
        fetchers[1:1] = [fetch_epoch_plutus]
        fetchers.insert(2, fetch_epoch_distinct_assets)
    for fetcher in fetchers:
        df = df.merge(fetcher(conn), on="epoch_no", how="left")
    conway = fetch_epoch_conway(conn)
    if conway is not None:
        df = df.merge(conway, on="epoch_no", how="left")
    df = df.sort_values("epoch_no").fillna(0).infer_objects(copy=False)
    if "new_assets" in df.columns:
        df["cumulative_assets"] = df["new_assets"].cumsum()
    return df


# --- DB / table / index sizes ---------------------------------------------


def fetch_db_size(conn: psycopg2.extensions.connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database());")
        return int(cur.fetchone()[0])


def fetch_table_sizes(conn: psycopg2.extensions.connection) -> list[tuple[str, int, int]]:
    with conn.cursor() as cur:
        cur.execute("""
          SELECT
            n.nspname || '.' || c.relname,
            pg_relation_size(c.oid)::bigint,
            pg_indexes_size(c.oid)::bigint
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE c.relkind = 'r'
            AND n.nspname NOT IN ('pg_catalog','information_schema')
          ORDER BY pg_total_relation_size(c.oid) DESC;
        """)
        return [(r[0], int(r[1]), int(r[2])) for r in cur.fetchall()]


def fetch_index_sizes(conn: psycopg2.extensions.connection) -> list[tuple[str, str, int]]:
    with conn.cursor() as cur:
        cur.execute("""
          SELECT
            ns.nspname || '.' || i.relname AS index_name,
            ns.nspname || '.' || t.relname AS table_name,
            pg_relation_size(i.oid)::bigint
          FROM pg_class i
          JOIN pg_index ix ON ix.indexrelid = i.oid
          JOIN pg_class t  ON t.oid = ix.indrelid
          JOIN pg_namespace ns ON ns.oid = i.relnamespace
          WHERE i.relkind = 'i'
            AND ns.nspname NOT IN ('pg_catalog','information_schema')
          ORDER BY pg_relation_size(i.oid) DESC;
        """)
        return [(r[0], r[1], int(r[2])) for r in cur.fetchall()]


def fetch_reltuples(conn: psycopg2.extensions.connection, tables: list[str]) -> dict[str, int]:
    """Cheap ANALYZE-driven row-count estimates from pg_class. Avoids COUNT(*)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relname, reltuples::bigint FROM pg_class WHERE relkind = 'r' AND relname = ANY(%s);",
            (tables,),
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}


def build_summary(
    conn: psycopg2.extensions.connection,
    db_size: int,
    tables: list[tuple[str, int, int]],
    indexes: list[tuple[str, str, int]],
    epoch_df: pd.DataFrame,
    era_df: pd.DataFrame,
) -> dict[str, Any]:
    """Mainnet-safe summary: derives most numbers from epoch_df aggregates
    (already computed) and pg_class.reltuples (instant). The only fresh queries
    are slot range (indexed two-hop), sync-time SUM (small table), and the UTXO
    count if tracking is on."""
    with conn.cursor() as cur:
        cur.execute("SELECT slot_no FROM block WHERE block_no IS NOT NULL ORDER BY block_no ASC LIMIT 1;")
        r = cur.fetchone()
        slot_min = int(r[0]) if r and r[0] is not None else None
        cur.execute("SELECT slot_no FROM block WHERE block_no IS NOT NULL ORDER BY block_no DESC LIMIT 1;")
        r = cur.fetchone()
        slot_max = int(r[0]) if r and r[0] is not None else None

    reltuples = fetch_reltuples(conn, ["ma_tx_mint", "multi_asset"])

    total_blocks = int(epoch_df["block_count"].sum()) if not epoch_df.empty else 0
    total_txs = int(epoch_df["tx_count"].sum()) if not epoch_df.empty else 0
    total_fees = int(epoch_df["total_fees"].sum()) if not epoch_df.empty else 0

    plutus_ratio_overall: float | None
    if "plutus_tx_count" in epoch_df.columns:
        plutus_tx = int(epoch_df["plutus_tx_count"].sum())
        plutus_ratio_overall = (plutus_tx / total_txs) if total_txs else 0.0
    else:
        plutus_ratio_overall = None

    total_sync_secs: float | None = None
    if table_exists(conn, "epoch_sync_time"):
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(seconds),0) FROM epoch_sync_time;")
            row = cur.fetchone()
            total_sync_secs = float(row[0]) if row and row[0] is not None else None

    utxo_tracking = utxo_tracking_enabled(conn)
    utxo_count: int | None = None
    if utxo_tracking:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tx_out WHERE consumed_by_tx_id IS NULL;")
            utxo_count = int(cur.fetchone()[0])

    all_tables = [(name, tb + ib) for name, tb, ib in tables]
    all_indexes = list(indexes)

    return {
        "total_sync_secs": total_sync_secs,
        "slot_min": slot_min,
        "slot_max": slot_max,
        "epoch_min": int(epoch_df["epoch_no"].min()) if not epoch_df.empty else None,
        "epoch_max": int(epoch_df["epoch_no"].max()) if not epoch_df.empty else None,
        "total_blocks": total_blocks,
        "total_txs": total_txs,
        "total_fees": total_fees,
        "total_mints": reltuples.get("ma_tx_mint", 0),
        "total_distinct_assets": reltuples.get("multi_asset", 0),
        "plutus_ratio_overall": plutus_ratio_overall,
        "db_size_bytes": db_size,
        "all_tables": all_tables,
        "all_indexes": all_indexes,
        "utxo_tracking": utxo_tracking,
        "utxo_count": utxo_count,
        "era_breakdown": era_df,
    }
