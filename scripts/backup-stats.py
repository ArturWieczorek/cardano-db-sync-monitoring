#!/usr/bin/env python3
"""Create timestamped backups of the SQLite stats databases.

Wraps the manual `cp data/.../foo.db foo.db.bak-<ts>` workflow you'd otherwise
do before destructive operations (deleting all rows for a version, manual
schema changes, etc.).

Why this exists rather than just `cp`: SQLite is configured in WAL mode, so a
naive filesystem copy of `foo.db` misses any pending data in `foo.db-wal` that
hasn't been checkpointed yet. `sqlite3 .backup` (and Python's
`Connection.backup()` equivalent) drains the WAL into a fresh single-file
snapshot — safe to run while the monitor is writing.

Usage examples:

    # Backup both DBs for an env (node + db-sync, whichever exist):
    python scripts/backup-stats.py --env preprod

    # Backup just one role:
    python scripts/backup-stats.py --env preprod --role node
    python scripts/backup-stats.py --env preprod --role db-sync

    # Backup an arbitrary path:
    python scripts/backup-stats.py --path /tmp/custom.db

    # List existing backups (oldest → newest):
    python scripts/backup-stats.py --env preprod --list

Backup naming: `<original>.bak-YYYYMMDD_HHMMSS`. Backups live next to the
source file, which means they're under `data/` and therefore git-ignored by
default.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROLE_DIR: dict[str, Path] = {
    "node": PROJECT_ROOT / "data" / "cardano-node",
    "db-sync": PROJECT_ROOT / "data" / "cardano-db-sync",
}


def backup_db(src_path: Path) -> Path:
    """Create a timestamped WAL-aware backup of `src_path`.

    Uses sqlite3.Connection.backup(), which internally calls the SQLite
    online-backup API. Drains the WAL into the destination so the result is a
    single consistent .db file with no .wal/.shm sidecars.

    Returns the path of the new backup. Raises FileNotFoundError if the source
    doesn't exist.
    """
    if not src_path.exists():
        raise FileNotFoundError(f"Source DB not found: {src_path}")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_path = src_path.with_name(f"{src_path.name}.bak-{ts}")
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dst_path


def list_backups(db_path: Path) -> list[Path]:
    """Existing .bak-* files for `db_path`, oldest first (lexical = timestamp order)."""
    return sorted(db_path.parent.glob(f"{db_path.name}.bak-*"))


def resolve_paths(args: argparse.Namespace) -> list[Path]:
    """Translate CLI args to the list of source DB paths to operate on."""
    if args.path:
        return [Path(args.path)]
    roles = [args.role] if args.role else list(ROLE_DIR.keys())
    paths = []
    for role in roles:
        db = ROLE_DIR[role] / f"{args.env}.db"
        if db.exists():
            paths.append(db)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backup the SQLite stats databases produced by the monitors."
    )
    parser.add_argument("--env", choices=["mainnet", "preprod", "preview"],
                        help="Environment name. Locates data/<role>/<env>.db.")
    parser.add_argument("--role", choices=["node", "db-sync"],
                        help="Which role's DB to back up. Default: both (when --env is given).")
    parser.add_argument("--path",
                        help="Backup a specific DB file (overrides --env/--role).")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="List existing backups for the selected DB(s) and exit.")
    args = parser.parse_args()

    if not args.path and not args.env:
        parser.error("specify --env (with optional --role) or --path")

    paths = resolve_paths(args)
    if not paths:
        if args.env:
            print(f"No stats DBs found for env={args.env}.", file=sys.stderr)
        else:
            print(f"Path not found: {args.path}", file=sys.stderr)
        return 1

    if args.list_only:
        for db in paths:
            backups = list_backups(db)
            if not backups:
                print(f"{db}: no backups")
                continue
            print(f"{db}:")
            for b in backups:
                size_mib = b.stat().st_size / 1024**2
                print(f"  {b.name}  ({size_mib:.1f} MiB)")
        return 0

    rc = 0
    for db in paths:
        try:
            backup = backup_db(db)
            size_mib = backup.stat().st_size / 1024**2
            print(f"Backed up {db}  ->  {backup} ({size_mib:.1f} MiB)")
        except FileNotFoundError as e:  # noqa: PERF203
            # Per-DB try/except is intentional: one missing DB shouldn't abort
            # backing up the others. The loop is at most 2 iterations.
            print(str(e), file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
