#!/usr/bin/env python3
"""Long-running collector for cardano-db-sync. Samples CPU/RAM, ingest metrics,
and hot-table row counts into a SQLite stats DB. Pure collector — no plotting,
no prompts. See db-sync-plot.py for visualization."""

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
from _common import (
    find_processes,
    format_bytes,
    format_duration_compact,
    format_size,
    get_cpu_details,
    get_memory_details,
    init_sqlite_schema,
    report_existing_history,
    utc_timestamp,
    warn,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cardano-db-sync"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Tables sampled for approximate row counts every interval.
# Uses pg_class.reltuples (cheap, ANALYZE-driven estimate) rather than COUNT(*).
HOT_TABLES: list[str] = [
    "block", "tx", "tx_out", "ma_tx_out", "ma_tx_mint",
    "multi_asset", "datum", "redeemer", "script",
]


class CardanoMonitor:
    def __init__(self, env: str, db_sync_ver: str, pg_host: str | None, pg_port: str | None,
                 pg_user: str | None, pg_dbname: str, interval: float, emit_json: bool,
                 match_arg: str | None = None) -> None:
        self.running: bool = True
        self.env: str = env
        self.db_sync_ver: str = db_sync_ver
        self.pg_host: str | None = pg_host
        self.pg_port: str | None = pg_port
        self.pg_user: str | None = pg_user
        self.pg_dbname: str = pg_dbname
        self.interval: float = interval
        self.emit_json: bool = emit_json
        self.run_label: str = f"cardano-db-sync {db_sync_ver} {env}"
        # Optional substring that must appear somewhere in the matched db-sync
        # process's command line (argv[0] + arguments). Used to disambiguate
        # when multiple cardano-db-sync instances run on one host (e.g. LSM
        # vs in-memory). None = no extra filter (default).
        self.match_arg: str | None = match_arg
        # UTXO probe state.
        self.utxo_tracking: bool = False
        self.utxo_probe_settled: bool = False
        self._utxo_timeout_warned: bool = False
        self._utxo_other_error_warned: bool = False
        # Cached at startup; never changes during a sync, so don't re-fetch.
        self.first_block_time: datetime | None = None
        # Long-lived autocommit connection used by steady-state sample queries
        # (get_tip / get_ingest_metrics / get_table_rowcounts / get_first_block_time
        # on the retry path). Set lazily by _ensure_loop_conn(); dropped and
        # re-opened on the next sample if a connection-level error occurs.
        # Setup-phase queries (wait_for_schema, detect_utxo_tracking) keep
        # their own short-lived connections — wait_for_schema runs before
        # the loop, and detect_utxo_tracking uses SET LOCAL statement_timeout
        # which requires an explicit transaction (incompatible with autocommit).
        self._loop_conn: psycopg2.extensions.connection | None = None

        self.db_file: str = str(DATA_DIR / f"{self.env}.db")

        self.init_db()

    def _connect(self, **kwargs: Any) -> psycopg2.extensions.connection:
        """Raw psycopg2.connect with our pg_* settings + caller overrides.

        Honors PGHOST/PGPORT/PGUSER/PGPASSWORD env vars when the CLI args are
        unset (None) — same convention as `psql`. Always supplies `dbname`
        because there's no PGDATABASE convention that fits multi-version A/B.
        """
        conn_kwargs: dict[str, Any] = {"dbname": self.pg_dbname}
        if self.pg_host is not None:
            conn_kwargs["host"] = self.pg_host
        if self.pg_port is not None:
            conn_kwargs["port"] = self.pg_port
        if self.pg_user is not None:
            conn_kwargs["user"] = self.pg_user
        conn_kwargs.update(kwargs)
        return psycopg2.connect(**conn_kwargs)

    @contextmanager
    def _pg(self, **kwargs: Any) -> Iterator[psycopg2.extensions.connection]:
        """Short-lived context-managed connection (closes on exit).

        Used by setup-phase queries — wait_for_schema (polls before postgres
        may be ready) and detect_utxo_tracking (uses SET LOCAL which requires
        an explicit transaction). For the steady-state sample loop, use
        _ensure_loop_conn() instead so we don't open a fresh TCP connection
        every 10 seconds for the lifetime of the sync.
        """
        conn = self._connect(**kwargs)
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_loop_conn(self) -> psycopg2.extensions.connection:
        """Return a live autocommit connection for the sample loop.

        Reused across the four steady-state query methods (get_tip,
        get_first_block_time, get_ingest_metrics, get_table_rowcounts) so the
        monitor doesn't open ~3-4 new TCP connections per sample. Opened
        lazily on first call; reopened automatically if the connection has
        been dropped (e.g. postgres restart, network blip) — the
        connection-level exception handlers in each query call
        `_drop_loop_conn()`, and the next sample's call here re-opens.

        Autocommit is on so each query commits immediately and doesn't hold a
        long-running transaction across the loop — important to let postgres
        clean up dead tuples (vacuum) without waiting on the monitor's
        snapshot.
        """
        if self._loop_conn is None or self._loop_conn.closed:
            self._loop_conn = self._connect()
            self._loop_conn.autocommit = True
        return self._loop_conn

    def _drop_loop_conn(self) -> None:
        """Force the loop connection to be reopened on next access.

        Called after a connection-level error (OperationalError /
        InterfaceError). Idempotent; safe to call when no conn is open.
        """
        if self._loop_conn is not None:
            try:
                self._loop_conn.close()
            except Exception:
                pass
            self._loop_conn = None

    def init_db(self) -> None:
        init_sqlite_schema(self.db_file, version_table="db_sync_version")
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                """CREATE TABLE IF NOT EXISTS ingest_metrics
                   (ts TEXT, slot_no INTEGER, version TEXT, tip_lag_sec REAL,
                    db_size_bytes INTEGER, max_block_no INTEGER, max_tx_id INTEGER,
                    utxo_count INTEGER)"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS table_rowcounts
                   (ts TEXT, slot_no INTEGER, version TEXT, table_name TEXT, row_count INTEGER)"""
            )
            conn.commit()

    def _match_db_sync_process(self, proc: Any) -> bool:
        """True if `proc` looks like a cardano-db-sync process.

        Criteria:
          - executable name (argv[0] basename if available, else `proc.name()`)
            starts with `cardano-db-sync`.
          - if `self.match_arg` is set, the substring must additionally appear
            anywhere in the full command line (argv[0] including its path +
            all arguments). Used to pick a specific instance when multiple
            db-sync runs share a host (LSM vs in-memory, etc.).

        Note: db-sync doesn't currently encode env-name in argv consistently
        (different distributions, configs, and --pg-dbname schemes vary), so
        unlike node-monitor we don't have a built-in env filter here. The
        --pg-dbname is what scopes db-sync to its postgres database;
        --match-arg is the explicit handle for picking one of multiple
        co-located instances.
        """
        cmdline = proc.info.get("cmdline") or []
        if cmdline:
            exe_base = os.path.basename(cmdline[0])
        else:
            exe_base = proc.info.get("name") or ""
        if not exe_base.startswith("cardano-db-sync"):
            return False
        if self.match_arg is not None:
            return any(self.match_arg in arg for arg in cmdline)
        return True

    def get_process(self) -> Any:
        matches = find_processes(self._match_db_sync_process)
        if len(matches) > 1:
            pids = ", ".join(str(p.pid) for p in matches)
            warn(
                f"Multiple cardano-db-sync processes match: {pids}. Using PID {matches[0].pid}. "
                "Pass --match-arg to disambiguate (substring of argv to require)."
            )
        return matches[0] if matches else None

    def get_tip(self) -> tuple[int, int | None, int | None, datetime | None] | None:
        """Return (slot_no, epoch_no, block_no, time) of the latest block.

        Uses the indexed reverse scan on block_no — fast on any size DB.
        Uses the shared loop connection (autocommit); on a connection-level
        error, drops it so the next sample reopens.
        """
        try:
            conn = self._ensure_loop_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT slot_no, epoch_no, block_no, time FROM block "
                    "WHERE block_no IS NOT NULL ORDER BY block_no DESC LIMIT 1;"
                )
                r = cur.fetchone()
            return (r[0], r[1], r[2], r[3]) if r else None
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            warn(f"Postgres connection lost during get_tip ({e}); will reconnect next sample.")
            self._drop_loop_conn()
            return None
        except Exception as e:
            warn(f"Postgres error: {e}")
            return None

    def get_first_block_time(self) -> datetime | None:
        """One-time fetch: timestamp of the earliest non-NULL-block_no block.

        Used to compute sync_percent without a seq scan over block.time on every
        sample. The first block's time is constant for the lifetime of a sync,
        so we cache it on the instance.
        """
        try:
            conn = self._ensure_loop_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT time FROM block WHERE block_no IS NOT NULL "
                    "ORDER BY block_no ASC LIMIT 1;"
                )
                r = cur.fetchone()
            return r[0] if r else None
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            warn(f"Postgres connection lost during get_first_block_time ({e}); will reconnect next sample.")
            self._drop_loop_conn()
            return None
        except Exception as e:
            warn(f"First-block fetch error: {e}")
            return None

    def compute_sync_percent(self, tip_time: datetime | None) -> float | None:
        if not self.first_block_time or not tip_time:
            return None
        fb = utc_timestamp(self.first_block_time)
        tip = utc_timestamp(tip_time)
        now = time.time()
        if now <= fb:
            return None
        return 100.0 * (tip - fb) / (now - fb)

    def compute_tip_lag(self, tip_time: datetime | None) -> float | None:
        if not tip_time:
            return None
        return time.time() - utc_timestamp(tip_time)

    def wait_for_schema(self) -> None:
        """Block until db-sync's schema migration has created the `block` table.

        Eliminates the noisy 'relation does not exist' errors during the first
        few iterations of a fresh sync. Polls every 2s; respects SIGINT/SIGTERM.
        """
        poll = 2.0
        notified = False
        waited = 0.0
        while self.running:
            try:
                with self._pg() as conn, conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('block') IS NOT NULL;")
                    row = cur.fetchone()
                    if row and row[0]:
                        if notified:
                            print(f"block table is now present (waited {waited:.0f}s).")
                        return
            except Exception:
                pass  # postgres itself may not be ready yet; keep polling silently.
            if not notified:
                print("Waiting for db-sync schema migration (block table not yet present)…")
                notified = True
            time.sleep(poll)
            waited += poll

    def detect_utxo_tracking(self) -> None:
        """Update self.utxo_tracking / self.utxo_probe_settled.

        Verdict is definitive only once tx_out has rows. db-sync populates
        consumed_by_tx_id at insert time, not retroactively — so if tx_out
        has any rows and none of them have the marker, tracking is
        permanently off for this run. Until tx_out has rows, we keep probing.
        """
        if self.utxo_probe_settled:
            return
        try:
            with self._pg(options="-c statement_timeout=5000") as conn, conn.cursor() as cur:
                cur.execute("SELECT EXISTS (SELECT 1 FROM tx_out);")
                row = cur.fetchone()
                tx_out_has_rows = bool(row and row[0])
                if not tx_out_has_rows:
                    return  # not yet definitive; try again next sample.
                cur.execute("SELECT EXISTS (SELECT 1 FROM tx_out WHERE consumed_by_tx_id IS NOT NULL);")
                row = cur.fetchone()
                self.utxo_tracking = bool(row and row[0])
                self.utxo_probe_settled = True
                if self.utxo_tracking:
                    print("UTXO tracking: ENABLED (tx_out.consumed_by_tx_id is populated).")
                else:
                    print(
                        "UTXO tracking: confirmed DISABLED (tx_out has rows but no "
                        "consumed_by_tx_id markers). utxo_count will not be sampled."
                    )
        except psycopg2.errors.QueryCanceled:
            if not self._utxo_timeout_warned:
                warn(
                    "UTXO tracking probe timed out (>5s). Will keep retrying silently. "
                    "If tracking IS enabled, ensure tx_out.consumed_by_tx_id has an index."
                )
                self._utxo_timeout_warned = True
        except Exception as e:
            msg = str(e)
            if "does not exist" in msg:
                return  # tx_out not created yet; the next sample will retry.
            if not self._utxo_other_error_warned:
                warn(f"UTXO tracking detection error: {e}")
                self._utxo_other_error_warned = True

    def get_ingest_metrics(self) -> dict[str, Any] | None:
        """DB size, max tx id, UTXO count if enabled.

        Tip lag and max_block_no are derived from get_tip()'s return value in
        run() — no extra query needed here. Keeping this method to one or two
        cheap queries is what makes mainnet-safe sampling possible.
        """
        try:
            conn = self._ensure_loop_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_database_size(current_database()), (SELECT MAX(id) FROM tx);"
                )
                row = cur.fetchone()
                result: dict[str, Any] = {
                    "db_size_bytes": int(row[0]) if row and row[0] is not None else None,
                    "max_tx_id": int(row[1]) if row and row[1] is not None else None,
                    "utxo_count": None,
                }
                if self.utxo_tracking:
                    cur.execute("SELECT COUNT(*) FROM tx_out WHERE consumed_by_tx_id IS NULL;")
                    r = cur.fetchone()
                    result["utxo_count"] = int(r[0]) if r and r[0] is not None else None
            return result
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            warn(f"Postgres connection lost during get_ingest_metrics ({e}); will reconnect next sample.")
            self._drop_loop_conn()
            return None
        except Exception as e:
            warn(f"ingest metrics error: {e}")
            return None

    def get_table_rowcounts(self) -> dict[str, int] | None:
        """Approximate live row counts (pg_class.reltuples) for HOT_TABLES."""
        try:
            conn = self._ensure_loop_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT relname, reltuples::bigint FROM pg_class "
                    "WHERE relkind = 'r' AND relname = ANY(%s);",
                    (HOT_TABLES,),
                )
                return {row[0]: int(row[1]) for row in cur.fetchall()}
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            warn(f"Postgres connection lost during get_table_rowcounts ({e}); will reconnect next sample.")
            self._drop_loop_conn()
            return None
        except Exception as e:
            warn(f"table rowcounts error: {e}")
            return None

    def stop(self, *_args: Any) -> None:
        self.running = False

    def emit_sample(self, *, ts: str, slot: int, epoch: int | None, sync_progress: float | None,
                    tip_lag_sec: float | None, mem: dict[str, float] | None,
                    cpu: dict[str, Any] | None, ingest: dict[str, Any] | None) -> None:
        """Format a single sample for stdout (json or pipe-separated)."""
        if self.emit_json:
            line = {
                "ts": ts,
                "env": self.env,
                "label": self.db_sync_ver,
                "version": self.run_label,
                "slot_no": slot,
                "epoch_no": epoch,
                "sync_percent": sync_progress,
                "tip_lag_sec": tip_lag_sec,
                "db_size_bytes": ingest["db_size_bytes"] if ingest else None,
                "max_tx_id": ingest["max_tx_id"] if ingest else None,
                "utxo_count": ingest["utxo_count"] if ingest else None,
                "cpu_percent": cpu["cpu_percent"] if cpu else None,
                "rss_mb": mem["rss"] if mem else None,
                "vms_mb": mem["vms"] if mem else None,
            }
            print(json.dumps(line))
            return
        epoch_str = str(epoch) if epoch is not None else "N/A"
        sync_str = f"{sync_progress:.2f}%" if sync_progress is not None else "N/A"
        cpu_str = f"{cpu['cpu_percent']:.1f}%" if cpu else "N/A"
        rss_str = format_size(mem["rss"] if mem else None)
        tip_str = format_duration_compact(tip_lag_sec)
        db_str = format_bytes(ingest["db_size_bytes"] if ingest else None)
        print(
            f"Slot {slot} | Epoch {epoch_str} | Sync {sync_str} | "
            f"TipLag {tip_str} | DB {db_str} | CPU {cpu_str} | RSS {rss_str}"
        )

    def run(self) -> None:
        print(
            f"=== db-sync monitor | env={self.env} | label={self.db_sync_ver} | "
            f"pg_db={self.pg_dbname} | interval={self.interval}s"
            + (" | output=json" if self.emit_json else "")
            + " ==="
        )
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        report_existing_history(self.db_file, "db_sync_version", self.run_label)
        self.wait_for_schema()
        if not self.running:
            print("Shutdown requested before schema was ready.")
            return

        self.detect_utxo_tracking()
        if not self.utxo_probe_settled:
            print("UTXO tracking: tx_out is still empty; will re-probe each sample until definitive.")

        self.first_block_time = self.get_first_block_time()
        if self.first_block_time is None:
            print("First-block timestamp not yet available; sync_percent will be N/A until a block lands.")

        proc = self.get_process()
        if proc:
            try:
                proc.cpu_percent(interval=None)  # prime the cpu_percent sampler
            except Exception:
                pass

        rows = 0
        while self.running:
            self.detect_utxo_tracking()
            if self.first_block_time is None:
                self.first_block_time = self.get_first_block_time()

            tip = self.get_tip()
            if tip is None:
                time.sleep(self.interval)
                continue
            slot, epoch, max_block_no, tip_time = tip
            tip_lag_sec = self.compute_tip_lag(tip_time)
            sync_progress = self.compute_sync_percent(tip_time)

            proc = self.get_process()
            mem = get_memory_details(proc) if proc else None
            cpu = get_cpu_details(proc) if proc else None
            ingest = self.get_ingest_metrics()
            tbl_counts = self.get_table_rowcounts()
            ts = datetime.now().isoformat()

            with sqlite3.connect(self.db_file) as conn:
                if mem:
                    conn.execute(
                        "INSERT INTO memory_metrics (ts, slot_no, rss, vms, uss, pss, swap, shared, version) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (ts, slot, mem["rss"], mem["vms"], mem["uss"],
                         mem["pss"], mem["swap"], mem["shared"], self.run_label),
                    )
                if cpu:
                    conn.execute(
                        "INSERT INTO cpu_metrics (ts, slot_no, cpu_percent, user_time, system_time, "
                        "children_user, children_system, iowait, ctx_switches, interrupts, version) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (ts, slot, cpu["cpu_percent"], cpu["user_time"], cpu["system_time"],
                         cpu["children_user"], cpu["children_system"],
                         cpu["iowait"], cpu["ctx_switches"], cpu["interrupts"], self.run_label),
                    )
                conn.execute(
                    "INSERT INTO db_sync_version VALUES (?,?)",
                    (ts, self.run_label),
                )
                if ingest:
                    conn.execute(
                        "INSERT INTO ingest_metrics (ts, slot_no, version, tip_lag_sec, "
                        "db_size_bytes, max_block_no, max_tx_id, utxo_count) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (ts, slot, self.run_label, tip_lag_sec, ingest["db_size_bytes"],
                         max_block_no, ingest["max_tx_id"], ingest["utxo_count"]),
                    )
                if tbl_counts:
                    conn.executemany(
                        "INSERT INTO table_rowcounts (ts, slot_no, version, table_name, row_count) "
                        "VALUES (?,?,?,?,?)",
                        [(ts, slot, self.run_label, name, count) for name, count in tbl_counts.items()],
                    )

            rows += 1
            self.emit_sample(
                ts=ts, slot=slot, epoch=epoch, sync_progress=sync_progress,
                tip_lag_sec=tip_lag_sec, mem=mem, cpu=cpu, ingest=ingest,
            )
            time.sleep(self.interval)

        # mypy doesn't see signal handlers mutating self.running, so it thinks
        # the loop above is infinite. The lines below ARE reached on SIGINT/SIGTERM.
        self._drop_loop_conn()  # type: ignore[unreachable]
        print(f"Shutting down. Wrote {rows} samples to {self.db_file}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cardano DB-Sync resources monitor (collector)")
    parser.add_argument("--env",
                        required=True,
                        choices=["mainnet", "preprod", "preview"],
                        help="Environment name")
    parser.add_argument("--db-sync-ver",
                        required=True,
                        help="Label used to tag this run in the DB (e.g. 13.6.0.5, 13.6.0.5-hasql)")
    parser.add_argument("--pg-host",
                        default=None,
                        help="Postgres host (default: PGHOST env var, then 'localhost')")
    parser.add_argument("--pg-port",
                        default=None,
                        help="Postgres port (default: PGPORT env var, then 5432)")
    parser.add_argument("--pg-user",
                        default=None,
                        help="Postgres user (default: PGUSER env var, then current user)")
    parser.add_argument("--pg-dbname",
                        required=True,
                        help="Postgres database name that cardano-db-sync writes to")
    parser.add_argument("--interval",
                        type=float,
                        default=10.0,
                        help="Sampling interval in seconds (default: 10)")
    parser.add_argument("--json",
                        dest="emit_json",
                        action="store_true",
                        help="Emit one JSON object per sample on stdout (instead of the "
                             "human-readable pipe-separated form). Each line includes env, "
                             "label, and version fields so it parses in isolation.")
    parser.add_argument("--match-arg",
                        default=None,
                        help="Additional substring required to appear somewhere in the matched "
                             "cardano-db-sync process's command line (argv[0] including path + "
                             "any argument). Use to disambiguate when multiple instances run on "
                             "one host (e.g. --match-arg lsm vs --match-arg inmem). If unset, "
                             "the first cardano-db-sync process found is used.")
    return parser.parse_args()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    args = parse_args()

    monitor = CardanoMonitor(
        env=args.env,
        db_sync_ver=args.db_sync_ver,
        pg_host=args.pg_host,
        pg_port=args.pg_port,
        pg_user=args.pg_user,
        pg_dbname=args.pg_dbname,
        interval=args.interval,
        emit_json=args.emit_json,
        match_arg=args.match_arg,
    )
    monitor.run()
