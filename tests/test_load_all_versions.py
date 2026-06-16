"""Tests for load_all_versions - the version enumerator that unions labels
across every metric table, not just the *_version table.

This is what surfaces a run collected only by an optional collector (disk/RTS),
or one saved under a mistyped --node-ver: such labels never reach node_version,
so the old single-table picker hid them entirely.
"""

import sqlite3

from _common import attach_slot_by_ts, load_all_versions

NODE_TABLES = [
    "node_version",
    "memory_metrics",
    "cpu_metrics",
    "node_ingest_metrics",
    "disk_metrics",
    "rts_metrics",
]


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE node_version (timestamp TEXT, version TEXT)")
        conn.execute("CREATE TABLE memory_metrics (ts TEXT, version TEXT)")
        conn.execute("CREATE TABLE disk_metrics (ts TEXT, version TEXT)")
        conn.execute("CREATE TABLE rts_metrics (ts TEXT, version TEXT)")
        # node-resource-monitor wrote a normally-labelled run.
        conn.execute("INSERT INTO node_version VALUES ('2026-01-01T00:00:00', 'cardano-node A mainnet')")
        conn.execute("INSERT INTO memory_metrics VALUES ('2026-01-01T00:00:00', 'cardano-node A mainnet')")
        # RTS was collected under a *different* (mistyped) label - never in node_version.
        conn.execute("INSERT INTO rts_metrics VALUES ('2026-01-02T00:00:00', 'cardano-node B mainnet')")
        conn.commit()
    finally:
        conn.close()


def test_surfaces_label_only_present_in_metric_table(tmp_path):
    db = str(tmp_path / "node.db")
    _make_db(db)
    versions = load_all_versions(db, NODE_TABLES)
    # The RTS-only label is visible even though it never hit node_version.
    assert "cardano-node B mainnet" in versions
    assert "cardano-node A mainnet" in versions


def test_orders_most_recent_first(tmp_path):
    db = str(tmp_path / "node.db")
    _make_db(db)
    versions = load_all_versions(db, NODE_TABLES)
    # B's latest sample (2026-01-02) is newer than A's (2026-01-01).
    assert versions[0] == "cardano-node B mainnet"


def test_dedupes_across_tables(tmp_path):
    db = str(tmp_path / "node.db")
    _make_db(db)
    versions = load_all_versions(db, NODE_TABLES)
    # A appears in both node_version and memory_metrics but only once here.
    assert versions.count("cardano-node A mainnet") == 1


def test_missing_tables_skipped(tmp_path):
    db = str(tmp_path / "node.db")
    _make_db(db)
    # node_ingest_metrics / cpu_metrics don't exist in this DB - must not raise.
    versions = load_all_versions(db, NODE_TABLES)
    assert len(versions) == 2


def test_empty_db_returns_empty(tmp_path):
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    assert load_all_versions(db, NODE_TABLES) == []


# --- attach_slot_by_ts -----------------------------------------------------


def _seed_mem(db, rows):
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memory_metrics (ts TEXT, slot_no INTEGER, version TEXT)")
    conn.executemany("INSERT INTO memory_metrics VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


def test_attach_slot_nearest_ts_per_version(tmp_path):
    import pandas as pd

    db = str(tmp_path / "m.db")
    _seed_mem(
        db,
        [
            ("2026-01-01T00:00:00", 100, "vA"),
            ("2026-01-01T00:01:00", 200, "vA"),
            ("2026-01-01T00:00:00", 900, "vB"),
        ],
    )
    df = pd.DataFrame(
        [
            {"ts": "2026-01-01T00:00:50", "version": "vA"},  # nearest -> 00:01:00 / slot 200
            {"ts": "2026-01-01T00:00:05", "version": "vB"},  # nearest -> 00:00:00 / slot 900
        ]
    )
    df["ts"] = pd.to_datetime(df["ts"])
    out = attach_slot_by_ts(df, db, ["vA", "vB"])
    got = dict(zip(out["version"], out["slot_no"]))
    assert got["vA"] == 200
    assert got["vB"] == 900


def test_attach_slot_missing_memory_table_is_nan(tmp_path):
    import pandas as pd

    db = str(tmp_path / "nomem.db")
    sqlite3.connect(db).close()  # no memory_metrics table at all
    df = pd.DataFrame([{"ts": pd.Timestamp("2026-01-01"), "version": "v"}])
    out = attach_slot_by_ts(df, db, ["v"])
    assert "slot_no" in out.columns
    assert out["slot_no"].notna().sum() == 0
