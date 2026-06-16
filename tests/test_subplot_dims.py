"""Tests for `_common.subplot_dims` - the shared panel/gap sizer used by every
stacked plot in node-plot.py and db-sync-plot.py.

The point of the helper is to avoid the fixed-*fraction* vertical_spacing trap,
where many rows make the gaps sum to most of the figure height (tiny panels,
huge whitespace - the original empty-looking RTS/disk plots). These tests lock
in that the gap stays a small, bounded share of the figure no matter the row
count, and below plotly's hard 1/(rows-1) cap.
"""

from _common import subplot_dims


def test_single_row_no_spacing():
    height, vspace = subplot_dims(1)
    assert vspace == 0.0
    assert height == 1 * 300 + 160  # panel + margins, no gaps


def test_height_grows_linearly_with_rows():
    h1, _ = subplot_dims(1)
    h2, _ = subplot_dims(2)
    h3, _ = subplot_dims(3)
    # Each added row adds one panel + one gap.
    assert h2 - h1 == 300 + 40
    assert h3 - h2 == 300 + 40


def test_custom_panel_and_gap():
    height, vspace = subplot_dims(4, panel_px=200, gap_px=20, margin_px=100)
    assert height == 4 * 200 + 3 * 20 + 100
    assert vspace == 20 / height


def test_gap_is_small_bounded_share_even_with_many_rows():
    # The regression guard: with many rows the total gap must stay a small
    # fraction of the figure (panels dominate), unlike a fixed-fraction spacing.
    for rows in (10, 38, 100):
        _, vspace = subplot_dims(rows)
        total_gap_fraction = (rows - 1) * vspace
        assert total_gap_fraction < 0.2  # gaps well under 20% of the figure


def test_vertical_spacing_under_plotly_cap():
    # plotly requires vertical_spacing <= 1/(rows-1); violating it raises.
    for rows in (2, 5, 38, 200):
        _, vspace = subplot_dims(rows)
        assert vspace < 1 / (rows - 1)
