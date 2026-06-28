"""Tests for the passive rollback collector (scripts/db-sync-rollback-monitor.py).

Covers schema creation, Prometheus-sample insertion, the version-label
invariant, and the log-tailing state machine that turns start/table/end log
lines into rollback_events + rollback_table_deletes rows. Uses real SQLite DBs
under tmp_path (no mocks) and a hyphenated-script import shim like the other
monitor tests.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("db_sync_rollback_monitor", SCRIPTS / "db-sync-rollback-monitor.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rbm = _load_module()


def _monitor(db_file: str, log_file: str | None = None):  # type: ignore[no-untyped-def]
    return rbm.DbSyncRollbackMonitor(
        env="preprod",
        db_sync_ver="13.7.1.0",
        url="http://127.0.0.1:8080/metrics",
        log_file=log_file,
        interval=2.0,
        timeout=5.0,
        emit_json=False,
        sqlite_db=db_file,
    )


SCRAPE = (
    "cardano_db_sync_db_queue_length 4\n"
    "cardano_db_sync_db_block_height 10500000\n"
    "cardano_db_sync_db_slot_height 90000000\n"
    "cardano_db_sync_node_block_height 10500003\n"
)


class TestSchema:
    def test_init_creates_three_tables(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        _monitor(db)
        with sqlite3.connect(db) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"rollback_samples", "rollback_events", "rollback_table_deletes"} <= names

    def test_registered_in_version_keyed_tables(self) -> None:
        from _common import VERSION_KEYED_TABLES

        db_sync = VERSION_KEYED_TABLES["db-sync"]
        assert {"rollback_samples", "rollback_events", "rollback_table_deletes"} <= set(db_sync)


class TestSampleRecording:
    def test_record_sample_inserts_row_with_label(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        m = _monitor(db)
        result = m.record_sample(SCRAPE)
        assert result is not None
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT version, db_block_height, db_slot_height, node_block_height, queue_length, slot_no "
                "FROM rollback_samples"
            ).fetchone()
        assert row[0] == "cardano-db-sync 13.7.1.0 preprod"
        assert row[1] == 10500000
        assert row[2] == 90000000
        assert row[3] == 10500003
        assert row[4] == 4.0
        assert row[5] == 90000000  # slot_no mirrors db_slot_height for the plot x-axis

    def test_empty_scrape_records_nothing(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        m = _monitor(db)
        assert m.record_sample("unrelated_metric 1\n") is None
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM rollback_samples").fetchone()[0] == 0


class TestLogProcessing:
    def test_start_summary_end_writes_one_event_and_table_rows(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        m = _monitor(db, log_file="/dev/null")
        log = (
            "[2026-06-28 12:00:00.00 UTC] Rollback: Deleting 137 blocks with block_no >= "
            "10499900 (slot >= 89999000, epoch 544)\n"
            "[2026-06-28 12:00:00.00 UTC] ----------------------- Rollback Summary: -----------------------\n"
            "[2026-06-28 12:00:00.00 UTC] Table: TxOut - Count: 1290\n"
            "[2026-06-28 12:00:00.00 UTC] Table: Tx - Count: 470\n"
            "[2026-06-28 12:00:12.50 UTC] Rollback: Successfully deleted 137 blocks\n"
        )
        written = m.consume_log_text(log)
        assert written == 1
        with sqlite3.connect(db) as conn:
            ev = conn.execute(
                "SELECT version, depth_blocks, delete_duration_sec, from_slot, to_slot, slot_no, source "
                "FROM rollback_events"
            ).fetchone()
            tables = conn.execute(
                "SELECT table_name, deleted_rows FROM rollback_table_deletes ORDER BY table_name"
            ).fetchall()
        assert ev[0] == "cardano-db-sync 13.7.1.0 preprod"
        assert ev[1] == 137
        assert ev[2] == pytest.approx(12.5)  # log-timestamped deletion duration
        # The log's "slot >= S" is the rollback target floor -> to_slot, not from_slot.
        assert ev[3] is None  # from_slot: log doesn't carry the pre-rollback tip
        assert ev[4] == 89999000  # to_slot
        assert ev[5] == 89999000  # slot_no mirrors the target for the plot x-axis
        assert ev[6] == "log"
        assert tables == [("Tx", 470), ("TxOut", 1290)]

    def test_partial_line_is_buffered_until_complete(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        m = _monitor(db, log_file="/dev/null")
        # Feed the end line split across two reads.
        assert m.consume_log_text("[2026-06-28 12:00:00 UTC] Rollback: Deleting 5 blocks with block_no >= 10\n") == 0
        assert m.consume_log_text("[2026-06-28 12:00:03 UTC] Rollback: Successfully ") == 0
        written = m.consume_log_text("deleted 5 blocks\n")
        assert written == 1
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT depth_blocks FROM rollback_events").fetchone()[0] == 5

    def test_end_without_start_writes_nothing(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        m = _monitor(db, log_file="/dev/null")
        assert m.consume_log_text("Rollback: Successfully deleted 5 blocks\n") == 0
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM rollback_events").fetchone()[0] == 0
