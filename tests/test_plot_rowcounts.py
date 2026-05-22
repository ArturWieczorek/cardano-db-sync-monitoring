"""Tests for scripts/db-sync-plot.py::plot_rowcounts.

Specifically locks down that the version label is always visible in the
rendered plot — both in the chart title and on every trace's legend label.

Background: plot_rowcounts originally only added the version to legend
entries when comparing multiple versions; for a single-version plot the
legend just said "block", "tx", etc., so a screenshot or standalone HTML
gave no clue which db-sync run it came from. These tests guard against
that regression returning.

Rendering is captured by monkey-patching Figure.write_html so the test
doesn't litter disk and we can inspect the Figure object directly.
"""

import importlib.util
from pathlib import Path

import pandas as pd
import plotly.graph_objs as go
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_plot_module():
    """Import scripts/db-sync-plot.py. Hyphen → importlib."""
    spec = importlib.util.spec_from_file_location(
        "db_sync_plot_module", SCRIPTS_DIR / "db-sync-plot.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


db_sync_plot = _load_plot_module()


def _make_df(rows: list[tuple[str, str, int, int]]) -> pd.DataFrame:
    """Build a rowcounts-shaped DataFrame.

    `rows` items: (version, table_name, slot_no, row_count). `ts` is set to
    a unique string per row so the time-axis sort is stable, but the tests
    use slot_no as the x-axis to keep them deterministic.
    """
    return pd.DataFrame([
        {
            "version": v, "table_name": t, "slot_no": s, "row_count": rc,
            "ts": f"2026-05-27T00:00:{i:02d}",
        }
        for i, (v, t, s, rc) in enumerate(rows)
    ])


@pytest.fixture
def capture_figure(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace Figure.write_html with a no-op that stashes the Figure.

    Returns a dict that gets populated with ``{"fig": <Figure>, "path": <str>}``
    after the plotter runs. The plotter still calls write_html (so we exercise
    the same code path), but no file is written.
    """
    captured: dict = {}

    def fake_write_html(self: go.Figure, path: str, *args, **kwargs) -> None:
        captured["fig"] = self
        captured["path"] = path

    monkeypatch.setattr(go.Figure, "write_html", fake_write_html)
    return captured


class TestPlotRowcountsTitle:
    def test_single_version_title_includes_short_token(
        self, tmp_path: Path, capture_figure: dict,
    ) -> None:
        df = _make_df([
            ("cardano-db-sync 13.7.1.0 preprod", "block", 100, 50),
            ("cardano-db-sync 13.7.1.0 preprod", "tx",    100, 200),
        ])
        db_sync_plot.plot_rowcounts(
            df, ["cardano-db-sync 13.7.1.0 preprod"],
            str(tmp_path), "preprod", "slot",
        )
        title_text = capture_figure["fig"].layout.title.text
        assert "13.7.1.0" in title_text, (
            f"Single-version title must name the version; got: {title_text!r}"
        )

    def test_two_versions_title_lists_both(
        self, tmp_path: Path, capture_figure: dict,
    ) -> None:
        df = _make_df([
            ("cardano-db-sync 13.6.0.5 preprod", "block", 100, 50),
            ("cardano-db-sync 13.7.1.0 preprod", "block", 100, 55),
        ])
        db_sync_plot.plot_rowcounts(
            df,
            ["cardano-db-sync 13.6.0.5 preprod",
             "cardano-db-sync 13.7.1.0 preprod"],
            str(tmp_path), "preprod", "slot",
        )
        title_text = capture_figure["fig"].layout.title.text
        assert "13.6.0.5" in title_text and "13.7.1.0" in title_text, (
            f"Comparison title must list both versions; got: {title_text!r}"
        )
        # "vs" separator is the convention used by out_path's filename; keep
        # the visual representation consistent.
        assert "vs" in title_text


class TestPlotRowcountsLegend:
    def test_single_version_trace_names_include_version(
        self, tmp_path: Path, capture_figure: dict,
    ) -> None:
        """Every trace's legend label must contain the version short token
        even when only one version is plotted. This is the specific gap that
        prompted the fix — previously single-version legends showed just the
        table name ('block', 'tx') with no version information."""
        df = _make_df([
            ("cardano-db-sync 13.7.1.0 preprod", "block", 100, 50),
            ("cardano-db-sync 13.7.1.0 preprod", "tx",    100, 200),
            ("cardano-db-sync 13.7.1.0 preprod", "tx_out", 100, 5000),
        ])
        db_sync_plot.plot_rowcounts(
            df, ["cardano-db-sync 13.7.1.0 preprod"],
            str(tmp_path), "preprod", "slot",
        )
        names = [t.name for t in capture_figure["fig"].data]
        assert names, "plot_rowcounts produced no traces for a non-empty DF"
        for n in names:
            assert "13.7.1.0" in n, (
                f"Single-version trace name must include the version; got: {n!r}"
            )
        # The table name should still be there too — version info shouldn't
        # come at the cost of identifying which table the trace represents.
        assert any("block" in n for n in names)
        assert any("tx" in n for n in names)
        assert any("tx_out" in n for n in names)

    def test_two_versions_each_trace_carries_its_own_version(
        self, tmp_path: Path, capture_figure: dict,
    ) -> None:
        df = _make_df([
            ("cardano-db-sync 13.6.0.5 preprod", "block", 100, 50),
            ("cardano-db-sync 13.7.1.0 preprod", "block", 100, 55),
        ])
        db_sync_plot.plot_rowcounts(
            df,
            ["cardano-db-sync 13.6.0.5 preprod",
             "cardano-db-sync 13.7.1.0 preprod"],
            str(tmp_path), "preprod", "slot",
        )
        names = [t.name for t in capture_figure["fig"].data]
        # One trace per (version, table) -- here 2 versions x 1 table = 2.
        assert len(names) == 2
        # Exactly one trace per version (each carries the right short token).
        with_13_6 = [n for n in names if "13.6.0.5" in n]
        with_13_7 = [n for n in names if "13.7.1.0" in n]
        assert len(with_13_6) == 1
        assert len(with_13_7) == 1


class TestPlotRowcountsLegendTitle:
    def test_legend_title_is_table_slash_version(
        self, tmp_path: Path, capture_figure: dict,
    ) -> None:
        """Legend title used to switch between 'Table' and 'Table / Version'
        depending on version count. Now that the version is always on each
        trace, 'Table / Version' applies in both modes."""
        df = _make_df([("cardano-db-sync 13.7.1.0 preprod", "block", 100, 50)])
        db_sync_plot.plot_rowcounts(
            df, ["cardano-db-sync 13.7.1.0 preprod"],
            str(tmp_path), "preprod", "slot",
        )
        assert capture_figure["fig"].layout.legend.title.text == "Table / Version"
