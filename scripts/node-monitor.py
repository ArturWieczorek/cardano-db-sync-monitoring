#!/usr/bin/env python3
"""Long-running collector for cardano-node. Samples CPU/RAM via psutil and the
node's tip via cardano-cli. Pure collector — no plotting, no prompts."""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from _common import (
    find_processes,
    format_size,
    get_cpu_details,
    get_memory_details,
    init_sqlite_schema,
    report_existing_history,
    warn,
)
from psutil import Process

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cardano-node"
DATA_DIR.mkdir(parents=True, exist_ok=True)


TESTNET_MAGIC_BY_ENV: dict[str, int] = {
    "preview": 2,
    "preprod": 1,
}


class CardanoNodeMonitor:
    def __init__(self, env: str, node_ver: str, cardano_cli_path: str,
                 cardano_node_path: str, socket_path: str | None,
                 interval: float, emit_json: bool) -> None:
        self.running: bool = True
        self.env: str = env
        self.node_ver: str = node_ver
        self.cardano_cli_path: str = cardano_cli_path
        self.cardano_node_path: str = cardano_node_path
        self.socket_path: str | None = socket_path
        self.interval: float = interval
        self.emit_json: bool = emit_json
        self.mainnet: bool = env == "mainnet"
        self.network_magic: int | None = TESTNET_MAGIC_BY_ENV.get(env)
        self.run_label: str = f"cardano-node {node_ver} {env}"

        self.db_file: str = str(DATA_DIR / f"{self.env}.db")

        self.init_db()

    def init_db(self) -> None:
        init_sqlite_schema(self.db_file, version_table="node_version")
        with sqlite3.connect(self.db_file) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS node_ingest_metrics
                   (ts TEXT, slot_no INTEGER, epoch_no INTEGER, era TEXT,
                    sync_progress REAL, version TEXT)"""
            )
            conn.commit()

    def _match_node_process(self, proc: Process) -> bool:
        """True if `proc` looks like a cardano-node for our env.

        Two criteria:
          - argv[0] basename starts with `self.cardano_node_path` (covers both
            `cardano-node` and version-suffixed binaries like `cardano-node-11.0.1`).
          - some argv arg contains `self.env` (--config preprod/..., --socket-path
            preprod/..., etc.) so we don't grab a different env's node.
        """
        cmdline = proc.info.get("cmdline") or []
        if not cmdline:
            return False
        exe_base = os.path.basename(cmdline[0])
        if not exe_base.startswith(self.cardano_node_path):
            return False
        return any(self.env in arg for arg in cmdline[1:])

    def get_process(self) -> Process | None:
        matches = find_processes(self._match_node_process)
        if len(matches) > 1:
            pids = ", ".join(str(p.pid) for p in matches)
            warn(f"Multiple cardano-node processes match env={self.env}: {pids}. Using PID {matches[0].pid}.")
        return matches[0] if matches else None

    def resolve_socket_path(self) -> str | None:
        """Return socket path: explicit --socket-path wins, else parse from the matched node's argv.

        Relative paths in the node's argv are resolved against the node process's CWD,
        since cardano-cli (run from this script's CWD) would otherwise miss them.
        """
        if self.socket_path:
            return self.socket_path
        proc = self.get_process()
        if not proc:
            return None
        try:
            cmdline = proc.cmdline()
            node_cwd = proc.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

        raw: str | None = None
        for i, arg in enumerate(cmdline):
            if arg == "--socket-path" and i + 1 < len(cmdline):
                raw = cmdline[i + 1]
                break
            if arg.startswith("--socket-path="):
                raw = arg.split("=", 1)[1]
                break
        if raw is None:
            return None
        return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(node_cwd, raw))

    def query_tip(self) -> dict[str, Any] | None:
        cmd = [self.cardano_cli_path, "query", "tip"]
        if self.mainnet:
            cmd.append("--mainnet")
        elif self.network_magic is not None:
            cmd.extend(["--testnet-magic", str(self.network_magic)])
        env = os.environ.copy()
        sock = self.resolve_socket_path()
        if sock:
            env["CARDANO_NODE_SOCKET_PATH"] = sock
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15, env=env)
            return json.loads(result.stdout.strip())
        except FileNotFoundError:
            warn(f"cardano-cli not found at {self.cardano_cli_path}")
            return None
        except subprocess.CalledProcessError as e:
            warn(f"cardano-cli error: {e.stderr.strip()}")
            return None
        except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            warn(f"cardano-cli parse/timeout error: {e}")
            return None

    def stop(self, *_args: Any) -> None:
        self.running = False

    def emit_sample(self, *, ts: str, slot: int, epoch: int | None, era: str | None,
                    sync_progress: float | None, mem: dict[str, float] | None,
                    cpu: dict[str, Any] | None) -> None:
        if self.emit_json:
            line = {
                "ts": ts,
                "env": self.env,
                "label": self.node_ver,
                "version": self.run_label,
                "slot_no": slot,
                "epoch_no": epoch,
                "era": era,
                "sync_percent": sync_progress,
                "cpu_percent": cpu["cpu_percent"] if cpu else None,
                "rss_mb": mem["rss"] if mem else None,
                "vms_mb": mem["vms"] if mem else None,
            }
            print(json.dumps(line))
            return
        epoch_str = str(epoch) if epoch is not None else "N/A"
        era_str = era if era is not None else "N/A"
        sync_str = f"{sync_progress:.2f}%" if sync_progress is not None else "N/A"
        cpu_str = f"{cpu['cpu_percent']:.1f}%" if cpu else "N/A"
        rss_str = format_size(mem["rss"] if mem else None)
        print(
            f"Slot {slot} | Epoch {epoch_str} | Era {era_str} | Sync {sync_str} | "
            f"CPU {cpu_str} | RSS {rss_str}"
        )

    def run(self) -> None:
        print(
            f"=== cardano-node monitor | env={self.env} | label={self.node_ver} | "
            f"interval={self.interval}s"
            + (" | output=json" if self.emit_json else "")
            + " ==="
        )
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        report_existing_history(self.db_file, "node_version", self.run_label)

        proc = self.get_process()
        if proc:
            try:
                proc.cpu_percent(interval=None)  # prime the cpu_percent sampler
            except Exception:
                pass

        rows = 0
        while self.running:
            tip = self.query_tip()
            slot = tip.get("slot") if tip else None
            if slot is None:
                time.sleep(self.interval)
                continue
            epoch = tip.get("epoch") if tip else None
            era = tip.get("era") if tip else None  # cardano-cli returns "Byron"/"Shelley"/.../"Conway"
            sp = tip.get("syncProgress") if tip else None
            sync_progress: float | None
            if sp is None or sp == "unavailable":
                sync_progress = None
            else:
                try:
                    sync_progress = float(sp)
                except (ValueError, TypeError):
                    sync_progress = None

            proc = self.get_process()
            mem = get_memory_details(proc) if proc else None
            cpu = get_cpu_details(proc) if proc else None
            ts = datetime.now().isoformat()

            with sqlite3.connect(self.db_file) as conn:
                if mem:
                    conn.execute(
                        "INSERT INTO memory_metrics (ts, slot_no, rss, vms, uss, pss, shared, swap, version) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (ts, slot, mem["rss"], mem["vms"], mem["uss"],
                         mem["pss"], mem["shared"], mem["swap"], self.run_label),
                    )
                if cpu:
                    conn.execute(
                        "INSERT INTO cpu_metrics (ts, slot_no, cpu_percent, user_time, system_time, "
                        "children_user, children_system, iowait, ctx_switches, interrupts, version) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (ts, slot, cpu["cpu_percent"], cpu["user_time"], cpu["system_time"],
                         cpu["children_user"], cpu["children_system"],
                         cpu["iowait"], cpu["ctx_switches"], cpu["interrupts"], self.run_label),
                    )
                conn.execute(
                    "INSERT INTO node_version VALUES (?,?)",
                    (ts, self.run_label),
                )
                conn.execute(
                    "INSERT INTO node_ingest_metrics (ts, slot_no, epoch_no, era, sync_progress, version) "
                    "VALUES (?,?,?,?,?,?)",
                    (ts, slot, epoch, era, sync_progress, self.run_label),
                )

            rows += 1
            self.emit_sample(
                ts=ts, slot=slot, epoch=epoch, era=era,
                sync_progress=sync_progress, mem=mem, cpu=cpu,
            )
            time.sleep(self.interval)

        print(f"Shutting down. Wrote {rows} samples to {self.db_file}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cardano Node resources monitor (collector)")
    parser.add_argument("--env",
                        required=True,
                        choices=["mainnet", "preprod", "preview"],
                        help="Environment name (network is derived from this)")
    parser.add_argument("--node-ver",
                        required=True,
                        help="Label used to tag this run in the DB (e.g. 11.0.1, test-refactor-11.0.1)")
    parser.add_argument("--cardano-cli-path",
                        default="cardano-cli",
                        help="Path to cardano-cli executable")
    parser.add_argument("--cardano-node-path",
                        default="cardano-node",
                        help="Executable name/path of the cardano-node process")
    parser.add_argument("--socket-path",
                        help="Override the node socket path. By default the script reads "
                             "--socket-path from the matched cardano-node process's command line, "
                             "so you usually don't need to set this.")
    parser.add_argument("--interval",
                        type=float,
                        default=10.0,
                        help="Sampling interval in seconds (default: 10)")
    parser.add_argument("--json",
                        dest="emit_json",
                        action="store_true",
                        help="Emit one JSON object per sample on stdout (instead of the "
                             "human-readable pipe-separated form).")
    return parser.parse_args()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    args = parse_args()
    monitor = CardanoNodeMonitor(
        env=args.env,
        node_ver=args.node_ver,
        cardano_cli_path=args.cardano_cli_path,
        cardano_node_path=args.cardano_node_path,
        socket_path=args.socket_path,
        interval=args.interval,
        emit_json=args.emit_json,
    )
    monitor.run()
