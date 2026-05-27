#!/usr/bin/env python3
"""Rename a version label across every version-keyed table in a stats DB.

Wraps the manual `UPDATE ... SET version = ...` recipe from the README so
renames go through a single source of truth — easy to forget tables when
typing the SQL by hand, and skipping `ingest_metrics` / `table_rowcounts`
(db-sync) or `node_ingest_metrics` (node) leaves the plot script joining
stale rows and silently dropping ingest/tables panels.

Schema (which tables carry a `version` column):

    db-sync DB: memory_metrics, cpu_metrics, db_sync_version,
                ingest_metrics, table_rowcounts        (5 tables)
    node DB:    memory_metrics, cpu_metrics, node_version,
                node_ingest_metrics                    (4 tables)

The script auto-detects role from the schema (presence of `db_sync_version`
vs `node_version`) and updates every table in one transaction.

Usage:

    # Rename a label in data/cardano-db-sync/preprod.db:
    python scripts/rename-version.py --env preprod --role db-sync \
        --from-version 'cardano-db-sync LSM-13.7.1.0-node 11.0.1 preprod' \
        --to-version   'cardano-db-sync LSM-13.7.1.0-node-11.0.1 preprod'

    # Same, by direct path:
    python scripts/rename-version.py \
        --path data/cardano-db-sync/preprod.db \
        --from-version '...old...' --to-version '...new...'

    # Show what would change without writing:
    python scripts/rename-version.py --env preprod --role node \
        --from-version 'cardano-node 11.0.1' \
        --to-version   'cardano-node 11.0.1 preprod' \
        --dry-run

A timestamped backup (see backup-stats.py) is taken next to the source DB
before the UPDATE runs. Pass --no-backup to skip it (you already took one).

If the target label already has rows in the DB the script refuses, since
that would merge two distinct sample series into one. Pass --merge if that
is actually what you want.
"""

import argparse

# Import sibling module with a hyphen in its name. The type: ignore pair
# matches the same pattern in tests/test_backup_stats.py — mypy's stubs for
# importlib.util conservatively type spec_from_file_location as returning
# `ModuleSpec | None`, and .loader as `Loader | None`. At runtime both are
# non-None for a real file path, so narrow with ignores rather than asserts.
import importlib.util
import sqlite3
import sys
from pathlib import Path

_BACKUP_PATH = Path(__file__).resolve().with_name("backup-stats.py")
_spec = importlib.util.spec_from_file_location("backup_stats", _BACKUP_PATH)
_backup_stats = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_backup_stats)  # type: ignore[union-attr]
backup_db = _backup_stats.backup_db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROLE_DIR: dict[str, Path] = {
    "node": PROJECT_ROOT / "data" / "cardano-node",
    "db-sync": PROJECT_ROOT / "data" / "cardano-db-sync",
}

VERSION_TABLES: dict[str, tuple[str, ...]] = {
    "db-sync": (
        "memory_metrics", "cpu_metrics", "db_sync_version",
        "ingest_metrics", "table_rowcounts",
    ),
    "node": (
        "memory_metrics", "cpu_metrics", "node_version",
        "node_ingest_metrics",
    ),
}


def detect_role(db_path: Path) -> str:
    """Return 'node' or 'db-sync' based on which version-table the DB has.

    The two roles are mutually exclusive: db-sync DBs carry `db_sync_version`,
    node DBs carry `node_version`. Older / partial DBs may have neither, in
    which case we bail rather than guess.
    """
    with sqlite3.connect(str(db_path)) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    if "db_sync_version" in names:
        return "db-sync"
    if "node_version" in names:
        return "node"
    raise SystemExit(
        f"{db_path}: cannot detect role — neither db_sync_version nor "
        f"node_version table present."
    )


def count_for(conn: sqlite3.Connection, tables: tuple[str, ...], version: str) -> dict[str, int]:
    """Row count under `version` for each existing table.

    Missing tables are silently skipped (returns no key) — keeps the script
    forward-compatible if a future schema drops one of them.
    """
    out: dict[str, int] = {}
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for t in tables:
        if t not in existing:
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE version = ?", (version,)
        ).fetchone()[0]
        out[t] = n
    return out


def rename_in_db(
    db_path: Path,
    from_version: str,
    to_version: str,
    *,
    dry_run: bool,
    merge: bool,
) -> int:
    """Rename `from_version` -> `to_version` across every version-keyed table.

    Returns 0 on success, non-zero on failure. Single transaction: either
    every table is rewritten or none are.
    """
    role = detect_role(db_path)
    tables = VERSION_TABLES[role]

    with sqlite3.connect(str(db_path)) as conn:
        before_from = count_for(conn, tables, from_version)
        before_to = count_for(conn, tables, to_version)

    total_from = sum(before_from.values())
    total_to = sum(before_to.values())

    print(f"{db_path}  (role={role}, {len(tables)} version-keyed tables)")
    print(f"  from: {from_version!r}  ({total_from:,} rows across {sum(1 for n in before_from.values() if n):,}/{len(before_from)} tables)")
    for t, n in before_from.items():
        if n:
            print(f"    {t}: {n:,}")

    if total_from == 0:
        print("  No rows match the source version. Nothing to do.")
        return 0

    if total_to > 0:
        print(f"  WARNING: target label already has {total_to:,} rows in this DB:")
        for t, n in before_to.items():
            if n:
                print(f"    {t}: {n:,}")
        if not merge:
            print(
                "  Refusing to merge two distinct series under one label. "
                "Re-run with --merge if that is really what you want.",
                file=sys.stderr,
            )
            return 2

    if dry_run:
        print("  --dry-run: not writing.")
        return 0

    with sqlite3.connect(str(db_path)) as conn:
        try:
            conn.execute("BEGIN")
            changes: dict[str, int] = {}
            for t in tables:
                # Skip tables that don't exist (forward-compat).
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (t,),
                ).fetchone()
                if not exists:
                    continue
                cur = conn.execute(
                    f"UPDATE {t} SET version = ? WHERE version = ?",
                    (to_version, from_version),
                )
                changes[t] = cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    total = sum(changes.values())
    print(f"  renamed {total:,} rows across {sum(1 for n in changes.values() if n):,} tables:")
    for t, n in changes.items():
        if n:
            print(f"    {t}: {n:,}")
    return 0


def resolve_path(args: argparse.Namespace) -> Path:
    """Translate CLI args to the single DB path to operate on."""
    if args.path:
        return Path(args.path)
    role = args.role
    if role is None:
        # Try to infer from the version string prefix.
        if args.from_version.startswith("cardano-db-sync "):
            role = "db-sync"
        elif args.from_version.startswith("cardano-node "):
            role = "node"
        else:
            raise SystemExit(
                "Cannot infer --role from --from-version prefix. "
                "Pass --role explicitly or use --path."
            )
    return ROLE_DIR[role] / f"{args.env}.db"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rename a version label across every version-keyed table of a "
            "stats SQLite DB (db-sync or node). Wraps the manual UPDATE recipe "
            "from the README."
        )
    )
    parser.add_argument("--env", choices=["mainnet", "preprod", "preview"],
                        help="Environment name. Locates data/<role>/<env>.db.")
    parser.add_argument("--role", choices=["node", "db-sync"],
                        help="Which role's DB to rename in. Inferred from "
                             "the --from-version prefix if omitted.")
    parser.add_argument("--path",
                        help="Operate on a specific DB file (overrides --env/--role).")
    parser.add_argument("--from-version", required=True,
                        help="Existing version label to rename.")
    parser.add_argument("--to-version", required=True,
                        help="New version label to write.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing.")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip the timestamped pre-rename backup. "
                             "Default is to take one (use backup-stats.py's API).")
    parser.add_argument("--merge", action="store_true",
                        help="Allow the rename even if the target label already has rows. "
                             "Use only when intentionally merging two series.")
    args = parser.parse_args()

    if not args.path and not args.env:
        parser.error("specify --env (with optional --role) or --path")

    if args.from_version == args.to_version:
        parser.error("--from-version and --to-version are identical; nothing to do.")

    db_path = resolve_path(args)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    if not args.dry_run and not args.no_backup:
        backup = backup_db(db_path)
        size_mb = backup.stat().st_size / 1024**2
        print(f"Backed up {db_path}  ->  {backup} ({size_mb:.1f} MB)")

    return rename_in_db(
        db_path,
        args.from_version,
        args.to_version,
        dry_run=args.dry_run,
        merge=args.merge,
    )


if __name__ == "__main__":
    sys.exit(main())
