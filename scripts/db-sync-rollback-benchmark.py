#!/usr/bin/env python3
"""Controlled rollback benchmark for comparing cardano-db-sync versions.

Measures the rollback *deletion phase* of a specific db-sync version by running
its `cardano-db-tool rollback --slot <N>` against a Postgres database that has
been synced to a normalized tip slot S. cardano-db-tool runs the exact delete
path production db-sync uses, but needs only a DB connection - no cardano-node,
no running db-sync process - so the measurement is isolated and reproducible.

Fair cross-version comparison (the answer to "the versions are at different
chain points"): sync ONE database to slot S and snapshot it; then for each
version's cardano-db-tool, restore that snapshot, roll back to S-D, and measure.
Identical starting bytes and identical depth make the db-sync version the only
variable. Run each version through this script and compare the reported stats.

What is measured per repetition: wall-clock deletion duration, peak RSS and CPU
of the db-tool process, and (optionally) whether a follow-up db-sync-compare
confirms the resulting data matches a reference. Results go to the version-keyed
`rollback_benchmarks` table in data/cardano-db-sync/<env>.db.

The rollback is destructive, so between repetitions the database must be reset to
slot S - pass `--restore-cmd` (e.g. a pg_restore of the snapshot). Recovery-phase
timing (re-applying blocks back to the tip) needs a live node + db-sync and is
out of scope here.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from _common import connect_writer, get_memory_details, warn
from _rollback import RollbackLogEnd, parse_rollback_log_line, summarize

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cardano-db-sync"

# How often the best-effort RSS sampler polls the db-tool subprocess.
_PEAK_SAMPLE_SEC = 0.05


def _max_optional(a: float | None, b: float | None) -> float | None:
    """Max of two optional floats, ignoring None; None only if both are None."""
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


class RollbackBenchmark:
    def __init__(
        self,
        env: str,
        db_sync_ver: str,
        db_tool: str,
        to_slot: int,
        from_slot: int | None,
        reps: int,
        restore_cmd: str | None,
        compare_cmd: str | None,
        tool_args: list[str],
        pgpassfile: str | None,
        emit_json: bool,
        sqlite_db: str | None,
    ) -> None:
        self.env = env
        self.db_sync_ver = db_sync_ver
        self.db_tool = db_tool
        self.to_slot = to_slot
        self.from_slot = from_slot
        self.reps = reps
        self.restore_cmd = restore_cmd
        self.compare_cmd = compare_cmd
        self.tool_args = tool_args
        self.pgpassfile = pgpassfile
        self.emit_json = emit_json
        self.run_label = f"cardano-db-sync {db_sync_ver} {env}"
        self.db_file = sqlite_db or str(DATA_DIR / f"{env}.db")
        Path(self.db_file).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def init_db(self) -> None:
        with connect_writer(self.db_file) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rollback_benchmarks
                   (ts TEXT, version TEXT, from_slot INTEGER, to_slot INTEGER,
                    depth_blocks INTEGER, repetition INTEGER,
                    delete_duration_sec REAL, recovery_duration_sec REAL,
                    peak_cpu_percent REAL, peak_rss_mib REAL, equivalence_ok INTEGER)"""
            )
            conn.commit()

    # --- overridable side-effecting steps (kept small so tests can stub them) ---

    def _invoke_tool(self) -> tuple[int, str, float, float | None, float | None]:
        """Run `cardano-db-tool rollback --slot <to_slot>` and measure it.

        Returns (returncode, stdout, wall_clock_sec, cpu_percent, peak_rss_mib).
        Wall-clock time IS the deletion duration: db-tool does only the rollback
        and exits, with no node feeding blocks back.

        CPU and peak RSS come from getrusage(RUSAGE_CHILDREN), which the kernel
        tracks exactly - so they're correct even when the rollback finishes in a
        few milliseconds (a sampling loop would miss it entirely). `cpu_percent`
        is utilization over the run ((user+sys) / wall * 100), not an instant
        peak. A best-effort psutil sample provides an RSS floor for the rare
        case where this child isn't the largest one getrusage has seen."""
        cmd = [self.db_tool, "rollback", "--slot", str(self.to_slot), *self.tool_args]
        env = dict(os.environ)
        if self.pgpassfile:
            env["PGPASSFILE"] = self.pgpassfile
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        sampled_rss = self._sample_peaks(proc.pid)
        stdout, _ = proc.communicate()  # reaps the child, so getrusage now includes it
        duration = time.monotonic() - start
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_sec = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        cpu_percent = (cpu_sec / duration * 100.0) if duration > 0 else None
        # ru_maxrss is the high-water RSS (KiB on Linux) across all reaped children;
        # an increase this rep is this child's exact peak. Combine with the sample.
        rusage_rss = after.ru_maxrss / 1024 if after.ru_maxrss > before.ru_maxrss else None
        peak_rss = _max_optional(sampled_rss, rusage_rss)
        return proc.returncode, stdout or "", duration, cpu_percent, peak_rss

    def _sample_peaks(self, pid: int) -> float | None:
        """Best-effort peak RSS (MiB) from polling the db-tool process. Takes one
        reading immediately so a sub-interval process still yields a value, then
        polls until exit. getrusage in `_invoke_tool` is the exact source; this is
        a floor/fallback. Returns None if the process is already gone."""
        try:
            ps = psutil.Process(pid)
        except psutil.Error:
            return None
        peak_rss: float | None = None
        while True:
            mem = get_memory_details(ps)
            if mem is not None:
                peak_rss = mem["rss"] if peak_rss is None else max(peak_rss, mem["rss"])
            try:
                if not ps.is_running() or ps.status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.Error:
                break
            time.sleep(_PEAK_SAMPLE_SEC)
        return peak_rss

    def _restore_snapshot(self) -> None:
        if self.restore_cmd:
            subprocess.run(self.restore_cmd, shell=True, check=True)

    def _run_compare(self) -> bool | None:
        """Run the comparison command; True if it exits 0 (data equivalent),
        False otherwise, None when no command was given."""
        if not self.compare_cmd:
            return None
        return subprocess.run(self.compare_cmd, shell=True).returncode == 0

    # --- orchestration ---------------------------------------------------------

    def run_once(self, repetition: int) -> dict[str, object]:
        rc, stdout, duration, peak_cpu, peak_rss = self._invoke_tool()
        if rc != 0:
            warn(f"cardano-db-tool rollback exited {rc}; output:\n{stdout}")
        depth_blocks = self._parse_deleted_blocks(stdout)
        equivalence = self._run_compare()
        row: dict[str, object] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "version": self.run_label,
            "from_slot": self.from_slot,
            "to_slot": self.to_slot,
            "depth_blocks": depth_blocks,
            "repetition": repetition,
            "delete_duration_sec": duration,
            "recovery_duration_sec": None,
            "peak_cpu_percent": peak_cpu,
            "peak_rss_mib": peak_rss,
            "equivalence_ok": None if equivalence is None else int(equivalence),
        }
        self._insert(row)
        return row

    @staticmethod
    def _parse_deleted_blocks(stdout: str) -> int | None:
        """Opportunistically pull a deleted-block count from db-tool output.
        db-tool runs with a null tracer so it usually prints little; this returns
        None when no count is present rather than guessing."""
        for line in stdout.splitlines():
            rec = parse_rollback_log_line(line)
            if isinstance(rec, RollbackLogEnd) and rec.blocks is not None:
                return rec.blocks
        return None

    def _insert(self, row: dict[str, object]) -> None:
        with connect_writer(self.db_file) as conn:
            conn.execute(
                "INSERT INTO rollback_benchmarks "
                "(ts, version, from_slot, to_slot, depth_blocks, repetition, delete_duration_sec, "
                "recovery_duration_sec, peak_cpu_percent, peak_rss_mib, equivalence_ok) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["ts"],
                    row["version"],
                    row["from_slot"],
                    row["to_slot"],
                    row["depth_blocks"],
                    row["repetition"],
                    row["delete_duration_sec"],
                    row["recovery_duration_sec"],
                    row["peak_cpu_percent"],
                    row["peak_rss_mib"],
                    row["equivalence_ok"],
                ),
            )

    def run(self) -> dict[str, object]:
        if self.reps > 1 and not self.restore_cmd:
            warn(
                "reps > 1 without --restore-cmd: the rollback is destructive, so repetitions "
                "after the first would roll back an already-rolled-back DB. Only the first "
                "repetition will be meaningful."
            )
        durations: list[float] = []
        rows: list[dict[str, object]] = []
        for rep in range(self.reps):
            # Restore before EVERY measured rep when a reset command is given, so
            # rep 0 starts from the same freshly-restored (cold-cache) state as the
            # rest - otherwise rep 0 would run against a possibly warm DB and skew
            # the aggregate. See the README note on cache fairness.
            if self.restore_cmd:
                self._restore_snapshot()
            row = self.run_once(rep)
            rows.append(row)
            durations.append(float(row["delete_duration_sec"]))  # type: ignore[arg-type]
            self._emit_rep(row)
        stats = summarize(durations)
        self._emit_summary(stats)
        return {"stats": stats, "rows": rows}

    def _emit_rep(self, row: dict[str, object]) -> None:
        if self.emit_json:
            print(json.dumps(row))
        else:
            rss = row["peak_rss_mib"]
            rss_str = f"{rss:.0f} MiB" if isinstance(rss, (int, float)) else "N/A"
            print(
                f"rep {row['repetition']}: deleted to slot {self.to_slot} in "
                f"{float(row['delete_duration_sec']):.2f}s (peak RSS {rss_str})"  # type: ignore[arg-type]
            )

    def _emit_summary(self, stats: dict[str, float | int | None]) -> None:
        med, lo, hi = stats["median"], stats["min"], stats["max"]
        if med is None:
            print("No repetitions ran.")
            return
        print(
            f"=== {self.run_label} | rollback to slot {self.to_slot} | n={stats['n']} ===\n"
            f"deletion duration: median {med:.2f}s, min {lo:.2f}s, max {hi:.2f}s, stdev {stats['stdev']:.2f}s"
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Controlled cardano-db-sync rollback benchmark (via cardano-db-tool)")
    p.add_argument("--env", required=True, choices=["mainnet", "preprod", "preview"], help="Environment name")
    p.add_argument(
        "--db-sync-ver",
        required=True,
        help="Label for the version under test (e.g. 13.7.1.0-node-11.0.1). Use the same label "
        "for the matching cardano-db-tool so the benchmark groups with the rest of the run.",
    )
    p.add_argument(
        "--db-tool",
        required=True,
        help="Path to this version's cardano-db-tool binary.",
    )
    p.add_argument("--to-slot", type=int, required=True, help="Slot to roll back to (the normalized S minus depth).")
    p.add_argument(
        "--from-slot",
        type=int,
        default=None,
        help="The normalized starting tip slot S the snapshot was taken at (recorded for reference).",
    )
    p.add_argument("--reps", type=int, default=3, help="Number of repetitions to time (default: 3).")
    p.add_argument(
        "--restore-cmd",
        default=None,
        help="Shell command that resets the DB back to slot S between repetitions (e.g. a pg_restore "
        "of the snapshot). Required for meaningful reps > 1, since the rollback is destructive.",
    )
    p.add_argument(
        "--compare-cmd",
        default=None,
        help="Optional shell command (e.g. a db-sync-compare invocation) run after the rollback; "
        "exit 0 is recorded as equivalence_ok=1.",
    )
    p.add_argument(
        "--tool-arg",
        action="append",
        default=[],
        dest="tool_args",
        help="Extra argument to pass through to cardano-db-tool rollback (repeatable). Keep these "
        "identical across versions, or treat the difference as the deliberate variable.",
    )
    p.add_argument("--pgpassfile", default=None, help="PGPASSFILE for the db-tool subprocess (else inherited env).")
    p.add_argument("--json", dest="emit_json", action="store_true", help="Emit one JSON object per repetition.")
    p.add_argument(
        "--sqlite-db", default=None, help="Override the SQLite DB path (default: data/cardano-db-sync/<env>.db)."
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined,union-attr]
    args = parse_args()
    bench = RollbackBenchmark(
        env=args.env,
        db_sync_ver=args.db_sync_ver,
        db_tool=args.db_tool,
        to_slot=args.to_slot,
        from_slot=args.from_slot,
        reps=args.reps,
        restore_cmd=args.restore_cmd,
        compare_cmd=args.compare_cmd,
        tool_args=args.tool_args,
        pgpassfile=args.pgpassfile,
        emit_json=args.emit_json,
        sqlite_db=args.sqlite_db,
    )
    bench.run()
