#!/usr/bin/env python3
"""Passive collector for cardano-db-sync rollback performance.

Scrapes the db-sync Prometheus endpoint (default port 8080) every interval and
appends the rollback-relevant gauges - queue length and the db/node tip heights -
to `rollback_samples` in `data/cardano-db-sync/<env>.db`, under the same version
label db-sync-resource-monitor.py uses, so the rollback series joins the rest of
the run. A rollback shows up as `db_block_height` going *backward*; recovery is
the db tip climbing back to the node tip. The queue-length spike and the tip gap
are exactly the signals operators watch in Grafana ("Queue Length", "Rollback
Recovery Times").

If `--log-file` is given, the db-sync log is tailed in parallel for the
authoritative deletion-phase signals: the `Rollback: Deleting N blocks ...`
start line, the per-table delete summary, and the `Successfully deleted ...` /
`... rollback completed successfully` end line. Each completed rollback is
written to `rollback_events` (source='log') with the exact deletion duration,
and its per-table counts to `rollback_table_deletes`. Without a log file the
monitor still records `rollback_samples`; events are then derived from the tip
series at plot/report time (see _rollback.derive_events).

This collector ONLY creates and INSERTs into its own three tables; it never
writes the existing sync-monitoring tables. WAL + connect_writer make sharing the
per-env DB with the other collectors safe. Pure collector - no plotting, no
prompts. Stops cleanly on SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _common import connect_writer, fetch_prometheus_text, warn
from _rollback import (
    M_DB_BLOCK_HEIGHT,
    M_DB_SLOT_HEIGHT,
    M_NODE_BLOCK_HEIGHT,
    M_QUEUE_LENGTH,
    RollbackLogEnd,
    RollbackLogStart,
    RollbackLogTableDelete,
    extract_log_timestamp,
    parse_db_sync_metrics,
    parse_rollback_log_line,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cardano-db-sync"

# db-sync's EKG-based Prometheus server exposes the gauges at the ROOT path, not
# /metrics (verified live against db-sync 13.7.2.1; the node's endpoint differs
# and uses /metrics). Point at the root by default.
DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:8080/"


def _as_int(metrics: dict[str, float], key: str) -> int | None:
    val = metrics.get(key)
    return int(val) if val is not None else None


def _iso(posix: float) -> str:
    return datetime.fromtimestamp(posix, tz=timezone.utc).isoformat()


class DbSyncRollbackMonitor:
    def __init__(
        self,
        env: str,
        db_sync_ver: str,
        url: str,
        log_file: str | None,
        interval: float,
        timeout: float,
        emit_json: bool,
        sqlite_db: str | None,
    ) -> None:
        self.running: bool = True
        self.env: str = env
        self.db_sync_ver: str = db_sync_ver
        self.url: str = url
        self.log_file: str | None = log_file
        self.interval: float = interval
        self.timeout: float = timeout
        self.emit_json: bool = emit_json
        self.run_label: str = f"cardano-db-sync {db_sync_ver} {env}"
        self.db_file: str = sqlite_db or str(DATA_DIR / f"{env}.db")
        Path(self.db_file).parent.mkdir(parents=True, exist_ok=True)
        # Log-tail state.
        self._log_pos: int = 0
        self._log_buf: str = ""
        self._pending: RollbackLogStart | None = None
        self._pending_ts: float | None = None
        self._pending_tables: list[RollbackLogTableDelete] = []
        self.init_db()

    def init_db(self) -> None:
        with connect_writer(self.db_file) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rollback_samples
                   (ts TEXT, slot_no INTEGER, version TEXT,
                    db_block_height INTEGER, db_slot_height INTEGER,
                    node_block_height INTEGER, queue_length REAL)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rollback_events
                   (ts TEXT, slot_no INTEGER, version TEXT,
                    event_start_ts TEXT, event_end_ts TEXT,
                    from_slot INTEGER, to_slot INTEGER,
                    depth_slots INTEGER, depth_blocks INTEGER,
                    delete_duration_sec REAL, recovery_duration_sec REAL,
                    max_queue_len REAL, peak_cpu_percent REAL, peak_rss_mib REAL,
                    source TEXT)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rollback_table_deletes
                   (ts TEXT, version TEXT, event_start_ts TEXT,
                    table_name TEXT, deleted_rows INTEGER)"""
            )
            conn.commit()

    def report_existing(self) -> None:
        try:
            with sqlite3.connect(self.db_file) as conn:
                row = conn.execute(
                    "SELECT COUNT(*), MIN(ts), MAX(ts) FROM rollback_samples WHERE version = ?",
                    (self.run_label,),
                ).fetchone()
                events = conn.execute(
                    "SELECT COUNT(*) FROM rollback_events WHERE version = ?",
                    (self.run_label,),
                ).fetchone()
        except Exception as e:
            warn(f"Startup history check failed: {e}")
            return
        count = (row or [0])[0] or 0
        ev_count = (events or [0])[0] or 0
        if count == 0:
            print(f"This version label has no existing rollback samples in {self.db_file}.")
            self._warn_if_label_looks_mistyped()
        else:
            print(
                f"Note: this version label already has {count:,} rollback samples and "
                f"{ev_count:,} recorded events in {self.db_file} (first {row[1]}, last {row[2]}). "
                "New samples will be appended."
            )

    def _warn_if_label_looks_mistyped(self) -> None:
        try:
            with sqlite3.connect(self.db_file) as conn:
                others = [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT version FROM rollback_samples WHERE version <> ?",
                        (self.run_label,),
                    ).fetchall()
                ]
        except Exception:
            return
        if others:
            warn(
                f"'{self.run_label}' is a new label; this DB already has: "
                f"{', '.join(others)}. If that's a typo, stop and re-run with the "
                "matching --db-sync-ver (otherwise the plot won't group these together)."
            )

    def record_sample(self, text: str) -> tuple[str, dict[str, float]] | None:
        """Parse a scrape, store one rollback_samples row, return (ts, metrics).
        None when the scrape held none of the db-sync gauges (endpoint reachable
        but not db-sync's, or metrics not yet populated)."""
        metrics = parse_db_sync_metrics(text)
        if not metrics:
            return None
        db_slot = _as_int(metrics, M_DB_SLOT_HEIGHT)
        ts = datetime.now(tz=timezone.utc).isoformat()
        row = (
            ts,
            db_slot,
            self.run_label,
            _as_int(metrics, M_DB_BLOCK_HEIGHT),
            db_slot,
            _as_int(metrics, M_NODE_BLOCK_HEIGHT),
            metrics.get(M_QUEUE_LENGTH),
        )
        with connect_writer(self.db_file) as conn:
            conn.execute(
                "INSERT INTO rollback_samples "
                "(ts, slot_no, version, db_block_height, db_slot_height, node_block_height, queue_length) "
                "VALUES (?,?,?,?,?,?,?)",
                row,
            )
        return ts, metrics

    def consume_log_text(self, text: str, now: float | None = None) -> int:
        """Process newly-read log text (may contain many lines / a partial tail).
        Returns the number of completed rollback events written. The trailing
        partial line is buffered for the next read."""
        self._log_buf += text
        lines = self._log_buf.split("\n")
        self._log_buf = lines.pop()  # keep the (possibly partial) last fragment
        written = 0
        for line in lines:
            written += self._process_log_line(line, now)
        return written

    def _process_log_line(self, line: str, now: float | None) -> int:
        rec = parse_rollback_log_line(line)
        if rec is None:
            return 0
        line_ts = extract_log_timestamp(line)
        if line_ts is None:
            line_ts = now if now is not None else time.time()
        if isinstance(rec, RollbackLogStart):
            # A new start supersedes any unfinished pending event (db-sync emits
            # the end line; a missing end means we never saw it - e.g. the log
            # rotated mid-rollback). Warn so dropped events aren't silent.
            if self._pending is not None:
                warn("Saw a new rollback start before the previous one's end line; dropping the unterminated event.")
            self._pending = rec
            self._pending_ts = line_ts
            self._pending_tables = []
            return 0
        if isinstance(rec, RollbackLogTableDelete):
            if self._pending is not None:
                self._pending_tables.append(rec)
            return 0
        if isinstance(rec, RollbackLogEnd):
            return self._finalize_event(rec, line_ts)
        return 0

    def _finalize_event(self, end: RollbackLogEnd, end_ts: float) -> int:
        if self._pending is None or self._pending_ts is None:
            return 0
        start = self._pending
        start_ts = self._pending_ts
        depth_blocks = start.blocks if start.blocks is not None else end.blocks
        duration = max(0.0, end_ts - start_ts)
        start_iso, end_iso = _iso(start_ts), _iso(end_ts)
        # The start line's "slot >= S" is the deletion floor: blocks at slot >= S
        # are removed, so S is the slot rolled back TO (the post-rollback tip
        # floor), not the pre-rollback tip. Record it as to_slot to keep the
        # column's meaning consistent with the metrics-derived events (to = the
        # lower/target slot). The log doesn't carry the pre-rollback tip, so
        # from_slot stays NULL.
        to_slot = start.slot_no
        with connect_writer(self.db_file) as conn:
            conn.execute(
                "INSERT INTO rollback_events "
                "(ts, slot_no, version, event_start_ts, event_end_ts, from_slot, to_slot, "
                "depth_slots, depth_blocks, delete_duration_sec, recovery_duration_sec, "
                "max_queue_len, peak_cpu_percent, peak_rss_mib, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    end_iso,
                    to_slot,
                    self.run_label,
                    start_iso,
                    end_iso,
                    None,
                    to_slot,
                    None,
                    depth_blocks,
                    duration,
                    None,
                    None,
                    None,
                    None,
                    "log",
                ),
            )
            if self._pending_tables:
                conn.executemany(
                    "INSERT INTO rollback_table_deletes "
                    "(ts, version, event_start_ts, table_name, deleted_rows) VALUES (?,?,?,?,?)",
                    [(end_iso, self.run_label, start_iso, t.table_name, t.deleted_rows) for t in self._pending_tables],
                )
        self._pending = None
        self._pending_ts = None
        self._pending_tables = []
        if not self.emit_json:
            depth_str = f"{depth_blocks} blocks" if depth_blocks is not None else "unknown depth"
            print(f"Rollback recorded: {depth_str}, deletion took {duration:.1f}s")
        return 1

    def poll_log(self) -> int:
        """Read any new bytes appended to the log file since last poll. Handles
        truncation/rotation by resetting to the start when the file shrinks."""
        if self.log_file is None:
            return 0
        try:
            size = Path(self.log_file).stat().st_size
            if size < self._log_pos:  # rotated/truncated
                self._log_pos = 0
                self._log_buf = ""
            if size == self._log_pos:
                return 0
            with open(self.log_file, encoding="utf-8", errors="replace") as f:
                f.seek(self._log_pos)
                chunk = f.read()
                self._log_pos = f.tell()
        except OSError as e:
            warn(f"Log read failed for {self.log_file}: {e}")
            return 0
        return self.consume_log_text(chunk)

    def emit(self, ts: str, metrics: dict[str, float]) -> None:
        db_blk = _as_int(metrics, M_DB_BLOCK_HEIGHT)
        node_blk = _as_int(metrics, M_NODE_BLOCK_HEIGHT)
        queue = metrics.get(M_QUEUE_LENGTH)
        gap = node_blk - db_blk if db_blk is not None and node_blk is not None else None
        if self.emit_json:
            print(
                json.dumps(
                    {
                        "ts": ts,
                        "env": self.env,
                        "label": self.db_sync_ver,
                        "version": self.run_label,
                        "db_block_height": db_blk,
                        "node_block_height": node_blk,
                        "block_gap": gap,
                        "queue_length": queue,
                    }
                )
            )
        else:
            gap_str = str(gap) if gap is not None else "N/A"
            q_str = f"{queue:.0f}" if queue is not None else "N/A"
            print(f"db_block={db_blk} node_block={node_blk} gap={gap_str} queue={q_str}")

    def stop(self, *_args: Any) -> None:
        self.running = False

    def _sleep(self) -> None:
        end = time.monotonic() + self.interval
        while self.running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    def run(self) -> None:
        print(
            f"=== cardano-db-sync rollback monitor | env={self.env} | label={self.db_sync_ver} | "
            f"url={self.url} | interval={self.interval:.0f}s"
            + (f" | log={self.log_file}" if self.log_file else " | log=off")
            + (" | output=json" if self.emit_json else "")
            + " ==="
        )
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.report_existing()
        if self.log_file:
            # Start tailing from the current end so we don't replay old rollbacks.
            try:
                self._log_pos = Path(self.log_file).stat().st_size
            except OSError:
                self._log_pos = 0

        samples = 0
        events = 0
        warned_empty = False
        while self.running:
            text = fetch_prometheus_text(self.url, self.timeout)
            if text is not None:
                try:
                    result = self.record_sample(text)
                except sqlite3.OperationalError as e:
                    warn(f"DB busy, dropped sample: {e}")
                    result = None
                if result is None:
                    if not warned_empty:
                        warn(
                            "Endpoint reachable but no db-sync metrics found - is this db-sync's "
                            "Prometheus port (default 8080), and is `PrometheusPort` enabled in its config?"
                        )
                        warned_empty = True
                else:
                    samples += 1
                    self.emit(*result)
            try:
                events += self.poll_log()
            except sqlite3.OperationalError as e:
                warn(f"DB busy, dropped log batch: {e}")
            self._sleep()

        print(f"Shutting down. Wrote {samples} samples and {events} rollback events to {self.db_file}.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="cardano-db-sync rollback performance monitor (Prometheus + log)")
    p.add_argument(
        "--env",
        required=True,
        choices=["mainnet", "preprod", "preview"],
        help="Environment name (selects data/cardano-db-sync/<env>.db and the version label)",
    )
    p.add_argument(
        "--db-sync-ver",
        required=True,
        help="Label tagging this run (e.g. 13.7.1.0-node-11.0.1). Match the --db-sync-ver "
        "you gave db-sync-resource-monitor so the rollback series joins the rest of the run.",
    )
    p.add_argument(
        "--prometheus-url",
        default=DEFAULT_PROMETHEUS_URL,
        help=f"db-sync Prometheus endpoint (default: {DEFAULT_PROMETHEUS_URL}). Set via the "
        "db-sync config `PrometheusPort` field (default port 8080).",
    )
    p.add_argument(
        "--log-file",
        default=None,
        help="Optional path to the db-sync log. When given, the deletion phase is timed "
        "from the log's own signals and per-table delete counts are recorded. Without it, "
        "events are derived from the tip series at plot time.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Sampling interval in seconds (default: 2 - short, so a fast rollback's tip "
        "regression and queue spike aren't missed).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-scrape HTTP timeout in seconds (default: 5). On timeout the sample is skipped.",
    )
    p.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit one JSON object per sample on stdout instead of the human form.",
    )
    p.add_argument(
        "--sqlite-db", default=None, help="Override the SQLite DB path (default: data/cardano-db-sync/<env>.db)."
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined,union-attr]
    args = parse_args()
    monitor = DbSyncRollbackMonitor(
        env=args.env,
        db_sync_ver=args.db_sync_ver,
        url=args.prometheus_url,
        log_file=args.log_file,
        interval=args.interval,
        timeout=args.timeout,
        emit_json=args.emit_json,
        sqlite_db=args.sqlite_db,
    )
    monitor.run()
