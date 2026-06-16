"""Tests for scripts/db-sync-stats-report.py - the LSM-vs-InMemory stats report.

Seeds a tiny db-sync stats DB with an InMemory build, an LSM build, and a
previous version, then exercises: build-label resolution per env, the assembled
section structure (per-build + both comparison sections), HTML output (no
kaleido), Markdown output (PNG render guarded by importorskip), and the
kaleido-missing error path (monkeypatched).
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from plotly.graph_objs import Figure

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[modname] = mod  # so dataclasses w/ future-annotations resolve
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# _report has no hyphen and is on sys.path (conftest) - import it directly so the
# test and the report script share one module instance.
import _report  # noqa: E402

report = _load("db_sync_stats_report", "db-sync-stats-report.py")

ENV = "preprod"
INMEM = "cardano-db-sync 13.7.1.0-node-11.0.1 preprod"
LSM = "cardano-db-sync LSM-13.7.1.0-node-11.0.1 preprod"
PREV = "cardano-db-sync 13.6.0.5-genesis preprod"


def _seed_db(path: str) -> None:
    """Minimal but query-valid db-sync stats DB for three version labels."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE db_sync_version (timestamp TEXT, version TEXT)")
        conn.execute("CREATE TABLE memory_metrics (ts TEXT, slot_no INT, rss REAL, version TEXT)")
        conn.execute("CREATE TABLE cpu_metrics (ts TEXT, slot_no INT, cpu_percent REAL, version TEXT)")
        conn.execute(
            "CREATE TABLE ingest_metrics (ts TEXT, slot_no INT, version TEXT, tip_lag_sec REAL, "
            "db_size_bytes INT, max_block_no INT, max_tx_id INT, utxo_count INT)"
        )
        conn.execute(
            "CREATE TABLE table_rowcounts (ts TEXT, slot_no INT, version TEXT, table_name TEXT, row_count INT)"
        )
        for v in (INMEM, LSM, PREV):
            for i in range(3):
                ts = f"2026-06-01T00:00:{i:02d}"
                conn.execute("INSERT INTO db_sync_version VALUES (?,?)", (ts, v))
                conn.execute("INSERT INTO memory_metrics VALUES (?,?,?,?)", (ts, 100 + i, 1000.0 + i, v))
                conn.execute("INSERT INTO cpu_metrics VALUES (?,?,?,?)", (ts, 100 + i, 50.0 + i, v))
                conn.execute(
                    "INSERT INTO ingest_metrics VALUES (?,?,?,?,?,?,?,?)",
                    (ts, 100 + i, v, 1.0 + i, 1_000_000 * (i + 1), 10 * i, 100 * i, 5000 + i),
                )
                for tbl in ("block", "tx", "tx_out"):
                    conn.execute(
                        "INSERT INTO table_rowcounts VALUES (?,?,?,?,?)",
                        (ts, 100 + i, v, tbl, 1000 * (i + 1)),
                    )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path) -> str:
    p = str(tmp_path / "preprod.db")
    _seed_db(p)
    return p


class _Args:
    def __init__(self, **kw):
        self.inmemory = kw.get("inmemory")
        self.lsm = kw.get("lsm")
        self.compare_to = kw.get("compare_to")
        self.format = kw.get("format", "both")
        self.outdir = kw.get("outdir")
        self.sqlite_db = kw.get("sqlite_db")
        self.scale = kw.get("scale", 2)
        self.html_max_points = kw.get("html_max_points")


class TestBuildFig:
    def test_builds_each_metric(self, db):
        for kind in ("cpu_ram", "ingest", "tables"):
            fig = report.build_fig(db, [INMEM], ENV, kind, "slot")
            assert isinstance(fig, Figure), kind
            assert len(fig.data) >= 1


class TestEnvReport:
    def test_sections_both_builds_and_both_comparisons(self, db):
        args = _Args(inmemory="13.7.1.0-node-11.0.1", lsm="LSM-13.7.1.0-node-11.0.1", compare_to="13.6.0.5-genesis")
        sections = report.build_env_report(db, ENV, args)
        titles = [s.title for s in sections]
        assert titles[0].startswith("InMemory version")
        assert titles[1].startswith("LSM version")
        assert "LSM vs InMemory Comparison" in titles
        assert any(t.startswith("This vs Previous") for t in titles)
        # Each build section has all 3 metrics x 2 axes = 6 images.
        assert len(sections[0].images) == 6
        assert len(sections[1].images) == 6

    def test_only_one_build_skips_comparison(self, db):
        args = _Args(inmemory="13.7.1.0-node-11.0.1")  # no --lsm
        sections = report.build_env_report(db, ENV, args)
        titles = [s.title for s in sections]
        assert any(t.startswith("InMemory") for t in titles)
        assert "LSM vs InMemory Comparison" not in titles

    def test_unknown_build_token_returns_none(self, db):
        args = _Args(inmemory="does-not-exist")
        assert report.build_env_report(db, ENV, args) is None

    def test_comparison_sections_use_slot_axis(self, db):
        # Comparisons overlay two runs that ran at different wall-clock times, so
        # they MUST align by slot, not time (time would shift the curves apart).
        args = _Args(inmemory="13.7.1.0-node-11.0.1", lsm="LSM-13.7.1.0-node-11.0.1", compare_to="13.6.0.5-genesis")
        sections = report.build_env_report(db, ENV, args)
        comparisons = [s for s in sections if s.title.startswith(("LSM vs InMemory", "This vs Previous"))]
        assert comparisons  # sanity
        for sec in comparisons:
            for img in sec.images:
                assert img.caption.endswith("slot-axis"), img.caption
                assert "slot-axis" in img.png_name and "time-axis" not in img.png_name


class TestOutputs:
    def test_html_needs_no_kaleido(self, db, tmp_path):
        args = _Args(
            inmemory="13.7.1.0-node-11.0.1", lsm="LSM-13.7.1.0-node-11.0.1", format="html", outdir=str(tmp_path / "out")
        )
        sections = report.build_env_report(db, ENV, args)
        report.write_env_report(ENV, sections, args)
        html = (tmp_path / "out" / ENV / "report.html").read_text()
        assert "LSM vs InMemory Comparison" in html
        assert "plotly" in html.lower()  # JS embedded
        assert not (tmp_path / "out" / ENV / "report.md").exists()

    def test_markdown_references_expected_pngs(self, db, tmp_path):
        pytest.importorskip("kaleido")
        args = _Args(
            inmemory="13.7.1.0-node-11.0.1", lsm="LSM-13.7.1.0-node-11.0.1", format="md", outdir=str(tmp_path / "out")
        )
        sections = report.build_env_report(db, ENV, args)
        report.write_env_report(ENV, sections, args)
        env_dir = tmp_path / "out" / ENV
        md = (env_dir / "report.md").read_text()
        assert "## InMemory version" in md and "## LSM version" in md
        png = "cardano-db-sync-preprod-13.7.1.0-node-11.0.1-slot-axis.png"
        assert png in md
        assert (env_dir / png).exists()  # actually rendered


class TestHtmlDownsampling:
    def _big_fig(self):
        import plotly.graph_objs as go

        xs = list(range(20000))
        return go.Figure([go.Scatter(x=xs, y=xs, mode="lines", name="t")])

    def test_downsample_caps_points_and_copies(self):
        fig = self._big_fig()
        out = _report.downsample_figure(fig, 4000)
        assert len(out.data[0].x) <= 4000
        assert len(fig.data[0].x) == 20000  # original untouched

    def test_zero_or_none_is_full_fidelity(self):
        fig = self._big_fig()
        assert len(_report.downsample_figure(fig, 0).data[0].x) == 20000

    def test_html_smaller_with_cap(self):
        sections = [
            _report.ReportSection(
                title="S", images=[_report.ReportImage(caption="c", png_name="c.png", fig=self._big_fig())]
            )
        ]
        full = _report.assemble_html("t", sections)
        capped = _report.assemble_html("t", sections, max_points=1000)
        assert len(capped) < len(full)

    def test_stale_artifacts_are_cleared(self, db, tmp_path):
        # A leftover PNG from a previous run (e.g. an old time-axis comparison)
        # must not survive into the new report's directory.
        env_dir = tmp_path / "out" / ENV
        env_dir.mkdir(parents=True)
        orphan = env_dir / "cardano-db-sync-preprod-LSM-vs-InMemory-time-axis.png"
        orphan.write_bytes(b"stale")
        args = _Args(inmemory="13.7.1.0-node-11.0.1", format="html", outdir=str(tmp_path / "out"))
        sections = report.build_env_report(db, ENV, args)
        report.write_env_report(ENV, sections, args)
        assert not orphan.exists()

    @staticmethod
    def _plot_div_style(html: str) -> str:
        import re

        m = re.search(r'class="plotly-graph-div" style="([^"]*)"', html)
        assert m, "no plotly plot div found in HTML"
        return m.group(1)

    def test_responsive_figure_gets_fixed_height_in_html(self):
        # A height-less (responsive) figure - like cpu_ram - would render as
        # height:100% and collapse/squish when stacked in the report body. The
        # report HTML must pin a height instead, without mutating the original.
        # (Check the plot div's own style, not the whole page: the inlined
        # plotly.js library itself contains "height:100%" CSS.)
        import plotly.graph_objs as go

        fig = go.Figure([go.Scatter(x=[1, 2, 3], y=[1, 2, 3])])  # no layout.height
        assert fig.layout.height is None
        sections = [
            _report.ReportSection(title="S", images=[_report.ReportImage(caption="c", png_name="c.png", fig=fig)])
        ]
        style = self._plot_div_style(_report.assemble_html("t", sections))
        assert f"height:{_report.HTML_FALLBACK_HEIGHT}px" in style
        assert "height:100%" not in style
        assert fig.layout.height is None  # original untouched

    def test_fixed_height_figure_unchanged_in_html(self):
        import plotly.graph_objs as go

        fig = go.Figure([go.Scatter(x=[1, 2], y=[1, 2])])
        fig.update_layout(height=1480)
        sections = [
            _report.ReportSection(title="S", images=[_report.ReportImage(caption="c", png_name="c.png", fig=fig)])
        ]
        style = self._plot_div_style(_report.assemble_html("t", sections))
        assert "height:1480px" in style

    def test_default_html_is_unchanged(self, db, tmp_path):
        # No --html-max-points -> behaves exactly as before (full data embedded).
        args = _Args(inmemory="13.7.1.0-node-11.0.1", format="html", outdir=str(tmp_path / "out"))
        args.html_max_points = None
        sections = report.build_env_report(db, ENV, args)
        report.write_env_report(ENV, sections, args)
        assert (tmp_path / "out" / ENV / "report.html").exists()


class TestKaleidoMissing:
    def test_render_png_raises_actionable_error(self, db, monkeypatch):
        fig = report.build_fig(db, [INMEM], ENV, "cpu_ram", "slot")

        def boom(*a, **k):
            raise ValueError("requires the kaleido package")

        monkeypatch.setattr(Figure, "write_image", boom)
        with pytest.raises(_report.KaleidoMissingError) as ei:
            _report.render_png(fig, "/tmp/x.png")
        assert "pip install" in str(ei.value)
