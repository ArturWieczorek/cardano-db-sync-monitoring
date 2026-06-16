"""Tests for the disk-size collectors (node db dir + db-sync ledger dir).

Covers the shared core in _disk_size.py (du_bytes, parse_path_flag, schema,
sampling, lsm-present-vs-absent) plus the per-role wiring of both subclasses
(label format, target DB path, process matching incl. db-sync's no-env rule).

Process matching is exercised against a tiny fake that mimics the bits of
psutil.Process the matcher reads (.info dict), so no real processes are needed.
"""

# Import the two concrete subclasses by loading the hyphenated script modules.
import importlib.util
import sqlite3
from pathlib import Path

from _disk_size import du_bytes, parse_path_flag

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


node_mod = _load("node_db_size_monitor", "node-db-size-monitor.py")
dbsync_mod = _load("db_sync_ledger_size_monitor", "db-sync-ledger-size-monitor.py")
NodeDbSizeMonitor = node_mod.NodeDbSizeMonitor
DbSyncLedgerSizeMonitor = dbsync_mod.DbSyncLedgerSizeMonitor


class FakeProc:
    """Minimal stand-in for psutil.Process for the matcher (reads .info only)."""

    def __init__(self, cmdline, name=None):
        self.info = {"cmdline": cmdline, "name": name}


# --- du_bytes --------------------------------------------------------------


class TestDuBytes:
    def test_counts_file_contents(self, tmp_path):
        (tmp_path / "a").write_bytes(b"x" * 1000)
        (tmp_path / "b").write_bytes(b"y" * 2000)
        size = du_bytes(str(tmp_path), timeout=30)
        # Apparent size includes the dir entry overhead, so >= the file bytes.
        assert size is not None and size >= 3000

    def test_nonexistent_path_returns_none(self, tmp_path):
        assert du_bytes(str(tmp_path / "nope"), timeout=30) is None

    def test_empty_dir_is_nonnegative(self, tmp_path):
        size = du_bytes(str(tmp_path), timeout=30)
        assert size is not None and size >= 0


# --- parse_path_flag -------------------------------------------------------


class TestParsePathFlag:
    def test_space_form_absolute(self):
        cmd = ["cardano-node", "run", "--database-path", "/abs/db", "--config", "c"]
        assert parse_path_flag(cmd, "/home/u", "--database-path") == "/abs/db"

    def test_equals_form_absolute(self):
        cmd = ["cardano-node", "run", "--database-path=/abs/db"]
        assert parse_path_flag(cmd, "/home/u", "--database-path") == "/abs/db"

    def test_relative_resolved_against_cwd(self):
        cmd = ["cardano-node", "run", "--database-path", "LSM-mainnet/db"]
        assert parse_path_flag(cmd, "/home/u/node", "--database-path") == "/home/u/node/LSM-mainnet/db"

    def test_state_dir_flag(self):
        cmd = ["cardano-db-sync", "--state-dir", "/data/ledger-state/lsm-mainnet"]
        assert parse_path_flag(cmd, "/x", "--state-dir") == "/data/ledger-state/lsm-mainnet"

    def test_missing_flag_returns_none(self):
        cmd = ["cardano-node", "run", "--config", "c"]
        assert parse_path_flag(cmd, "/x", "--database-path") is None

    def test_flag_as_last_token_returns_none(self):
        # `--database-path` with no following value must not IndexError.
        cmd = ["cardano-node", "run", "--database-path"]
        assert parse_path_flag(cmd, "/x", "--database-path") is None


# --- subclass wiring -------------------------------------------------------


def _node(tmp_path, **kw):
    defaults = dict(
        env="mainnet",
        version="LSM-11.0.1",
        explicit_path=None,
        lsm_subdir="lsm",
        interval=1,
        du_timeout=30,
        emit_json=False,
        sqlite_db=str(tmp_path / "node.db"),
    )
    defaults.update(kw)
    return NodeDbSizeMonitor(**defaults)


def _dbsync(tmp_path, **kw):
    defaults = dict(
        env="mainnet",
        version="LSM-13.7.1.0",
        explicit_path=None,
        lsm_subdir="lsm",
        interval=1,
        du_timeout=30,
        emit_json=False,
        sqlite_db=str(tmp_path / "dbsync.db"),
    )
    defaults.update(kw)
    return DbSyncLedgerSizeMonitor(**defaults)


class TestRunLabel:
    def test_node_label(self, tmp_path):
        assert _node(tmp_path).run_label == "cardano-node LSM-11.0.1 mainnet"

    def test_dbsync_label(self, tmp_path):
        assert _dbsync(tmp_path).run_label == "cardano-db-sync LSM-13.7.1.0 mainnet"

    def test_default_db_paths(self, tmp_path):
        # Default (no --sqlite-db) points at the role's data dir.
        n = NodeDbSizeMonitor(
            env="preprod",
            version="v",
            explicit_path="/x",
            lsm_subdir="lsm",
            interval=1,
            du_timeout=1,
            emit_json=False,
            sqlite_db=None,
        )
        d = DbSyncLedgerSizeMonitor(
            env="preprod",
            version="v",
            explicit_path="/x",
            lsm_subdir="lsm",
            interval=1,
            du_timeout=1,
            emit_json=False,
            sqlite_db=None,
        )
        assert n.db_file.endswith("data/cardano-node/preprod.db")
        assert d.db_file.endswith("data/cardano-db-sync/preprod.db")


class TestProcessMatching:
    def test_node_matches_versioned_binary_with_env(self, tmp_path):
        m = _node(tmp_path)
        proc = FakeProc(["/opt/cardano-node-11.0.1", "run", "--config", "LSM-mainnet/config.json"])
        assert m._match_process(proc) is True

    def test_node_requires_env_in_argv(self, tmp_path):
        # env=mainnet but argv mentions only preprod -> no match.
        m = _node(tmp_path)
        proc = FakeProc(["/opt/cardano-node", "run", "--config", "preprod/config.json"])
        assert m._match_process(proc) is False

    def test_node_match_arg_disambiguates(self, tmp_path):
        m = _node(tmp_path, match_arg="LSM-mainnet")
        lsm = FakeProc(["/opt/cardano-node", "run", "--database-path", "LSM-mainnet/db"])
        inmem = FakeProc(["/opt/cardano-node", "run", "--database-path", "inmem-mainnet/db"])
        assert m._match_process(lsm) is True
        assert m._match_process(inmem) is False

    def test_dbsync_matches_without_env_in_argv(self, tmp_path):
        # db-sync has no env flag; prefix alone matches even with no 'mainnet' arg.
        m = _dbsync(tmp_path)
        proc = FakeProc(["/usr/local/bin/cardano-db-sync", "--state-dir", "/data/ls"])
        assert m._match_process(proc) is True

    def test_dbsync_match_arg(self, tmp_path):
        m = _dbsync(tmp_path, match_arg="lsm-state")
        ok = FakeProc(["cardano-db-sync", "--state-dir", "/data/lsm-state"])
        no = FakeProc(["cardano-db-sync", "--state-dir", "/data/inmem-state"])
        assert m._match_process(ok) is True
        assert m._match_process(no) is False

    def test_non_matching_binary_rejected(self, tmp_path):
        m = _node(tmp_path)
        assert m._match_process(FakeProc(["/usr/bin/xed", "config.json"])) is False
        assert m._match_process(FakeProc([])) is False


# --- schema + sampling -----------------------------------------------------


class TestSchemaAndSampling:
    def test_init_creates_disk_metrics_in_wal(self, tmp_path):
        m = _node(tmp_path, explicit_path="/x")
        with sqlite3.connect(m.db_file) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert "disk_metrics" in tables
        assert mode.lower() == "wal"

    def test_init_is_idempotent(self, tmp_path):
        _node(tmp_path, explicit_path="/x")
        _node(tmp_path, explicit_path="/x")  # second init must not raise

    def test_sample_lsm_present(self, tmp_path):
        dbdir = tmp_path / "db"
        (dbdir / "immutable").mkdir(parents=True)
        (dbdir / "lsm").mkdir()
        (dbdir / "immutable" / "chunk").write_bytes(b"0" * 5000)
        (dbdir / "lsm" / "sst").write_bytes(b"0" * 1500)
        m = _node(tmp_path, explicit_path=str(dbdir))
        result = m.sample_once(str(dbdir))
        assert result is not None
        _ts, total, lsm = result
        assert total >= 6500
        assert lsm >= 1500
        # Row landed under the right version label.
        with sqlite3.connect(m.db_file) as conn:
            row = conn.execute("SELECT total_bytes, lsm_bytes, version FROM disk_metrics").fetchone()
        assert row[0] == total and row[1] == lsm
        assert row[2] == "cardano-node LSM-11.0.1 mainnet"

    def test_sample_lsm_absent_records_zero(self, tmp_path):
        # In-memory build: no lsm/ subdir -> lsm_bytes must be 0, not None/error.
        dbdir = tmp_path / "db"
        (dbdir / "immutable").mkdir(parents=True)
        (dbdir / "immutable" / "chunk").write_bytes(b"0" * 4000)
        m = _node(tmp_path, explicit_path=str(dbdir))
        result = m.sample_once(str(dbdir))
        assert result is not None
        _ts, total, lsm = result
        assert total >= 4000
        assert lsm == 0

    def test_sample_returns_none_on_bad_path(self, tmp_path):
        m = _node(tmp_path, explicit_path="/x")
        assert m.sample_once(str(tmp_path / "does-not-exist")) is None

    def test_resolve_path_prefers_explicit(self, tmp_path):
        m = _node(tmp_path, explicit_path="/explicit/db")
        assert m.resolve_path() == "/explicit/db"

    def test_report_existing_counts(self, tmp_path, capsys):
        dbdir = tmp_path / "db"
        dbdir.mkdir()
        (dbdir / "f").write_bytes(b"0" * 1000)
        m = _node(tmp_path, explicit_path=str(dbdir))
        m.report_existing()
        assert "no existing disk samples" in capsys.readouterr().out
        m.sample_once(str(dbdir))
        m.report_existing()
        assert "already has 1 disk samples" in capsys.readouterr().out
