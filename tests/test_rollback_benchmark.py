"""Tests for the controlled rollback benchmark (scripts/db-sync-rollback-benchmark.py).

The side-effecting steps (running cardano-db-tool, restoring the snapshot,
running db-sync-compare) are stubbed on the instance, so the orchestration -
repetition loop, row insertion, stats aggregation, equivalence wiring, and the
destructive-reps guard - is exercised against a real SQLite DB under tmp_path
without any external binaries.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "db_sync_rollback_benchmark", SCRIPTS / "db-sync-rollback-benchmark.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rbb = _load_module()

LABEL = "cardano-db-sync 13.7.1.0 preprod"


def _bench(db_file: str, reps: int, restore_cmd: str | None = "true", compare_cmd: str | None = None):  # type: ignore[no-untyped-def]
    return rbb.RollbackBenchmark(
        env="preprod",
        db_sync_ver="13.7.1.0",
        db_tool="/path/to/cardano-db-tool",
        to_slot=89999000,
        from_slot=90000000,
        reps=reps,
        restore_cmd=restore_cmd,
        compare_cmd=compare_cmd,
        tool_args=[],
        pgpassfile=None,
        emit_json=False,
        sqlite_db=db_file,
    )


class TestSchema:
    def test_init_creates_benchmark_table(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        _bench(db, reps=1)
        with sqlite3.connect(db) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "rollback_benchmarks" in names

    def test_registered_in_version_keyed_tables(self) -> None:
        from _common import VERSION_KEYED_TABLES

        assert "rollback_benchmarks" in VERSION_KEYED_TABLES["db-sync"]


class TestRunLoop:
    def test_records_one_row_per_repetition(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        m = _bench(db, reps=3)
        durations = iter([2.0, 4.0, 6.0])
        m._invoke_tool = lambda: (0, "Rollback: Successfully deleted 137 blocks", next(durations), 50.0, 120.0)
        m._restore_snapshot = lambda: None
        out = m.run()
        assert out["stats"]["n"] == 3
        assert out["stats"]["median"] == pytest.approx(4.0)
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT repetition, delete_duration_sec, depth_blocks, version, from_slot, to_slot, peak_rss_mib "
                "FROM rollback_benchmarks ORDER BY repetition"
            ).fetchall()
        assert [r[0] for r in rows] == [0, 1, 2]
        assert [r[1] for r in rows] == [2.0, 4.0, 6.0]
        assert all(r[2] == 137 for r in rows)  # parsed from db-tool output
        assert all(r[3] == LABEL for r in rows)
        assert rows[0][4] == 90000000 and rows[0][5] == 89999000
        assert all(r[6] == 120.0 for r in rows)

    def test_restore_runs_before_every_rep(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        m = _bench(db, reps=3)
        order: list[str] = []
        m._invoke_tool = lambda: (0, "", 1.0, None, None)
        m._restore_snapshot = lambda: order.append("restore")

        orig_run_once = m.run_once

        def _run_once(rep: int):  # type: ignore[no-untyped-def]
            order.append(f"rep{rep}")
            return orig_run_once(rep)

        m.run_once = _run_once
        m.run()
        # Every rep is preceded by a restore so rep 0 isn't a warm-cache outlier.
        assert order.count("restore") == 3
        assert order == ["restore", "rep0", "restore", "rep1", "restore", "rep2"]

    def test_equivalence_flag_recorded(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        m = _bench(db, reps=1, compare_cmd="db-sync-compare ...")
        m._invoke_tool = lambda: (0, "", 1.0, None, None)
        m._run_compare = lambda: True
        m.run()
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT equivalence_ok FROM rollback_benchmarks").fetchone()[0] == 1

    def test_no_compare_leaves_equivalence_null(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preprod.db")
        m = _bench(db, reps=1, compare_cmd=None)
        m._invoke_tool = lambda: (0, "", 1.0, None, None)
        m.run()
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT equivalence_ok FROM rollback_benchmarks").fetchone()[0] is None


class TestDestructiveRepsGuard:
    def test_warns_when_reps_gt_one_without_restore(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db = str(tmp_path / "preprod.db")
        m = _bench(db, reps=2, restore_cmd=None)
        m._invoke_tool = lambda: (0, "", 1.0, None, None)
        m.run()
        assert "without --restore-cmd" in capsys.readouterr().err


class TestParseDeletedBlocks:
    def test_returns_none_when_absent(self) -> None:
        assert rbb.RollbackBenchmark._parse_deleted_blocks("nothing useful here") is None

    def test_parses_count(self) -> None:
        assert rbb.RollbackBenchmark._parse_deleted_blocks("Successfully deleted 42 blocks") == 42


class TestSamplePeaks:
    def test_returns_rss_for_a_real_short_subprocess(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        m = _bench(str(tmp_path / "preprod.db"), reps=1)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
        try:
            peak = m._sample_peaks(proc.pid)
        finally:
            proc.wait()
        # A live process always yields at least one RSS reading (the immediate one).
        assert peak is not None and peak > 0

    def test_returns_none_for_dead_pid(self, tmp_path: Path) -> None:
        m = _bench(str(tmp_path / "preprod.db"), reps=1)
        # PID 2^31-1 is effectively never a live process.
        assert m._sample_peaks(2**31 - 1) is None
