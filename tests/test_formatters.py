"""Pure-function tests for the size/duration formatters in _common."""

from _common import (
    format_bytes,
    format_duration,
    format_duration_compact,
    format_size,
)


class TestFormatSize:
    """format_size takes a value in MiB and produces 'X.X MiB' or 'X.X GiB'.

    Labels are strict binary units (MiB/GiB) because the values are binary
    (bytes / 1024^n). Calling a GiB a "GB" would overstate it by ~7.4%.
    """

    def test_none(self):
        assert format_size(None) == "N/A"

    def test_small(self):
        assert format_size(123.4) == "123.4 MiB"

    def test_at_mib_gib_boundary(self):
        # 1024 MiB == 1 GiB; format promotes to GiB.
        assert format_size(1024.0) == "1.0 GiB"

    def test_gib(self):
        assert format_size(2048.0) == "2.0 GiB"

    def test_zero(self):
        assert format_size(0.0) == "0.0 MiB"


class TestFormatBytes:
    """format_bytes promotes through KiB → MiB → GiB → TiB (binary units)."""

    def test_none(self):
        assert format_bytes(None) == "N/A"

    def test_bytes(self):
        assert format_bytes(512) == "512 B"

    def test_kib(self):
        assert format_bytes(2048) == "2.00 KiB"

    def test_mib(self):
        assert format_bytes(5 * 1024**2) == "5.00 MiB"

    def test_gib(self):
        assert format_bytes(7 * 1024**3) == "7.00 GiB"

    def test_tib(self):
        assert format_bytes(2 * 1024**4) == "2.00 TiB"


class TestFormatDuration:
    """format_duration is the long form used in reports."""

    def test_none(self):
        assert format_duration(None) == "N/A"

    def test_seconds(self):
        assert format_duration(45) == "0h 00m 45s (45 sec)"

    def test_minutes(self):
        assert format_duration(125) == "0h 02m 05s (125 sec)"

    def test_hours(self):
        assert format_duration(3725) == "1h 02m 05s (3725 sec)"


class TestFormatDurationCompact:
    """format_duration_compact picks the largest unit that fits."""

    def test_none(self):
        assert format_duration_compact(None) == "N/A"

    def test_negative(self):
        # Tip lag can briefly be slightly negative at exact tip; clamp display.
        assert format_duration_compact(-5) == "0s"

    def test_seconds(self):
        assert format_duration_compact(42) == "42s"

    def test_minutes(self):
        # 90s → 2m (rounded by truncation)
        assert format_duration_compact(120) == "2m"

    def test_hours(self):
        # 90 minutes = 1.5h
        assert format_duration_compact(5400) == "1.5h"

    def test_days(self):
        # 3.0 days
        assert format_duration_compact(3 * 86400) == "3.0d"
