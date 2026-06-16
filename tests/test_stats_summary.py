"""Tests for scripts/stats-summary.py - the cross-role text stats summary.

Seeds a tiny node DB and a tiny db-sync DB, then checks role auto-detection, the
overview table, the detailed per-version view (headline numbers + units), and
that sections backed by an absent table are skipped without error.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


summ = _load("stats_summary", "stats-summary.py")

DBSYNC_V = "cardano-db-sync 13.7.1.0-node-11.0.1 preprod"
NODE_V = "cardano-node LSM-11.0.1 mainnet"
GIB = 1024**3


def _seed_dbsync(path: str, *, with_disk: bool = False, lsm: bool = True) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE db_sync_version (timestamp TEXT, version TEXT)")
        conn.execute(
            "CREATE TABLE memory_metrics (ts TEXT, slot_no INT, rss REAL, vms REAL, uss REAL, pss REAL, swap REAL, shared REAL, version TEXT)"
        )
        conn.execute("CREATE TABLE cpu_metrics (ts TEXT, slot_no INT, cpu_percent REAL, version TEXT)")
        conn.execute(
            "CREATE TABLE ingest_metrics (ts TEXT, slot_no INT, version TEXT, tip_lag_sec REAL, db_size_bytes INT, max_block_no INT, max_tx_id INT, utxo_count INT)"
        )
        conn.execute(
            "CREATE TABLE table_rowcounts (ts TEXT, slot_no INT, version TEXT, table_name TEXT, row_count INT)"
        )
        if with_disk:
            conn.execute(
                "CREATE TABLE disk_metrics (ts TEXT, slot_no INT, path TEXT, total_bytes INT, lsm_bytes INT, version TEXT)"
            )
        for i in range(3):
            ts = f"2026-05-27T00:0{i}:00"
            conn.execute("INSERT INTO db_sync_version VALUES (?,?)", (ts, DBSYNC_V))
            conn.execute(
                "INSERT INTO memory_metrics (ts, rss, version) VALUES (?,?,?)", (ts, 1024.0 * (i + 1), DBSYNC_V)
            )
            conn.execute("INSERT INTO cpu_metrics (ts, cpu_percent, version) VALUES (?,?,?)", (ts, 50.0 + i, DBSYNC_V))
            conn.execute(
                "INSERT INTO ingest_metrics (ts, version, tip_lag_sec, db_size_bytes, max_block_no, max_tx_id, utxo_count) VALUES (?,?,?,?,?,?,?)",
                (ts, DBSYNC_V, 1.0 + i, 25 * GIB, 4_000_000 + i, 5_000_000 + i, None),
            )
            for tbl, rc in (("tx_out", 20_000_000), ("block", 4_000_000)):
                conn.execute(
                    "INSERT INTO table_rowcounts (ts, version, table_name, row_count) VALUES (?,?,?,?)",
                    (ts, DBSYNC_V, tbl, rc + i),
                )
            if with_disk:
                conn.execute(
                    "INSERT INTO disk_metrics (ts, path, total_bytes, lsm_bytes, version) VALUES (?,?,?,?,?)",
                    (ts, "/ledger", 30 * GIB + i, (2 * GIB + i if lsm else 0), DBSYNC_V),
                )
        conn.commit()
    finally:
        conn.close()


def _seed_node(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE node_version (timestamp TEXT, version TEXT)")
        conn.execute(
            "CREATE TABLE memory_metrics (ts TEXT, slot_no INT, rss REAL, vms REAL, uss REAL, pss REAL, swap REAL, shared REAL, version TEXT)"
        )
        conn.execute("CREATE TABLE cpu_metrics (ts TEXT, slot_no INT, cpu_percent REAL, version TEXT)")
        conn.execute(
            "CREATE TABLE node_ingest_metrics (ts TEXT, slot_no INT, epoch_no INT, era TEXT, sync_progress REAL, version TEXT)"
        )
        conn.execute(
            "CREATE TABLE disk_metrics (ts TEXT, slot_no INT, path TEXT, total_bytes INT, lsm_bytes INT, version TEXT)"
        )
        conn.execute("CREATE TABLE rts_metrics (ts TEXT, slot_no INT, metric TEXT, value REAL, version TEXT)")
        for i in range(3):
            ts = f"2026-06-04T00:0{i}:00"
            conn.execute("INSERT INTO node_version VALUES (?,?)", (ts, NODE_V))
            conn.execute("INSERT INTO memory_metrics (ts, rss, version) VALUES (?,?,?)", (ts, 2048.0 * (i + 1), NODE_V))
            conn.execute("INSERT INTO cpu_metrics (ts, cpu_percent, version) VALUES (?,?,?)", (ts, 100.0 + i, NODE_V))
            conn.execute(
                "INSERT INTO node_ingest_metrics (ts, epoch_no, era, sync_progress, version) VALUES (?,?,?,?,?)",
                (ts, 600 + i, "Conway", 100.0, NODE_V),
            )
            conn.execute(
                "INSERT INTO disk_metrics (ts, path, total_bytes, lsm_bytes, version) VALUES (?,?,?,?,?)",
                (ts, "/node/db", 200 * GIB + i, 9 * GIB + i, NODE_V),
            )
            conn.execute(
                "INSERT INTO rts_metrics (ts, metric, value, version) VALUES (?,?,?,?)",
                (ts, "cardano_node_metrics_RTS_gcHeapBytes_int", 5.0 * GIB, NODE_V),
            )
        conn.commit()
    finally:
        conn.close()


class TestRoleDetect:
    def test_db_sync(self, tmp_path):
        p = str(tmp_path / "preprod.db")
        _seed_dbsync(p)
        with sqlite3.connect(p) as c:
            assert summ.detect_role(c) == "db-sync"

    def test_node(self, tmp_path):
        p = str(tmp_path / "mainnet.db")
        _seed_node(p)
        with sqlite3.connect(p) as c:
            assert summ.detect_role(c) == "node"


class TestOverview:
    def test_lists_versions_with_headline_numbers(self, tmp_path, capsys):
        p = str(tmp_path / "preprod.db")
        _seed_dbsync(p)
        with sqlite3.connect(p) as c:
            summ.overview(c, "db-sync", [DBSYNC_V])
        out = capsys.readouterr().out
        assert "13.7.1.0-node-11.0.1" in out
        assert "GiB" in out  # peak RAM rendered in binary units
        assert "peak disk" not in out  # no disk_metrics -> column omitted

    def test_disk_and_lsm_columns_when_present(self, tmp_path, capsys):
        p = str(tmp_path / "mainnet.db")
        _seed_node(p)  # node seed has disk_metrics with lsm_bytes > 0
        with sqlite3.connect(p) as c:
            summ.overview(c, "node", [NODE_V])
        out = capsys.readouterr().out
        assert "peak disk" in out and "peak lsm" in out

    def test_lsm_column_omitted_for_inmemory(self, tmp_path, capsys):
        p = str(tmp_path / "preprod.db")
        _seed_dbsync(p, with_disk=True, lsm=False)  # disk measured, no lsm dir
        with sqlite3.connect(p) as c:
            summ.overview(c, "db-sync", [DBSYNC_V])
        out = capsys.readouterr().out
        assert "peak disk" in out
        assert "peak lsm" not in out


class TestDetail:
    def test_db_sync_detail(self, tmp_path, capsys):
        p = str(tmp_path / "preprod.db")
        _seed_dbsync(p)
        with sqlite3.connect(p) as c:
            summ.summarize_version(c, "db-sync", DBSYNC_V, top=10)
        out = capsys.readouterr().out
        assert "peak RAM" in out and "GiB" in out
        assert "final DB size" in out
        assert "tx_out" in out and "20,000,002" in out  # top table, max row count
        assert "UTXO count" in out and "tracking off" in out
        assert "On-disk size" not in out  # no disk_metrics table -> section skipped

    def test_node_detail_has_disk_sync_rts(self, tmp_path, capsys):
        p = str(tmp_path / "mainnet.db")
        _seed_node(p)
        with sqlite3.connect(p) as c:
            summ.summarize_version(c, "node", NODE_V, top=10)
        out = capsys.readouterr().out
        assert "On-disk size" in out and "peak total" in out
        assert "peak lsm/" in out  # this node run used LSM (lsm_bytes > 0)
        assert "Sync state" in out and "Conway" in out and "epoch" in out.lower()
        assert "RTS / runtime" in out and "peak heap" in out

    def test_dbsync_with_disk_shows_section(self, tmp_path, capsys):
        p = str(tmp_path / "preprod.db")
        _seed_dbsync(p, with_disk=True)
        with sqlite3.connect(p) as c:
            summ.summarize_version(c, "db-sync", DBSYNC_V, top=10)
        out = capsys.readouterr().out
        assert "On-disk size" in out and "peak lsm/" in out

    def test_disk_without_lsm_hides_lsm_lines(self, tmp_path, capsys):
        # In-memory build: disk measured but lsm_bytes == 0 -> no lsm/ lines.
        p = str(tmp_path / "preprod.db")
        _seed_dbsync(p, with_disk=True, lsm=False)
        with sqlite3.connect(p) as c:
            summ.summarize_version(c, "db-sync", DBSYNC_V, top=10)
        out = capsys.readouterr().out
        assert "On-disk size" in out and "peak total" in out
        assert "lsm/" not in out

    def test_top_caps_table_list(self, tmp_path, capsys):
        # --top must bound how many tables the db-sync detail view prints. The
        # section header reports the count actually shown, so top=1 against the
        # two seeded tables (tx_out, block) must read "Top 1", not "Top 2", and
        # the surviving table is the larger one (tx_out). We assert on the header
        # count rather than the bare word "block" - that word also appears in the
        # Ingest section's "max block" line.
        p = str(tmp_path / "preprod.db")
        _seed_dbsync(p)  # seed writes two tables: tx_out, block
        with sqlite3.connect(p) as c:
            summ.summarize_version(c, "db-sync", DBSYNC_V, top=1)
        out = capsys.readouterr().out
        assert "Top 1 tables by row count" in out
        assert "Top 2 tables" not in out
        assert "tx_out" in out  # the larger one survives the cap

    def test_top_shows_all_when_under_cap(self, tmp_path, capsys):
        # With the default top well above the two seeded tables, both are shown.
        p = str(tmp_path / "preprod.db")
        _seed_dbsync(p)
        with sqlite3.connect(p) as c:
            summ.summarize_version(c, "db-sync", DBSYNC_V, top=10)
        out = capsys.readouterr().out
        assert "Top 2 tables by row count" in out


class _Args:
    """Stand-in for the argparse.Namespace resolve_db consumes."""

    def __init__(self, **kw):
        self.env = kw.get("env")
        self.sqlite_db = kw.get("sqlite_db")
        self.role = kw.get("role")
        self.version = kw.get("version")
        self.top = kw.get("top", 10)


class TestDetectRoleError:
    def test_neither_version_table_raises(self, tmp_path):
        # A DB with metric tables but no *_version table can't be classified.
        p = str(tmp_path / "mystery.db")
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE memory_metrics (ts TEXT, rss REAL, version TEXT)")
        conn.commit()
        conn.close()
        with sqlite3.connect(p) as c:
            with pytest.raises(SystemExit):
                summ.detect_role(c)


class TestResolveDb:
    """resolve_db picks (db_path, role) from --sqlite-db / --role / --env. This
    branching had no coverage; each branch and its error path is exercised here.
    DATA_DIR is monkeypatched to tmp dirs so the real data/ tree is never touched."""

    def test_explicit_sqlite_db_autodetects_role(self, tmp_path):
        p = str(tmp_path / "x.db")
        _seed_node(p)
        db, role = summ.resolve_db(_Args(sqlite_db=p))
        assert db == p and role == "node"

    def test_explicit_sqlite_db_missing_raises(self, tmp_path):
        with pytest.raises(SystemExit):
            summ.resolve_db(_Args(sqlite_db=str(tmp_path / "nope.db")))

    def test_explicit_role_overrides_detection(self, tmp_path):
        # A node DB but --role db-sync forces db-sync without auto-detecting.
        p = str(tmp_path / "x.db")
        _seed_node(p)
        _db, role = summ.resolve_db(_Args(sqlite_db=p, role="db-sync"))
        assert role == "db-sync"

    def test_env_plus_role(self, tmp_path, monkeypatch):
        nd = tmp_path / "node"
        nd.mkdir()
        p = str(nd / "mainnet.db")
        _seed_node(p)
        monkeypatch.setattr(summ, "DATA_DIR", {"node": nd, "db-sync": tmp_path / "dbsync"})
        db, role = summ.resolve_db(_Args(env="mainnet", role="node"))
        assert db == p and role == "node"

    def test_env_plus_role_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summ, "DATA_DIR", {"node": tmp_path / "node", "db-sync": tmp_path / "dbsync"})
        with pytest.raises(SystemExit):
            summ.resolve_db(_Args(env="preview", role="node"))

    def test_env_autodetects_the_single_existing_db(self, tmp_path, monkeypatch):
        nd, dd = tmp_path / "node", tmp_path / "dbsync"
        nd.mkdir()
        dd.mkdir()
        p = str(nd / "preprod.db")
        _seed_node(p)
        monkeypatch.setattr(summ, "DATA_DIR", {"node": nd, "db-sync": dd})
        db, role = summ.resolve_db(_Args(env="preprod"))
        assert db == p and role == "node"

    def test_env_ambiguous_when_both_exist_raises(self, tmp_path, monkeypatch):
        nd, dd = tmp_path / "node", tmp_path / "dbsync"
        nd.mkdir()
        dd.mkdir()
        _seed_node(str(nd / "preprod.db"))
        _seed_dbsync(str(dd / "preprod.db"))
        monkeypatch.setattr(summ, "DATA_DIR", {"node": nd, "db-sync": dd})
        with pytest.raises(SystemExit):
            summ.resolve_db(_Args(env="preprod"))

    def test_env_none_existing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summ, "DATA_DIR", {"node": tmp_path / "node", "db-sync": tmp_path / "dbsync"})
        with pytest.raises(SystemExit):
            summ.resolve_db(_Args(env="preprod"))


class TestMain:
    """End-to-end through main(): argv parsing -> resolve_db -> render. Catches
    glue breakage the unit tests above each miss in isolation."""

    def test_overview_via_sqlite_db(self, tmp_path, monkeypatch, capsys):
        p = str(tmp_path / "mainnet.db")
        _seed_node(p)
        monkeypatch.setattr(sys, "argv", ["stats-summary.py", "--sqlite-db", p])
        summ.main()
        out = capsys.readouterr().out
        assert "role: node" in out
        assert "LSM-11.0.1" in out
        assert "(pass --version" in out  # overview footer

    def test_detail_via_version_flag(self, tmp_path, monkeypatch, capsys):
        p = str(tmp_path / "mainnet.db")
        _seed_node(p)
        monkeypatch.setattr(sys, "argv", ["stats-summary.py", "--sqlite-db", p, "--version", "LSM-11.0.1"])
        summ.main()
        out = capsys.readouterr().out
        assert "Sync state" in out and "Conway" in out
        assert "RTS / runtime" in out

    def test_no_versions_raises(self, tmp_path, monkeypatch):
        # Role is detectable (node_version table exists) but it has no rows, so
        # there is nothing to summarize -> a clean SystemExit, not a crash.
        p = str(tmp_path / "empty.db")
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE node_version (timestamp TEXT, version TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(sys, "argv", ["stats-summary.py", "--sqlite-db", p])
        with pytest.raises(SystemExit):
            summ.main()
