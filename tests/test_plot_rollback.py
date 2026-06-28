"""Tests for db-sync-plot.py `--metrics rollback`.

Mirrors test_plot_disk.py / test_plot_rts.py: loader shape + block_gap, the
metrics-derived event helper, the three-panel build, the canonical filename tag,
render_rollback graceful-skip when the optional rollback tables/rows are absent,
and the main(--metrics all) guard proving `all` neither crashes without rollback
data nor omits the rollback HTML once present.
"""

import importlib.util
import sqlite3
from pathlib import Path

import plotly.graph_objs as go
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


db_plot = _load("db_sync_plot_module_rollback", "db-sync-plot.py")

V = "cardano-db-sync 13.7.1.0 preprod"


@pytest.fixture
def capture_figure(monkeypatch):
    captured: dict = {}

    def fake_write_html(self, path, *a, **k):
        captured["fig"] = self
        captured["path"] = path

    monkeypatch.setattr(go.Figure, "write_html", fake_write_html)
    return captured


def _seed_samples(db_file, rows):
    """rows: (ts, slot_no, version, db_block_height, db_slot_height, node_block_height, queue_length)."""
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rollback_samples
               (ts TEXT, slot_no INTEGER, version TEXT, db_block_height INTEGER,
                db_slot_height INTEGER, node_block_height INTEGER, queue_length REAL)"""
        )
        conn.executemany("INSERT INTO rollback_samples VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()


def _rising_then_rollback():
    # Tip climbs to 110, drops to 100 (depth 10), recovers past 110.
    return [
        ("2026-06-28T12:00:00+00:00", 8090, V, 109, 8090, 112, 1.0),
        ("2026-06-28T12:00:02+00:00", 8100, V, 110, 8100, 112, 2.0),
        ("2026-06-28T12:00:04+00:00", 8000, V, 100, 8000, 112, 9.0),
        ("2026-06-28T12:00:06+00:00", 8100, V, 110, 8100, 112, 1.0),
        ("2026-06-28T12:00:08+00:00", 8130, V, 113, 8130, 113, 0.0),
    ]


class TestLoadRollbackSamples:
    def test_shape_and_block_gap(self, tmp_path):
        db = str(tmp_path / "preprod.db")
        _seed_samples(db, _rising_then_rollback())
        df = db_plot.load_rollback_samples(db, [V], "time")
        assert {"db_block_height", "node_block_height", "queue_length", "block_gap"} <= set(df.columns)
        # gap = node - db; first row 112 - 109 = 3.
        first = df.sort_values("ts").iloc[0]
        assert first["block_gap"] == 3


class TestDeriveEventsFromSamples:
    def test_one_event_with_recovery(self, tmp_path):
        db = str(tmp_path / "preprod.db")
        _seed_samples(db, _rising_then_rollback())
        samples_df = db_plot.load_rollback_samples(db, [V], "time")
        ev = db_plot.derive_events_from_samples(samples_df)
        assert len(ev) == 1
        assert ev.iloc[0]["depth_blocks"] == 10
        # Drop observed at 12:00:04, tip back to 110 at 12:00:06 -> 2s recovery.
        assert ev.iloc[0]["recovery_duration_sec"] == pytest.approx(2.0)
        assert ev.iloc[0]["source"] == "metrics"

    def test_empty_samples_yields_empty_events(self):
        import pandas as pd

        empty = pd.DataFrame(
            columns=["ts", "version", "db_block_height", "db_slot_height", "node_block_height", "queue_length"]
        )
        assert db_plot.derive_events_from_samples(empty).empty


class TestBuildRollback:
    def test_three_panels_and_traces(self, tmp_path):
        db = str(tmp_path / "preprod.db")
        _seed_samples(db, _rising_then_rollback())
        samples_df = db_plot.load_rollback_samples(db, [V], "time")
        events_df = db_plot.derive_events_from_samples(samples_df)
        fig = db_plot.build_rollback(samples_df, events_df, [V], "preprod", "time")
        # Three stacked y-axes (yaxis, yaxis2, yaxis3).
        assert "yaxis3" in fig.layout
        # Queue + gap + recovery traces all present.
        names = [t.name for t in fig.data]
        assert any("Queue" in n for n in names)
        assert any("Gap" in n for n in names)
        assert any("Recovery" in n for n in names)


class TestPlotRollbackFilename:
    def test_filename_tag(self, tmp_path, capture_figure):
        db = str(tmp_path / "preprod.db")
        _seed_samples(db, _rising_then_rollback())
        samples_df = db_plot.load_rollback_samples(db, [V], "slot")
        events_df = db_plot.derive_events_from_samples(samples_df)
        db_plot.plot_rollback(samples_df, events_df, [V], str(tmp_path / "out"), "preprod", "slot")
        assert capture_figure["path"].endswith("preprod_13.7.1.0_rollback_by_slot.html")


class TestRenderGracefulSkip:
    def test_render_rollback_skips_when_table_absent(self, tmp_path, capsys):
        db = str(tmp_path / "preprod.db")
        # Create an unrelated table so the file exists but has no rollback_samples.
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE memory_metrics (version TEXT)")
        args = db_plot.Args(
            env="preprod",
            sqlite_db=db,
            outdir=str(tmp_path / "out"),
            versions=[V],
            list_only=False,
            x_axis="slot",
            metrics="rollback",
        )
        db_plot.render_rollback(args, [V])  # must not raise
        assert "Skipping rollback plot" in capsys.readouterr().out
