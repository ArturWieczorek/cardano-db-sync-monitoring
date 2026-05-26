# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`scripts/backup-stats.py`** — wraps Python's `sqlite3.Connection.backup()`
  (the WAL-aware backup API) so you don't need to remember the right
  invocation before destructive operations. Supports `--env [--role]` for the
  conventional `data/<role>/<env>.db` layout, `--path` for arbitrary DBs, and
  `--list` to inventory existing backups. Naming: `<original>.bak-YYYYMMDD_HHMMSS`,
  dropped next to the source so it's already covered by `.gitignore`.
- **`tests/test_backup_stats.py`** — functional tests using `tmp_path` covering
  the backup API path, list semantics, immutability of the backup against
  subsequent source writes, and FileNotFoundError on missing sources.
- `backup-stats.py` added to the smoke-test parametrize list.

### Changed

- **db-sync-monitor connection reuse.** The sample loop's four steady-state
  query methods (`get_tip`, `get_first_block_time`, `get_ingest_metrics`,
  `get_table_rowcounts`) now share a single autocommit psycopg2 connection
  opened lazily via `_ensure_loop_conn()`. Previously each opened its own
  short-lived connection per sample — at `--interval=10s` that was ~24
  connection cycles per minute (~35K/day) per env. Now it's one persistent
  connection. Connection-level errors (`OperationalError`,
  `InterfaceError`) drop the loop conn so the next sample reopens —
  self-healing across postgres restarts / network blips.
- Setup-phase queries (`wait_for_schema`, `detect_utxo_tracking`) keep their
  own short-lived connections via `_pg()`. `wait_for_schema` runs before the
  loop and may be polled while postgres itself is starting; `detect_utxo_tracking`
  uses `SET LOCAL statement_timeout` which requires an explicit transaction
  and is incompatible with the loop conn's autocommit mode.
- Internal: `_pg` was previously the only connection helper. It's now a thin
  wrapper around the new private `_connect()` (returns a raw connection),
  shared with `_ensure_loop_conn()`. No behaviour change at the call sites
  that still use `_pg`.

### Plot layout improvements

- **node-plot ingest plot**: layout fixes for the era bar + per-epoch chart.
  Subplot title caveat moved out of the era subplot's title (where it caused
  plotly to render a multi-line title and crowd the bar chart) into the main
  figure title as a gray sub-line. Subplot titles back to single-line.
  Spacing tuned: `vertical_spacing=0.18`, `row_heights=[0.45, 0.55]`,
  `height=800`, explicit `margin=dict(t=150, b=60, l=80, r=40)`,
  `title.pad.b=30`. `<code>` tags replaced with `<b>` (plotly's title
  HTML doesn't support `<code>`).

## [1.0.0] — 2026-05-26

First formal release. The project started as a basic resource collector + a few
report queries; this release marks the point where it became a complete A/B
testing toolkit for cardano-node and cardano-db-sync, with a tested core,
documented internals, and CI.

### Added

#### Monitors (`scripts/db-sync-monitor.py`, `scripts/node-monitor.py`)

- **Append-only time-series collection** to SQLite. Each `--env` writes to one
  `data/<role>/<env>.db`; multiple `--db-sync-ver` / `--node-ver` labels coexist
  in the same DB so versions can be compared later.
- **`--json` output mode** emits one JSON object per sample on stdout in
  addition to (or instead of) the human-readable pipe-separated form. Each line
  includes env, label, version, slot_no, epoch_no, era, sync_percent, tip_lag,
  db_size, cpu_percent, rss_mb. Suitable for piping into log analyzers.
- **`--interval` flag** controls sampling cadence (default 10s).
- **Postgres env var support**: `--pg-host` / `--pg-port` / `--pg-user` default
  to `None` and fall back to `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`, matching
  the `psql` convention.
- **Startup history notice**: monitor reports at start whether the chosen
  version label already has samples in the DB ("3,606 samples, last
  2026-05-26T11:31:38") so multi-session runs are visible.
- **`SIGINT`/`SIGTERM` handlers** with clean shutdown reporting the row count
  written. Suitable for `systemd`, `nohup`, `tmux`.
- **Line-buffered stdout** so `tee` / journald / file redirects show samples
  immediately.
- **`wait_for_schema`** in db-sync-monitor polls every 2s until the `block`
  table exists, eliminating "relation does not exist" noise during the first
  few iterations of a fresh sync.
- **UTXO tracking auto-detection**: monitor probes `tx_out.consumed_by_tx_id`
  with a 5-second `statement_timeout` safety net. Only samples `utxo_count`
  when populated. Re-probes each sample until definitive.
- **`node_ingest_metrics` table** on the node side: per-sample `slot_no`,
  `epoch_no`, `era`, `sync_progress`. Enables per-epoch and per-era sync
  duration plots on the node side (previously db-sync-only).
- **Hot-table row count sampling** in db-sync-monitor: cheap `pg_class.reltuples`
  estimates for `block`, `tx`, `tx_out`, `ma_tx_out`, `ma_tx_mint`, `multi_asset`,
  `datum`, `redeemer`, `script` every interval. Plot via `--metrics tables`.
- **Ingest metrics**: tip lag, DB size, max block_no, max tx_id, UTXO count
  (when enabled) — all sampled every interval and plottable.

#### Plotters (`scripts/db-sync-plot.py`, `scripts/node-plot.py`)

- **`--versions a,b` flag**: non-interactive multi-version selection. Accepts
  full labels or short tokens. Ambiguous matches raise; missing labels error
  out instead of silently dropping.
- **`--list` flag**: list available versions in the DB and exit. Scriptable.
- **`--x-axis {slot, time}` flag**: plot against `slot_no` (default — best for
  chain-content comparison) or wall-clock `ts` (best for diagnosing stalls
  and at-tip behavior).
- **`--metrics` flag** on db-sync-plot: `cpu_ram` (default), `ingest`, `tables`,
  or `all` — selects which time-series to plot.
- **`--metrics` flag** on node-plot: `cpu_ram` (default), `ingest`, or `all`.
  Ingest mode produces sync time by era (bar chart) and sync duration per
  epoch (line chart).
- **Gap-aware plotting**: inserts NaN-marker rows between consecutive samples
  more than 5× the sample interval apart. Plotly breaks the line cleanly
  across multi-session gaps instead of drawing misleading near-vertical cliffs.
- **A/B comparison plots**: multiple `--versions` overlay all panels with one
  trace per version; era bars become grouped bars; legend is enabled.
- **Missing-data warning**: when a requested version has no rows in the chosen
  metric's table (e.g., older runs that pre-date `ingest_metrics`), the plot
  warns explicitly and drops the missing version cleanly instead of rendering
  an empty trace.

#### Report (`scripts/db-sync-report.py`)

- **A/B comparison mode**: `--pg-dbname dbA,dbB` produces a headline-deltas
  summary (Total sync time, Final DB size, etc. with Δ column) plus per-version
  detail sections, all in one combined `dbA_vs_dbB_*` text file.
- **`--with-p95`** opt-in: per-epoch p95 tx size via `PERCENTILE_CONT`.
  Expensive on mainnet (5-20 min) so default-off.
- **`--skip-slow`** opt-out: skips the per-epoch Plutus adoption and
  cumulative-distinct-assets queries when faster turnaround is needed.
- **Stage-by-stage progress logging**: `[3/8] Rendering per-epoch HTML…` so a
  hang is locatable. Total elapsed time printed on completion.
- **Per-epoch chart panels** now cover: block count, total fees, total output,
  avg + p95 tx size, sum tx size, plutus ratio, MA mint count, cumulative
  distinct multi-assets, reward count, stake count, Conway voting & drep
  registrations (guarded with `to_regclass`).
- **Sync time by era**: top bar chart in the per-epoch HTML showing total
  seconds spent in each era (Byron / Shelley / Allegra / Mary / Alonzo /
  Babbage / Conway), derived from `epoch_param.protocol_major`.
- **Summary report text file**: total sync time, slot/epoch range, mean
  slots/sec, sync-time-per-era table, chain activity totals, all tables and
  all indexes (no top-N truncation), UTXO set size if tracked.
- **Size report text file**: per-table data + index breakdown, all indexes
  sorted largest first.

#### Architecture

- **`scripts/_common.py`**: shared helpers (formatters, utc_timestamp,
  ERA_BY_PROTOCOL_MAJOR + era_for + era_sort_key, find_process/find_processes,
  get_memory_details/get_cpu_details, has_table/has_column,
  init_sqlite_schema, report_existing_history, short,
  load_versions_from_sqlite, resolve_versions, insert_gap_breaks,
  compute_epoch_durations, warn, step). Imported by all 5 scripts and tests.
- **`scripts/_db_sync_queries.py`**: postgres data layer extracted from
  db-sync-report.py — pg_connect, table_exists, utxo_tracking_enabled,
  query_df, all `fetch_*` per-epoch and per-era fetchers, `assemble_epoch_df`,
  size / index queries, build_summary.

#### Infrastructure

- **`tests/`** with 79 tests covering pure functions: formatters, utc_timestamp
  (including timezone-bug regression), era_for / ERA_ORDER / era_sort_key,
  resolve_versions, insert_gap_breaks, compute_epoch_durations, plus smoke
  tests that subprocess-invoke each script with `--help`.
- **`.github/workflows/ci.yml`**: runs on push and PR across Python
  3.10 / 3.11 / 3.12. Steps: `py_compile`, `ruff check`, `mypy`, `pytest`.
- **`pyproject.toml`**: `[project.dependencies]` (runtime) split from
  `[project.optional-dependencies.dev]` (pytest/ruff/mypy/pre-commit). Pytest
  configured to promote unexpected FutureWarning to errors.
- **`requirements.txt` / `requirements-dev.txt`** split; `uv.lock` generated
  via `uv pip compile pyproject.toml --extra dev` for reproducible installs.
- **`SQLite WAL mode`** enabled in `init_sqlite_schema` (`PRAGMA journal_mode=WAL`)
  so plot/report scripts can read concurrently while the monitor writes.

#### Documentation

- **`docs/` folder** with six markdown files covering the *why*:
  time-series fundamentals, Cardano domain primer, graph catalog (one entry
  per chart), statistics primer, database internals, glossary.
- **`README.md`** expanded with Quickstart (from zero to A/B comparison),
  Per-script quickstart cheat sheet, troubleshooting section catalogue,
  WAL mode subsection, mainnet-safe flags, Monitor measurement-bias subsection.

### Changed

- **Era classification** in the report now reads from
  `epoch_param.protocol_major` (active ledger protocol version per epoch)
  instead of `block.proto_major` (per-block producer signal). The latter
  could cause Babbage epochs containing Conway-signaling blocks to be
  mislabeled as Conway. The signaling/active distinction is now documented
  in `docs/02-cardano-domain-primer.md`.
- **`fetch_era_sync`** uses `MIN(epoch_no)` semantics implicit via SQL grouping
  and the `era_for()` Python mapping; the previous SQL `CASE` is gone, so
  adding a new era is a one-line dict change.
- **All `pd.read_sql_query(sql, psycopg2_conn)` calls** replaced with a
  thin cursor-based `query_df` helper to avoid pandas' "psycopg2 not in
  officially-tested set" FutureWarning. No SQLAlchemy dependency added.
- **`total_output` SQL cast** changed from `::numeric` to `::double precision`
  so psycopg2 returns float64 directly instead of Python `Decimal` (which
  ended up as object-dtype in pandas and tripped fillna FutureWarning).
- **`MIN`/`MAX(block.time)` seq-scans** replaced with indexed two-hop reads
  via `block_no` (PK). Monitor postgres load per sample dropped from
  hundreds of MB read to a few KB — important because the original seq scans
  competed with db-sync writes for I/O and inflated the sync time we were
  trying to measure.
- **psycopg2 connection lifecycle** uses an explicit `@contextmanager` that
  calls `conn.close()` in `finally`. `with psycopg2.connect()` alone leaks
  connections (it commits/rollbacks on exit but doesn't close).
- **Top-N truncation in reports** removed: size and summary reports now list
  all tables and all indexes sorted largest first. On mainnet that's hundreds
  of indexes — long but complete.

### Fixed

- **Timezone bug in `tip_lag_sec` / `sync_percent`.** psycopg2 returns naive
  datetimes for postgres `timestamp` columns; `datetime.timestamp()` was
  interpreting them as local time instead of UTC. On a `UTC+N` host, `TipLag`
  was off by exactly N hours and `Sync %` was off by a corresponding amount
  near tip. Fixed via `utc_timestamp()` in `_common.py`; regression test in
  `tests/test_time.py` locks the behavior.
- **Pandas FutureWarning on `fillna(0)`** for object-dtype columns silenced
  via `pd.set_option("future.no_silent_downcasting", True)` at module load
  in `db-sync-report.py`. Chaining `.infer_objects(copy=False)` after fillna
  alone was insufficient — the warning fires from inside fillna's downcast
  path before any chained call.
- **Pandas FutureWarning on `pd.concat` with all-NA columns** silenced
  locally in `insert_gap_breaks` via `warnings.catch_warnings()`.
- **Pandas warning about psycopg2 connections** silenced by replacing
  `pd.read_sql_query` with the `query_df` helper everywhere.
- **`p95_tx_size` column** is omitted from the SELECT (rather than returned
  as all-`NULL::float`) when `--with-p95` isn't passed, so pandas doesn't
  end up with an object-dtype column to wrangle through fillna.
- **The `block.proto_major` era classification bug** (described above under
  "Changed") was originally a bug — fixing it required understanding the
  signaling-vs-active distinction in Cardano protocol versioning. Documented
  in the migration code path of the report.

### Documentation

- **README Quickstart** at the top: end-to-end recipe for running A/B
  comparison on db-sync from install through final HTML+text reports.
- **README Per-script Quickstart** as a cheat sheet block — common invocations
  (foreground, background, JSON, A/B, time-axis) for each of the five scripts.
- **Troubleshooting section** in README catalogues real messages users encounter
  (UTXO probe timeout, missing `ts` column, schema migration wait, missing
  `ingest_metrics`, the `TipLag = UTC offset` bug, gap-cliff plots, etc.)
  with cause + fix for each.
- **`docs/` folder** as a separate documentation tree explaining *why*
  (vs the README's *how to run*):
  - `01-time-series-fundamentals.md` — sampling theory, slot vs time axis,
    gap problem.
  - `02-cardano-domain-primer.md` — slot/epoch/era, chain vs wall-clock time,
    proto_major vs protocol_major distinction.
  - `03-graph-catalog.md` — one entry per chart with healthy/regression shapes.
  - `04-statistics-primer.md` — mean vs median vs p95, rate derivatives,
    sampling assumptions, comparison methodology.
  - `05-database-internals.md` — SQLite WAL deeper than README, postgres
    indexes, `PERCENTILE_CONT` cost, the `consumed_by_tx_id` trap.
  - `06-glossary.md` — terms by domain with subtle-distinction pairs.

### Infrastructure (security & reproducibility)

- **`LICENSE`** added — Apache License 2.0, matching the IntersectMBO
  cardano-node-tests repo.
- **`uv.lock`** committed for fully reproducible dev installs.
- **`.gitignore`** extended with `*.db-wal` and `*.db-shm` so SQLite WAL files
  outside `/data/` are still ignored.

---

[Unreleased]: https://github.com/your-org/db-sync-monitoring/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/your-org/db-sync-monitoring/releases/tag/v1.0.0
