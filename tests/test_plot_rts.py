"""Tests for node-plot.py `--metrics rts` (RTS/runtime metrics from
node-rts-monitor.py).

Mirrors test_plot_disk.py: loader shape + gap breaks, plotter (one subplot per
metric, filename tag), render_rts graceful-skip when the optional rts_metrics
table/rows are absent, and the main(--metrics all) guard proving `all` neither
crashes without rts data nor omits the rts HTML once present.
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


node_plot = _load("node_plot_module_rts", "node-plot.py")


@pytest.fixture
def capture_figure(monkeypatch):
    captured: dict = {}

    def fake_write_html(self, path, *a, **k):
        captured["fig"] = self
        captured["path"] = path

    monkeypatch.setattr(go.Figure, "write_html", fake_write_html)
    return captured


def _seed_rts(db_file, rows):
    """rows: (ts, slot_no, metric, value, version)."""
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rts_metrics
               (ts TEXT, slot_no INTEGER, metric TEXT, value REAL, version TEXT)"""
        )
        conn.executemany("INSERT INTO rts_metrics VALUES (?,?,?,?,?)", rows)
        conn.commit()


V = "cardano-node LSM-11.0.1 mainnet"


# --- load_rts --------------------------------------------------------------


class TestLoadRts:
    def test_shape_and_versions(self, tmp_path):
        db = str(tmp_path / "x.db")
        _seed_rts(
            db,
            [
                ("2026-06-03T10:00:00", 100, "gcMinorNum", 5.0, V),
                ("2026-06-03T10:00:00", 100, "gcMinorNum", 5.0, "cardano-node OTHER mainnet"),
            ],
        )
        df = node_plot.load_rts(db, [V], "slot")
        assert {"ts", "slot_no", "metric", "value", "version"} <= set(df.columns)
        assert set(df["version"].dropna().unique()) == {V}

    def test_gap_break_per_metric(self, tmp_path):
        # 10s RTS cadence with a ~1h outage. Adaptive threshold (5x 10s = 50s)
        # ignores the normal samples and breaks the line across the outage.
        db = str(tmp_path / "x.db")
        _seed_rts(
            db,
            [
                ("2026-06-03T10:00:00", 100, "gcMinorNum", 5.0, V),
                ("2026-06-03T10:00:10", 101, "gcMinorNum", 6.0, V),
                ("2026-06-03T10:00:20", 102, "gcMinorNum", 7.0, V),
                # ~1h outage here
                ("2026-06-03T11:00:00", 200, "gcMinorNum", 9.0, V),
                ("2026-06-03T11:00:10", 201, "gcMinorNum", 9.5, V),
            ],
        )
        df = node_plot.load_rts(db, [V], "time")
        assert df["value"].isna().sum() == 1  # exactly one NaN gap-break marker
        assert df["value"].notna().sum() == 5  # normal 10s cadence not broken


# --- plot_rts --------------------------------------------------------------


class TestPlotRts:
    def test_one_subplot_trace_per_metric_per_version(self, tmp_path, capture_figure):
        import pandas as pd

        data = pd.DataFrame(
            [
                {
                    "ts": pd.Timestamp("2026-06-03T10:00:00"),
                    "slot_no": 100,
                    "metric": "gcMinorNum",
                    "value": 5.0,
                    "version": V,
                },
                {
                    "ts": pd.Timestamp("2026-06-03T10:00:10"),
                    "slot_no": 101,
                    "metric": "gcMinorNum",
                    "value": 6.0,
                    "version": V,
                },
                {
                    "ts": pd.Timestamp("2026-06-03T10:00:00"),
                    "slot_no": 100,
                    "metric": "gcLiveBytes",
                    "value": 4.2e9,
                    "version": V,
                },
            ]
        )
        node_plot.plot_rts(data, [V], str(tmp_path), "mainnet", "slot")
        names = [t.name for t in capture_figure["fig"].data]
        assert any("gcMinorNum" in n for n in names)
        assert any("gcLiveBytes" in n for n in names)

    def test_filename_tag(self, tmp_path, capture_figure):
        import pandas as pd

        data = pd.DataFrame(
            [
                {
                    "ts": pd.Timestamp("2026-06-03T10:00:00"),
                    "slot_no": 100,
                    "metric": "gcMinorNum",
                    "value": 5.0,
                    "version": V,
                },
            ]
        )
        node_plot.plot_rts(data, [V], str(tmp_path), "mainnet", "slot")
        assert capture_figure["path"].endswith("_rts_by_slot.html")
        node_plot.plot_rts(data, [V], str(tmp_path), "mainnet", "time")
        assert capture_figure["path"].endswith("_rts_by_time.html")

    def test_y_axes_are_labelled_per_metric(self, tmp_path, capture_figure):
        import pandas as pd

        # Two metrics with different units -> two subplots, each y-axis labelled.
        data = pd.DataFrame(
            [
                {
                    "ts": pd.Timestamp("2026-06-03T10:00:00"),
                    "slot_no": 100,
                    "metric": "rts_gc_current_bytes_used",
                    "value": 4.2e9,
                    "version": V,
                },
                {
                    "ts": pd.Timestamp("2026-06-03T10:00:00"),
                    "slot_no": 100,
                    "metric": "rts_gc_num_gcs",
                    "value": 12.0,
                    "version": V,
                },
            ]
        )
        node_plot.plot_rts(data, [V], str(tmp_path), "mainnet", "slot")
        layout = capture_figure["fig"].layout
        # Metrics are sorted by name: bytes_used (yaxis), num_gcs (yaxis2).
        titles = {layout["yaxis"].title.text, layout["yaxis2"].title.text}
        assert titles == {"bytes", "count"}

    def test_panels_dominate_over_gaps_with_many_metrics(self, tmp_path, capture_figure):
        # Regression for the layout bug: a fixed *fraction* vertical_spacing made
        # the gaps balloon to ~80% of the figure with many metrics (tiny panels).
        # The panels must collectively own most of the figure height.
        import pandas as pd

        rows = [
            {
                "ts": pd.Timestamp("2026-06-03T10:00:00"),
                "slot_no": 100,
                "metric": f"rts_metric_{i:02d}",
                "value": float(i),
                "version": V,
            }
            for i in range(30)
        ]
        node_plot.plot_rts(pd.DataFrame(rows), [V], str(tmp_path), "mainnet", "slot")
        layout = capture_figure["fig"].layout.to_plotly_json()
        domains = [v["domain"] for k, v in layout.items() if k.startswith("yaxis") and "domain" in v]
        # 30 detail panels + 1 overview panel (all share the "value" unit group).
        assert len(domains) == 31
        panel_fraction = sum(hi - lo for lo, hi in domains)
        assert panel_fraction > 0.7  # panels dominate; gaps are a small remainder


class TestRtsGrouping:
    """Overview-by-unit + per-metric detail layout."""

    def _multi_unit_df(self):
        import pandas as pd

        ts = pd.Timestamp("2026-06-03T10:00:00")
        specs = [
            ("rts_gc_current_bytes_used", 4.0e9),  # bytes
            ("rts_gc_max_bytes_used", 5.0e9),  # bytes
            ("rts_gc_cpu_ms", 12.0),  # milliseconds
            ("rts_gc_wall_ms", 20.0),  # milliseconds
            ("rts_gc_num_gcs", 7.0),  # count (single -> no overview)
        ]
        return pd.DataFrame([{"ts": ts, "slot_no": 100, "metric": m, "value": val, "version": V} for m, val in specs])

    def test_groups_ordered_by_unit(self):
        metrics = ["rts_gc_wall_ms", "rts_gc_current_bytes_used", "rts_gc_num_gcs"]
        groups = node_plot._rts_unit_groups(metrics)
        units = [u for u, _ in groups]
        # bytes before milliseconds before count (per _RTS_UNIT_ORDER).
        assert units == ["bytes", "milliseconds", "count"]

    def test_overview_panels_only_for_multi_metric_groups(self, tmp_path, capture_figure):
        node_plot.plot_rts(self._multi_unit_df(), [V], str(tmp_path), "mainnet", "slot")
        titles = [a.text for a in capture_figure["fig"].layout.annotations]
        overview = [t for t in titles if t.startswith("Overview")]
        # bytes (2 metrics) and milliseconds (2) get overviews; count (1) does not.
        assert overview == ["Overview - bytes (log y)", "Overview - milliseconds (log y)"]
        # Every metric still has its own detail panel below.
        for m in ("rts_gc_current_bytes_used", "rts_gc_cpu_ms", "rts_gc_num_gcs"):
            assert m in titles

    def test_bytes_overview_uses_log_y(self, tmp_path, capture_figure):
        node_plot.plot_rts(self._multi_unit_df(), [V], str(tmp_path), "mainnet", "slot")
        # First panel is the bytes overview -> log y; count detail stays linear.
        assert capture_figure["fig"].layout["yaxis"].type == "log"


class TestRtsYUnit:
    """The y-axis unit inferred from each RTS metric name (there is no single
    shared unit; metrics mix bytes/ms/counts/ticks)."""

    @pytest.mark.parametrize(
        "metric,unit",
        [
            ("cardano_node_metrics_RTS_gcLiveBytes_int", "bytes"),
            ("cardano_node_metrics_RTS_alloc_int", "bytes"),
            ("rts_gc_current_bytes_used", "bytes"),
            ("rts_gc_par_max_bytes_copied", "bytes"),
            ("cardano_node_metrics_RTS_gcMajorNum_int", "count"),
            ("rts_gc_num_gcs", "count"),
            ("rts_gc_num_bytes_usage_samples", "count"),  # count, despite "bytes" in name
            ("cardano_node_metrics_RTS_threads_int", "count"),
            ("rts_gc_cpu_ms", "milliseconds"),
            ("rts_gc_nm_sync_max_elapsed_ms", "milliseconds"),
            ("cardano_node_metrics_RTS_gcticks_int", "ticks"),
            ("rts_gc_peak_megabytes_allocated", "megabytes"),
            ("some_unknown_metric", "value"),
        ],
    )
    def test_unit_mapping(self, metric, unit):
        assert node_plot._rts_y_unit(metric) == unit


# --- render_rts graceful-skip ----------------------------------------------


def _args(db, tmp_path):
    return node_plot.Args(
        env="mainnet",
        sqlite_db=db,
        outdir=str(tmp_path / "plots"),
        versions=[V],
        list_only=False,
        x_axis="slot",
        metrics="rts",
    )


class TestRenderRtsGracefulSkip:
    def test_no_table_does_not_raise(self, tmp_path, capsys):
        db = str(tmp_path / "empty.db")
        sqlite3.connect(db).close()
        node_plot.render_rts(_args(db, tmp_path), [V])  # must not raise
        assert "Skipping rts plot" in capsys.readouterr().out

    def test_empty_rows_does_not_raise(self, tmp_path, capsys):
        db = str(tmp_path / "x.db")
        _seed_rts(db, [])
        node_plot.render_rts(_args(db, tmp_path), [V])
        assert "Skipping rts plot" in capsys.readouterr().out

    def test_present_rows_writes_html(self, tmp_path):
        db = str(tmp_path / "x.db")
        _seed_rts(
            db,
            [
                ("2026-06-03T10:00:00", 100, "gcMinorNum", 5.0, V),
                ("2026-06-03T10:00:10", 101, "gcMinorNum", 6.0, V),
            ],
        )
        node_plot.render_rts(_args(db, tmp_path), [V])
        assert list((tmp_path / "plots").rglob("*_rts_by_slot.html"))


# --- main(--metrics all) ---------------------------------------------------


def _seed_node_min(db_file, version):
    """Minimal monitor DB so main() can resolve the version + render cpu_ram;
    deliberately no rts_metrics so `all` must skip rts gracefully."""
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE node_version (timestamp TEXT, version TEXT)")
        conn.execute("INSERT INTO node_version VALUES (?,?)", ("2026-06-03T10:00:00", version))
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
        conn.execute(
            """CREATE TABLE node_ingest_metrics
               (ts TEXT, slot_no INTEGER, epoch_no INTEGER, era TEXT,
                sync_progress REAL, version TEXT)"""
        )
        for i in range(2):
            ts = f"2026-06-03T10:0{i}:00"
            conn.execute(
                "INSERT INTO memory_metrics (ts, slot_no, rss, version) VALUES (?,?,?,?)",
                (ts, 100 + i, 500.0 + i, version),
            )
            conn.execute(
                "INSERT INTO cpu_metrics (ts, slot_no, cpu_percent, version) VALUES (?,?,?,?)",
                (ts, 100 + i, 10.0 + i, version),
            )
            conn.execute(
                "INSERT INTO node_ingest_metrics VALUES (?,?,?,?,?,?)", (ts, 100 + i, 0, "Byron", 1.0 + i, version)
            )
        conn.commit()


def test_all_skips_rts_when_absent(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "mainnet.db")
    _seed_node_min(db, V)
    argv = [
        "node-plot.py",
        "--env",
        "mainnet",
        "--sqlite-db",
        db,
        "--outdir",
        str(tmp_path / "plots"),
        "--versions",
        "LSM-11.0.1",
        "--metrics",
        "all",
        "--x-axis",
        "time",
    ]
    monkeypatch.setattr("sys.argv", argv)
    node_plot.main()  # must not raise
    assert "Skipping rts plot" in capsys.readouterr().out
    assert list((tmp_path / "plots").rglob("*_cpu_ram_by_time.html"))
    assert not list((tmp_path / "plots").rglob("*_rts_by_time.html"))


def test_all_includes_rts_when_present(tmp_path, monkeypatch):
    db = str(tmp_path / "mainnet.db")
    _seed_node_min(db, V)
    _seed_rts(
        db,
        [
            ("2026-06-03T10:00:00", 100, "gcMinorNum", 5.0, V),
            ("2026-06-03T10:01:00", 101, "gcMinorNum", 6.0, V),
        ],
    )
    argv = [
        "node-plot.py",
        "--env",
        "mainnet",
        "--sqlite-db",
        db,
        "--outdir",
        str(tmp_path / "plots"),
        "--versions",
        "LSM-11.0.1",
        "--metrics",
        "all",
        "--x-axis",
        "time",
    ]
    monkeypatch.setattr("sys.argv", argv)
    node_plot.main()
    assert list((tmp_path / "plots").rglob("*_rts_by_time.html"))
