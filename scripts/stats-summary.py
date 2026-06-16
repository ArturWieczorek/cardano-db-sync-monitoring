#!/usr/bin/env python3
"""Print a nicely formatted summary of a run from the SQLite stats DB - the
"what happened in this run, at a glance" tool. Works for both cardano-node and
cardano-db-sync DBs (role auto-detected from the schema). SQLite-read-only.

Distinct from the two report tools:
  - db-sync-epoch-report.py        - Postgres per-epoch/size/summary report.
  - db-sync-stats-report.py  - SQLite plots -> PNG/HTML comparison report.
  - stats-summary.py (this)  - SQLite stats -> quick text summary in the terminal.

It runs the same queries documented in docs/12-useful-queries.md and converts
units for you (RAM and disk shown as binary GiB/MiB).

Examples:
    # Overview of every run in an environment's DB:
    python3 scripts/stats-summary.py --env preprod

    # Deep dive on one build:
    python3 scripts/stats-summary.py --env preprod --version 13.7.1.0-node-11.0.1
    python3 scripts/stats-summary.py --env mainnet --role node --version LSM-11.0.1
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from _common import (
    VERSION_KEYED_TABLES,
    format_bytes,
    format_duration,
    load_all_versions,
    resolve_versions,
    short,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = {
    "node": PROJECT_ROOT / "data" / "cardano-node",
    "db-sync": PROJECT_ROOT / "data" / "cardano-db-sync",
}
ENVS = ("mainnet", "preprod", "preview")


# --- helpers ---------------------------------------------------------------


def detect_role(conn: sqlite3.Connection) -> str:
    """'node' or 'db-sync' from which version table the DB carries."""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "db_sync_version" in names:
        return "db-sync"
    if "node_version" in names:
        return "node"
    raise SystemExit("Can't tell the DB role: neither db_sync_version nor node_version present.")


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _gib_from_mib(mib: float | None) -> str:
    """Reuse the binary byte formatter for a MiB value (memory)."""
    return format_bytes(int(mib * 1024**2)) if mib is not None else "N/A"


def _gib_from_bytes(b: int | None) -> str:
    return format_bytes(int(b)) if b is not None else "N/A"


def _duration_secs(conn: sqlite3.Connection, table: str, label: str) -> float | None:
    return _scalar(
        conn,
        f"SELECT (julianday(MAX(ts)) - julianday(MIN(ts))) * 86400 FROM {table} WHERE version = ? AND ts IS NOT NULL",
        (label,),
    )


def _print_section(title: str, pairs: list[tuple[str, str]]) -> None:
    if not pairs:
        return
    print(f"\n  {title}")
    width = max(len(k) for k, _ in pairs)
    for k, v in pairs:
        print(f"    {k:<{width}}  {v}")


# --- detail view -----------------------------------------------------------


def summarize_version(conn: sqlite3.Connection, role: str, label: str, top: int) -> None:
    print(f"\n{'=' * 70}\n  {label}\n{'=' * 70}")

    # Run
    samples = _scalar(conn, "SELECT COUNT(*) FROM memory_metrics WHERE version = ?", (label,))
    first = _scalar(conn, "SELECT MIN(ts) FROM memory_metrics WHERE version = ?", (label,))
    last = _scalar(conn, "SELECT MAX(ts) FROM memory_metrics WHERE version = ?", (label,))
    dur = _duration_secs(conn, "memory_metrics", label)
    _print_section(
        "Run",
        [
            ("samples", f"{samples:,}" if samples else "0"),
            ("first sample", str(first or "N/A")),
            ("last sample", str(last or "N/A")),
            ("duration", format_duration(dur)),
        ],
    )

    # Memory & CPU
    peak_rss = _scalar(conn, "SELECT MAX(rss) FROM memory_metrics WHERE version = ?", (label,))
    start_rss = _scalar(conn, "SELECT rss FROM memory_metrics WHERE version = ? ORDER BY ts LIMIT 1", (label,))
    end_rss = _scalar(conn, "SELECT rss FROM memory_metrics WHERE version = ? ORDER BY ts DESC LIMIT 1", (label,))
    peak_cpu = _scalar(conn, "SELECT MAX(cpu_percent) FROM cpu_metrics WHERE version = ?", (label,))
    avg_cpu = _scalar(conn, "SELECT AVG(cpu_percent) FROM cpu_metrics WHERE version = ?", (label,))
    _print_section(
        "Memory & CPU",
        [
            ("peak RAM (RSS)", _gib_from_mib(peak_rss)),
            ("RAM start -> end", f"{_gib_from_mib(start_rss)} -> {_gib_from_mib(end_rss)}"),
            ("peak CPU", f"{peak_cpu:.0f}%" if peak_cpu is not None else "N/A"),
            ("avg CPU", f"{avg_cpu:.0f}%" if avg_cpu is not None else "N/A"),
        ],
    )

    # Disk (optional collector)
    if _table_exists(conn, "disk_metrics") and _scalar(
        conn, "SELECT COUNT(*) FROM disk_metrics WHERE version = ?", (label,)
    ):
        peak_total = _scalar(conn, "SELECT MAX(total_bytes) FROM disk_metrics WHERE version = ?", (label,))
        peak_lsm = _scalar(conn, "SELECT MAX(lsm_bytes) FROM disk_metrics WHERE version = ?", (label,))
        final = conn.execute(
            "SELECT total_bytes, lsm_bytes, path FROM disk_metrics WHERE version = ? ORDER BY ts DESC LIMIT 1",
            (label,),
        ).fetchone()
        # Only show the lsm/ subdir lines when the build actually used LSM
        # (in-memory builds have no lsm/ dir, so lsm_bytes stays 0).
        used_lsm = bool(peak_lsm and peak_lsm > 0)
        pairs = [("peak total", _gib_from_bytes(peak_total))]
        if used_lsm:
            pairs.append(("peak lsm/", _gib_from_bytes(peak_lsm)))
        pairs.append(("final total", _gib_from_bytes(final[0])))
        if used_lsm:
            pairs.append(("final lsm/", _gib_from_bytes(final[1])))
        pairs.append(("measured path", str(final[2])))
        _print_section("On-disk size", pairs)

    if role == "db-sync":
        _summarize_db_sync(conn, label, top)
    else:
        _summarize_node(conn, label)


def _summarize_db_sync(conn: sqlite3.Connection, label: str, top: int) -> None:
    row = conn.execute(
        "SELECT db_size_bytes, max_block_no, max_tx_id, utxo_count FROM ingest_metrics "
        "WHERE version = ? AND db_size_bytes IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (label,),
    ).fetchone()
    if row:
        _print_section(
            "Ingest",
            [
                ("final DB size", _gib_from_bytes(row[0])),
                ("max block", f"{row[1]:,}" if row[1] is not None else "N/A"),
                ("max tx id", f"{row[2]:,}" if row[2] is not None else "N/A"),
                ("UTXO count", f"{row[3]:,}" if row[3] is not None else "N/A (tracking off)"),
            ],
        )
    lag = conn.execute(
        "SELECT AVG(tip_lag_sec), MAX(tip_lag_sec) FROM ingest_metrics WHERE version = ?",
        (label,),
    ).fetchone()
    if lag and lag[0] is not None:
        _print_section(
            "Tip lag",
            [
                ("avg", format_duration(lag[0])),
                ("max", format_duration(lag[1])),
            ],
        )
    tables = conn.execute(
        "SELECT table_name, MAX(row_count) FROM table_rowcounts WHERE version = ? "
        "GROUP BY table_name ORDER BY MAX(row_count) DESC LIMIT ?",
        (label, top),
    ).fetchall()
    if tables:
        _print_section(f"Top {len(tables)} tables by row count", [(name, f"{rc:,}") for name, rc in tables])


def _summarize_node(conn: sqlite3.Connection, label: str) -> None:
    row = conn.execute(
        "SELECT epoch_no, era, sync_progress FROM node_ingest_metrics "
        "WHERE version = ? AND epoch_no IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (label,),
    ).fetchone()
    if row:
        _print_section(
            "Sync state",
            [
                ("latest epoch", str(row[0])),
                ("era", str(row[1])),
                ("sync progress", f"{row[2]:.2f}%" if row[2] is not None else "N/A"),
            ],
        )
    if _table_exists(conn, "rts_metrics") and _scalar(
        conn, "SELECT COUNT(*) FROM rts_metrics WHERE version = ?", (label,)
    ):
        pairs = []
        for metric, lbl in (
            ("cardano_node_metrics_RTS_gcHeapBytes_int", "peak heap"),
            ("cardano_node_metrics_RTS_gcLiveBytes_int", "peak live"),
        ):
            v = _scalar(conn, "SELECT MAX(value) FROM rts_metrics WHERE version = ? AND metric = ?", (label, metric))
            if v is not None:
                pairs.append((lbl, _gib_from_bytes(v)))
        _print_section("RTS / runtime", pairs)


# --- overview view ---------------------------------------------------------


def overview(conn: sqlite3.Connection, role: str, versions: list[str]) -> None:
    has_disk = _table_exists(conn, "disk_metrics")
    # Per version: base headline numbers plus optional peak total / peak lsm bytes.
    data = []
    any_disk = any_lsm = False
    for v in versions:
        samples = _scalar(conn, "SELECT COUNT(*) FROM memory_metrics WHERE version = ?", (v,)) or 0
        dur = _duration_secs(conn, "memory_metrics", v)
        peak_rss = _scalar(conn, "SELECT MAX(rss) FROM memory_metrics WHERE version = ?", (v,))
        peak_cpu = _scalar(conn, "SELECT MAX(cpu_percent) FROM cpu_metrics WHERE version = ?", (v,))
        peak_disk = peak_lsm = None
        if has_disk:
            peak_disk = _scalar(conn, "SELECT MAX(total_bytes) FROM disk_metrics WHERE version = ?", (v,))
            peak_lsm = _scalar(conn, "SELECT MAX(lsm_bytes) FROM disk_metrics WHERE version = ?", (v,))
            any_disk = any_disk or bool(peak_disk)
            any_lsm = any_lsm or bool(peak_lsm and peak_lsm > 0)
        data.append((v, samples, dur, peak_rss, peak_cpu, peak_disk, peak_lsm))

    # Columns: disk/lsm only appear when there's data (and lsm only if a build
    # actually used LSM - in-memory runs have lsm_bytes = 0).
    headers = ["version", "samples", "duration", "peak RAM", "peak CPU"]
    if any_disk:
        headers.append("peak disk")
    if any_lsm:
        headers.append("peak lsm")
    rows = []
    for v, samples, dur, peak_rss, peak_cpu, peak_disk, peak_lsm in data:
        row = [
            short(v),
            f"{samples:,}",
            format_duration(dur),
            _gib_from_mib(peak_rss),
            f"{peak_cpu:.0f}%" if peak_cpu is not None else "-",
        ]
        if any_disk:
            row.append(_gib_from_bytes(peak_disk) if peak_disk else "-")
        if any_lsm:
            row.append(_gib_from_bytes(peak_lsm) if (peak_lsm and peak_lsm > 0) else "-")
        rows.append(row)

    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    print(f"\n  {role} stats - {len(versions)} version(s)\n")
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        print("  " + "  ".join(r[i].ljust(widths[i]) for i in range(len(headers))))
    print("\n  (pass --version <token> for a detailed breakdown)")


# --- plumbing --------------------------------------------------------------


def resolve_db(args: argparse.Namespace) -> tuple[str, str]:
    """Return (db_path, role)."""
    if args.sqlite_db:
        db = args.sqlite_db
        if not Path(db).exists():
            raise SystemExit(f"{db} not found.")
        with sqlite3.connect(db) as c:
            return db, (args.role or detect_role(c))
    if args.role:
        db = str(DATA_DIR[args.role] / f"{args.env}.db")
        if not Path(db).exists():
            raise SystemExit(f"{db} not found.")
        return db, args.role
    # No role given: find which role's DB exists for this env.
    candidates = [(r, str(DATA_DIR[r] / f"{args.env}.db")) for r in ("node", "db-sync")]
    found = [(r, p) for r, p in candidates if Path(p).exists()]
    if not found:
        raise SystemExit(f"No stats DB for env '{args.env}' under data/cardano-node or data/cardano-db-sync.")
    if len(found) > 1:
        raise SystemExit(f"Both node and db-sync DBs exist for '{args.env}'; pass --role to pick one.")
    return found[0][1], found[0][0]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", choices=ENVS, help="Environment (selects data/<role>/<env>.db).")
    p.add_argument("--sqlite-db", default=None, help="Explicit DB path (role auto-detected).")
    p.add_argument("--role", choices=["node", "db-sync"], default=None, help="Override role auto-detection.")
    p.add_argument("--version", default=None, help="Version token for a detailed view (else: overview of all).")
    p.add_argument("--top", type=int, default=10, help="Tables to show in the db-sync detail view (default 10).")
    args = p.parse_args()
    if not args.env and not args.sqlite_db:
        p.error("pass --env (or --sqlite-db)")
    return args


def main() -> None:
    args = parse_args()
    db, role = resolve_db(args)
    with sqlite3.connect(db) as conn:
        versions = load_all_versions(db, list(VERSION_KEYED_TABLES[role]))
        if not versions:
            raise SystemExit(f"No versions found in {db}.")
        print(f"DB: {db}  (role: {role})")
        if args.version:
            label = resolve_versions([args.version], versions)[0]
            summarize_version(conn, role, label, args.top)
        else:
            overview(conn, role, versions)


if __name__ == "__main__":
    main()
