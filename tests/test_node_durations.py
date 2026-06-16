"""Tests for compute_epoch_durations - wall-clock duration per epoch from
node-resource-monitor samples.

This is the core math behind node-plot.py's `--metrics ingest` mode (sync time
by era + per-epoch duration line). It groups samples by (version, epoch_no)
and computes max(ts) minus min(ts).
"""

import pandas as pd
from _common import compute_epoch_durations


def _df(rows: list[tuple]) -> pd.DataFrame:
    """Build a (ts, version, epoch_no, era) DataFrame from a list of tuples."""
    df = pd.DataFrame(rows, columns=["ts", "version", "epoch_no", "era"])
    df["ts"] = pd.to_datetime(df["ts"])
    return df


class TestSingleEpoch:
    def test_multiple_samples_compute_max_minus_min(self):
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 100, "Babbage"),
                ("2026-01-01 00:00:30", "v1", 100, "Babbage"),
                ("2026-01-01 00:01:30", "v1", 100, "Babbage"),
            ]
        )
        out = compute_epoch_durations(df)
        assert len(out) == 1
        row = out.iloc[0]
        assert row["version"] == "v1"
        assert row["epoch_no"] == 100
        assert row["era"] == "Babbage"
        assert row["duration_sec"] == 90.0  # 1m30s

    def test_single_sample_yields_zero_duration(self):
        # Boundary case: an epoch with one sample has max == min.
        df = _df([("2026-01-01 00:00:00", "v1", 100, "Babbage")])
        out = compute_epoch_durations(df)
        assert len(out) == 1
        assert out.iloc[0]["duration_sec"] == 0.0


class TestMultipleEpochs:
    def test_independent_per_epoch_groups(self):
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 100, "Babbage"),
                ("2026-01-01 00:01:00", "v1", 100, "Babbage"),
                ("2026-01-01 00:02:00", "v1", 101, "Babbage"),  # different epoch
                ("2026-01-01 00:04:00", "v1", 101, "Babbage"),
            ]
        )
        out = compute_epoch_durations(df).sort_values("epoch_no").reset_index(drop=True)
        assert len(out) == 2
        assert out.iloc[0]["duration_sec"] == 60.0  # epoch 100: 1 min
        assert out.iloc[1]["duration_sec"] == 120.0  # epoch 101: 2 min

    def test_era_transition(self):
        # Epoch in Babbage, then epoch in Conway.
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 162, "Babbage"),
                ("2026-01-01 00:02:00", "v1", 162, "Babbage"),
                ("2026-01-01 00:03:00", "v1", 163, "Conway"),
                ("2026-01-01 00:04:00", "v1", 163, "Conway"),
            ]
        )
        out = compute_epoch_durations(df).sort_values("epoch_no").reset_index(drop=True)
        assert out.iloc[0]["era"] == "Babbage"
        assert out.iloc[1]["era"] == "Conway"


class TestMultiVersion:
    def test_versions_isolated(self):
        # Same epoch_no, different versions → separate rows.
        df = _df(
            [
                ("2026-01-01 00:00:00", "v1", 100, "Babbage"),
                ("2026-01-01 00:01:00", "v1", 100, "Babbage"),
                ("2026-01-01 00:00:00", "v2", 100, "Babbage"),
                ("2026-01-01 00:03:00", "v2", 100, "Babbage"),
            ]
        )
        out = compute_epoch_durations(df)
        assert len(out) == 2
        durations = {row["version"]: row["duration_sec"] for _, row in out.iterrows()}
        assert durations == {"v1": 60.0, "v2": 180.0}


class TestRobustness:
    def test_empty_input_returns_empty(self):
        empty = pd.DataFrame(columns=["ts", "version", "epoch_no", "era"])
        out = compute_epoch_durations(empty)
        assert out.empty
        # Expected schema is preserved even when empty.
        assert list(out.columns) == ["version", "epoch_no", "era", "duration_sec"]

    def test_returns_expected_columns(self):
        df = _df([("2026-01-01 00:00:00", "v1", 100, "Babbage")])
        out = compute_epoch_durations(df)
        assert list(out.columns) == ["version", "epoch_no", "era", "duration_sec"]

    def test_unsorted_input_handled(self):
        # Samples arrive out of order - max/min must still produce the right span.
        df = _df(
            [
                ("2026-01-01 00:02:00", "v1", 100, "Babbage"),
                ("2026-01-01 00:00:00", "v1", 100, "Babbage"),
                ("2026-01-01 00:01:00", "v1", 100, "Babbage"),
            ]
        )
        out = compute_epoch_durations(df)
        assert out.iloc[0]["duration_sec"] == 120.0
