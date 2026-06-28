"""Unit tests for the shared rollback core (scripts/_rollback.py).

This is the correctness-critical layer: Prometheus metric extraction, db-sync
log-line parsing (version-tolerant), and pure event derivation from a sample
series. No SQLite / HTTP / processes here - those are exercised by the monitor
and benchmark tests.
"""

from __future__ import annotations

import pytest
from _rollback import (
    M_DB_BLOCK_HEIGHT,
    M_DB_SLOT_HEIGHT,
    M_NODE_BLOCK_HEIGHT,
    M_QUEUE_LENGTH,
    RollbackEvent,
    RollbackLogEnd,
    RollbackLogStart,
    RollbackLogTableDelete,
    RollbackSample,
    derive_events,
    extract_log_timestamp,
    parse_db_sync_metrics,
    parse_rollback_log_line,
    summarize,
)

# --- parse_db_sync_metrics -------------------------------------------------

SCRAPE = """\
# HELP cardano_db_sync_db_queue_length DOCSTRING
# TYPE cardano_db_sync_db_queue_length gauge
cardano_db_sync_db_queue_length 7.0
cardano_db_sync_db_block_height 1.05e7
cardano_db_sync_db_slot_height 9.0e7
cardano_db_sync_node_block_height 10500005.0
cardano_db_sync_db_epoch_sync_duration_seconds 12.5
some_other_metric{label="x"} 3.0
"""


class TestParseMetrics:
    def test_extracts_known_gauges(self) -> None:
        m = parse_db_sync_metrics(SCRAPE)
        assert m[M_QUEUE_LENGTH] == 7.0
        assert m[M_DB_BLOCK_HEIGHT] == 10_500_000.0
        assert m[M_DB_SLOT_HEIGHT] == 90_000_000.0
        assert m[M_NODE_BLOCK_HEIGHT] == 10_500_005.0

    def test_ignores_unrelated_metrics(self) -> None:
        m = parse_db_sync_metrics(SCRAPE)
        assert "some_other_metric" not in m

    def test_empty_scrape_is_empty(self) -> None:
        assert parse_db_sync_metrics("") == {}

    def test_partial_scrape_returns_only_present(self) -> None:
        m = parse_db_sync_metrics("cardano_db_sync_db_block_height 42\n")
        assert m == {M_DB_BLOCK_HEIGHT: 42.0}


# --- parse_rollback_log_line ----------------------------------------------


class TestParseLogStart:
    def test_full_start_line_with_slot_and_epoch(self) -> None:
        line = "[db-sync:Info] Rollback: Deleting 137 blocks with block_no >= 10499900 (slot >= 89999000, epoch 544)"
        rec = parse_rollback_log_line(line)
        assert isinstance(rec, RollbackLogStart)
        assert rec.blocks == 137
        assert rec.block_no == 10499900
        assert rec.slot_no == 89999000
        assert rec.epoch_no == 544

    def test_rolling_back_to_slot_delayed_delete_form(self) -> None:
        # Real db-sync 13.7.2.1 line when the node is far ahead (delayed delete).
        line = (
            "[db-sync-node:Info:188] [2026-06-28 18:22:38.26 UTC] Delaying delete of 1242 blocks "
            "after 4365893 while rolling back to (slot 114393593, hash 2250a082). "
            "Applying blocks until a new block is found."
        )
        rec = parse_rollback_log_line(line)
        assert isinstance(rec, RollbackLogStart)
        assert rec.blocks == 1242
        assert rec.block_no == 4365893
        assert rec.slot_no == 114393593  # the rollback target slot
        assert rec.epoch_no is None

    def test_rolling_back_to_slot_without_block_count(self) -> None:
        rec = parse_rollback_log_line("Rolling back to (slot 99, hash ab)")
        assert isinstance(rec, RollbackLogStart)
        assert rec.slot_no == 99
        assert rec.blocks is None
        assert rec.block_no is None

    def test_older_equal_to_or_greater_than_form(self) -> None:
        line = "Rolling back, Deleting 5 blocks equal to or greater than 1200"
        rec = parse_rollback_log_line(line)
        assert isinstance(rec, RollbackLogStart)
        assert rec.blocks == 5
        assert rec.block_no == 1200
        assert rec.slot_no is None
        assert rec.epoch_no is None


class TestParseLogTableDelete:
    def test_table_count_line(self) -> None:
        rec = parse_rollback_log_line("Table: TxOut - Count: 1290")
        assert isinstance(rec, RollbackLogTableDelete)
        assert rec.table_name == "TxOut"
        assert rec.deleted_rows == 1290


class TestParseLogEnd:
    def test_successfully_deleted_line_has_count(self) -> None:
        rec = parse_rollback_log_line("Rollback: Successfully deleted 137 blocks")
        assert isinstance(rec, RollbackLogEnd)
        assert rec.blocks == 137

    def test_completion_line_without_count(self) -> None:
        rec = parse_rollback_log_line("Database rollback completed successfully")
        assert isinstance(rec, RollbackLogEnd)
        assert rec.blocks is None


class TestParseLogMisc:
    def test_unrelated_line_returns_none(self) -> None:
        assert parse_rollback_log_line("Starting ChainSync with peer 1.2.3.4") is None

    def test_blank_line_returns_none(self) -> None:
        assert parse_rollback_log_line("") is None


class TestExtractLogTimestamp:
    def test_bracketed_utc_timestamp(self) -> None:
        line = "[host:cardano.db-sync:Info:42] [2026-06-28 12:00:05.50 UTC] Rollback: Deleting 1 blocks ..."
        ts = extract_log_timestamp(line)
        assert ts is not None
        # 12:00:05.5 minus 12:00:00.0 on the same day should be 5.5s apart.
        base = extract_log_timestamp("[2026-06-28 12:00:00.00 UTC] x")
        assert base is not None
        assert round(ts - base, 3) == 5.5

    def test_iso_t_separator_without_utc(self) -> None:
        assert extract_log_timestamp("[2026-06-28T12:00:00] x") is not None

    def test_no_timestamp_returns_none(self) -> None:
        assert extract_log_timestamp("Rollback: Deleting 1 blocks with block_no >= 5") is None

    def test_nanosecond_fraction_truncated_not_dropped(self) -> None:
        ts = extract_log_timestamp("[2026-06-28 12:00:05.123456789 UTC] x")
        base = extract_log_timestamp("[2026-06-28 12:00:05 UTC] x")
        assert ts is not None and base is not None
        # 0.123456 s after the whole second (truncated to microseconds).
        assert round(ts - base, 6) == pytest.approx(0.123456, abs=1e-6)

    def test_comma_decimal_separator(self) -> None:
        ts = extract_log_timestamp("[2026-06-28 12:00:05,500 UTC] x")
        base = extract_log_timestamp("[2026-06-28 12:00:05 UTC] x")
        assert ts is not None and base is not None
        assert round(ts - base, 3) == 0.5


# --- derive_events ----------------------------------------------------------


def _s(ts: float, blk: int | None, slot: int | None, node: int | None, q: float | None) -> RollbackSample:
    return RollbackSample(ts=ts, db_block_height=blk, db_slot_height=slot, node_block_height=node, queue_length=q)


class TestDeriveEventsNoRollback:
    def test_empty_series(self) -> None:
        assert derive_events([]) == []

    def test_monotonic_increasing_series_has_no_events(self) -> None:
        samples = [_s(float(i), 100 + i, 1000 + 10 * i, 105 + i, 0.0) for i in range(10)]
        assert derive_events(samples) == []


class TestDeriveEventsSingleRollback:
    def test_depth_and_recovery_are_measured(self) -> None:
        # tip climbs to 110, drops to 100 (depth 10), then recovers past 110.
        samples = [
            _s(0.0, 108, 8080, 112, 1.0),
            _s(1.0, 110, 8100, 112, 2.0),  # pre-rollback tip
            _s(2.0, 100, 8000, 112, 9.0),  # rollback observed here (start)
            _s(3.0, 104, 8040, 112, 5.0),  # recovering
            _s(4.0, 110, 8100, 112, 1.0),  # caught back up to pre-rollback tip (recovery end)
            _s(5.0, 113, 8130, 113, 0.0),
        ]
        events = derive_events(samples)
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, RollbackEvent)
        assert ev.start_ts == 2.0
        assert ev.from_block == 110
        assert ev.to_block == 100
        assert ev.depth_blocks == 10
        assert ev.from_slot == 8100
        assert ev.to_slot == 8000
        assert ev.depth_slots == 100
        assert ev.end_ts == 4.0
        assert ev.recovery_duration_sec == 2.0
        assert ev.max_queue_len == 9.0

    def test_unrecovered_rollback_has_no_end(self) -> None:
        samples = [
            _s(0.0, 110, 8100, 112, 1.0),
            _s(1.0, 90, 7900, 112, 12.0),  # drop, never climbs back to 110
            _s(2.0, 95, 7950, 112, 8.0),
        ]
        events = derive_events(samples)
        assert len(events) == 1
        ev = events[0]
        assert ev.depth_blocks == 20
        assert ev.end_ts is None
        assert ev.recovery_duration_sec is None
        assert ev.max_queue_len == 12.0


class TestDeriveEventsMultiple:
    def test_two_separate_rollbacks(self) -> None:
        samples = [
            _s(0.0, 100, 1000, 130, 0.0),
            _s(1.0, 90, 900, 130, 5.0),  # rollback 1 (depth 10)
            _s(2.0, 101, 1010, 130, 0.0),  # recovered
            _s(3.0, 110, 1100, 130, 0.0),
            _s(4.0, 95, 950, 130, 7.0),  # rollback 2 (depth 15)
            _s(5.0, 111, 1110, 130, 0.0),  # recovered
        ]
        events = derive_events(samples)
        assert len(events) == 2
        assert events[0].depth_blocks == 10
        assert events[1].depth_blocks == 15


class TestSummarize:
    def test_empty(self) -> None:
        assert summarize([]) == {"n": 0, "median": None, "min": None, "max": None, "stdev": None}

    def test_single_value_has_zero_stdev(self) -> None:
        s = summarize([4.0])
        assert s["n"] == 1
        assert s["median"] == 4.0
        assert s["min"] == 4.0
        assert s["max"] == 4.0
        assert s["stdev"] == 0.0

    def test_multiple_values(self) -> None:
        s = summarize([2.0, 4.0, 6.0])
        assert s["n"] == 3
        assert s["median"] == 4.0
        assert s["min"] == 2.0
        assert s["max"] == 6.0
        assert s["stdev"] == pytest.approx(2.0)


class TestDeriveEventsRecoveryFromBottom:
    def test_recovery_measured_from_lowest_point_not_first_drop(self) -> None:
        # Drop spans two samples (110 -> 105 -> 100), then recovers at ts=3.
        samples = [
            _s(0.0, 110, 1100, 120, 1.0),  # pre-rollback tip
            _s(1.0, 105, 1050, 120, 4.0),  # drop first observed
            _s(2.0, 100, 1000, 120, 9.0),  # bottom
            _s(3.0, 110, 1100, 120, 0.0),  # recovered
        ]
        ev = derive_events(samples)[0]
        assert ev.start_ts == 1.0
        assert ev.to_block == 100
        assert ev.depth_blocks == 10
        # Recovery is bottom (ts=2) -> recovered (ts=3) = 1.0, not 2.0 from first drop.
        assert ev.recovery_duration_sec == 1.0

    def test_partial_recovery_then_deeper_is_one_event(self) -> None:
        # 110 -> 100 -> 105 (partial) -> 95 (deeper, never reached 110) -> 111.
        # Must stay a SINGLE event whose bottom is 95, not two events.
        samples = [
            _s(0.0, 110, 1100, 120, 1.0),
            _s(1.0, 100, 1000, 120, 5.0),  # drop observed
            _s(2.0, 105, 1050, 120, 4.0),  # partial recover, still < 110
            _s(3.0, 95, 950, 120, 8.0),  # deeper bottom
            _s(4.0, 111, 1110, 120, 0.0),  # finally recovered
        ]
        events = derive_events(samples)
        assert len(events) == 1
        assert events[0].from_block == 110
        assert events[0].to_block == 95
        assert events[0].depth_blocks == 15
        assert events[0].recovery_duration_sec == 1.0  # bottom ts=3 -> recovered ts=4

    def test_max_queue_includes_the_pre_drop_spike(self) -> None:
        # Queue peaks on the sample just before the tip drop is observed.
        samples = [
            _s(0.0, 110, 1100, 120, 47.0),  # queue spike as rollback enqueued
            _s(1.0, 100, 1000, 120, 3.0),  # drop observed
            _s(2.0, 110, 1100, 120, 0.0),  # recovered
        ]
        ev = derive_events(samples)[0]
        assert ev.max_queue_len == 47.0


class TestDeriveEventsMissingData:
    def test_none_heights_are_skipped(self) -> None:
        samples = [
            _s(0.0, None, None, None, None),
            _s(1.0, 110, 8100, 112, 1.0),
            _s(2.0, 100, 8000, 112, 9.0),  # rollback
            _s(3.0, None, None, None, None),  # scrape gap mid-recovery
            _s(4.0, 110, 8100, 112, 0.0),  # recovered
        ]
        events = derive_events(samples)
        assert len(events) == 1
        assert events[0].depth_blocks == 10
        assert events[0].end_ts == 4.0
