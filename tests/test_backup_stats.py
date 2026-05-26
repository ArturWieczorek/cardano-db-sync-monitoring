"""Functional tests for scripts/backup-stats.py.

Doesn't shell out to the script — imports its functions directly so we exercise
the sqlite3 backup API path. Uses pytest's tmp_path fixture so we never touch
the real data/ directory.
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_backup_module():
    """Import scripts/backup-stats.py. The hyphen in the filename prevents
    plain `import backup-stats`, so we use importlib.util."""
    spec = importlib.util.spec_from_file_location(
        "backup_stats_module", SCRIPTS_DIR / "backup-stats.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


backup_stats = _load_backup_module()


def _make_test_db(path: Path) -> None:
    """Create a small SQLite DB with one table + a few rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, value REAL)")
        conn.executemany(
            "INSERT INTO samples (id, value) VALUES (?, ?)",
            [(i, i * 1.5) for i in range(10)],
        )
        conn.commit()
    finally:
        conn.close()


class TestBackupDb:
    def test_creates_timestamped_backup(self, tmp_path: Path) -> None:
        src = tmp_path / "test.db"
        _make_test_db(src)
        backup = backup_stats.backup_db(src)
        assert backup.exists()
        assert backup.name.startswith("test.db.bak-")
        assert backup.parent == src.parent  # backup lives next to source

    def test_backup_is_a_valid_sqlite_db(self, tmp_path: Path) -> None:
        src = tmp_path / "test.db"
        _make_test_db(src)
        backup = backup_stats.backup_db(src)
        conn = sqlite3.connect(str(backup))
        try:
            rows = conn.execute("SELECT id, value FROM samples ORDER BY id").fetchall()
        finally:
            conn.close()
        assert rows == [(i, i * 1.5) for i in range(10)]

    def test_backup_unaffected_by_subsequent_source_writes(self, tmp_path: Path) -> None:
        """The backup is a point-in-time snapshot — modifying the source after
        backup must not change the backup's content."""
        src = tmp_path / "test.db"
        _make_test_db(src)
        backup = backup_stats.backup_db(src)
        # Now write more rows to the source.
        conn = sqlite3.connect(str(src))
        try:
            conn.execute("INSERT INTO samples (id, value) VALUES (99, 99.9)")
            conn.commit()
        finally:
            conn.close()
        # Backup should still have only the original 10 rows.
        conn = sqlite3.connect(str(backup))
        try:
            (n,) = conn.execute("SELECT COUNT(*) FROM samples").fetchone()
        finally:
            conn.close()
        assert n == 10

    def test_missing_source_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            backup_stats.backup_db(tmp_path / "does-not-exist.db")


class TestListBackups:
    def test_no_backups_returns_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        _make_test_db(db)
        assert backup_stats.list_backups(db) == []

    def test_returns_only_matching_backups(self, tmp_path: Path) -> None:
        db = tmp_path / "target.db"
        _make_test_db(db)
        # Two backups of target.db
        b1 = backup_stats.backup_db(db)
        b2 = backup_stats.backup_db(db)
        # A backup-shaped file for a DIFFERENT base name — should not be listed.
        other = tmp_path / "other.db.bak-20990101_000000"
        other.write_text("")
        results = backup_stats.list_backups(db)
        assert b1 in results
        assert b2 in results
        assert other not in results

    def test_results_are_lex_sorted(self, tmp_path: Path) -> None:
        db = tmp_path / "lex.db"
        _make_test_db(db)
        # Create backups with hand-rolled names to control sort order.
        names = [
            "lex.db.bak-20260526_120000",
            "lex.db.bak-20260525_120000",  # older
            "lex.db.bak-20260527_120000",  # newer
        ]
        for n in names:
            (tmp_path / n).write_text("")
        results = backup_stats.list_backups(db)
        assert [r.name for r in results] == sorted(names)
