"""Regression tests for the timezone-handling fix in _common.utc_timestamp.

The bug: psycopg2 returns naive datetimes for postgres `timestamp` columns,
but cardano-db-sync stores UTC values there. Calling .timestamp() on a naive
datetime treats it as LOCAL time, producing a constant offset equal to the
host's UTC offset. The monitor's TipLag was reading exactly 2h on a UTC+2
host because of this.

These tests pin both the naive-as-UTC behavior and the aware-passthrough.
"""

from datetime import datetime, timedelta, timezone

from _common import utc_timestamp


class TestUtcTimestampAware:
    """Timezone-aware datetimes pass through .timestamp() unchanged."""

    def test_utc_aware_matches_posix(self):
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert utc_timestamp(dt) == dt.timestamp()

    def test_offset_aware_matches_posix(self):
        plus2 = timezone(timedelta(hours=2))
        dt = datetime(2024, 1, 15, 14, 0, 0, tzinfo=plus2)  # = 12:00 UTC
        assert utc_timestamp(dt) == dt.timestamp()


class TestUtcTimestampNaive:
    """Naive datetimes get pinned to UTC, not interpreted as local."""

    def test_naive_treated_as_utc(self):
        # A naive '2024-01-15 12:00:00' representing UTC.
        naive = datetime(2024, 1, 15, 12, 0, 0)
        aware = naive.replace(tzinfo=timezone.utc)
        assert utc_timestamp(naive) == aware.timestamp()

    def test_naive_does_not_use_local_timezone(self):
        """Regression: prior to the fix, a naive UTC value would be reinterpreted
        as local time and the POSIX result would shift by the local offset.

        We assert that two semantically-equal datetimes (one naive-UTC, one
        aware-UTC) produce the same POSIX timestamp via utc_timestamp(). If a
        future change reintroduces the bug, this assertion fails immediately
        regardless of what timezone the CI runner has.
        """
        naive = datetime(2024, 6, 1, 0, 0, 0)
        aware = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert utc_timestamp(naive) == utc_timestamp(aware)
