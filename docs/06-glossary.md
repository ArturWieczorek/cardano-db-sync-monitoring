# 06 — Glossary

Quick term reference. Things mentioned across the project, the README, and the other docs. Skim for what you need; come back when a term shows up that you don't recognize.

Organized by domain rather than alphabetically, because related terms tend to make sense together.

---

## Cardano

**ADA** — the native currency. Stored as `lovelace` in the database (1 ADA = 1,000,000 lovelace).

**Block** — a batch of transactions and chain state, signed by a stake pool. On Cardano, ~one block every 20 seconds at tip.

**Block producer** — a node (typically a stake pool) authorized to produce a block in a given slot. Selected probabilistically based on stake.

**Byron / Shelley / Allegra / Mary / Alonzo / Babbage / Conway** — Cardano's chronological eras, in order of activation. Each is a hard-fork-delimited phase of the chain with its own rules.

**Catch-up sync** — the phase of a node/db-sync where it processes historical blocks faster than real-time. Contrast with at-tip operation.

**Chain time** — time measured in slots. 1 slot = 1 second of chain time. The chain doesn't strictly care about wall-clock time except as it determines whose turn it is to produce a block.

**Drep (delegated representative)** — a Conway-era role: a stakeholder who registers to vote on governance actions on behalf of those who delegate to them.

**Epoch** — a stretch of 432,000 slots = 5 days. Stake snapshots, reward calculations, and protocol parameter updates happen at epoch boundaries.

**Era** — a stretch of consecutive epochs between two hard forks. Each era's rules govern how transactions within it are validated.

**Genesis** — the chain's initial state. On mainnet, established in 2017. On preprod, in 2022.

**Hard fork combinator (HFC)** — Cardano's mechanism for transitioning between eras. Activates at an epoch boundary; from that epoch onward the new era's rules apply.

**Hard fork (HF)** — colloquially, the activation event itself.

**Lovelace** — 1/1,000,000 of an ADA. The unit ADA values are stored in throughout the schema.

**Mainnet** — the production Cardano network with real economic value.

**Multi-asset (MA, also: native tokens)** — non-ADA tokens minted directly on Cardano without smart contracts. Introduced in the Mary era.

**Plutus** — Cardano's smart-contract platform. Active from Alonzo onward. A "Plutus transaction" is one that invokes a Plutus script — these have a `redeemer` attached.

**Preprod / Preview** — public testnets. Preprod is the long-lived stable one; preview is a faster-iteration testnet.

**Protocol version** — the major.minor version the ledger is operating under. Major version increments happen at hard forks; minor at parameter updates within an era.

**Redeemer** — the data a Plutus script consumer provides to invoke the script. Presence of any redeemer rows for a transaction = it's a Plutus transaction.

**Reward** — ADA paid out to stake delegators each epoch as a return on staking.

**Slot** — a 1-second time unit. Every slot may or may not produce a block.

**Stake pool** — a node operated by an SPO (stake pool operator) that produces blocks.

**Stake snapshot** — a record of the entire stake distribution at an epoch boundary. Used to determine block-producer eligibility for upcoming epochs.

**Tip** — the latest block on the chain. "Reaching tip" = a syncing node catches up to the network's current head.

**Transaction (tx)** — the basic operation that moves ADA/tokens or invokes scripts.

**Tx_in / Tx_out** — the inputs (consumed UTXOs) and outputs (new UTXOs) of a transaction. db-sync stores these in their own tables.

**UTXO (Unspent Transaction Output)** — an output of a past transaction that has not yet been consumed by a later transaction. The set of UTXOs is the chain's working balance.

**Vasil / Valentine** — informal names for specific Cardano hard forks (Vasil → Babbage initial; Valentine → intra-Babbage parameter update).

---

## cardano-db-sync

**`block`** — db-sync's table recording every block. Columns of note: `id`, `block_no`, `slot_no`, `epoch_no`, `time`, `proto_major` (per-block signaling), `tx_count`.

**`consumed_by_tx_id`** — a column on `tx_out` pointing to the tx that consumed it. Populated only when enabled in db-sync config. When populated, makes UTXO-set-size queries cheap.

**`epoch_param`** — per-epoch protocol parameters, including `protocol_major` (active era's major version).

**`epoch_stake`** — per-epoch stake snapshot rows.

**`epoch_sync_time`** — db-sync's own measurement of how long it took to sync each epoch (in seconds). Source for the era-bar chart in the report.

**`ma_tx_mint`** — multi-asset mint events. One row per minted token batch.

**`ma_tx_out`** — multi-asset values held in outputs.

**`multi_asset`** — distinct multi-asset identities. One row per unique asset across the chain.

**`tx`** — db-sync's table of transactions. Columns of note: `fee`, `size`, `out_sum`.

**`tx_in` / `tx_out`** — db-sync's transaction-input and transaction-output tables.

**`redeemer`** — script invocations attached to transactions. Used to detect Plutus transactions.

**`voting_procedure` / `drep_registration` / `gov_action_proposal`** — Conway-era governance tables.

**NearTip migrations** — a phase of db-sync near sync completion where it creates "client-only" indexes that were deferred during catch-up. The "Creating Indexes" log messages.

**Protocol version (proto_major / protocol_major)** — see distinction below in the "subtle distinctions" section.

---

## PostgreSQL

**`autovacuum`** — Postgres's background process that reclaims space from dead tuples and updates table statistics.

**`COUNT(*)` vs `COUNT(column)`** — the first counts rows; the second counts non-NULL values in `column`. Behavior differs only when the column has NULLs.

**`EXISTS`** — a predicate that short-circuits as soon as one matching row is found. Faster than `COUNT(*) > 0` for "is there at least one?".

**`EXPLAIN ANALYZE`** — runs a query and reports its actual execution plan with timings. The first thing to look at when a query is slow.

**Index scan** — using an index to find rows matching a predicate. O(log N) per lookup.

**Partial index** — an index that includes only rows matching a WHERE clause (e.g., `CREATE INDEX ... WHERE consumed_by_tx_id IS NOT NULL`). Smaller and faster than a full index when the predicate matches a minority of rows.

**`PERCENTILE_CONT`** — Postgres's continuous-percentile aggregate. Requires sorting all values per group; O(N log N).

**`pg_class`** — Postgres's catalog table describing all tables, indexes, sequences. `reltuples` is the planner's estimate of row count.

**`pg_database_size(...)`** — function returning the total disk usage of a database (table data + indexes + free space).

**`pg_relation_size(oid)` / `pg_indexes_size(oid)`** — table-level data size and total index size for that table.

**`reltuples`** — `pg_class` column with the planner's estimate of a table's row count. Cheap to read; updated by ANALYZE.

**Rollback journal** — SQLite's default journaling mode. Mutually exclusive with concurrent readers. Contrast with WAL.

**Sequential scan (seq scan)** — reading every row in a table. O(N). The fallback when no index applies.

**`statement_timeout`** — a Postgres setting capping how long a query can run before being cancelled. Defaults to 0 (unlimited). We use `SET LOCAL statement_timeout = '5000'` for queries with potentially-unbounded worst cases.

**Streaming aggregation** — aggregates that process each row once (SUM, COUNT, MIN, MAX). Constant memory.

**`to_regclass('table_name')`** — returns the OID of the named relation if it exists, NULL otherwise. Safe way to probe "does this table exist?".

---

## SQLite

**`PRAGMA journal_mode`** — controls SQLite's journaling mode. Common values: `delete` (rollback journal, default), `wal` (write-ahead log).

**Checkpoint** — operation that merges WAL contents into the main DB file. Done automatically; can be forced with `PRAGMA wal_checkpoint`.

**`.db-shm`** — shared memory file. Created when a DB is opened in WAL mode; coordinates readers and writers.

**`.db-wal`** — write-ahead log file. Holds pending writes until checkpointed.

**Single-writer** — SQLite allows only one writer at a time, regardless of journaling mode. Multiple readers + one writer is the maximum concurrency.

---

## Python / pandas / plotly

**`@contextmanager`** — decorator that turns a function with one `yield` into a context manager (usable with `with`). We use it for `_pg` to ensure connections close.

**`DataFrame`** — pandas's tabular data structure. Rows × columns, like a SQL table. Most of our plotting reads SQLite → DataFrame → plotly.

**`FutureWarning`** — pandas's "we're changing this behaviour in a future version" notice. We selectively suppress known-benign ones (e.g., gap-marker concat) and treat others as errors in tests.

**`groupby`** — pandas operation that partitions a DataFrame by one or more keys for aggregation. Like SQL GROUP BY.

**`infer_objects`** — pandas DataFrame method that converts object-dtype columns to concrete numeric types where possible.

**`plotly`** — the charting library we use. Generates self-contained interactive HTML files.

**`plotly.Scatter`** — line/marker chart. With `mode="lines"` and no `connectgaps=True`, doesn't connect across NaN y-values.

**`plotly.Bar`** — bar chart. With `barmode="group"` (in `update_layout`), multiple Bar traces are grouped side-by-side rather than stacked.

**`psutil`** — Python library for inspecting running processes. We use it to read CPU% and memory of the cardano-node and cardano-db-sync processes.

**`psycopg2`** — Python's PostgreSQL driver. We use `psycopg2-binary` for ease of installation.

**`pytest`** — Python's test framework. We use it for the test suite in `tests/`.

**`ruff`** — Python linter + formatter. Configured in `pyproject.toml`. Runs in CI.

**`mypy`** — Python static type checker. Configured in `pyproject.toml`. Runs in CI.

---

## Project-specific terms

**`--db-sync-ver` / `--node-ver`** — your label for a particular run. Used to tag rows in the SQLite stats DB so multiple runs of the same env can be distinguished. Free-form string; pick something that distinguishes the run from others you'll compare it against.

**`--env`** — the Cardano environment: `mainnet`, `preprod`, or `preview`. Determines network magic and SQLite DB filename.

**`--interval`** — sampling interval in seconds. Default 10. Smaller = more granular data but more overhead.

**`--metrics cpu_ram | ingest | tables | all`** — for the plot scripts, which graph kind to produce. `cpu_ram` is the default (memory/CPU); `ingest` adds tip lag / DB size / rates / UTXO; `tables` shows hot-table row counts; `all` produces them all.

**`--x-axis slot | time`** — for the plot scripts, whether to use `slot_no` or wall-clock `ts` on the x-axis. Slot is best for chain-content comparison; time is best for diagnosing stalls.

**`--with-p95`** — for the report, whether to compute p95 tx size per epoch. Expensive on mainnet; off by default.

**`--skip-slow`** — for the report, whether to skip the expensive per-epoch fetchers (plutus adoption + cumulative distinct assets). Saves time at the cost of two report panels.

**`--json`** — for the monitors, emit one JSON object per sample on stdout instead of the pipe-separated form.

**`db_sync_version` / `node_version`** — the SQLite tables that hold one row per sample tagged with the version label. Their distinct `version` column is the list of runs.

**`era_for(proto_major)`** — function in `_common.py` mapping a protocol major number to its era name.

**Gap break** — a NaN-valued marker row inserted between consecutive samples that are more than 5× the sample interval apart. Forces plotly to break the line at that point so visualization doesn't fabricate values across sampling gaps.

**`HOT_TABLES`** — constant in `db-sync-monitor.py` listing the postgres tables we sample row counts of every interval. Currently: `block`, `tx`, `tx_out`, `ma_tx_out`, `ma_tx_mint`, `multi_asset`, `datum`, `redeemer`, `script`.

**`ingest_metrics` / `node_ingest_metrics`** — the SQLite tables holding sampled chain progress data (tip lag, db size, rates, UTXO count on the db-sync side; slot/epoch/era/sync_progress on the node side). Newer than `memory_metrics`/`cpu_metrics`; older monitor runs don't have these rows.

**`memory_metrics` / `cpu_metrics`** — the original SQLite tables, holding psutil samples of the process under observation.

**`run_label`** — the full label written into the `version` column of every row. Format: `cardano-db-sync <ver> <env>` or `cardano-node <ver> <env>`.

**`table_rowcounts`** — the SQLite table holding `pg_class.reltuples` samples per hot table per sample interval. Newer than the original schema.

**`UTXO tracking`** — db-sync's config-gated population of `tx_out.consumed_by_tx_id`. When enabled, makes UTXO-set-size queries cheap. The monitor probes for it at startup.

**`utc_timestamp(dt)`** — `_common.py` helper that returns POSIX-UTC seconds from a datetime, treating naive datetimes as UTC (the fix for the timezone bug).

**`wait_for_schema`** — db-sync-monitor method that polls until the `block` table exists. Prevents the "relation does not exist" noise during the first few iterations of a fresh sync.

---

## Subtle distinctions worth knowing

These are pairs of terms that look similar but mean different things — getting them wrong has caused bugs in this project.

**`block.proto_major` vs `epoch_param.protocol_major`** — the first is per-block, written by the block producer, and can include *signaling* for the next era's protocol version before the hard fork actually activates. The second is per-epoch, written by the ledger, and reflects the actual active protocol version. For era classification, always use the second. For "voting" analysis, use the first.

**Chain time vs wall-clock time** — chain time advances at one slot per second (whether or not a block is produced); wall-clock time is what your computer's clock says. During catch-up sync they diverge wildly (chain time advances much faster than wall-clock). At tip they're approximately equal.

**Tip lag vs sync percent** — tip lag is `wall_clock_now - latest_block_time`, in seconds. Sync percent is `(latest_block_time - first_block_time) / (now - first_block_time) × 100`, a ratio. Tip lag is a more honest "are we caught up" signal because it doesn't saturate near 100% the way sync percent does.

**Wall-clock duration of an epoch (per-epoch sync time) vs chain duration of an epoch** — chain duration is fixed at 432,000 seconds (5 days). Wall-clock duration is how long it actually took the node/db-sync to process that epoch — much less during catch-up.

**Mean vs median vs p95** — see [04 — Statistics primer](04-statistics-primer.md). Mean is the total-over-count; median is the middle of the sorted values; p95 is the value below which 95% of observations fall.

**Append vs overwrite** — the monitors *append* to SQLite tagged with the version label; they never overwrite. If you re-run with the same label, the new samples join the old ones, separated only by the gap in `ts`. This is intentional (and matches Prometheus/InfluxDB convention) but can produce confusing visuals if you forget — see the gap-break logic.

**`COUNT(*)` vs `pg_class.reltuples`** — first is exact, requires a scan. Second is the planner's estimate, updated periodically by ANALYZE. For monitoring use, reltuples is plenty good and ~5 orders of magnitude faster on a big table.

---

That's the lot. If a term in any of the project docs isn't here, the most likely place to look is the [README](../README.md) (operator perspective) or the inline docstrings in `scripts/_common.py` and `scripts/_db_sync_queries.py` (developer perspective).
