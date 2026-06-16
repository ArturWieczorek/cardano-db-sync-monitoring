# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0] - 2026-06-16

Adds the SQLite stats-report tooling: per-environment InMemory-vs-LSM comparison
reports for both cardano-db-sync and cardano-node (`db-sync-stats-report.py` /
`node-stats-report.py`, sharing `scripts/_report.py`), a cross-role
`stats-summary.py` text overview, a slot axis for the on-disk size plots, and an
extensive `docs/` set (07-13). Also renames the monitors and the Postgres report
to state their purpose, and hardens the collectors (shared SQLite write timeout,
a single source of truth for version-keyed tables).

### Added

- **Slot axis for the on-disk size plots (`--metrics disk --x-axis slot`).** The
  disk collectors record no slot (`disk_metrics.slot_no` is NULL), so disk could
  only be plotted against wall-clock time - which made two-run comparisons
  useless (the runs sit in disjoint time windows and never overlap). The plot
  scripts now derive a slot per disk sample by nearest-timestamp lookup against
  the concurrently-collected `memory_metrics` (new `_common.attach_slot_by_ts`),
  so disk growth can be compared aligned by chain position. Works on existing
  data (no collector change); falls back to the time axis with a notice if no
  concurrent resource samples exist. Filenames tag the axis (`_disk_by_slot` /
  `_disk_by_time`).

- **`scripts/stats-summary.py`** - a quick formatted text summary of a run from
  the SQLite stats DB ("what happened, at a glance"). Cross-role (auto-detects
  node vs db-sync), SQLite-read-only, no new dependency. Without `--version` it
  prints a one-row-per-version overview (samples, duration, peak RAM, peak CPU,
  peak disk, and peak `lsm/` for LSM builds); with `--version <token>` a detailed
  breakdown (peak and start→end RAM, CPU, on-disk + `lsm/` size, and role-specific
  ingest+table sizes or sync state+RTS heap). The `lsm/` figures appear only when
  the build actually used LSM (in-memory runs have none). Runs the queries from the new `docs/12 - Useful queries`
  cookbook and renders bytes/MiB as binary GiB. Reuses `_common`'s
  `format_bytes` / `format_duration`.
- **`docs/12 - Useful queries`** - a copy-paste SQL cookbook for the SQLite stats
  DBs (both roles), units pre-converted to GiB.

- **Auto-generated db-sync stats reports (`scripts/db-sync-stats-report.py`).**
  Assembles the time-series stats plots into a per-environment LSM-vs-InMemory
  comparison document, automating the old manual "open each plot HTML, click
  Plotly's camera, paste the PNG into a template" workflow. Per env and per build
  it lays out CPU&RAM, Ingest, and Table Row Counts on both slot and time axes,
  then an LSM-vs-InMemory section and (with `--compare-to`) a this-vs-previous
  section. Two renderings via `--format`: Markdown + auto-rendered PNGs (needs
  the new optional `kaleido` extra: `pip install '.[report]'`) and/or a
  self-contained interactive HTML (no extra dependency). Per-figure progress is
  printed, and an optional `--html-max-points N` downsamples the interactive
  HTML's traces (PNGs stay full-resolution) to shrink large multi-day reports
  while keeping a single offline file. Reads the SQLite stats DBs only - distinct
  from the Postgres-based `db-sync-epoch-report.py`. The plotting
  functions in `db-sync-plot.py` were split into reusable `build_*` figure
  builders, and shared report machinery lives in `scripts/_report.py` (also the
  basis for the cardano-node report below).

- **Auto-generated cardano-node stats reports (`scripts/node-stats-report.py`).**
  The cardano-node sibling of `db-sync-stats-report.py`, built on the same
  `scripts/_report.py` machinery. Per env (`data/cardano-node/<env>.db`) and per
  build it lays out CPU&RAM, Ingest, On-disk Size, and RTS plots, then an
  LSM-vs-InMemory section and (with `--compare-to`) a this-vs-previous section.
  Same `--format`/`--scale`/`--html-max-points`/`--sqlite-db` flags and the same
  Markdown+PNG vs self-contained-HTML renderings as the db-sync report. Two
  node-specific behaviours: Ingest is plotted on an epoch axis (sync time by era +
  per-epoch duration), so it renders once per build rather than on both slot and
  time axes; and the On-disk Size / RTS plots come from the optional
  `node-db-size-monitor.py` / `node-rts-monitor.py` collectors, so they are
  silently skipped for any build that never ran them (RTS is also kept out of the
  comparison overlays as it is many panels). To enable this reuse, `node-plot.py`'s
  `plot_*` functions were split into pure `build_*` figure builders mirroring the
  db-sync side (purely additive - the `plot_*` writers and all existing behaviour
  are unchanged). Tests added: `tests/test_node_stats_report.py` (and
  `node-stats-report.py` registered in `tests/test_scripts_smoke.py`). README
  updated; full guide in new `docs/13 - Generating reports (cardano-node)`.

- **Overview-then-detail layout for the dense plots.** `node-plot.py --metrics
  rts` leads with an overview section - one panel per unit-group (bytes,
  milliseconds, count, ticks) with two or more metrics, overlaying the group on
  a shared axis (`bytes`/`milliseconds` use a log y-axis for their wide dynamic
  range) - then the per-metric detail panels ordered by group. Each panel's
  y-axis is now labelled with the unit inferred from the metric name
  (`_rts_y_unit`); previously RTS panels had no y-axis label. `db-sync-plot.py
  --metrics tables` gets the same shape: the all-tables log-y overlay becomes an
  overview panel, followed by one detail panel per hot table.

### Changed

- **Renamed three scripts to state their purpose.** The two vague "monitor"
  scripts and the ambiguously-named Postgres report were renamed (behaviour
  unchanged - only filenames):
  - `node-monitor.py` → `node-resource-monitor.py`
  - `db-sync-monitor.py` → `db-sync-resource-monitor.py`
  - `db-sync-report.py` → `db-sync-epoch-report.py` (the per-epoch Postgres
    report; now clearly distinct from the SQLite `db-sync-stats-report.py`)

  "resource" reflects that the main monitors capture CPU/RAM **plus** tip/sync/
  ingest (not just CPU/RAM). All references across scripts, tests, README, docs,
  and AGENTS were updated. **Update any cron jobs or scripts** that invoke the
  old names.

- **The dense plots use consistent, generous panel sizing.** A shared
  `_common.subplot_dims(rows)` sizes the many-panel stacked subplots - RTS,
  db-sync ingest, and the row-counts tables view - in fixed pixels (≈300px
  panel, ≈40px gap), replacing the ad-hoc per-plot height formulas and the
  fixed-*fraction* `vertical_spacing` that made gaps balloon to most of the
  figure with many rows. The few-panel plots (cpu_ram, on-disk size, and the
  node sync-by-era/per-epoch plot - all ≤2 panels) are deliberately left at
  Plotly's responsive auto-height so they fill the browser viewport, which reads
  better than a pinned height for so few panels.

### Fixed

- **Empty disk plot.** `insert_gap_breaks` used a fixed 50s gap threshold
  ("~5x the 10s sample interval"), but `node-db-size-monitor.py` samples every
  60s - so a NaN line-break was inserted between *every* disk sample and the
  plot rendered completely blank. The threshold is now adaptive: when no
  explicit `gap_sec` is given it is computed per series as 5x the median
  inter-sample interval (falling back to 50s when unmeasurable), so any
  collector cadence works. 10s series are unaffected (still ~50s).
- **RTS (and other optional-collector) versions invisible in the plot picker.**
  `node-plot.py` enumerated versions from `node_version` only, which just
  `node-resource-monitor.py` writes. A run collected solely by the disk or RTS collector
  - or saved under a mistyped `--node-ver` - never appeared and couldn't be
  plotted. The picker now unions distinct labels across every version-keyed
  node table (new `load_all_versions` helper).
- **`rename-version.py` skipped `disk_metrics` and `rts_metrics`.** Its node
  table list predated those tables (added in 1.1.0 / 1.2.0), so relabeling a
  version silently left disk/RTS rows behind - the exact "silently dropping
  panels" failure the script exists to prevent. Both tables are now included.
- **Disk/RTS collectors now warn on a likely-mistyped `--node-ver`.** When a run
  starts under a brand-new label while the DB already holds other labels, the
  collector prints the existing labels so the mismatch is caught at collection
  time, not weeks later when the plot drops the series.
- **Collector crash on `database is locked`.** Several collectors write the *same*
  per-env SQLite file (`node-resource-monitor` + `node-rts-monitor` + `node-db-size-monitor`
  for node; `db-sync-resource-monitor` + `db-sync-ledger-size-monitor` for db-sync), and WAL
  serializes writers. Under `sqlite3`'s default 5s busy timeout, a brief two-writer
  collision - or a maintenance pass (`rename-version`, `backup-stats`) holding the
  lock on a multi-hundred-MB DB - raised an uncaught `sqlite3.OperationalError` that
  killed the collector, ending a multi-day run on one lost race (observed on a live
  mainnet sync). Every writer now opens its connection through a new
  `_common.connect_writer()` with a 30s busy timeout (`WRITE_TIMEOUT_SEC`), so a
  blocked writer waits for the lock instead of failing; and each collector loop now
  catches a residual `OperationalError`, drops that one sample with a warning, and
  keeps running. Covered by `tests/test_rts_monitor.py::TestBusyResilience`.

### Changed

- **Single source of truth for version-keyed tables.** The list of tables
  carrying a `version` column now lives once in `_common.VERSION_KEYED_TABLES`,
  consumed by `rename-version.py` and both plot pickers (previously duplicated
  and hand-maintained, which is why `disk_metrics`/`rts_metrics` were missed
  twice). A new drift guard, `tests/test_version_tables_registry.py`, scans the
  collectors' `CREATE TABLE` statements and fails CI if a version-keyed table is
  left unregistered. `db-sync-plot.py`'s picker now also unions across all its
  version-keyed tables for parity.
- **Renamed `uv.lock` to `requirements.lock`.** The file is `uv pip compile`
  output in pip requirements format, not a real uv TOML lockfile, so `uv sync`
  rejected it with a parse error. The new name reflects what it actually is and
  steers contributors to `uv pip install -r` / `pip install -r` instead of
  `uv sync`. Regenerate with `uv pip compile pyproject.toml --extra dev -o
  requirements.lock`; references in `README.md`, `AGENTS.md`, and
  `.pre-commit-config.yaml` were updated to match.

### Documentation

- **New post-mortem** `docs/postmortems/2026-06-06-empty-disk-and-invisible-rts.md`
  (linked from `docs/README.md`) documenting the three defects above, the
  underlying drift, and the `VERSION_KEYED_TABLES` invariant. Corrected the
  now-stale gap-threshold descriptions (adaptive, not a fixed 50s) across
  `docs/01`, `docs/03`, `docs/06`, `docs/08`, and the README; fixed the node
  version-keyed table count (4 → 6) and the rename SQL recipe in the README and
  `AGENTS.md`.
- **New doc `09 - Reading and smoothing bursty rates`.** The block/tx insert-rate
  panels look spiky, and the existing advice ("use a 5-sample rolling mean") turns
  out to be the wrong tool for the worst panel. Measured on the mainnet stats DB, a
  5-sample rolling mean cuts the block-rate spread by only ~1% - because most of
  that spread is real signal (the Byron bulk-load runs ~50x faster than the modern
  chain, ~1080 vs ~23 blocks/sec), not fast noise. The doc teaches, from first
  principles and with the real numbers as worked examples, how to *measure*
  spikiness (coefficient of variation, percentiles, jumpiness), how to tell signal
  from noise, and which display techniques actually help (logarithmic y-axis and
  rolling median, not a short rolling mean). Cross-linked from doc 04 and doc 03,
  whose now-too-blunt "smooth it mentally / 5-sample mean" notes were corrected to
  point at it.
- **Doc 08 now walks the connection-reuse code line by line.** Added a "the code,
  line by line" subsection to "Connection lifecycle" covering `_pg`,
  `_ensure_loop_conn`, `_drop_loop_conn`, and a representative loop query
  (`get_tip`) - explaining lazy-open, reuse, why the long-lived loop connection is
  autocommit (so it never pins a snapshot and blocks Postgres VACUUM), and the two
  distinct error paths (drop-and-reconnect on a connection-level error vs keep-the-
  connection on a transient query error).
- **New post-mortem** `docs/postmortems/2026-06-08-database-is-locked-collector-crash.md`
  (linked from `docs/README.md`) documenting the shared-file write contention, the
  `connect_writer` / survive-a-transient-`OperationalError` invariant, and the fix.
  Corrected two now-false claims that asserted the very assumption that broke -
  `README.md` ("only ever has one monitor process per env") and `docs/08` ("each
  file has a single writer") - and added a *Multiple writers and `busy_timeout`*
  section to `docs/05`, with `docs/08`'s commit-cadence note updated to
  `connect_writer`.

## [1.2.0] - 2026-06-04

Adds a standalone cardano-node RTS/runtime metrics collector - GHC GC,
allocations, heap/live bytes, and mempool, scraped from the node's Prometheus
endpoint - plus the plot to compare those curves across versions. `node-resource-monitor.py`
is untouched: the scrape is isolated behind a timeout, so it carries zero risk
to a running resource collector.

### Added

#### RTS/runtime collector (`scripts/node-rts-monitor.py`)

- **RTS/runtime monitoring for cardano-node.** A new standalone collector
  scrapes the node's Prometheus endpoint (default
  `http://127.0.0.1:12798/metrics`) and appends a `(metric, value)` time series
  to the same `data/cardano-node/<env>.db` under the same version label
  `node-resource-monitor.py` uses, so the RTS curves join the rest of the run's metrics
  by `version`. Captures the runtime signals psutil can't - GC counts /
  allocations / heap & live bytes (where a GHC-version or allocator change shows
  up) plus mempool gauges.
- **Kept separate from `node-resource-monitor.py` on purpose - zero risk.** It adds an
  HTTP scrape per interval, isolated behind `--timeout` and a `try/except`, so a
  slow or unreachable endpoint just skips the sample and can never stall or
  crash the psutil/tip sampling `node-resource-monitor.py` does. `node-resource-monitor.py` is
  left completely untouched.
- **Robust to metric-name variation.** Names vary by node version / tracing
  backend, so the collector scrapes the whole endpoint and keeps the names
  matching a curated case-insensitive substring allowlist (`rts`, `gc`, `alloc`,
  `heap`, `live`, `mempool`, override with `--include`) rather than exact names.
  `--list-metrics` prints everything the endpoint currently exposes (and how
  many match the allowlist) so you can discover your node's exact names.
- **Long/narrow `rts_metrics` table** (`ts`, `slot_no`, `metric`, `value`,
  `version`) - one row per (sample, metric) - so any metric name works without
  schema churn. Created in WAL mode so it writes concurrently with the main
  monitor on the same `<env>.db`. `slot_no` is stamped from the node's `slotNum`
  gauge so the series can share the slot x-axis with the other node metrics.
  Non-finite values (`NaN`/`±Inf`) are dropped so they can't poison plots.
- **Mempool folded into the same script, not a second one.** The generic
  key/value table makes a mempool gauge just another allowlist entry; a separate
  script would only re-scrape the same endpoint for no reason.
- `--json` per-sample output, startup history notice, `--interval` (default 10s,
  the scrape is light), `SIGINT`/`SIGTERM` clean shutdown with a sample-count
  summary, and line-buffered stdout - same operational conventions as the
  existing monitors.

#### Plotter (`scripts/node-plot.py`)

- **`--metrics rts`**: one subplot per distinct RTS metric (sorted by name),
  each overlaying one line per version. Values are plotted raw as scraped
  (counts for GC numbers, bytes for heap/live/allocated), each metric on its own
  y-axis. Honors `--x-axis slot` (default) and `--x-axis time` - `slot_no` is
  populated from the node's `slotNum` gauge - and gap-breaks each metric's line
  independently on an outage. Output filename tagged `_rts_by_<x-axis>`.
- **`rts` folded into `--metrics all`** as a graceful no-op when the
  `rts_metrics` table or the selected versions' rows are absent: the RTS
  collector is optional, so `all` keeps producing the other plots and just
  prints a "skipping rts" notice - same guarantee as `disk`.

#### Tests

- **`tests/test_rts_monitor.py`** - covers Prometheus text parsing (labels,
  comments, trailing timestamps, non-finite rejection), the substring allowlist
  (new-tracing `cardano_node_metrics_RTS_*` and old-EKG `rts_gc_*` shapes), slot
  extraction, the monkeypatched-`urlopen` fetch (success and failure-returns-None
  without raising), and schema/`record()` insertion against a tmp SQLite DB.
- **`tests/test_plot_rts.py`** - covers `load_rts` (column shape, per-metric
  gap-breaks), `plot_rts` (one subplot/trace per metric, `_rts_by_slot` /
  `_rts_by_time` filename), `render_rts` graceful-skip when the optional table or
  rows are absent, and the `--metrics all` guard proving `all` neither crashes
  without rts data nor omits the rts HTML once present.
- `node-rts-monitor.py` added to the smoke-test `--help` parametrize list.

## [1.1.0] - 2026-06-03

Adds standalone on-disk size monitoring for both roles - the node's database
directory and db-sync's ledger-state directory, including the optional `lsm/`
subdir for LSM-backed builds - plus the plots to compare them, and corrects all
size/memory units from the SI-labelled `MB`/`GB` to the binary `MiB`/`GiB` they
were actually computing.

### Added

#### Disk-size collectors (`scripts/node-db-size-monitor.py`, `scripts/db-sync-ledger-size-monitor.py`)

- **On-disk size monitoring for both roles.** Two new standalone collectors
  measure a directory's apparent size via `du -sb` and append a time series to
  the role's `data/<role>/<env>.db` under the same version label the matching
  `*-monitor.py` uses, so the disk curve joins the rest of the run's metrics by
  `version`:
  - `node-db-size-monitor.py` measures the node's `--database-path` directory.
  - `db-sync-ledger-size-monitor.py` measures db-sync's `--state-dir` directory.
- **`lsm/` subdir tracked separately.** Each sample records both the total
  directory size and, separately, the size of the `lsm/` subdir. On
  stock/in-memory builds the subdir is absent, so `lsm_bytes` is simply 0 and
  the subdir is never even stat-walked - works unchanged for both LSM and stock
  builds.
- **Kept separate from the main monitors on purpose.** A `du` of a
  multi-hundred-GB mainnet directory is a heavy, cache-polluting tree walk, so
  it runs on its own coarse cadence (`--interval`, default 60s) rather than
  biasing the 10s CPU/RAM samples - and can be started/stopped independently.
  `--du-timeout` (default 120s) skips a sample on timeout rather than writing a
  bogus 0.
- **Shared core in `scripts/_disk_size.py`.** `DiskSizeMonitor` holds all the
  common mechanics (schema, sampling, loop, signal handling, summary); the two
  roles differ only by subclass attributes (`DATA_DIR`, `BINARY_PREFIX`,
  `PATH_FLAG`, `LABEL_PREFIX`, `ENV_IN_ARGV`). Unit-testable pure helpers
  `du_bytes` and `parse_path_flag` (the latter handles `--flag value` and
  `--flag=value`, resolving relative paths against the owning process's CWD).
- **Auto path discovery.** With no `--path`, the collector finds the owning
  process and parses the directory out of its argv. `--match-arg` disambiguates
  when multiple node/db-sync processes run side by side (e.g. an LSM build next
  to an in-memory one), mirroring the `*-monitor.py` matcher. db-sync takes no
  env flag, so it matches on the `cardano-db-sync` binary prefix rather than
  env-in-argv.
- **`disk_metrics` table** (`ts, slot_no, path, total_bytes, lsm_bytes,
  version`), created in WAL mode so the disk collector writes concurrently with
  the main monitor on the same `<env>.db`. `--json` per-sample output, startup
  history notice, `SIGINT`/`SIGTERM` clean shutdown with a peak/final summary,
  and line-buffered stdout - same operational conventions as the existing
  monitors.

#### Plotters (`scripts/db-sync-plot.py`, `scripts/node-plot.py`)

- **`--metrics disk`** on both plotters: on-disk size over wall-clock time, one
  trace per version. Row 1 is the total directory size; row 2 (the `lsm/`
  subdir) is added only when at least one selected version actually has an lsm
  subdir, so stock/in-memory runs aren't padded with a flat zero line - but in
  a mixed LSM-vs-in-memory comparison the row is shown (the zero line is itself
  the point). Always plotted against `ts` (disk_metrics has no `slot_no`, and
  disk growth reads naturally against wall-clock); output filename tagged
  `_disk_by_time`.
- **`disk` folded into `--metrics all`** on both plotters, but as a graceful
  no-op when the `disk_metrics` table or its rows are absent: most DBs won't
  have run the optional disk collector, so `all` keeps producing the
  cpu_ram/ingest/tables plots and just prints a "skipping disk" notice instead
  of aborting the batch.

#### Tests

- **`tests/test_disk_size.py`** - covers the shared core (`du_bytes`,
  `parse_path_flag`, schema, sampling, lsm-present-vs-absent) and the per-role
  wiring of both subclasses (label format, target DB path, process matching
  incl. db-sync's no-env rule), using a fake process so no real processes are
  needed.
- **`tests/test_plot_disk.py`** - covers `load_disk` (column shape, GiB
  conversion, ts parsing, gap-breaks), `plot_disk` (lsm row only when present,
  one total trace per version, `_disk_by_time` filename), and the explicit
  regression guard that `--metrics all` still renders the other plots and skips
  disk without crashing on a DB that has no `disk_metrics`.
- Both new collectors added to the smoke-test `--help` parametrize list.

### Changed

- **Size and memory units corrected from SI to binary across the board.**
  Every value the tool computes is binary (bytes / 1024ⁿ), but the labels said
  `MB`/`GB`/`KB`/`TB`, which overstates a binary value by ~7.4% at the GiB
  level (a GiB is 1.074 GB). Labels are now the matching binary units:
  - `_common.py` `format_size` → `MiB`/`GiB`; `format_bytes` →
    `KiB`/`MiB`/`GiB`/`TiB`.
  - `db-sync-plot.py` / `node-plot.py` axis titles and the internal
    `db_size_mb`→`db_size_mib` column → `MiB`/`GiB`.
  - `db-sync-resource-monitor.py` / `node-resource-monitor.py` `--json` keys `rss_mb`/`vms_mb`
    renamed to `rss_mib`/`vms_mib`.
  - `backup-stats.py` / `rename-version.py` backup-size print → `MiB`.
  - `tests/test_formatters.py` updated to assert the binary labels.

### Documentation

- **README** updated for this release: latest-release banner bumped to
  `v1.1.0`, a new "On-disk size monitors" section documenting both collectors
  and the `disk_metrics` table, `--metrics disk` added to both plotters' metric
  tables and quickstarts, and all sample output / units switched to MiB/GiB
  (including the `db-sync-epoch-report.py` size-report example and the `--json` key
  names).

## [1.0.0] - 2026-05-27

First formal release. The project started as a basic resource collector + a few
report queries; this release marks the point where it became a complete A/B
testing toolkit for cardano-node and cardano-db-sync, with a tested core,
documented internals, and CI.

### Added

#### Monitors (`scripts/db-sync-resource-monitor.py`, `scripts/node-resource-monitor.py`)

- **`--match-arg <substring>` flag on both monitors.** Disambiguates when
  multiple `cardano-node` (or `cardano-db-sync`) processes are running on the
  same host for the same env - e.g. an LSM-backed build next to an in-memory
  one for A/B comparison. The substring must appear somewhere in the matched
  process's command line (argv[0] including its full path, plus any argument);
  a plain `in` check, no regex. Without the flag, behaviour is unchanged
  (first matching process wins, with the existing "Multiple ... match" warning).
  db-sync-resource-monitor's `get_process` was refactored from a simple `name.startswith`
  check to a full-cmdline matcher so the substring search has something
  meaningful to look at; see the new `_match_db_sync_process` method.
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
- **`wait_for_schema`** in db-sync-resource-monitor polls every 2s until the `block`
  table exists, eliminating "relation does not exist" noise during the first
  few iterations of a fresh sync.
- **UTXO tracking auto-detection**: monitor probes `tx_out.consumed_by_tx_id`
  with a 5-second `statement_timeout` safety net. Only samples `utxo_count`
  when populated. Re-probes each sample until definitive.
- **`node_ingest_metrics` table** on the node side: per-sample `slot_no`,
  `epoch_no`, `era`, `sync_progress`. Enables per-epoch and per-era sync
  duration plots on the node side (previously db-sync-only).
- **Hot-table row count sampling** in db-sync-resource-monitor: cheap `pg_class.reltuples`
  estimates for `block`, `tx`, `tx_out`, `ma_tx_out`, `ma_tx_mint`, `multi_asset`,
  `datum`, `redeemer`, `script` every interval. Plot via `--metrics tables`.
- **Ingest metrics**: tip lag, DB size, max block_no, max tx_id, UTXO count
  (when enabled) - all sampled every interval and plottable.

#### Plotters (`scripts/db-sync-plot.py`, `scripts/node-plot.py`)

- **`--versions a,b` flag**: non-interactive multi-version selection. Accepts
  full labels or short tokens. Ambiguous matches raise; missing labels error
  out instead of silently dropping.
- **`--list` flag**: list available versions in the DB and exit. Scriptable.
- **`--x-axis {slot, time}` flag**: plot against `slot_no` (default - best for
  chain-content comparison) or wall-clock `ts` (best for diagnosing stalls
  and at-tip behavior).
- **`--metrics` flag** on db-sync-plot: `cpu_ram` (default), `ingest`, `tables`,
  or `all` - selects which time-series to plot.
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

#### Report (`scripts/db-sync-epoch-report.py`)

- **A/B comparison mode**: `--pg-dbname dbA,dbB` produces a headline-deltas
  summary (Total sync time, Final DB size, etc. with Δ column) plus per-version
  detail sections, all in one combined `dbA_vs_dbB_*` text file.
- **`--with-p95`** opt-in: per-epoch p95 tx size via `PERCENTILE_CONT`.
  Expensive on mainnet (5-20 min) so default-off.
- **`--skip-slow`** opt-out: skips the per-epoch Plutus adoption and
  cumulative-distinct-assets queries when faster turnaround is needed.
- **Stage-by-stage progress logging**: `[3/8] Rendering per-epoch HTML...` so a
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

#### Utilities

- **`scripts/backup-stats.py`** - wraps Python's `sqlite3.Connection.backup()`
  (the WAL-aware backup API) so you don't need to remember the right
  invocation before destructive operations. Supports `--env [--role]` for the
  conventional `data/<role>/<env>.db` layout, `--path` for arbitrary DBs, and
  `--list` to inventory existing backups. Naming: `<original>.bak-YYYYMMDD_HHMMSS`,
  dropped next to the source so it's already covered by `.gitignore`.
- **`scripts/rename-version.py`** - wraps the README's "rename a version label"
  SQL recipe so renames go through one source of truth. Auto-detects role
  (db-sync vs node) from the schema and updates every version-keyed table in
  one transaction: 5 for db-sync (`memory_metrics`, `cpu_metrics`,
  `db_sync_version`, `ingest_metrics`, `table_rowcounts`), 4 for node
  (`memory_metrics`, `cpu_metrics`, `node_version`, `node_ingest_metrics`).
  Motivation: the previous README recipe only listed the three "core" tables
  and silently left `ingest_metrics`/`table_rowcounts` (or `node_ingest_metrics`)
  under the old label, which made `_ingest.html` and `_tables.html` come up
  empty for the renamed version. Supports `--env [--role]` and `--path`,
  `--dry-run`, refuses to merge two labels into one unless `--merge` is set,
  takes a timestamped backup (via `backup-stats.py`'s API) unless `--no-backup`.
  Role is inferred from the `cardano-db-sync `/`cardano-node ` prefix of
  `--from-version` when `--role` is omitted.

#### Architecture

- **`scripts/_common.py`**: shared helpers (formatters, utc_timestamp,
  ERA_BY_PROTOCOL_MAJOR + era_for + era_sort_key, find_process/find_processes,
  get_memory_details/get_cpu_details, has_table/has_column,
  init_sqlite_schema, report_existing_history, short,
  load_versions_from_sqlite, resolve_versions, insert_gap_breaks,
  compute_epoch_durations, warn, step). Imported by all 5 scripts and tests.
- **`scripts/_db_sync_queries.py`**: postgres data layer extracted from
  db-sync-epoch-report.py - pg_connect, table_exists, utxo_tracking_enabled,
  query_df, all `fetch_*` per-epoch and per-era fetchers, `assemble_epoch_df`,
  size / index queries, build_summary.

#### Infrastructure

- **`tests/`** with 79 tests covering pure functions: formatters, utc_timestamp
  (including timezone-bug regression), era_for / ERA_ORDER / era_sort_key,
  resolve_versions, insert_gap_breaks, compute_epoch_durations, plus smoke
  tests that subprocess-invoke each script with `--help`.
- **`tests/test_backup_stats.py`** - functional tests using `tmp_path` covering
  the backup API path, list semantics, immutability of the backup against
  subsequent source writes, and FileNotFoundError on missing sources.
- **`tests/test_rename_version.py`** - functional tests using `tmp_path`
  covering role detection (db-sync, node, neither), `count_for` skipping
  missing tables, full-table renames for both roles, dry-run no-op,
  target-label collision refusal, `--merge` collapsing two series, and
  no-op when the source label has zero rows.
- `backup-stats.py` and `rename-version.py` added to the smoke-test
  parametrize list.
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
- **README "Disambiguating multiple processes per env" section** covering
  the silent-drift and sibling-takeover risks of the default matcher, the
  `--match-arg` fix, and the "monitor both side-by-side" pattern.
- **README "Removing records" and "Updating stats for a wrong db-sync version"**
  sections now cover all five db-sync tables (previously listed only three);
  the node section adds `node_ingest_metrics` (previously missing) and grew a
  rename block alongside its delete block. Intro spells out which tables each
  role has, so the gap doesn't reopen next time the schema grows.
- **`AGENTS.md`** - top-level guide for AI agents (Claude Code, Cursor, etc.)
  working in this repo. Loosely modeled on IntersectMBO/cardano-node-tests's
  AGENTS.md but rewritten for our reality (small Python tool, scripts +
  tests, SQLite stats DBs, uv venv). Includes an architecture outline (which
  file lives where and why), style/lint/typing rules, a hard requirement to
  add tests for every new script and run the full suite to green before
  declaring done, and the workflow contract that README and CHANGELOG must
  be updated in the same change as user-facing code. Closes the recurring
  gap where agents would add a script without a test or land a behavior
  change without touching the docs.

### Changed

- **db-sync-resource-monitor connection reuse.** The sample loop's four steady-state
  query methods (`get_tip`, `get_first_block_time`, `get_ingest_metrics`,
  `get_table_rowcounts`) now share a single autocommit psycopg2 connection
  opened lazily via `_ensure_loop_conn()`. Previously each opened its own
  short-lived connection per sample - at `--interval=10s` that was ~24
  connection cycles per minute (~35K/day) per env. Now it's one persistent
  connection. Connection-level errors (`OperationalError`,
  `InterfaceError`) drop the loop conn so the next sample reopens -
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
- **node-plot ingest plot layout**: layout fixes for the era bar + per-epoch
  chart. Subplot title caveat moved out of the era subplot's title (where it
  caused plotly to render a multi-line title and crowd the bar chart) into the
  main figure title as a gray sub-line. Subplot titles back to single-line.
  Spacing tuned: `vertical_spacing=0.18`, `row_heights=[0.45, 0.55]`,
  `height=800`, explicit `margin=dict(t=150, b=60, l=80, r=40)`,
  `title.pad.b=30`. `<code>` tags replaced with `<b>` (plotly's title
  HTML doesn't support `<code>`).
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
  hundreds of MB read to a few KB - important because the original seq scans
  competed with db-sync writes for I/O and inflated the sync time we were
  trying to measure.
- **psycopg2 connection lifecycle** uses an explicit `@contextmanager` that
  calls `conn.close()` in `finally`. `with psycopg2.connect()` alone leaks
  connections (it commits/rollbacks on exit but doesn't close).
- **Top-N truncation in reports** removed: size and summary reports now list
  all tables and all indexes sorted largest first. On mainnet that's hundreds
  of indexes - long but complete.

### Fixed

- **Plot output filenames now self-describe env, metric kind, and x-axis.**
  Previously `out_path` treated `cpu_ram` as the "default" plot and gave
  it no kind tag at all (so `LSM-13.7.1.0_time.html` could be any of the
  three plots - there was no way to tell), the x-axis was encoded as a
  bare `_time` suffix (which read like a timestamp at a glance) or no
  suffix at all for `--x-axis slot`, and the env was only in the parent
  directory - once a file was moved or shared the env context vanished.
  New scheme is `<env>_<versions>_<kind>_by_<axis>.html` - every file
  carries all four pieces of context, e.g.
  `preprod_13.7.1.0_cpu_ram_by_slot.html`,
  `preprod_13.7.1.0_ingest_by_time.html`,
  `preprod_13.6.0.5_vs_13.7.1.0_tables_by_time.html` (env appears once at
  the front since the SQLite stats DB is per-env, so both versions in a
  comparison are guaranteed to share it). Identical change applied to
  `node-plot.py`'s `out_path` so both scripts stay in lockstep, and the
  scheme now matches db-sync-epoch-report's env-prefixed output convention.
  README updated; `tests/test_plot_out_path.py` (new) pins the scheme
  parametrized over both scripts, including an env-first-word assertion
  and a "env appears once not per version" assertion. Defensive
  `TestNoLegacyCollision` class fails if anyone partially reverts.
  Existing plots in `plots/` are left under their old names - only
  newly-generated plots use the new scheme.
- **db-sync-plot `--metrics tables`: version label is now always visible.**
  Previously the table-rowcounts plot only put the version in legend entries
  when comparing multiple versions; for a single-version run the legend
  showed just bare table names (`block`, `tx`, `tx_out`, ...) and the chart
  title had no version either. The version was only encoded in the
  filename, which you lose the moment the HTML is opened standalone or
  shared as a screenshot. Now the version short token (e.g. `13.7.1.0`) is
  appended to the chart title and to every trace's legend label in both
  single- and multi-version modes. Legend title is always
  `"Table / Version"`. `tests/test_plot_rowcounts.py` (new) locks both
  positions in place; the `cpu_ram` and `ingest` plots already carried the
  version on their traces and were unaffected.
- **Timezone bug in `tip_lag_sec` / `sync_percent`.** psycopg2 returns naive
  datetimes for postgres `timestamp` columns; `datetime.timestamp()` was
  interpreting them as local time instead of UTC. On a `UTC+N` host, `TipLag`
  was off by exactly N hours and `Sync %` was off by a corresponding amount
  near tip. Fixed via `utc_timestamp()` in `_common.py`; regression test in
  `tests/test_time.py` locks the behavior.
- **Pandas FutureWarning on `fillna(0)`** for object-dtype columns silenced
  via `pd.set_option("future.no_silent_downcasting", True)` at module load
  in `db-sync-epoch-report.py`. Chaining `.infer_objects(copy=False)` after fillna
  alone was insufficient - the warning fires from inside fillna's downcast
  path before any chained call.
- **Pandas FutureWarning on `pd.concat` with all-NA columns** silenced
  locally in `insert_gap_breaks` via `warnings.catch_warnings()`.
- **Pandas warning about psycopg2 connections** silenced by replacing
  `pd.read_sql_query` with the `query_df` helper everywhere.
- **`p95_tx_size` column** is omitted from the SELECT (rather than returned
  as all-`NULL::float`) when `--with-p95` isn't passed, so pandas doesn't
  end up with an object-dtype column to wrangle through fillna.
- **The `block.proto_major` era classification bug** (described above under
  "Changed") was originally a bug - fixing it required understanding the
  signaling-vs-active distinction in Cardano protocol versioning. Documented
  in the migration code path of the report.

### Documentation

- **README Quickstart** at the top: end-to-end recipe for running A/B
  comparison on db-sync from install through final HTML+text reports.
- **README Per-script Quickstart** as a cheat sheet block - common invocations
  (foreground, background, JSON, A/B, time-axis) for each of the five scripts.
- **Troubleshooting section** in README catalogues real messages users encounter
  (UTXO probe timeout, missing `ts` column, schema migration wait, missing
  `ingest_metrics`, the `TipLag = UTC offset` bug, gap-cliff plots, etc.)
  with cause + fix for each.
- **`docs/` folder** as a separate documentation tree explaining *why*
  (vs the README's *how to run*):
  - `01-time-series-fundamentals.md` - sampling theory, slot vs time axis,
    gap problem.
  - `02-cardano-domain-primer.md` - slot/epoch/era, chain vs wall-clock time,
    proto_major vs protocol_major distinction.
  - `03-graph-catalog.md` - one entry per chart with healthy/regression shapes.
  - `04-statistics-primer.md` - mean vs median vs p95, rate derivatives,
    sampling assumptions, comparison methodology.
  - `05-database-internals.md` - SQLite WAL deeper than README, postgres
    indexes, `PERCENTILE_CONT` cost, the `consumed_by_tx_id` trap.
  - `06-glossary.md` - terms by domain with subtle-distinction pairs.

### Infrastructure (security & reproducibility)

- **`LICENSE`** added - Apache License 2.0, matching the IntersectMBO
  cardano-node-tests repo.
- **`uv.lock`** committed for fully reproducible dev installs.
- **`.gitignore`** extended with `*.db-wal` and `*.db-shm` so SQLite WAL files
  outside `/data/` are still ignored.

---

[Unreleased]: https://github.com/ArturWieczorek/cardano-db-sync-monitoring/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/ArturWieczorek/cardano-db-sync-monitoring/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/ArturWieczorek/cardano-db-sync-monitoring/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/ArturWieczorek/cardano-db-sync-monitoring/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/ArturWieczorek/cardano-db-sync-monitoring/releases/tag/v1.0.0
