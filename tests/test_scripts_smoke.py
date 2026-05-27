"""Smoke tests for the entry-point scripts.

`py_compile` catches syntax errors but not import-time runtime errors (e.g., a
NameError in module-level code, a broken import in a sibling module, an argparse
configuration mistake). These tests subprocess-invoke each script with `--help`
and assert it exits cleanly with the expected usage text on stdout.

Cheap (a few hundred ms total) and catches the kind of breakage where someone
removes a helper from `_common.py` without updating the caller.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

SCRIPTS = [
    "db-sync-monitor.py",
    "node-monitor.py",
    "db-sync-plot.py",
    "node-plot.py",
    "db-sync-report.py",
    "backup-stats.py",
    "rename-version.py",
]


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_help_exits_zero(script: str) -> None:
    """`python scripts/<x>.py --help` exits 0 and prints usage text on stdout."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"{script} --help exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "usage:" in result.stdout, (
        f"{script} --help didn't print 'usage:' header\n"
        f"stdout:\n{result.stdout}"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_missing_required_args_exits_nonzero(script: str) -> None:
    """`python scripts/<x>.py` with no args fails with non-zero exit + error message on stderr.

    Each entry-point has at least one required argument (`--env` for monitors
    and plotters, `--pg-dbname` for the report). Running with nothing should
    fail predictably; argparse writes to stderr, exit 2 by convention.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, (
        f"{script} with no args unexpectedly exited 0\nstdout:\n{result.stdout}"
    )
    # argparse error messages always start with `usage:` and contain `error:`.
    assert "usage:" in result.stderr
    assert "error:" in result.stderr


def test_db_sync_report_rejects_three_pg_dbnames() -> None:
    """Comparison mode is capped at two DBs; passing three should fail cleanly."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "db-sync-report.py"),
            "--pg-dbname",
            "a,b,c",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "accepts one DB or two comma-separated" in result.stderr
