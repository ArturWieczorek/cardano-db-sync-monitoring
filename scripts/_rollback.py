"""Shared core for db-sync rollback performance tracking.

Pure, side-effect-light helpers (text/HTTP in, data out) used by both the
passive rollback monitor (db-sync-rollback-monitor.py) and the controlled
rollback benchmark (db-sync-rollback-benchmark.py):

  - Prometheus scrape extraction for the db-sync gauges that reveal a rollback
    (db tip going *backward*) and recovery (db tip catching the node tip again).
  - cardano-db-sync log-line parsing for the authoritative deletion-phase
    signals (start line, per-table delete counts, completion line).
  - Pure rollback-event derivation from a chronological sample series.

No SQLite, no argparse, no process management here - those live in the
collector/benchmark scripts. This module is where the correctness-critical
parsing and event logic is unit-tested.

Metric names and log strings are grounded in the cardano-db-sync source:
Cardano.DbSync.Metrics (gauge names), Cardano.Db.Statement.Base /
Cardano.DbSync.Rollback (log lines). Log parsing is deliberately tolerant of
wording so it survives version-to-version phrasing drift.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from _common import parse_prometheus_text

# --- db-sync Prometheus gauges ---------------------------------------------

M_QUEUE_LENGTH = "cardano_db_sync_db_queue_length"
M_DB_BLOCK_HEIGHT = "cardano_db_sync_db_block_height"
M_DB_SLOT_HEIGHT = "cardano_db_sync_db_slot_height"
M_NODE_BLOCK_HEIGHT = "cardano_db_sync_node_block_height"
M_EPOCH_SYNC_DURATION = "cardano_db_sync_db_epoch_sync_duration_seconds"

# The gauges we keep from a scrape; everything else on the endpoint is ignored.
DB_SYNC_METRIC_KEYS: tuple[str, ...] = (
    M_QUEUE_LENGTH,
    M_DB_BLOCK_HEIGHT,
    M_DB_SLOT_HEIGHT,
    M_NODE_BLOCK_HEIGHT,
    M_EPOCH_SYNC_DURATION,
)


def parse_db_sync_metrics(text: str) -> dict[str, float]:
    """Extract just the db-sync rollback-relevant gauges from a Prometheus scrape.

    Returns only the keys in `DB_SYNC_METRIC_KEYS` that are present; an empty or
    unrelated scrape yields an empty dict.
    """
    parsed = parse_prometheus_text(text)
    return {k: parsed[k] for k in DB_SYNC_METRIC_KEYS if k in parsed}


# --- db-sync log-line parsing ----------------------------------------------


@dataclass
class RollbackLogStart:
    """The line db-sync emits when it begins (or schedules) a rollback. `blocks`
    and `block_no` are None for phrasings that don't carry them (e.g. a bare
    "rolling back to (slot N)"); `slot_no` is the rollback target slot."""

    blocks: int | None
    block_no: int | None
    slot_no: int | None
    epoch_no: int | None


@dataclass
class RollbackLogTableDelete:
    """One per-table line from the post-rollback deletion summary."""

    table_name: str
    deleted_rows: int


@dataclass
class RollbackLogEnd:
    """The line marking the end of the deletion phase. `blocks` is the count
    from 'Successfully deleted N blocks', or None for the bare completion line."""

    blocks: int | None


# "Rollback: Deleting 137 blocks with block_no >= 10499900 (slot >= 89999000, epoch 544)"
_START_FULL_RE = re.compile(
    r"Deleting\s+(?P<blocks>\d+)\s+blocks?\s+with\s+block_no\s*>=\s*(?P<block_no>\d+)"
    r"\s*\(\s*slot\s*>=\s*(?P<slot>\d+)\s*,\s*epoch\s*(?P<epoch>\d+)\s*\)",
    re.IGNORECASE,
)
# Older / terser phrasing without the slot+epoch tail.
_START_SIMPLE_RE = re.compile(
    r"Deleting\s+(?P<blocks>\d+)\s+blocks?\s+(?:equal to or greater than|with\s+block_no\s*>=|>=)\s*(?P<block_no>\d+)",
    re.IGNORECASE,
)
# Rollback-to-a-point phrasing, including the "delayed delete" optimization db-sync
# uses when the node is far ahead (it rolls the ledger back and applies forward,
# deleting later). Verified live against 13.7.2.1, e.g.:
#   "Delaying delete of 1242 blocks after 4365893 while rolling back to (slot 114393593, hash ..)"
# Both the block count + "after <block_no>" and the "rolling back to (slot N" target
# are optional/positional, so capture them independently.
_START_ROLLING_RE = re.compile(r"rolling back to\s*\(\s*slot\s+(?P<slot>\d+)", re.IGNORECASE)
_DELETE_OF_RE = re.compile(r"delete of\s+(?P<blocks>\d+)\s+blocks?(?:\s+after\s+(?P<block_no>\d+))?", re.IGNORECASE)
_TABLE_RE = re.compile(r"Table:\s*(?P<name>\w+)\s*-\s*Count:\s*(?P<rows>\d+)", re.IGNORECASE)
_END_COUNT_RE = re.compile(r"Successfully deleted\s+(?P<blocks>\d+)\s+blocks?", re.IGNORECASE)
_END_PLAIN_RE = re.compile(r"rollback completed successfully", re.IGNORECASE)


# db-sync (iohk-monitoring) lines carry a bracketed UTC timestamp, e.g.
#   [host:cardano.db-sync:Info:42] [2026-06-28 12:00:05.50 UTC] Rollback: Deleting ...
# Fraction is optional and may use '.' or ',' as the separator with any number
# of digits; we normalize and truncate to microseconds before parsing.
_LOG_TS_RE = re.compile(r"\[(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?P<frac>[.,]\d+)?\s*(?:UTC)?\]")


def extract_log_timestamp(line: str) -> float | None:
    """POSIX seconds from a db-sync log line's bracketed UTC timestamp, or None.

    Lets the monitor time the deletion phase from the log's own clock (accurate
    to the log's resolution) instead of the poll-boundary wall clock. Tolerates
    '.' or ',' fractional separators and sub-microsecond (e.g. nanosecond)
    precision by truncating to 6 digits, since strptime's %f caps at microseconds.
    Returns None when the line has no recognizable timestamp, so the caller can
    fall back to read-time.
    """
    m = _LOG_TS_RE.search(line)
    if not m:
        return None
    raw = m.group("ts").replace("T", " ")
    frac = m.group("frac")
    if frac:
        raw += "." + frac[1:7]  # drop the [.,] separator, keep <=6 digits
    fmt = "%Y-%m-%d %H:%M:%S.%f" if frac else "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def parse_rollback_log_line(
    line: str,
) -> RollbackLogStart | RollbackLogTableDelete | RollbackLogEnd | None:
    """Classify a single db-sync log line as a rollback start / per-table delete
    / end record, or None if it isn't a rollback signal.

    End is matched before start so the count-bearing completion line is never
    mistaken for a start; the per-table line is distinctive enough to test in
    any order.
    """
    if not line:
        return None
    m = _END_COUNT_RE.search(line)
    if m:
        return RollbackLogEnd(blocks=int(m.group("blocks")))
    if _END_PLAIN_RE.search(line):
        return RollbackLogEnd(blocks=None)
    m = _TABLE_RE.search(line)
    if m:
        return RollbackLogTableDelete(table_name=m.group("name"), deleted_rows=int(m.group("rows")))
    m = _START_FULL_RE.search(line)
    if m:
        return RollbackLogStart(
            blocks=int(m.group("blocks")),
            block_no=int(m.group("block_no")),
            slot_no=int(m.group("slot")),
            epoch_no=int(m.group("epoch")),
        )
    m = _START_SIMPLE_RE.search(line)
    if m:
        return RollbackLogStart(
            blocks=int(m.group("blocks")),
            block_no=int(m.group("block_no")),
            slot_no=None,
            epoch_no=None,
        )
    m = _START_ROLLING_RE.search(line)
    if m:
        d = _DELETE_OF_RE.search(line)
        return RollbackLogStart(
            blocks=int(d.group("blocks")) if d else None,
            block_no=int(d.group("block_no")) if d and d.group("block_no") else None,
            slot_no=int(m.group("slot")),
            epoch_no=None,
        )
    return None


# --- rollback-event derivation from a sample series ------------------------


@dataclass
class RollbackSample:
    """One Prometheus poll, in chronological order. Heights/queue are None when
    that gauge was missing from the scrape (or the scrape failed)."""

    ts: float  # POSIX seconds
    db_block_height: int | None
    db_slot_height: int | None
    node_block_height: int | None
    queue_length: float | None


@dataclass
class RollbackEvent:
    """A detected rollback: the db tip fell from `from_block` to `to_block` and
    (if observed) climbed back to >= `from_block` at `end_ts`."""

    start_ts: float
    end_ts: float | None
    from_block: int | None
    to_block: int | None
    from_slot: int | None
    to_slot: int | None
    depth_blocks: int | None
    depth_slots: int | None
    recovery_duration_sec: float | None
    max_queue_len: float | None


def derive_events(samples: list[RollbackSample]) -> list[RollbackEvent]:
    """Find rollback events in a chronological sample series.

    A rollback is detected where `db_block_height` drops below the previous
    (non-None) height. From that edge we track the lowest point reached
    (`to_block`) and the first sample whose height climbs back to the
    pre-rollback tip (`from_block`) - that sample is recovery end. Samples with
    a missing height are skipped for edge detection but still counted for the
    max-queue window. Detection resumes after each event's recovery point, so a
    multi-step recovery is one event, and a fresh drop afterwards is a new one.
    """
    events: list[RollbackEvent] = []
    n = len(samples)
    prev_idx: int | None = None  # index of the last sample with a known height
    i = 0
    while i < n:
        cur = samples[i]
        if cur.db_block_height is None:
            i += 1
            continue
        if prev_idx is not None:
            prev = samples[prev_idx]
            if prev.db_block_height is not None and cur.db_block_height < prev.db_block_height:
                event, resume = _consume_rollback(samples, prev, i)
                events.append(event)
                # Resume scanning after the recovery point (or end of series).
                i = resume
                prev_idx = resume - 1 if resume - 1 >= 0 else None
                # Walk prev_idx back to the last known height before resume.
                while prev_idx is not None and prev_idx >= 0 and samples[prev_idx].db_block_height is None:
                    prev_idx -= 1
                if prev_idx is not None and prev_idx < 0:
                    prev_idx = None
                continue
        prev_idx = i
        i += 1
    return events


def _consume_rollback(samples: list[RollbackSample], pre: RollbackSample, start_i: int) -> tuple[RollbackEvent, int]:
    """Build one RollbackEvent starting at index `start_i` (the first sample
    below `pre`'s tip). Returns the event and the index to resume scanning from."""
    from_block = pre.db_block_height
    from_slot = pre.db_slot_height
    to_block = samples[start_i].db_block_height
    to_slot = samples[start_i].db_slot_height
    bottom_ts = samples[start_i].ts
    # The queue often spikes as the rollback is enqueued, i.e. on the sample
    # just before the observed drop - seed the window from `pre` so we don't miss it.
    max_q = pre.queue_length
    end_ts: float | None = None
    j = start_i
    n = len(samples)
    while j < n:
        s = samples[j]
        if s.db_block_height is not None and (to_block is None or s.db_block_height < to_block):
            to_block = s.db_block_height
            to_slot = s.db_slot_height
            bottom_ts = s.ts
        if s.queue_length is not None and (max_q is None or s.queue_length > max_q):
            max_q = s.queue_length
        if s.db_block_height is not None and from_block is not None and s.db_block_height >= from_block:
            end_ts = s.ts
            break
        j += 1
    depth_blocks = from_block - to_block if from_block is not None and to_block is not None else None
    depth_slots = from_slot - to_slot if from_slot is not None and to_slot is not None else None
    # Recovery is the re-apply climb from the lowest observed tip back to the
    # pre-rollback height, so it's measured from the bottom sample (not the first
    # drop). Both are interval-quantized to the sample cadence.
    recovery = end_ts - bottom_ts if end_ts is not None else None
    event = RollbackEvent(
        start_ts=samples[start_i].ts,
        end_ts=end_ts,
        from_block=from_block,
        to_block=to_block,
        from_slot=from_slot,
        to_slot=to_slot,
        depth_blocks=depth_blocks,
        depth_slots=depth_slots,
        recovery_duration_sec=recovery,
        max_queue_len=max_q,
    )
    resume = j + 1 if end_ts is not None else n
    return event, resume


# --- benchmark statistics --------------------------------------------------


def summarize(values: list[float]) -> dict[str, float | int | None]:
    """Median / min / max / sample-stdev of a list of measurements (e.g. the
    deletion durations across N benchmark repetitions). Empty -> all None;
    stdev is 0.0 for a single value (no spread to estimate)."""
    n = len(values)
    if n == 0:
        return {"n": 0, "median": None, "min": None, "max": None, "stdev": None}
    return {
        "n": n,
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if n > 1 else 0.0,
    }
