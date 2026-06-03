"""Shared core for the on-disk size collectors (node db dir, db-sync ledger dir).

Both collectors measure a directory's apparent size via ``du -sb`` on a coarse
cadence and append a time series to the role's SQLite stats DB under the run's
version label, so the disk curve joins the rest of the run's metrics
(memory_metrics / cpu_metrics / *_ingest_metrics) by ``version``.

Kept separate from the main *-monitor.py loops on purpose: a ``du`` of a
multi-hundred-GB mainnet directory is a heavy, cache-polluting tree walk, so it
runs on its own coarse interval (60s default) rather than biasing the 10s
CPU/RAM samples — and can be started/stopped independently.

The only per-role differences are captured by subclass attributes:
  - ``DATA_DIR``      where the role's SQLite DBs live
  - ``BINARY_PREFIX`` argv[0] basename prefix used to find the owning process
  - ``PATH_FLAG``     argv flag carrying the directory to measure
  - ``LABEL_PREFIX``  version-label prefix (matches the *-monitor.py label)
  - ``ENV_IN_ARGV``   whether the env name must appear in argv (node: yes,
                      db-sync: no — db-sync takes no env flag)

Everything else (schema, sampling, loop, summary, signal handling) lives here.

Works for both LSM and stock builds: the ``lsm/`` subdir only exists on LSM
runs, so on a stock/in-memory run ``lsm_bytes`` is simply 0 and the subdir is
never even stat-walked.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from _common import find_processes, format_bytes, warn
from psutil import Process

# --- pure helpers (unit-tested directly) -----------------------------------


def du_bytes(path: str, timeout: float) -> int | None:
    """Apparent size of ``path`` in bytes via ``du -sb`` (matches the existing
    bash logs). Returns None on error/timeout so the caller can skip the sample
    rather than writing a bogus 0."""
    try:
        out = subprocess.run(
            ["du", "-sb", path],
            capture_output=True, text=True, check=True, timeout=timeout,
        )
        return int(out.stdout.split()[0])
    except subprocess.TimeoutExpired:
        warn(f"du timed out (>{timeout:.0f}s) on {path}; skipping this sample.")
        return None
    except (subprocess.CalledProcessError, ValueError, IndexError, FileNotFoundError) as e:
        warn(f"du failed on {path}: {e}")
        return None


def parse_path_flag(cmdline: list[str], cwd: str, flag: str) -> str | None:
    """Extract the value of ``flag`` (e.g. --database-path / --state-dir) from a
    process command line, handling both ``--flag value`` and ``--flag=value``.
    Relative values are resolved against ``cwd`` (the owning process's working
    directory), since the disk collector may run from elsewhere. Returns None if
    the flag isn't present."""
    raw: str | None = None
    for i, arg in enumerate(cmdline):
        if arg == flag and i + 1 < len(cmdline):
            raw = cmdline[i + 1]
            break
        if arg.startswith(flag + "="):
            raw = arg.split("=", 1)[1]
            break
    if raw is None:
        return None
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(cwd, raw))


# --- collector base ---------------------------------------------------------


class DiskSizeMonitor:
    # Subclasses override these.
    DATA_DIR: Path
    BINARY_PREFIX: str
    PATH_FLAG: str
    LABEL_PREFIX: str
    ENV_IN_ARGV: bool

    def __init__(self, env: str, version: str, explicit_path: str | None,
                 lsm_subdir: str, interval: float, du_timeout: float,
                 emit_json: bool, sqlite_db: str | None,
                 binary_prefix: str | None = None,
                 match_arg: str | None = None) -> None:
        self.running: bool = True
        self.env: str = env
        self.version: str = version
        self.explicit_path: str | None = explicit_path
        self.lsm_subdir: str = lsm_subdir
        self.interval: float = interval
        self.du_timeout: float = du_timeout
        self.emit_json: bool = emit_json
        self.match_arg: str | None = match_arg
        # Allow overriding the binary prefix (node-monitor exposes
        # --cardano-node-path for version-suffixed binaries); default to class.
        self.binary_prefix: str = binary_prefix or self.BINARY_PREFIX
        # Same label the *-monitor.py writes, so the disk series joins the run.
        self.run_label: str = f"{self.LABEL_PREFIX} {version} {env}"
        self.db_file: str = sqlite_db or str(self.DATA_DIR / f"{env}.db")
        Path(self.db_file).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    # --- schema -----------------------------------------------------------

    def init_db(self) -> None:
        """Create disk_metrics if absent. WAL mode so this collector can write
        concurrently with the main monitor on the same <env>.db."""
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS disk_metrics
                   (ts TEXT, slot_no INTEGER, path TEXT,
                    total_bytes INTEGER, lsm_bytes INTEGER, version TEXT)"""
            )
            conn.commit()

    def report_existing(self) -> None:
        try:
            with sqlite3.connect(self.db_file) as conn:
                row = conn.execute(
                    "SELECT COUNT(*), MIN(ts), MAX(ts) FROM disk_metrics WHERE version = ?",
                    (self.run_label,),
                ).fetchone()
        except Exception as e:
            warn(f"Startup history check failed: {e}")
            return
        count = (row or [0])[0] or 0
        if count == 0:
            print(f"This version label has no existing disk samples in {self.db_file}.")
        else:
            print(
                f"Note: this version label already has {count:,} disk samples in "
                f"{self.db_file} (first {row[1]}, last {row[2]}). New samples will be appended."
            )

    # --- process / path discovery -----------------------------------------

    def _match_process(self, proc: Process) -> bool:
        """True if ``proc`` is the role's process. argv[0] basename must start
        with the binary prefix; if ENV_IN_ARGV, the env name must appear in some
        argument; if match_arg is set it must appear somewhere in the command
        line."""
        cmdline = proc.info.get("cmdline") or []
        exe_base = os.path.basename(cmdline[0]) if cmdline else (proc.info.get("name") or "")
        if not exe_base.startswith(self.binary_prefix):
            return False
        if self.ENV_IN_ARGV and not any(self.env in arg for arg in cmdline[1:]):
            return False
        if self.match_arg is not None:
            return any(self.match_arg in arg for arg in cmdline)
        return True

    def resolve_path(self) -> str | None:
        """The dir to measure: explicit path wins, else parse PATH_FLAG out of
        the owning process's argv (resolved against its CWD)."""
        if self.explicit_path:
            return self.explicit_path
        matches = find_processes(self._match_process)
        if not matches:
            return None
        if len(matches) > 1:
            pids = ", ".join(str(p.pid) for p in matches)
            warn(f"Multiple {self.binary_prefix} processes match: {pids}. "
                 f"Using PID {matches[0].pid}. Pass --match-arg to disambiguate.")
        proc = matches[0]
        try:
            cmdline = proc.cmdline()
            cwd = proc.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None
        return parse_path_flag(cmdline, cwd, self.PATH_FLAG)

    # --- sampling ---------------------------------------------------------

    def sample_once(self, path: str) -> tuple[str, int, int] | None:
        """Take one (total, lsm) sample of ``path`` and insert a row. Returns
        (ts, total_bytes, lsm_bytes), or None if the du failed (caller skips).
        ``lsm_bytes`` is 0 when the lsm subdir is absent (stock/in-memory run)."""
        total = du_bytes(path, self.du_timeout)
        if total is None:
            return None
        lsm_path = os.path.join(path, self.lsm_subdir)
        lsm = du_bytes(lsm_path, self.du_timeout) if os.path.isdir(lsm_path) else 0
        lsm = lsm if lsm is not None else 0
        ts = datetime.now().isoformat()
        with sqlite3.connect(self.db_file) as conn:
            conn.execute(
                "INSERT INTO disk_metrics (ts, slot_no, path, total_bytes, lsm_bytes, version) "
                "VALUES (?,?,?,?,?,?)",
                (ts, None, path, total, lsm, self.run_label),
            )
        return ts, total, lsm

    def emit(self, path: str, total: int, lsm: int) -> None:
        if self.emit_json:
            import json
            print(json.dumps({
                "ts": datetime.now().isoformat(), "env": self.env, "label": self.version,
                "version": self.run_label, "path": path,
                "total_bytes": total, "lsm_bytes": lsm,
            }))
        else:
            lsm_str = f" | lsm {format_bytes(lsm)}" if lsm else ""
            print(f"{path} | total {format_bytes(total)}{lsm_str}")

    # --- lifecycle --------------------------------------------------------

    def stop(self, *_args: Any) -> None:
        self.running = False

    def _sleep(self) -> None:
        """Interruptible sleep: wake promptly on SIGINT/SIGTERM rather than
        sitting out the full (coarse) interval."""
        end = time.monotonic() + self.interval
        while self.running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    def run(self) -> None:
        print(
            f"=== {self.LABEL_PREFIX} disk-size monitor | env={self.env} | label={self.version} | "
            f"interval={self.interval:.0f}s"
            + (" | output=json" if self.emit_json else "")
            + " ==="
        )
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.report_existing()

        path = self.resolve_path()
        if not path:
            warn(f"Could not determine the directory to measure. Pass --path explicitly, "
                 f"or ensure the {self.binary_prefix} process is running with {self.PATH_FLAG}.")
            raise SystemExit(1)
        if not os.path.isdir(path):
            warn(f"Path is not a directory (yet?): {path}. Will keep retrying.")
        print(f"Measuring: {path}  (lsm subdir: {self.lsm_subdir}/)")

        rows = 0
        peak_total = 0
        peak_lsm = 0
        final_total: int | None = None
        while self.running:
            sample = self.sample_once(path)
            if sample is None:
                self._sleep()
                continue
            _ts, total, lsm = sample
            rows += 1
            peak_total = max(peak_total, total)
            peak_lsm = max(peak_lsm, lsm)
            final_total = total
            self.emit(path, total, lsm)
            self._sleep()

        print(
            f"Shutting down. Wrote {rows} samples to {self.db_file}.\n"
            f"  peak total: {format_bytes(peak_total)}"
            + (f" | peak lsm: {format_bytes(peak_lsm)}" if peak_lsm else "")
            + (f" | final total: {format_bytes(final_total)}" if final_total is not None else "")
        )
