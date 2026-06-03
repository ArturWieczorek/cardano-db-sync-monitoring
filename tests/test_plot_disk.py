"""Tests for the on-disk size plotting (--metrics disk) in both plot scripts.

Coverage:
  - load_disk: column shape, GiB conversion, ts parsing, gap-break insertion.
  - plot_disk: lsm row shown only when an lsm subdir exists; GB y-axis; one
    total trace per version; filename tagged _disk_by_time.
  - render_disk: GRACEFUL no-op (never raises) when the disk_metrics table or
    rows are absent — this is the property that keeps `--metrics all` working
    on every pre-existing DB, which has no disk_metrics.
  - main(--metrics all): the regression guard the user explicitly asked for —
    on a DB with cpu_ram data but NO disk_metrics, `all` still renders the
    other plots and skips disk without crashing; once disk_metrics exists, the
    disk HTML appears too.

Figures are captured by monkeypatching Figure.write_html so plot_disk tests
don't litter disk; the main() integration test writes real HTML to tmp_path.
"""

import importlib.util
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objs as go
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


node_plot = _load("node_plot_module", "node-plot.py")
dbsync_plot = _load("db_sync_plot_module", "db-sync-plot.py")

# Both scripts implement the disk feature identically; run the shared behavioural
# tests against each module via parametrization.
PLOT_MODULES = [
    pytest.param(node_plot, "cardano-node", id="node"),
    pytest.param(dbsync_plot, "cardano-db-sync", id="db-sync"),
]


@pytest.fixture
def capture_figure(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    def fake_write_html(self: go.Figure, path: str, *args, **kwargs) -> None:
        captured["fig"] = self
        captured["path"] = path

    monkeypatch.setattr(go.Figure, "write_html", fake_write_html)
    return captured


GIB = 1024 ** 3


def _seed_disk(db_file: str, rows: list[tuple]) -> None:
    """rows: (ts, slot_no, path, total_bytes, lsm_bytes, version)."""
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS disk_metrics
               (ts TEXT, slot_no INTEGER, path TEXT,
                total_bytes INTEGER, lsm_bytes INTEGER, version TEXT)"""
        )
        conn.executemany("INSERT INTO disk_metrics VALUES (?,?,?,?,?,?)", rows)
        conn.commit()


# --- load_disk -------------------------------------------------------------


@pytest.mark.parametrize("mod,_label", PLOT_MODULES)
class TestLoadDisk:
    def test_columns_and_gib_conversion(self, mod, _label, tmp_path):
        db = str(tmp_path / "x.db")
        _seed_disk(db, [
            ("2026-06-03T10:00:00", None, "/db", 2 * GIB, 1 * GIB, "cardano-node v mainnet"),
        ])
        df = mod.load_disk(db, ["cardano-node v mainnet"])
        assert {"ts", "total_bytes", "lsm_bytes", "total_gb", "lsm_gb", "version"} <= set(df.columns)
        assert df["total_gb"].iloc[0] == pytest.approx(2.0)
        assert df["lsm_gb"].iloc[0] == pytest.approx(1.0)
        assert pd.api.types.is_datetime64_any_dtype(df["ts"])

    def test_filters_to_requested_versions(self, mod, _label, tmp_path):
        db = str(tmp_path / "x.db")
        _seed_disk(db, [
            ("2026-06-03T10:00:00", None, "/db", GIB, 0, "cardano-node A mainnet"),
            ("2026-06-03T10:00:00", None, "/db", GIB, 0, "cardano-node B mainnet"),
        ])
        df = mod.load_disk(db, ["cardano-node A mainnet"])
        assert set(df["version"].dropna().unique()) == {"cardano-node A mainnet"}

    def test_gap_break_inserts_nan_row(self, mod, _label, tmp_path):
        # Two samples >50s apart -> insert_gap_breaks adds a NaN marker row so
        # plotly breaks the line instead of drawing a cliff.
        db = str(tmp_path / "x.db")
        _seed_disk(db, [
            ("2026-06-03T10:00:00", None, "/db", GIB, 0, "cardano-node v mainnet"),
            ("2026-06-03T10:05:00", None, "/db", 2 * GIB, 0, "cardano-node v mainnet"),
        ])
        df = mod.load_disk(db, ["cardano-node v mainnet"])
        assert len(df) == 3  # 2 real + 1 gap-break marker
        assert df["total_gb"].isna().any()


# --- plot_disk -------------------------------------------------------------


def _disk_df(rows: list[tuple]) -> pd.DataFrame:
    """rows: (version, ts, total_bytes, lsm_bytes)."""
    df = pd.DataFrame([
        {"version": v, "ts": ts, "total_bytes": tb, "lsm_bytes": lb}
        for (v, ts, tb, lb) in rows
    ])
    df["ts"] = pd.to_datetime(df["ts"])
    df["total_gb"] = df["total_bytes"] / GIB
    df["lsm_gb"] = df["lsm_bytes"] / GIB
    return df


@pytest.mark.parametrize("mod,_label", PLOT_MODULES)
class TestPlotDisk:
    def test_lsm_present_adds_second_row(self, mod, _label, tmp_path, capture_figure):
        df = _disk_df([
            ("cardano-node LSM mainnet", "2026-06-03T10:00:00", 3 * GIB, 1 * GIB),
            ("cardano-node LSM mainnet", "2026-06-03T10:01:00", 4 * GIB, 1 * GIB),
        ])
        mod.plot_disk(df, ["cardano-node LSM mainnet"], str(tmp_path), "mainnet")
        fig = capture_figure["fig"]
        # Total + LSM traces => 2 traces for one version.
        names = [t.name for t in fig.data]
        assert any(n.startswith("Total -") for n in names)
        assert any(n.startswith("LSM -") for n in names)

    def test_lsm_absent_single_row_only(self, mod, _label, tmp_path, capture_figure):
        # In-memory build: lsm_bytes all 0 -> no LSM row/traces at all.
        df = _disk_df([
            ("cardano-node INMEM mainnet", "2026-06-03T10:00:00", 3 * GIB, 0),
            ("cardano-node INMEM mainnet", "2026-06-03T10:01:00", 4 * GIB, 0),
        ])
        mod.plot_disk(df, ["cardano-node INMEM mainnet"], str(tmp_path), "mainnet")
        names = [t.name for t in capture_figure["fig"].data]
        assert any(n.startswith("Total -") for n in names)
        assert not any(n.startswith("LSM -") for n in names)

    def test_mixed_comparison_shows_lsm_row(self, mod, _label, tmp_path, capture_figure):
        # LSM vs in-memory: lsm row IS shown (the in-memory line at zero is the point).
        df = _disk_df([
            ("cardano-node LSM mainnet", "2026-06-03T10:00:00", 3 * GIB, 1 * GIB),
            ("cardano-node INMEM mainnet", "2026-06-03T10:00:00", 3 * GIB, 0),
        ])
        versions = ["cardano-node LSM mainnet", "cardano-node INMEM mainnet"]
        mod.plot_disk(df, versions, str(tmp_path), "mainnet")
        names = [t.name for t in capture_figure["fig"].data]
        assert sum(n.startswith("Total -") for n in names) == 2
        assert sum(n.startswith("LSM -") for n in names) == 2

    def test_filename_tagged_by_time(self, mod, _label, tmp_path, capture_figure):
        df = _disk_df([("cardano-node v mainnet", "2026-06-03T10:00:00", GIB, 0)])
        mod.plot_disk(df, ["cardano-node v mainnet"], str(tmp_path), "mainnet")
        assert capture_figure["path"].endswith("_disk_by_time.html")

    def test_y_axis_is_gib(self, mod, _label, tmp_path, capture_figure):
        df = _disk_df([("cardano-node v mainnet", "2026-06-03T10:00:00", 5 * GIB, 0)])
        mod.plot_disk(df, ["cardano-node v mainnet"], str(tmp_path), "mainnet")
        assert capture_figure["fig"].layout.yaxis.title.text == "GiB"


# --- render_disk graceful-skip ---------------------------------------------


def _args(mod, db_file, tmp_path, metrics="disk"):
    return mod.Args(
        env="mainnet", sqlite_db=db_file, outdir=str(tmp_path / "plots"),
        versions=["cardano-node v mainnet"], list_only=False,
        x_axis="time", metrics=metrics,
    )


@pytest.mark.parametrize("mod,_label", PLOT_MODULES)
class TestRenderDiskGracefulSkip:
    def test_no_table_does_not_raise(self, mod, _label, tmp_path, capsys):
        db = str(tmp_path / "empty.db")
        sqlite3.connect(db).close()  # DB exists but has no disk_metrics table
        mod.render_disk(_args(mod, db, tmp_path), ["cardano-node v mainnet"])  # must NOT raise
        assert "Skipping disk plot" in capsys.readouterr().out

    def test_empty_rows_does_not_raise(self, mod, _label, tmp_path, capsys):
        db = str(tmp_path / "x.db")
        _seed_disk(db, [])  # table exists, no rows
        mod.render_disk(_args(mod, db, tmp_path), ["cardano-node v mainnet"])
        assert "Skipping disk plot" in capsys.readouterr().out

    def test_missing_version_does_not_raise(self, mod, _label, tmp_path, capsys):
        db = str(tmp_path / "x.db")
        _seed_disk(db, [
            ("2026-06-03T10:00:00", None, "/db", GIB, 0, "cardano-node OTHER mainnet"),
        ])
        mod.render_disk(_args(mod, db, tmp_path), ["cardano-node v mainnet"])
        assert "Skipping disk plot" in capsys.readouterr().out

    def test_present_rows_writes_html(self, mod, _label, tmp_path):
        db = str(tmp_path / "x.db")
        _seed_disk(db, [
            ("2026-06-03T10:00:00", None, "/db", GIB, 0, "cardano-node v mainnet"),
            ("2026-06-03T10:01:00", None, "/db", 2 * GIB, 0, "cardano-node v mainnet"),
        ])
        mod.render_disk(_args(mod, db, tmp_path), ["cardano-node v mainnet"])
        out = list((tmp_path / "plots").rglob("*_disk_by_time.html"))
        assert len(out) == 1


# --- main(--metrics all) regression guard ----------------------------------
#
# The existing `all` aborts (SystemExit) if the ingest/tables tables are
# missing, so to isolate the *disk* behaviour we seed a realistic full monitor
# DB (everything the current monitor writes) EXCEPT disk_metrics. That proves
# `all` reaches the disk stage and skips it gracefully rather than crashing.


def _seed_version_and_cpuram(conn: sqlite3.Connection, version_table: str, version: str) -> None:
    conn.execute(f"CREATE TABLE {version_table} (timestamp TEXT, version TEXT)")
    conn.execute(f"INSERT INTO {version_table} VALUES (?,?)", ("2026-06-03T10:00:00", version))
    conn.execute(
        """CREATE TABLE memory_metrics
           (ts TEXT, slot_no INTEGER, rss REAL, vms REAL, uss REAL,
            pss REAL, swap REAL, shared REAL, version TEXT)"""
    )
    conn.execute(
        """CREATE TABLE cpu_metrics
           (ts TEXT, slot_no INTEGER, cpu_percent REAL, user_time REAL,
            system_time REAL, children_user REAL, children_system REAL,
            iowait REAL, ctx_switches INTEGER, interrupts INTEGER, version TEXT)"""
    )
    for i in range(2):
        ts = f"2026-06-03T10:0{i}:00"
        conn.execute("INSERT INTO memory_metrics (ts, slot_no, rss, version) VALUES (?,?,?,?)",
                     (ts, 100 + i, 500.0 + i, version))
        conn.execute("INSERT INTO cpu_metrics (ts, slot_no, cpu_percent, version) VALUES (?,?,?,?)",
                     (ts, 100 + i, 10.0 + i, version))


def _seed_node_full(db_file: str, version: str) -> None:
    """Everything node-monitor.py writes (cpu_ram + node_ingest_metrics), no disk."""
    with sqlite3.connect(db_file) as conn:
        _seed_version_and_cpuram(conn, "node_version", version)
        conn.execute(
            """CREATE TABLE node_ingest_metrics
               (ts TEXT, slot_no INTEGER, epoch_no INTEGER, era TEXT,
                sync_progress REAL, version TEXT)"""
        )
        for i in range(2):
            conn.execute(
                "INSERT INTO node_ingest_metrics VALUES (?,?,?,?,?,?)",
                (f"2026-06-03T10:0{i}:00", 100 + i, 0, "Byron", 1.0 + i, version),
            )
        conn.commit()


def _seed_dbsync_full(db_file: str, version: str) -> None:
    """Everything db-sync-monitor.py writes (cpu_ram + ingest_metrics +
    table_rowcounts), no disk."""
    with sqlite3.connect(db_file) as conn:
        _seed_version_and_cpuram(conn, "db_sync_version", version)
        conn.execute(
            """CREATE TABLE ingest_metrics
               (ts TEXT, slot_no INTEGER, tip_lag_sec REAL, db_size_bytes INTEGER,
                max_block_no INTEGER, max_tx_id INTEGER, utxo_count INTEGER, version TEXT)"""
        )
        conn.execute(
            """CREATE TABLE table_rowcounts
               (ts TEXT, slot_no INTEGER, table_name TEXT, row_count INTEGER, version TEXT)"""
        )
        for i in range(2):
            ts = f"2026-06-03T10:0{i}:00"
            conn.execute(
                "INSERT INTO ingest_metrics (ts, slot_no, tip_lag_sec, db_size_bytes, "
                "max_block_no, max_tx_id, utxo_count, version) VALUES (?,?,?,?,?,?,?,?)",
                (ts, 100 + i, 10.0 - i, 1000 + i, 50 + i, 200 + i, None, version),
            )
            conn.execute(
                "INSERT INTO table_rowcounts (ts, slot_no, table_name, row_count, version) "
                "VALUES (?,?,?,?,?)",
                (ts, 100 + i, "block", 50 + i, version),
            )
        conn.commit()


def _run_main(mod, db, tmp_path, monkeypatch, script_name):
    outdir = str(tmp_path / "plots")
    argv = [script_name, "--env", "mainnet", "--sqlite-db", db,
            "--outdir", outdir, "--versions", "v", "--metrics", "all", "--x-axis", "time"]
    monkeypatch.setattr("sys.argv", argv)
    mod.main()


def test_node_all_skips_disk_when_absent(tmp_path, monkeypatch, capsys):
    """Core regression guard: `--metrics all` on a DB with no disk_metrics still
    renders cpu_ram + ingest and must NOT crash; disk is skipped gracefully."""
    db = str(tmp_path / "mainnet.db")
    _seed_node_full(db, "cardano-node v mainnet")
    _run_main(node_plot, db, tmp_path, monkeypatch, "node-plot.py")
    out = capsys.readouterr().out
    assert "Skipping disk plot" in out
    assert list((tmp_path / "plots").rglob("*_cpu_ram_by_time.html"))
    assert list((tmp_path / "plots").rglob("*_ingest_by_time.html"))
    assert not list((tmp_path / "plots").rglob("*_disk_by_time.html"))


def test_node_all_includes_disk_when_present(tmp_path, monkeypatch):
    """Once disk_metrics exists for the version, `all` emits the disk HTML too."""
    db = str(tmp_path / "mainnet.db")
    version = "cardano-node v mainnet"
    _seed_node_full(db, version)
    _seed_disk(db, [
        ("2026-06-03T10:00:00", None, "/db", GIB, 0, version),
        ("2026-06-03T10:01:00", None, "/db", 2 * GIB, 0, version),
    ])
    _run_main(node_plot, db, tmp_path, monkeypatch, "node-plot.py")
    assert list((tmp_path / "plots").rglob("*_disk_by_time.html"))


def test_dbsync_all_skips_disk_when_absent(tmp_path, monkeypatch, capsys):
    """Same guard on the db-sync side (its `all` also includes tables)."""
    db = str(tmp_path / "mainnet.db")
    _seed_dbsync_full(db, "cardano-db-sync v mainnet")
    _run_main(dbsync_plot, db, tmp_path, monkeypatch, "db-sync-plot.py")
    out = capsys.readouterr().out
    assert "Skipping disk plot" in out
    assert list((tmp_path / "plots").rglob("*_cpu_ram_by_time.html"))
    assert list((tmp_path / "plots").rglob("*_tables_by_time.html"))
    assert not list((tmp_path / "plots").rglob("*_disk_by_time.html"))


def test_dbsync_all_includes_disk_when_present(tmp_path, monkeypatch):
    db = str(tmp_path / "mainnet.db")
    version = "cardano-db-sync v mainnet"
    _seed_dbsync_full(db, version)
    _seed_disk(db, [
        ("2026-06-03T10:00:00", None, "/ls", GIB, 0, version),
        ("2026-06-03T10:01:00", None, "/ls", 2 * GIB, 0, version),
    ])
    _run_main(dbsync_plot, db, tmp_path, monkeypatch, "db-sync-plot.py")
    assert list((tmp_path / "plots").rglob("*_disk_by_time.html"))
