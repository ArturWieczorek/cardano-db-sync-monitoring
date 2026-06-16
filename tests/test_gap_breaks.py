"""Tests for insert_gap_breaks - the helper that inserts NaN-marker rows where
consecutive samples are separated by a wall-clock gap > threshold.

Without these markers, plotly draws a misleading straight line across periods
when the monitor wasn't running (e.g. a restart). The marker row breaks the
line at that point.
"""

import pandas as pd
from _common import insert_gap_breaks


def _df(rows: list[tuple]) -> pd.DataFrame:
    """Build a (ts, version, rss) DataFrame from a list of tuples."""
    return pd.DataFrame(rows, columns=["ts", "version", "rss"]).assign(
        ts=lambda d: pd.to_datetime(d["ts"]),
    )


class TestNoGap:
    def test_consecutive_samples_unchanged(self):
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 100.0),
                ("2026-01-01 00:00:10", "v1", 110.0),
                ("2026-01-01 00:00:20", "v1", 120.0),
            ]
        )
        out = insert_gap_breaks(df, ["version"], gap_sec=50.0)
        assert len(out) == 3
        # All original values present; no NaN row added.
        assert out["rss"].notna().all()

    def test_single_row_unchanged(self):
        df = _df([("2026-01-01 00:00:00", "v1", 100.0)])
        out = insert_gap_breaks(df, ["version"], gap_sec=50.0)
        assert len(out) == 1
        assert out.iloc[0]["rss"] == 100.0

    def test_empty_dataframe(self):
        empty = pd.DataFrame(columns=["ts", "version", "rss"])
        out = insert_gap_breaks(empty, ["version"], gap_sec=50.0)
        assert out.empty


class TestWithGap:
    def test_one_gap_inserts_one_break(self):
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 100.0),
                ("2026-01-01 00:00:10", "v1", 110.0),
                # Gap: 1 hour
                ("2026-01-01 01:00:00", "v1", 200.0),
                ("2026-01-01 01:00:10", "v1", 210.0),
            ]
        )
        out = insert_gap_breaks(df, ["version"], gap_sec=50.0)
        # 4 original rows + 1 break row
        assert len(out) == 5
        # The break row has NaN in rss
        nan_rows = out[out["rss"].isna()]
        assert len(nan_rows) == 1
        # And the marker's ts sits between the two surrounding samples
        marker_ts = nan_rows.iloc[0]["ts"]
        assert pd.Timestamp("2026-01-01 00:00:10") < marker_ts < pd.Timestamp("2026-01-01 01:00:00")
        # Version still carried on the marker so it stays grouped correctly.
        assert nan_rows.iloc[0]["version"] == "v1"

    def test_multiple_gaps_one_break_each(self):
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 100.0),
                ("2026-01-01 00:10:00", "v1", 200.0),  # gap
                ("2026-01-01 00:10:10", "v1", 210.0),
                ("2026-01-01 00:20:00", "v1", 300.0),  # gap
            ]
        )
        out = insert_gap_breaks(df, ["version"], gap_sec=50.0)
        assert out["rss"].isna().sum() == 2

    def test_sub_threshold_gap_no_break(self):
        # Gap = 30s, threshold = 50s → no marker.
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 100.0),
                ("2026-01-01 00:00:30", "v1", 110.0),
            ]
        )
        out = insert_gap_breaks(df, ["version"], gap_sec=50.0)
        assert len(out) == 2
        assert out["rss"].notna().all()


class TestMultiGroup:
    def test_gap_only_in_one_group(self):
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 100.0),
                ("2026-01-01 00:00:10", "v1", 110.0),
                ("2026-01-01 01:00:00", "v1", 200.0),  # v1 gap
                ("2026-01-01 00:00:00", "v2", 50.0),
                ("2026-01-01 00:00:10", "v2", 55.0),
                ("2026-01-01 00:00:20", "v2", 60.0),
            ]
        )
        out = insert_gap_breaks(df, ["version"], gap_sec=50.0)
        # Only v1's gap inserts a marker.
        nan = out[out["rss"].isna()]
        assert len(nan) == 1
        assert nan.iloc[0]["version"] == "v1"

    def test_independent_groupings(self):
        # Multi-key grouping: (version, table_name). One v1+t1 gap, one v2+t2 gap.
        rows = [
            ("2026-01-01 00:00:00", "v1", "t1", 1.0),
            ("2026-01-01 01:00:00", "v1", "t1", 2.0),  # gap in v1+t1
            ("2026-01-01 00:00:00", "v1", "t2", 10.0),
            ("2026-01-01 00:00:10", "v1", "t2", 11.0),  # no gap in v1+t2
            ("2026-01-01 00:00:00", "v2", "t2", 100.0),
            ("2026-01-01 01:00:00", "v2", "t2", 200.0),  # gap in v2+t2
        ]
        df = pd.DataFrame(rows, columns=["ts", "version", "table_name", "rows"])
        df["ts"] = pd.to_datetime(df["ts"])
        out = insert_gap_breaks(df, ["version", "table_name"], gap_sec=50.0)
        nan = out[out["rows"].isna()]
        assert len(nan) == 2
        markers = {(r["version"], r["table_name"]) for _, r in nan.iterrows()}
        assert markers == {("v1", "t1"), ("v2", "t2")}


class TestRobustness:
    def test_all_null_ts_passthrough(self):
        df = pd.DataFrame(
            {
                "ts": [pd.NaT, pd.NaT],
                "version": ["v1", "v1"],
                "rss": [1.0, 2.0],
            }
        )
        out = insert_gap_breaks(df, ["version"], gap_sec=50.0)
        # When the entire ts column is NaT, the function returns the input
        # untouched (it can't measure gaps without timestamps).
        assert len(out) == 2

    def test_missing_ts_column_passthrough(self):
        df = pd.DataFrame({"version": ["v1"], "rss": [1.0]})
        out = insert_gap_breaks(df, ["version"], gap_sec=50.0)
        assert out.equals(df)


class TestAdaptiveThreshold:
    """With no explicit gap_sec, the threshold adapts to each series' own
    cadence (5x the median inter-sample interval). This is what fixed the empty
    disk plot: the disk collector samples every 60s, and the old fixed 50s
    default treated every normal sample as a gap, breaking the line everywhere.
    """

    def _cadence(self, step_sec: int, n: int, start="2026-01-01 00:00:00"):
        base = pd.Timestamp(start)
        return _df([(str(base + pd.Timedelta(seconds=step_sec * i)), "v1", float(i)) for i in range(n)])

    def test_60s_cadence_not_broken_at_every_point(self):
        # The regression: a steady 60s series must NOT get a break between every
        # pair of points (which rendered the disk plot empty).
        df = self._cadence(step_sec=60, n=10)
        out = insert_gap_breaks(df, ["version"])  # adaptive default
        assert out["rss"].isna().sum() == 0
        assert len(out) == 10

    def test_60s_cadence_breaks_only_real_outage(self):
        # Steady 60s cadence, then a 1h outage, then more 60s samples.
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 0.0),
                ("2026-01-01 00:01:00", "v1", 1.0),
                ("2026-01-01 00:02:00", "v1", 2.0),
                ("2026-01-01 01:02:00", "v1", 3.0),  # ~1h outage
                ("2026-01-01 01:03:00", "v1", 4.0),
            ]
        )
        out = insert_gap_breaks(df, ["version"])  # adaptive default
        assert out["rss"].isna().sum() == 1

    def test_explicit_gap_sec_still_overrides(self):
        # Passing gap_sec keeps the old fixed-threshold behavior: a 60s series
        # under a 50s threshold breaks at every step.
        df = self._cadence(step_sec=60, n=4)
        out = insert_gap_breaks(df, ["version"], gap_sec=50.0)
        assert out["rss"].isna().sum() == 3  # break between each of the 4 points
