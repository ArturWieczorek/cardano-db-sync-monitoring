# AI Agent Development Guide

This file is the entry point for AI agents (Claude Code, Cursor, Codex, etc.)
working in this repository. It captures the rules and conventions a human
maintainer would otherwise have to repeat in every session. Read it before
making changes; keep it up to date as the codebase evolves.

The format is loosely modeled on
[IntersectMBO/cardano-node-tests `AGENTS.md`](https://github.com/IntersectMBO/cardano-node-tests/blob/master/AGENTS.md),
adapted to this repo's reality: it's a small Python tool, not a node-test suite.


## Repository purpose

`db-sync-monitoring` is a stand-alone Python tool for **tracking and
visualizing resource and ingest metrics** of `cardano-db-sync` and
`cardano-node` runs. It exists to let an engineer A/B-compare two builds
(e.g. LSM-backed vs in-memory ledger state, or two db-sync versions) on the
same preprod/preview/mainnet chain.

A typical workflow:

1. Run `cardano-node` + `cardano-db-sync` for version A against its own
   Postgres DB.
2. Attach `scripts/db-sync-monitor.py` (or `node-monitor.py`) — it samples
   `psutil` for CPU/RSS, polls Postgres for ingest/table-row metrics, and
   writes everything to a local SQLite stats DB.
3. Repeat for version B.
4. Run `db-sync-plot.py` / `node-plot.py` to generate side-by-side HTML
   plots, and `db-sync-report.py` for the Postgres-side text reports.

The tool is not a service. It's a collection of CLI scripts the engineer runs
by hand from `scripts/`. There is no daemon, no orchestrator, no shared state
beyond the SQLite files under `data/`.


## Code architecture

```
db-sync-monitoring/
├── scripts/                           # CLI entry points + shared helpers
│   ├── _common.py                     # Shared utilities: schema init, version
│   │                                  #   resolution, plot helpers, time/era
│   │                                  #   converters. Imported by every script.
│   ├── _db_sync_queries.py            # Postgres SQL used by db-sync-monitor
│   │                                  #   and db-sync-report (kept in one place
│   │                                  #   so query changes don't drift).
│   ├── db-sync-monitor.py             # Samples a running cardano-db-sync
│   │                                  #   (psutil + Postgres) into
│   │                                  #   data/cardano-db-sync/<env>.db.
│   ├── node-monitor.py                # Same idea for cardano-node, writing to
│   │                                  #   data/cardano-node/<env>.db. No
│   │                                  #   Postgres — uses cardano-cli for tip.
│   ├── db-sync-plot.py                # Reads the db-sync SQLite DB and emits
│   │                                  #   HTML plots (cpu_ram / ingest / tables).
│   ├── node-plot.py                   # Same for node DB (cpu_ram / ingest).
│   ├── db-sync-report.py              # Postgres-side text report: per-epoch
│   │                                  #   stats, headline deltas, table-size
│   │                                  #   breakdown. Compares 1 or 2 PG DBs.
│   ├── backup-stats.py                # WAL-aware backup of the SQLite stats
│   │                                  #   DBs. Run before destructive ops.
│   └── rename-version.py              # Rewrite a version label across every
│                                      #   version-keyed table in one transaction.
│
├── tests/                             # pytest suite (functional, not unit-
│   │                                  #   pure — uses tmp_path SQLite DBs).
│   ├── conftest.py                    # Shared fixtures.
│   ├── test_backup_stats.py           # backup_db API + list semantics.
│   ├── test_rename_version.py         # role detection, rename, CLI, backup.
│   ├── test_resolve_versions.py       # _common.resolve_versions input mapping.
│   ├── test_gap_breaks.py             # insert_gap_breaks() for plot continuity.
│   ├── test_era.py                    # protocol-major → era mapping.
│   ├── test_node_durations.py         # per-epoch wall-clock duration helper.
│   ├── test_formatters.py             # number/duration formatting helpers.
│   ├── test_time.py                   # UTC timestamp conversion helpers.
│   └── test_scripts_smoke.py          # subprocess `--help` for every entry
│                                      #   point — catches import/argparse
│                                      #   breakage no other test would.
│
├── data/                              # SQLite stats DBs (git-ignored).
│   ├── cardano-db-sync/<env>.db       #   written by db-sync-monitor.py
│   └── cardano-node/<env>.db          #   written by node-monitor.py
│
├── plots/                             # HTML plot output (git-ignored).
├── stats/                             # Text reports from db-sync-report (git-ignored).
├── archive/                           # Old snapshots / one-off artifacts.
├── docs/                              # Domain primers (Cardano, time series,
│                                      #   stats, sqlite internals, glossary).
├── img/                               # Screenshots embedded in README.
│
├── README.md                          # End-user docs. Quickstart at top, per-
│                                      #   script reference, then troubleshooting.
├── CHANGELOG.md                       # Keep-a-Changelog format. All user-
│                                      #   facing changes land under [Unreleased].
├── AGENTS.md                          # This file.
├── pyproject.toml                     # Project metadata, deps, ruff/mypy/pytest
│                                      #   config. requires-python = ">=3.10".
├── requirements.txt                   # Runtime deps (pandas, plotly, psutil,
│                                      #   psycopg2-binary). Generated from
│                                      #   pyproject.toml's [project.dependencies].
├── requirements-dev.txt               # Dev deps (pytest, ruff, mypy, pre-commit).
├── Makefile                           # `make venv` / `make install` / `make shell`.
├── uv.lock                            # Resolved dep tree (uv).
└── .pre-commit-config.yaml            # ruff + ruff-format + mypy on commit.
```

### A few load-bearing conventions

- **Script naming**: `<role>-<verb>.py`. `<role>` is one of `db-sync`, `node`,
  or empty (for cross-role tools like `backup-stats`, `rename-version`).
  Hyphens, not underscores — these are CLI invocations, not importable modules.
  Files imported as modules (`_common`, `_db_sync_queries`) start with `_` and
  use underscores. Don't mix the two styles for one file.
- **SQLite schema**: db-sync DBs have 5 version-keyed tables (`memory_metrics`,
  `cpu_metrics`, `db_sync_version`, `ingest_metrics`, `table_rowcounts`); node
  DBs have 4 (`memory_metrics`, `cpu_metrics`, `node_version`,
  `node_ingest_metrics`). **If you add a new version-keyed table to either
  schema, you must also update `rename-version.py`'s `VERSION_TABLES`, its
  README recipe, and its test fixtures — or rename will silently leave stale
  rows behind.** This exact gap caused a real bug; the test suite has cases
  to catch it now (`test_rename_version.py::TestRenameInDb`).
- **Version labels**: stored as a single string like
  `cardano-db-sync 13.7.1.0-node-11.0.1 preprod` (three space-separated parts:
  product, version, env). The plot script joins by exact-string match across
  tables, so labels must be consistent across all version-keyed tables of one
  DB. Use `scripts/rename-version.py` for renames, not raw SQL.
- **WAL mode**: every SQLite stats DB runs in WAL. Naive `cp` of a `.db` file
  misses pending writes in `.db-wal`. Use `scripts/backup-stats.py` (or the
  `backup_db()` API it exposes) before any destructive operation.


## Making code changes

### Style

- **Python ≥ 3.10**, type hints on every function signature.
- **Ruff** is the only linter/formatter. Config lives in `pyproject.toml`
  (`[tool.ruff]`). Selected rule families: `E`, `F`, `I`, `UP`, `NPY`, `PERF`,
  `RUF`. Line length 120. `E501` is intentionally disabled — long log/format
  strings are common and wrapping them hurts readability.
- **Mypy** runs in non-strict mode (`pyproject.toml` `[tool.mypy]`). Pandas/
  plotly are dynamic enough that strict mode buries real findings in cosmetic
  noise. Don't enable `strict` casually; if you find a real issue mypy missed,
  add a targeted check rather than a global flag.
- **Docstrings**: required on every public function. Lead with one sentence
  describing what the function does; follow with paragraphs only when the
  *why* is non-obvious (a hidden constraint, a subtle invariant, a workaround
  for a known issue). Don't restate the type signature in prose.
- **Comments**: write them only when removing the comment would confuse a
  future reader. Don't narrate what the code does — well-named identifiers
  already do that. Don't reference the task or PR that motivated the change.

### Running ruff and mypy

The pre-commit hook runs both automatically, but to check manually:

```bash
.venv/bin/ruff check scripts tests
.venv/bin/ruff format --check scripts tests
.venv/bin/mypy
```

If you don't have the venv yet: `make install` (creates `.venv` and installs
runtime deps via `uv`), then `uv pip install -e ".[dev]"` to add the dev
toolchain.

### "Trust but verify" rules

- **Never edit `data/`, `plots/`, `stats/`, or `archive/`** by hand. These
  hold real run outputs and backups; an agent treating them as transient
  scratch space has destroyed work in the past. They're git-ignored on
  purpose. If you need to test against a real DB, copy it to `tmp_path`
  (pytest) or `/tmp/` first.
- **Use `scripts/backup-stats.py` before destructive SQL.** `cp` of a WAL-
  mode DB is silently wrong. The same applies to manual schema edits.
- **Don't run the monitor scripts (`db-sync-monitor.py`, `node-monitor.py`)
  unprompted** to "verify the change works." They sample real `psutil`, talk
  to a real Postgres, and write into `data/`. If you must verify, run with
  `--help` (covered by `test_scripts_smoke.py`) or with a small `--interval`
  against a disposable DB the user explicitly set up.


## Tests

### Rules

1. **Every new script or substantial new behavior must come with tests in the
   same change.** "Substantial" = anything an agent or user could later
   regress without noticing. Trivial doc fixes and rename-only refactors are
   exempt; everything else is not.
2. **Tests must hit real code paths.** No mocks of the function under test;
   no asserts that just round-trip the input. If you find yourself writing
   `mock.return_value = expected; assert func() == expected`, delete the
   test — it's tautological. SQLite is fast enough to use real `tmp_path`
   databases for everything in this repo.
3. **After any code change, run the full test suite and confirm every test
   passes before reporting the work as done.** Not just the new tests —
   the whole suite. A "my new test passes" report while three unrelated
   tests fail is a bug report dressed up as a completion notice.
4. **`test_scripts_smoke.py` must list every new CLI entry point.** It runs
   `--help` and a no-args invocation against each script via subprocess —
   the cheapest catch for import-time breakage (e.g. someone deletes a
   helper from `_common.py` without updating callers).
5. **Aim for meaningful coverage, not 100 %.** The rollback `except` block
   and `if __name__ == "__main__"` guard in `rename-version.py` are
   intentionally uncovered; covering them needs invasive monkey-patching
   for little real-world benefit. Use `coverage run` + `coverage report -m`
   to see which lines are missed, then make a judgement call.

### Running the suite

```bash
.venv/bin/python -m pytest                       # full suite, ~5 s
.venv/bin/python -m pytest tests/test_<x>.py -v  # one file
.venv/bin/python -m pytest -k <substring>        # by test name pattern
```

With coverage (the `coverage` package is dev-only; install with
`uv pip install coverage` if missing):

```bash
.venv/bin/python -m coverage run --source=scripts -m pytest
.venv/bin/python -m coverage report -m --include='scripts/<x>.py'
```

`pyproject.toml`'s `[tool.pytest.ini_options]` promotes `FutureWarning` to a
test failure. If a test starts failing on a warning from a dependency upgrade,
fix the underlying call site rather than silencing the warning broadly —
silencing belongs at the specific call site that triggers it.

### Writing tests — patterns to follow

- **One test file per script.** `tests/test_<script>.py` mirrors
  `scripts/<script>.py`. Cross-cutting helpers (`_common.py`,
  `_db_sync_queries.py`) get their own test file per helper area
  (`test_resolve_versions.py`, `test_gap_breaks.py`, etc.) rather than one
  monolithic `test_common.py`.
- **`importlib`-load hyphenated scripts.** `scripts/foo-bar.py` can't be
  `import foo-bar`; `tests/test_backup_stats.py` shows the pattern (Use
  `importlib.util.spec_from_file_location`).
- **Group tests in classes** (`TestBackupDb`, `TestRenameInDb`, etc.) so
  pytest's `-k` lookups stay readable. One class per logical surface; one
  test method per behavior.
- **Use `tmp_path`** for any test that touches the filesystem. Never write
  into the repo's `data/` or `plots/`.
- **Use `monkeypatch.setattr(sys, "argv", [...])`** to drive `main()`
  end-to-end. `capsys` captures stdout/stderr for the assertions.


## Documentation

Documentation lives in three places and they all need to stay in sync with
the code:

### README.md

User-facing reference. When you change behavior a user would observe — new
flag, renamed flag, new script, changed default, new failure mode — **update
the README in the same change**. The README has a "Troubleshooting" section
near the bottom for known error messages and what they mean; new user-
reachable errors belong there.

Don't add sections to the README for an internal refactor that has no user-
visible effect; the README is not a changelog.

### CHANGELOG.md

[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. **Every
user-facing change gets a `[Unreleased]` entry in the appropriate section
(`Added` / `Changed` / `Fixed` / `Removed`) in the same commit as the code
change.** Entry style in this repo:

- Lead with a bolded short identifier (script name, flag name, feature) so
  the reader can scan; follow with the substance.
- Say *why* the change exists, not just *what* changed. When the change
  fixes a specific past failure mode, name it ("previously the rename
  recipe missed `ingest_metrics`, leaving `_ingest.html` empty for the
  renamed version"). The motivation is the load-bearing part of the entry.
- Mention companion changes (tests added, README updated) in the same bullet
  or as sibling bullets — they document the full scope.

### Inline (`docs/`)

The `docs/` subdir holds domain primers (Cardano eras, time series, SQLite
WAL, etc.) for engineers new to the area. Add to it only when there's a
genuinely reusable concept; don't move release notes there.


## Commits

- One logical change per commit. A new script + its tests + its README +
  CHANGELOG entries belong in the same commit because they're meaningless
  apart from each other; an unrelated linter fix does not.
- Subject line: imperative mood, under 70 characters
  (`Add scripts/rename-version.py for atomic version renames`).
- Body (if needed): wrap at 72 chars, explain *why* not *what*. The diff
  shows what.
- Don't bypass hooks (`--no-verify`). If ruff/mypy/the pre-commit suite
  fails, fix the failure — don't suppress it. The hook config is in
  `.pre-commit-config.yaml` and pins versions to roughly match
  `pyproject.toml` / `uv.lock`; bump them together.


## Checklist for "is my change done"

Before reporting any non-trivial change complete:

- [ ] Code change builds + imports cleanly (`test_scripts_smoke.py` covers
      this for entry-point scripts).
- [ ] Tests added for new behavior; existing tests still real (no mocks of
      the function under test).
- [ ] `.venv/bin/python -m pytest` — full suite passes.
- [ ] `.venv/bin/ruff check scripts tests` clean.
- [ ] `.venv/bin/mypy` clean (or any new finding is intentional + justified
      in the commit message).
- [ ] README.md updated if the change affects user-observable behavior.
- [ ] CHANGELOG.md `[Unreleased]` entry added with the *why*, not just the
      *what*.
- [ ] If you touched the SQLite schema, you also touched `rename-version.py`
      and its README/test fixtures (see "load-bearing conventions" above).
