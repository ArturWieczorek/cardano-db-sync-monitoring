#!/usr/bin/env python3
"""Collector for cardano-node RTS / runtime metrics, scraped from the node's
Prometheus endpoint. Appends a (metric, value) time series to `rts_metrics` in
the same `data/cardano-node/<env>.db`, under the same version label
node-resource-monitor.py uses - so the RTS curves join the rest of the run.

Why a separate script (and node-resource-monitor.py left untouched):
  - It adds an HTTP scrape per interval. That's isolated here behind a timeout
    and try/except, so a slow/unreachable endpoint can never stall or crash the
    psutil/tip sampling node-resource-monitor.py does. Zero risk to the running monitor.

What it captures that psutil can't: GC counts / allocations / heap & live bytes
(the resource signals a GHC-version or allocator change shows up in), plus
mempool gauges. cardano-node exposes these on its Prometheus endpoint when run
with `+RTS -T` and configured with `hasPrometheus: [host, port]` (default port
12798). Metric *names* vary by node version / tracing backend, so we scrape the
whole endpoint and keep the ones matching a curated substring allowlist
(`--include` to override). `--list-metrics` prints everything the endpoint
currently exposes, so you can discover the exact names for your node.

The table is intentionally long/narrow - one row per (sample, metric) - so any
metric name works without schema churn:

    SELECT DISTINCT metric FROM rts_metrics WHERE version = '<label>';
    SELECT ts, value FROM rts_metrics
     WHERE version = '<label>' AND metric = 'cardano_node_metrics_RTS_gcMajorNum_int';

Plot with node-plot.py --metrics rts (or --metrics all). Pure collector - no
plotting, no prompts. Stops cleanly on SIGINT/SIGTERM.
"""

import argparse
import json
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from _common import connect_writer, warn

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cardano-node"

DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:12798/metrics"

# Substrings (case-insensitive) of metric names to keep. Tuned to match both the
# new tracing system (`cardano_node_metrics_RTS_gc*`, `..._txsInMempool_int`,
# `..._mempoolBytes_int`) and the older EKG names (`rts_gc_*`). Broad on purpose
# - over-matching a few extra gauges is harmless; missing the GC ones isn't.
DEFAULT_INCLUDE: tuple[str, ...] = (
    "rts",
    "gc",
    "alloc",
    "heap",
    "live",
    "mempool",
)

# Metric whose value is the current slot, used to stamp slot_no on each row so
# the RTS series can share the slot x-axis with the other metrics. Matched by
# substring so it works across naming schemes (`cardano_node_metrics_slotNum_int`).
_SLOT_SUBSTR = "slotnum"

# name{labels}  value  [timestamp]  - labels and trailing timestamp optional.
_METRIC_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(?P<rest>.+)$")


def parse_prometheus_text(text: str) -> dict[str, float]:
    """Parse Prometheus text-exposition format into {metric_name: value}.

    Skips `#` HELP/TYPE/comment lines and blank lines, strips any `{labels}`
    from the key, takes the first whitespace token after the name as the value
    (ignoring an optional trailing timestamp), and drops anything that isn't a
    finite float (NaN / +Inf / -Inf / unparseable). Last value wins on
    duplicate names (our gauges are unlabeled, so this doesn't bite)."""
    out: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        tokens = m.group("rest").split()
        if not tokens:
            continue
        try:
            val = float(tokens[0])
        except ValueError:
            continue
        # Reject NaN / ±Inf - they'd poison plots and aggregations.
        if val != val or val in (float("inf"), float("-inf")):
            continue
        out[m.group("name")] = val
    return out


def select_metrics(metrics: dict[str, float], includes: list[str]) -> dict[str, float]:
    """Keep only metrics whose name contains one of `includes` (case-insensitive)."""
    inc = [s.lower() for s in includes]
    return {k: v for k, v in metrics.items() if any(s in k.lower() for s in inc)}


def extract_slot(metrics: dict[str, float]) -> int | None:
    """Current slot from the scraped metrics (slotNum-like gauge), or None.

    Read from the *full* scrape, not the selected subset, so slot stamping
    doesn't depend on the allowlist including the slot gauge."""
    for k, v in metrics.items():
        if _SLOT_SUBSTR in k.lower():
            try:
                return int(v)
            except (ValueError, OverflowError):
                return None
    return None


def fetch_metrics(url: str, timeout: float) -> str | None:
    """GET the Prometheus endpoint. Returns the body, or None on any failure
    (logged) - the caller skips the sample rather than crashing the loop."""
    try:
        with urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError, ValueError) as e:
        warn(f"Prometheus scrape failed for {url}: {e}")
        return None


class NodeRtsMonitor:
    def __init__(
        self,
        env: str,
        node_ver: str,
        url: str,
        includes: list[str],
        interval: float,
        timeout: float,
        emit_json: bool,
        sqlite_db: str | None,
    ) -> None:
        self.running: bool = True
        self.env: str = env
        self.node_ver: str = node_ver
        self.url: str = url
        self.includes: list[str] = includes
        self.interval: float = interval
        self.timeout: float = timeout
        self.emit_json: bool = emit_json
        self.run_label: str = f"cardano-node {node_ver} {env}"
        self.db_file: str = sqlite_db or str(DATA_DIR / f"{env}.db")
        Path(self.db_file).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def init_db(self) -> None:
        with connect_writer(self.db_file) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rts_metrics
                   (ts TEXT, slot_no INTEGER, metric TEXT, value REAL, version TEXT)"""
            )
            conn.commit()

    def report_existing(self) -> None:
        try:
            with sqlite3.connect(self.db_file) as conn:
                row = conn.execute(
                    "SELECT COUNT(*), MIN(ts), MAX(ts) FROM rts_metrics WHERE version = ?",
                    (self.run_label,),
                ).fetchone()
        except Exception as e:
            warn(f"Startup history check failed: {e}")
            return
        count = (row or [0])[0] or 0
        if count == 0:
            print(f"This version label has no existing rts samples in {self.db_file}.")
            self._warn_if_label_looks_mistyped()
        else:
            print(
                f"Note: this version label already has {count:,} rts samples in "
                f"{self.db_file} (first {row[1]}, last {row[2]}). New samples will be appended."
            )

    def _warn_if_label_looks_mistyped(self) -> None:
        """If this is a brand-new label but the DB already carries other labels,
        the new one is probably a typo (e.g. passing the env-dir name instead of
        the node version). List the existing labels so the mismatch is caught at
        collection time, not weeks later when the plot silently drops the series."""
        try:
            with sqlite3.connect(self.db_file) as conn:
                others = [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT version FROM rts_metrics WHERE version <> ?",
                        (self.run_label,),
                    ).fetchall()
                ]
        except Exception:
            return
        if others:
            warn(
                f"'{self.run_label}' is a new label; this DB already has: "
                f"{', '.join(others)}. If that's a typo, stop and re-run with the "
                "matching --node-ver (otherwise the plot won't group these together)."
            )

    def record(self, text: str) -> tuple[str, int | None, dict[str, float]] | None:
        """Parse a scrape, store the selected metrics, return (ts, slot, selected).
        None when nothing in the scrape matched the allowlist (so the caller can
        warn about RTS metrics not being exposed) - distinct from a fetch failure."""
        parsed = parse_prometheus_text(text)
        selected = select_metrics(parsed, self.includes)
        if not selected:
            return None
        slot = extract_slot(parsed)
        ts = datetime.now().isoformat()
        rows = [(ts, slot, name, val, self.run_label) for name, val in sorted(selected.items())]
        with connect_writer(self.db_file) as conn:
            conn.executemany(
                "INSERT INTO rts_metrics (ts, slot_no, metric, value, version) VALUES (?,?,?,?,?)",
                rows,
            )
        return ts, slot, selected

    def emit(self, ts: str, slot: int | None, selected: dict[str, float]) -> None:
        if self.emit_json:
            print(
                json.dumps(
                    {
                        "ts": ts,
                        "env": self.env,
                        "label": self.node_ver,
                        "version": self.run_label,
                        "slot_no": slot,
                        "metrics": selected,
                    }
                )
            )
        else:
            slot_str = str(slot) if slot is not None else "N/A"
            print(f"Slot {slot_str} | {len(selected)} rts metrics")

    def stop(self, *_args: Any) -> None:
        self.running = False

    def _sleep(self) -> None:
        end = time.monotonic() + self.interval
        while self.running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    def list_metrics(self) -> int:
        text = fetch_metrics(self.url, self.timeout)
        if text is None:
            return 1
        parsed = parse_prometheus_text(text)
        for name in sorted(parsed):
            print(f"{name} {parsed[name]}")
        print(
            f"\n{len(parsed)} metrics at {self.url}; "
            f"{len(select_metrics(parsed, self.includes))} match the current allowlist.",
            file=sys.stderr,
        )
        return 0

    def run(self) -> None:
        print(
            f"=== cardano-node RTS monitor | env={self.env} | label={self.node_ver} | "
            f"url={self.url} | interval={self.interval:.0f}s" + (" | output=json" if self.emit_json else "") + " ==="
        )
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.report_existing()

        rows = 0
        warned_empty = False
        while self.running:
            text = fetch_metrics(self.url, self.timeout)
            if text is None:
                self._sleep()
                continue
            try:
                result = self.record(text)
            except sqlite3.OperationalError as e:
                # Another writer held the lock past the busy timeout (rare). Drop
                # this scrape with a warning rather than killing a multi-day run.
                warn(f"DB busy, dropped scrape: {e}")
                self._sleep()
                continue
            if result is None:
                if not warned_empty:
                    warn(
                        "Endpoint reachable but no metrics matched the allowlist - is the node "
                        "running with `+RTS -T`? Use --list-metrics to see what's exposed, or "
                        "adjust --include."
                    )
                    warned_empty = True
                self._sleep()
                continue
            ts, slot, selected = result
            rows += 1
            self.emit(ts, slot, selected)
            self._sleep()

        print(f"Shutting down. Wrote {rows} samples to {self.db_file}.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="cardano-node RTS/runtime metrics monitor (Prometheus scraper)")
    p.add_argument(
        "--env",
        required=True,
        choices=["mainnet", "preprod", "preview"],
        help="Environment name (selects data/cardano-node/<env>.db and the version label)",
    )
    p.add_argument(
        "--node-ver",
        required=True,
        help="Label tagging this run (e.g. LSM-11.0.1). Match the --node-ver you gave "
        "node-resource-monitor so the RTS series joins the rest of the run.",
    )
    p.add_argument(
        "--prometheus-url",
        default=DEFAULT_PROMETHEUS_URL,
        help=f"Node Prometheus endpoint (default: {DEFAULT_PROMETHEUS_URL}). For multiple "
        "nodes on one host, pass each instance's distinct port.",
    )
    p.add_argument(
        "--include",
        default=",".join(DEFAULT_INCLUDE),
        help="Comma-separated case-insensitive substrings; a metric is kept if its name "
        f"contains any of them. Default: {','.join(DEFAULT_INCLUDE)}",
    )
    p.add_argument(
        "--interval", type=float, default=10.0, help="Sampling interval in seconds (default: 10 - the scrape is light)."
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-scrape HTTP timeout in seconds (default: 5). On timeout the sample is skipped.",
    )
    p.add_argument(
        "--list-metrics",
        action="store_true",
        help="Scrape once, print every metric the endpoint exposes (and how many match "
        "the allowlist), then exit. Use this to discover your node's metric names.",
    )
    p.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit one JSON object per sample on stdout instead of the human form.",
    )
    p.add_argument(
        "--sqlite-db", default=None, help="Override the SQLite DB path (default: data/cardano-node/<env>.db)."
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined,union-attr]
    args = parse_args()
    includes = [s.strip() for s in args.include.split(",") if s.strip()]
    monitor = NodeRtsMonitor(
        env=args.env,
        node_ver=args.node_ver,
        url=args.prometheus_url,
        includes=includes,
        interval=args.interval,
        timeout=args.timeout,
        emit_json=args.emit_json,
        sqlite_db=args.sqlite_db,
    )
    if args.list_metrics:
        sys.exit(monitor.list_metrics())
    monitor.run()
