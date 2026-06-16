"""Tests for scripts/node-stats-report.py - the InMemory-vs-LSM node stats report.

Seeds a tiny cardano-node stats DB with an InMemory build, an LSM build, and a
previous version, then exercises: build-label resolution per env, the assembled
section structure (per-build + both comparison sections), HTML output (no
kaleido), Markdown output (PNG render guarded by importorskip), and the
kaleido-missing error path (monkeypatched). Mirrors test_db_sync_stats_report.py.
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

report = _load("node_stats_report", "node-stats-report.py")

ENV = "preprod"
INMEM = "cardano-node 11.0.1 preprod"
LSM = "cardano-node LSM-11.0.1 preprod"
PREV = "cardano-node 10.1.4 preprod"


def _seed_db(path: str) -> None:
    """Minimal but query-valid cardano-node stats DB for three version labels."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE node_version (timestamp TEXT, version TEXT)")
        conn.execute("CREATE TABLE memory_metrics (ts TEXT, slot_no INT, rss REAL, version TEXT)")
        conn.execute("CREATE TABLE cpu_metrics (ts TEXT, slot_no INT, cpu_percent REAL, version TEXT)")
        conn.execute(
            "CREATE TABLE node_ingest_metrics (ts TEXT, slot_no INT, epoch_no INT, era TEXT, "
            "sync_progress REAL, version TEXT)"
        )
        conn.execute("CREATE TABLE disk_metrics (ts TEXT, slot_no INT, total_bytes INT, lsm_bytes INT, version TEXT)")
        conn.execute("CREATE TABLE rts_metrics (ts TEXT, slot_no INT, metric TEXT, value REAL, version TEXT)")
        for v in (INMEM, LSM, PREV):
            # LSM build has a non-zero lsm subdir; others have none.
            lsm_bytes = 5_000_000 if "LSM-" in v else 0
            for i in range(4):  # span two epochs so per-epoch durations are non-empty
                ts = f"2026-06-01T00:00:{i:02d}"
                epoch = 200 + (i // 2)
                conn.execute("INSERT INTO node_version VALUES (?,?)", (ts, v))
                conn.execute("INSERT INTO memory_metrics VALUES (?,?,?,?)", (ts, 100 + i, 1000.0 + i, v))
                conn.execute("INSERT INTO cpu_metrics VALUES (?,?,?,?)", (ts, 100 + i, 50.0 + i, v))
                conn.execute(
                    "INSERT INTO node_ingest_metrics VALUES (?,?,?,?,?,?)",
                    (ts, 100 + i, epoch, "Conway", 0.9 + i / 100, v),
                )
                conn.execute(
                    "INSERT INTO disk_metrics VALUES (?,?,?,?,?)",
                    (ts, 100 + i, 1_000_000 * (i + 1), lsm_bytes, v),
                )
                for metric in ("gcLiveBytes", "gcMajorNum"):
                    conn.execute(
                        "INSERT INTO rts_metrics VALUES (?,?,?,?,?)",
                        (ts, 100 + i, metric, 100.0 * (i + 1), v),
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
        # cpu_ram/disk/rts on slot, ingest on epoch - one figure each.
        for kind, axis in (("cpu_ram", "slot"), ("ingest", "epoch"), ("disk", "slot"), ("rts", "slot")):
            fig = report.build_fig(db, [INMEM], ENV, kind, axis)
            assert isinstance(fig, Figure), kind
            assert len(fig.data) >= 1

    def test_missing_optional_table_returns_none(self, db, tmp_path):
        # A DB with no disk_metrics / rts_metrics table must not raise - the
        # optional collectors simply weren't run, so those metrics are skipped.
        bare = str(tmp_path / "bare.db")
        conn = sqlite3.connect(bare)
        conn.execute("CREATE TABLE memory_metrics (ts TEXT, slot_no INT, rss REAL, version TEXT)")
        conn.execute("CREATE TABLE cpu_metrics (ts TEXT, slot_no INT, cpu_percent REAL, version TEXT)")
        conn.execute("INSERT INTO memory_metrics VALUES ('2026-06-01T00:00:00',100,1000.0,?)", (INMEM,))
        conn.execute("INSERT INTO cpu_metrics VALUES ('2026-06-01T00:00:00',100,50.0,?)", (INMEM,))
        conn.commit()
        conn.close()
        assert report.build_fig(bare, [INMEM], ENV, "disk", "slot") is None
        assert report.build_fig(bare, [INMEM], ENV, "rts", "slot") is None
        assert report.build_fig(bare, [INMEM], ENV, "ingest", "epoch") is None


class TestEnvReport:
    def test_sections_both_builds_and_both_comparisons(self, db):
        args = _Args(inmemory="11.0.1", lsm="LSM-11.0.1", compare_to="10.1.4")
        sections = report.build_env_report(db, ENV, args)
        titles = [s.title for s in sections]
        assert titles[0].startswith("InMemory version")
        assert titles[1].startswith("LSM version")
        assert "LSM vs InMemory Comparison" in titles
        assert any(t.startswith("This vs Previous") for t in titles)
        # Each build section: cpu_ram(2 axes) + ingest(1 epoch) + disk(2) + rts(2) = 7.
        assert len(sections[0].images) == 7
        assert len(sections[1].images) == 7

    def test_only_one_build_skips_comparison(self, db):
        args = _Args(inmemory="11.0.1")  # no --lsm
        sections = report.build_env_report(db, ENV, args)
        titles = [s.title for s in sections]
        assert any(t.startswith("InMemory") for t in titles)
        assert "LSM vs InMemory Comparison" not in titles

    def test_unknown_build_token_returns_none(self, db):
        args = _Args(inmemory="does-not-exist")
        assert report.build_env_report(db, ENV, args) is None

    def test_comparison_sections_use_chain_position_axes(self, db):
        # Comparisons overlay two runs that ran at different wall-clock times, so
        # they MUST align by chain position: slot for cpu_ram/disk, epoch for
        # ingest - never the time axis (which would shift the curves apart).
        args = _Args(inmemory="11.0.1", lsm="LSM-11.0.1", compare_to="10.1.4")
        sections = report.build_env_report(db, ENV, args)
        comparisons = [s for s in sections if s.title.startswith(("LSM vs InMemory", "This vs Previous"))]
        assert comparisons  # sanity
        for sec in comparisons:
            for img in sec.images:
                assert img.caption.endswith(("slot-axis", "epoch-axis")), img.caption
                assert "time-axis" not in img.png_name


class TestOutputs:
    def test_html_needs_no_kaleido(self, db, tmp_path):
        args = _Args(inmemory="11.0.1", lsm="LSM-11.0.1", format="html", outdir=str(tmp_path / "out"))
        sections = report.build_env_report(db, ENV, args)
        report.write_env_report(ENV, sections, args)
        html = (tmp_path / "out" / ENV / "report.html").read_text()
        assert "LSM vs InMemory Comparison" in html
        assert "plotly" in html.lower()  # JS embedded
        assert not (tmp_path / "out" / ENV / "report.md").exists()

    def test_markdown_references_expected_pngs(self, db, tmp_path):
        pytest.importorskip("kaleido")
        args = _Args(inmemory="11.0.1", lsm="LSM-11.0.1", format="md", outdir=str(tmp_path / "out"))
        sections = report.build_env_report(db, ENV, args)
        report.write_env_report(ENV, sections, args)
        env_dir = tmp_path / "out" / ENV
        md = (env_dir / "report.md").read_text()
        assert "## InMemory version" in md and "## LSM version" in md
        png = "cardano-node-preprod-11.0.1-slot-axis.png"
        assert png in md
        assert (env_dir / png).exists()  # actually rendered

    def test_stale_artifacts_are_cleared(self, db, tmp_path):
        # A leftover PNG from a previous run must not survive into the new run's dir.
        env_dir = tmp_path / "out" / ENV
        env_dir.mkdir(parents=True)
        orphan = env_dir / "cardano-node-preprod-LSM-vs-InMemory-time-axis.png"
        orphan.write_bytes(b"stale")
        args = _Args(inmemory="11.0.1", format="html", outdir=str(tmp_path / "out"))
        sections = report.build_env_report(db, ENV, args)
        report.write_env_report(ENV, sections, args)
        assert not orphan.exists()


class TestKaleidoMissing:
    def test_render_png_raises_actionable_error(self, db, monkeypatch):
        fig = report.build_fig(db, [INMEM], ENV, "cpu_ram", "slot")

        def boom(*a, **k):
            raise ValueError("requires the kaleido package")

        monkeypatch.setattr(Figure, "write_image", boom)
        with pytest.raises(_report.KaleidoMissingError) as ei:
            _report.render_png(fig, "/tmp/x.png")
        assert "pip install" in str(ei.value)
