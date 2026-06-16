"""Drift guard: every table that carries a `version` column must be registered
in `_common.VERSION_KEYED_TABLES`.

Why this exists: the version-keyed table list was historically duplicated and
hand-maintained, so two new collectors shipped with their tables unregistered -
`disk_metrics` (1.1.0) and `rts_metrics` (1.2.0). The fallout was an empty disk
plot, RTS data invisible to the plot picker, and `rename-version.py` silently
leaving those rows behind under the old label. This test scans the collectors'
source for `CREATE TABLE` statements that define a `version` column and fails if
any such table is missing from the registry - so the omission is caught in CI
the moment a new metric table is added, not weeks later in a broken plot.

See docs/postmortems/2026-06-06-empty-disk-and-invisible-rts.md.
"""

import re
from pathlib import Path

from _common import VERSION_KEYED_TABLES

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

# CREATE TABLE [IF NOT EXISTS] <name> ( <body> )
#   - <name> may be quoted; we skip names built from an f-string placeholder
#     ({version_table}), which are the *_version tables created dynamically and
#     always registered.
#   - <body> is captured non-greedily up to the closing paren; the project's
#     schemas are flat (no nested parens), so this is unambiguous.
_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?['\"]?(\w+)['\"]?\s*\((.*?)\)",
    re.IGNORECASE | re.DOTALL,
)


def _version_keyed_tables_in_source() -> dict[str, str]:
    """Map {table_name: defining_file} for every CREATE TABLE in scripts/ whose
    body declares a `version` column."""
    found: dict[str, str] = {}
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        text = path.read_text()
        for name, body in _CREATE_RE.findall(text):
            if "{" in name:  # dynamically-named (*_version) - skip
                continue
            if re.search(r"\bversion\b", body, re.IGNORECASE):
                found.setdefault(name, path.name)
    return found


def test_every_version_keyed_table_is_registered():
    registered = {t for tables in VERSION_KEYED_TABLES.values() for t in tables}
    in_source = _version_keyed_tables_in_source()
    unregistered = {name: file for name, file in in_source.items() if name not in registered}
    assert not unregistered, (
        "These tables declare a `version` column but are missing from "
        "_common.VERSION_KEYED_TABLES (rename-version.py and the plot pickers "
        f"will silently skip them): {unregistered}. Add them to the registry."
    )


def test_known_collector_tables_are_detected_by_the_scan():
    # Sanity check on the scanner itself: if these stop being detected, the
    # regex has drifted and the guard above would silently pass on everything.
    in_source = _version_keyed_tables_in_source()
    for expected in ("memory_metrics", "cpu_metrics", "disk_metrics", "rts_metrics"):
        assert expected in in_source, f"scanner failed to find {expected} - the CREATE TABLE regex may be stale"


def test_registry_matches_rename_version_consumer():
    # rename-version.py must use the shared registry, not its own copy.
    import importlib.util

    spec = importlib.util.spec_from_file_location("rename_version", SCRIPTS_DIR / "rename-version.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert mod.VERSION_TABLES is VERSION_KEYED_TABLES
