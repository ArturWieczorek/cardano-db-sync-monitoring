"""Functional tests for scripts/rename-version.py.

Doesn't shell out to the script — imports its functions directly and exercises
them against synthetic SQLite DBs in `tmp_path`. The real data/ directory is
never touched.

Coverage targets:
- detect_role for db-sync, node, and "neither" schemas
- count_for tolerates tables that don't exist (forward-compat shape)
- rename_in_db rewrites every version-keyed table for both roles
- dry-run writes nothing
- target-label collision is refused unless --merge
- a no-op source (zero rows) returns success without touching the DB
"""

import argparse
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_rename_module():
    """Import scripts/rename-version.py. The hyphen in the filename prevents
    plain `import rename-version`, so we use importlib.util."""
    spec = importlib.util.spec_from_file_location(
        "rename_version_module", SCRIPTS_DIR / "rename-version.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


rename_version = _load_rename_module()


def _make_db_sync_db(path: Path, rows_per_version: dict[str, int]) -> None:
    """Create a minimal db-sync stats DB containing all five version-keyed
    tables. Each table receives `n` rows for each (version, n) in the map."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE memory_metrics  (version TEXT, payload TEXT)")
        conn.execute("CREATE TABLE cpu_metrics     (version TEXT, payload TEXT)")
        conn.execute("CREATE TABLE db_sync_version (timestamp TEXT, version TEXT)")
        conn.execute("CREATE TABLE ingest_metrics  (version TEXT, payload TEXT)")
        conn.execute("CREATE TABLE table_rowcounts (version TEXT, payload TEXT)")
        for v, n in rows_per_version.items():
            for t in ("memory_metrics", "cpu_metrics",
                      "ingest_metrics", "table_rowcounts"):
                conn.executemany(
                    f"INSERT INTO {t} (version, payload) VALUES (?, ?)",
                    [(v, f"p{i}") for i in range(n)],
                )
            conn.executemany(
                "INSERT INTO db_sync_version (timestamp, version) VALUES (?, ?)",
                [(f"t{i}", v) for i in range(n)],
            )
        conn.commit()
    finally:
        conn.close()


def _make_node_db(path: Path, rows_per_version: dict[str, int]) -> None:
    """Create a minimal node stats DB with all four version-keyed tables."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE memory_metrics      (version TEXT, payload TEXT)")
        conn.execute("CREATE TABLE cpu_metrics         (version TEXT, payload TEXT)")
        conn.execute("CREATE TABLE node_version        (timestamp TEXT, version TEXT)")
        conn.execute("CREATE TABLE node_ingest_metrics (version TEXT, payload TEXT)")
        for v, n in rows_per_version.items():
            for t in ("memory_metrics", "cpu_metrics", "node_ingest_metrics"):
                conn.executemany(
                    f"INSERT INTO {t} (version, payload) VALUES (?, ?)",
                    [(v, f"p{i}") for i in range(n)],
                )
            conn.executemany(
                "INSERT INTO node_version (timestamp, version) VALUES (?, ?)",
                [(f"t{i}", v) for i in range(n)],
            )
        conn.commit()
    finally:
        conn.close()


def _total_rows_for_version(db: Path, tables: tuple[str, ...], version: str) -> int:
    """Sum rows for `version` across all `tables` (which must all exist)."""
    conn = sqlite3.connect(str(db))
    try:
        total = 0
        for t in tables:
            (n,) = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE version = ?", (version,)
            ).fetchone()
            total += n
        return total
    finally:
        conn.close()


class TestDetectRole:
    def test_db_sync_schema(self, tmp_path: Path) -> None:
        db = tmp_path / "dbsync.db"
        _make_db_sync_db(db, {"v1": 1})
        assert rename_version.detect_role(db) == "db-sync"

    def test_node_schema(self, tmp_path: Path) -> None:
        db = tmp_path / "node.db"
        _make_node_db(db, {"v1": 1})
        assert rename_version.detect_role(db) == "node"

    def test_neither_table_raises(self, tmp_path: Path) -> None:
        db = tmp_path / "unrelated.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE foo (x INTEGER)")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(SystemExit, match="cannot detect role"):
            rename_version.detect_role(db)


class TestCountFor:
    def test_counts_per_table(self, tmp_path: Path) -> None:
        db = tmp_path / "dbsync.db"
        _make_db_sync_db(db, {"v1": 3, "v2": 5})
        with sqlite3.connect(str(db)) as conn:
            counts = rename_version.count_for(
                conn, rename_version.VERSION_TABLES["db-sync"], "v1"
            )
        assert counts == {
            "memory_metrics": 3,
            "cpu_metrics": 3,
            "db_sync_version": 3,
            "ingest_metrics": 3,
            "table_rowcounts": 3,
        }

    def test_missing_tables_are_skipped(self, tmp_path: Path) -> None:
        """If a table named in VERSION_TABLES doesn't exist in the DB,
        count_for omits its key rather than blowing up. This guards against
        future schema removals."""
        db = tmp_path / "partial.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE memory_metrics (version TEXT)")
            conn.execute("CREATE TABLE db_sync_version (timestamp TEXT, version TEXT)")
            conn.execute("INSERT INTO memory_metrics VALUES ('v1')")
            conn.execute("INSERT INTO db_sync_version VALUES ('t', 'v1')")
            conn.commit()
        finally:
            conn.close()
        with sqlite3.connect(str(db)) as conn:
            counts = rename_version.count_for(
                conn, rename_version.VERSION_TABLES["db-sync"], "v1"
            )
        assert counts == {"memory_metrics": 1, "db_sync_version": 1}


class TestRenameInDb:
    def test_db_sync_renames_all_five_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "dbsync.db"
        _make_db_sync_db(db, {"old": 7})
        rc = rename_version.rename_in_db(
            db, "old", "new", dry_run=False, merge=False,
        )
        assert rc == 0
        tables = rename_version.VERSION_TABLES["db-sync"]
        assert _total_rows_for_version(db, tables, "old") == 0
        assert _total_rows_for_version(db, tables, "new") == 7 * len(tables)

    def test_node_renames_all_four_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "node.db"
        _make_node_db(db, {"old": 4})
        rc = rename_version.rename_in_db(
            db, "old", "new", dry_run=False, merge=False,
        )
        assert rc == 0
        tables = rename_version.VERSION_TABLES["node"]
        assert _total_rows_for_version(db, tables, "old") == 0
        assert _total_rows_for_version(db, tables, "new") == 4 * len(tables)

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        db = tmp_path / "dbsync.db"
        _make_db_sync_db(db, {"old": 2})
        tables = rename_version.VERSION_TABLES["db-sync"]
        before = _total_rows_for_version(db, tables, "old")
        rc = rename_version.rename_in_db(
            db, "old", "new", dry_run=True, merge=False,
        )
        assert rc == 0
        # Both labels are exactly as they were before the call.
        assert _total_rows_for_version(db, tables, "old") == before
        assert _total_rows_for_version(db, tables, "new") == 0

    def test_target_collision_is_refused(self, tmp_path: Path) -> None:
        db = tmp_path / "dbsync.db"
        _make_db_sync_db(db, {"old": 3, "new": 1})  # both labels present
        tables = rename_version.VERSION_TABLES["db-sync"]
        before_old = _total_rows_for_version(db, tables, "old")
        before_new = _total_rows_for_version(db, tables, "new")
        rc = rename_version.rename_in_db(
            db, "old", "new", dry_run=False, merge=False,
        )
        assert rc == 2
        # Nothing moved.
        assert _total_rows_for_version(db, tables, "old") == before_old
        assert _total_rows_for_version(db, tables, "new") == before_new

    def test_merge_collapses_two_labels(self, tmp_path: Path) -> None:
        db = tmp_path / "dbsync.db"
        _make_db_sync_db(db, {"old": 3, "new": 1})
        tables = rename_version.VERSION_TABLES["db-sync"]
        rc = rename_version.rename_in_db(
            db, "old", "new", dry_run=False, merge=True,
        )
        assert rc == 0
        assert _total_rows_for_version(db, tables, "old") == 0
        # 3 (old) + 1 (existing new) per table = 4 each.
        assert _total_rows_for_version(db, tables, "new") == 4 * len(tables)

    def test_no_op_source_zero_rows(self, tmp_path: Path) -> None:
        """A from-version with no matching rows is a clean no-op."""
        db = tmp_path / "dbsync.db"
        _make_db_sync_db(db, {"v1": 5})
        tables = rename_version.VERSION_TABLES["db-sync"]
        before_v1 = _total_rows_for_version(db, tables, "v1")
        rc = rename_version.rename_in_db(
            db, "ghost", "ghost-new", dry_run=False, merge=False,
        )
        assert rc == 0
        assert _total_rows_for_version(db, tables, "v1") == before_v1
        assert _total_rows_for_version(db, tables, "ghost") == 0
        assert _total_rows_for_version(db, tables, "ghost-new") == 0


class TestRenameInDbExtra:
    """Cases the basic happy-path tests don't exercise."""

    def test_skips_missing_table_during_update(self, tmp_path: Path) -> None:
        """UPDATE loop's forward-compat branch: a table named in
        VERSION_TABLES but absent from the DB must be silently skipped, not
        crash. Mirrors count_for's behavior; protects against future schema
        drops (`table_rowcounts` being dropped, say) breaking the rename.
        """
        db = tmp_path / "partial.db"
        conn = sqlite3.connect(str(db))
        try:
            # Build a db-sync schema MISSING `table_rowcounts`.
            conn.execute("CREATE TABLE memory_metrics  (version TEXT, payload TEXT)")
            conn.execute("CREATE TABLE cpu_metrics     (version TEXT, payload TEXT)")
            conn.execute("CREATE TABLE db_sync_version (timestamp TEXT, version TEXT)")
            conn.execute("CREATE TABLE ingest_metrics  (version TEXT, payload TEXT)")
            for t in ("memory_metrics", "cpu_metrics", "ingest_metrics"):
                conn.execute(f"INSERT INTO {t} VALUES ('old', 'p')")
            conn.execute("INSERT INTO db_sync_version VALUES ('t0', 'old')")
            conn.commit()
        finally:
            conn.close()

        rc = rename_version.rename_in_db(
            db, "old", "new", dry_run=False, merge=False,
        )
        assert rc == 0
        # The four tables that exist were updated; no error from the missing one.
        present = ("memory_metrics", "cpu_metrics",
                   "db_sync_version", "ingest_metrics")
        assert _total_rows_for_version(db, present, "old") == 0
        assert _total_rows_for_version(db, present, "new") == 4

    def test_idempotent_second_rename_is_noop(self, tmp_path: Path) -> None:
        """Running the same rename twice is safe — second pass finds no
        rows under the old label and returns 0 without touching anything."""
        db = tmp_path / "dbsync.db"
        _make_db_sync_db(db, {"old": 3})
        tables = rename_version.VERSION_TABLES["db-sync"]

        rc1 = rename_version.rename_in_db(
            db, "old", "new", dry_run=False, merge=False,
        )
        rows_after_first = _total_rows_for_version(db, tables, "new")

        rc2 = rename_version.rename_in_db(
            db, "old", "new", dry_run=False, merge=False,
        )
        rows_after_second = _total_rows_for_version(db, tables, "new")

        assert rc1 == 0 and rc2 == 0
        assert rows_after_first == rows_after_second  # second pass changed nothing
        assert _total_rows_for_version(db, tables, "old") == 0

    def test_special_chars_in_version_string(self, tmp_path: Path) -> None:
        """Parameterized queries must handle quotes, spaces, and slashes in
        version strings (which are arbitrary user text). A naive f-string
        substitution would break or open SQL injection here.
        """
        db = tmp_path / "dbsync.db"
        weird_old = "cardano-db-sync 13.6.0.5/test 'quoted' preprod"
        weird_new = "cardano-db-sync 13.6.0.5/test--renamed preprod"
        _make_db_sync_db(db, {weird_old: 2})
        tables = rename_version.VERSION_TABLES["db-sync"]

        rc = rename_version.rename_in_db(
            db, weird_old, weird_new, dry_run=False, merge=False,
        )
        assert rc == 0
        assert _total_rows_for_version(db, tables, weird_old) == 0
        assert _total_rows_for_version(db, tables, weird_new) == 2 * len(tables)


class TestResolvePath:
    """resolve_path covers --path, explicit --role, and inferred --role."""

    def _ns(self, **kwargs) -> argparse.Namespace:
        """Build a Namespace with sensible defaults for every CLI field."""
        base = dict(
            path=None, env=None, role=None,
            from_version="", to_version="",
            dry_run=False, no_backup=False, merge=False,
        )
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_explicit_path_overrides(self) -> None:
        ns = self._ns(path="/tmp/anywhere.db",
                      from_version="cardano-db-sync 1 preprod",
                      env="preprod")
        assert rename_version.resolve_path(ns) == Path("/tmp/anywhere.db")

    def test_db_sync_prefix_infers_role(self) -> None:
        ns = self._ns(env="preprod",
                      from_version="cardano-db-sync 13.6.0.5 preprod",
                      to_version="cardano-db-sync 13.6.0.5 preprod-renamed")
        assert rename_version.resolve_path(ns) == (
            rename_version.ROLE_DIR["db-sync"] / "preprod.db"
        )

    def test_node_prefix_infers_role(self) -> None:
        ns = self._ns(env="mainnet",
                      from_version="cardano-node 11.0.1 mainnet",
                      to_version="cardano-node 11.0.1 mainnet-renamed")
        assert rename_version.resolve_path(ns) == (
            rename_version.ROLE_DIR["node"] / "mainnet.db"
        )

    def test_explicit_role_overrides_prefix_inference(self) -> None:
        """--role wins over the version-string prefix when both are present."""
        ns = self._ns(env="preview", role="node",
                      from_version="cardano-db-sync ignored preview",
                      to_version="cardano-db-sync ignored preview-renamed")
        assert rename_version.resolve_path(ns) == (
            rename_version.ROLE_DIR["node"] / "preview.db"
        )

    def test_unknown_prefix_raises(self) -> None:
        ns = self._ns(env="preprod",
                      from_version="not-a-known-prefix",
                      to_version="not-a-known-prefix-renamed")
        with pytest.raises(SystemExit, match="Cannot infer --role"):
            rename_version.resolve_path(ns)


class TestMainCli:
    """End-to-end via monkeypatched sys.argv. Verifies that argparse wiring,
    backup integration, and exit codes work together — none of which is
    exercised by direct rename_in_db calls."""

    def _argv(self, monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["rename-version.py", *args])

    def test_default_takes_backup_and_renames(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without --dry-run and without --no-backup, main() leaves a
        timestamped backup next to the source AND renames the rows."""
        db = tmp_path / "test.db"
        _make_db_sync_db(db, {"old": 2})

        self._argv(monkeypatch,
                   "--path", str(db),
                   "--from-version", "old",
                   "--to-version", "new")
        rc = rename_version.main()
        assert rc == 0

        backups = list(tmp_path.glob("test.db.bak-*"))
        assert len(backups) == 1, f"expected one backup, got {backups}"

        tables = rename_version.VERSION_TABLES["db-sync"]
        assert _total_rows_for_version(db, tables, "old") == 0
        assert _total_rows_for_version(db, tables, "new") == 2 * len(tables)

    def test_dry_run_skips_both_backup_and_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--dry-run must not take a backup (it's a backup-of-a-no-op,
        wasteful) and must not write."""
        db = tmp_path / "test.db"
        _make_db_sync_db(db, {"old": 2})
        tables = rename_version.VERSION_TABLES["db-sync"]
        before = _total_rows_for_version(db, tables, "old")

        self._argv(monkeypatch,
                   "--path", str(db),
                   "--from-version", "old",
                   "--to-version", "new",
                   "--dry-run")
        rc = rename_version.main()
        assert rc == 0
        assert list(tmp_path.glob("test.db.bak-*")) == []
        assert _total_rows_for_version(db, tables, "old") == before

    def test_no_backup_flag_skips_backup_but_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--no-backup means the caller asserts they already have a backup;
        we should still apply the rename."""
        db = tmp_path / "test.db"
        _make_db_sync_db(db, {"old": 2})

        self._argv(monkeypatch,
                   "--path", str(db),
                   "--from-version", "old",
                   "--to-version", "new",
                   "--no-backup")
        rc = rename_version.main()
        assert rc == 0
        assert list(tmp_path.glob("test.db.bak-*")) == []
        tables = rename_version.VERSION_TABLES["db-sync"]
        assert _total_rows_for_version(db, tables, "new") == 2 * len(tables)

    def test_missing_db_returns_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pointing --path at a nonexistent file exits 1 with a stderr msg."""
        self._argv(monkeypatch,
                   "--path", str(tmp_path / "does-not-exist.db"),
                   "--from-version", "old",
                   "--to-version", "new")
        rc = rename_version.main()
        assert rc == 1
        assert "DB not found" in capsys.readouterr().err

    def test_identical_versions_argparse_error(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Identical --from-version and --to-version: argparse-style error
        (SystemExit 2, message on stderr) so the user notices the typo."""
        self._argv(monkeypatch,
                   "--path", "/tmp/anything.db",
                   "--from-version", "same",
                   "--to-version", "same")
        with pytest.raises(SystemExit) as ei:
            rename_version.main()
        assert ei.value.code == 2
        assert "identical" in capsys.readouterr().err

    def test_neither_path_nor_env_rejected(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """One of --env or --path is required; main() should bail."""
        self._argv(monkeypatch,
                   "--from-version", "a",
                   "--to-version", "b")
        with pytest.raises(SystemExit) as ei:
            rename_version.main()
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "--env" in err and "--path" in err
