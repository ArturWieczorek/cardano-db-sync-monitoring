# Documentation

This is the *why* and *how to read* docs, complementing the operator-focused [project README](../README.md) (which is the *how to run*).

Aimed at someone who is competent with command-line tools but new to performance monitoring as a discipline. Concretely: you can run the scripts and see graphs, but you want to understand what the graphs mean, what they can and can't tell you, and why we made the design choices we did.

Read these in order if you're new; pick one if you have a specific question.

| # | File | What it covers |
|:---:|:---|:---|
| 01 | [Time-series fundamentals](01-time-series-fundamentals.md) | What "monitoring" actually is. Why we sample over time. Slot-axis vs time-axis with worked examples. Why naive line plots mislead. |
| 02 | [Cardano domain primer](02-cardano-domain-primer.md) | Just enough Cardano to read the graphs: slot / epoch / era, chain-time vs wall-clock, why catch-up sync is faster than real time. |
| 03 | [Graph catalog](03-graph-catalog.md) | One entry per chart we produce. Source columns, what healthy looks like, what a regression looks like, common misreads. |
| 04 | [Statistics primer](04-statistics-primer.md) | Mean vs median vs p95 with worked tx-size examples. Derivatives. Sampling assumptions. Comparing distributions fairly. |
| 05 | [Database internals](05-database-internals.md) | SQLite WAL mechanics. Postgres index theory. `pg_class.reltuples`. `PERCENTILE_CONT` cost. `consumed_by_tx_id` trap. |
| 06 | [Glossary](06-glossary.md) | Quick term reference - Cardano, db-sync, postgres, sqlite, python, project-specific. |
| 07 | [System performance primer](07-system-performance-primer.md) | Agnostic performance-engineering foundations: CPU/RAM/disk, swap & page cache, CPU% over 100, SSD vs HDD, io_uring, LUKS/dm-crypt, why separate disks matter, I/O- vs CPU- vs memory-bound, PSI & how to measure, OOM/segfault/EIO. Ends with Cardano LSM vs in-memory backend. |
| 08 | [Data access and databases](08-data-access-and-databases.md) | Newcomer on-ramp to how the code talks to databases: client/server (Postgres) vs embedded (SQLite), drivers and cursors, connection lifecycle and pooling, transactions/autocommit, isolation levels and why, parameterized queries, Postgres vs SQLite WAL, the end-to-end data flow, and the data-shaping algorithms. Includes a line-by-line walkthrough of the monitor's connection-reuse code. |
| 09 | [Reading and smoothing bursty rates](09-reading-and-smoothing-bursty-rates.md) | Why the block/tx rate panels look spiky, and how to deal with it. Measuring spikiness from first principles (coefficient of variation, percentiles, jumpiness) with real mainnet numbers; noise vs signal; and which display techniques actually help - rolling mean vs rolling median, logarithmic axes, percentile clipping. |
| 10 | [Generating reports (cardano-db-sync)](10-generating-reports.md) | How to use `db-sync-stats-report.py` to auto-build the per-environment LSM-vs-InMemory comparison report. The version-token naming model (and the common "unrecognized arguments" mistake), worked examples, output layout, md/PNG vs interactive HTML, `--html-max-points`, why comparisons use the slot axis, and troubleshooting. |
| 11 | [Report generator internals](11-report-generator-internals.md) | How the report generator works inside, step by step: the one-way data flow, the three-file split (`db-sync-plot.py` / `_report.py` / the orchestrator), a function-by-function walk through a single run, the design choices and why, and how to extend it (incl. the cardano-node report). |
| 12 | [Useful queries](12-useful-queries.md) | A copy-paste SQL cookbook for the SQLite stats DBs (both roles): peak RAM, on-disk / `lsm/` size, CPU, db-sync ingest & table sizes, node sync state & RTS heap, LSM-vs-InMemory side-by-side, gap detection. Units pre-converted to GiB. |
| 13 | [Generating reports (cardano-node)](13-node-report-generation.md) | The cardano-node sibling of doc 10: how to use `node-stats-report.py` to auto-build the per-environment LSM-vs-InMemory node report. Same version-token model and formats; node's metric set (CPU & RAM, Ingest, On-disk Size, RTS), the epoch-axis Ingest figure, and the optional auto-skipped Disk/RTS sections. |
| 14 | [Reading the rollback graphs](14-reading-the-rollback-graphs.md) | Plain-language, step-by-step guide to the `--metrics rollback` plot: what a rollback is, the three panels (queue length, node-db gap, event duration), the rollback markers, why a panel can be empty, how to spot a regression, and where the raw numbers live. |

## Reading paths by goal

- **"I want to understand the A/B comparison output"** → 01 → 02 → 03
- **"I want to understand the statistics behind 'p95 tx size' and 'sync rate'"** → 04
- **"The block/tx rate panels look spiky - how do I read them, and should I smooth them?"** → 04 → 09
- **"I want to understand why the SQL queries are written the way they are"** → 05
- **"I'm new to databases and want to understand how the code connects and moves data (psycopg, SQLite, pooling, transactions, isolation levels, WAL)"** → 08 → 05
- **"I keep hitting terms I don't know"** → 06, used as a lookup
- **"I want to understand what I/O-bound means, swap, PSI, io_uring, or why my disk/encryption setup matters"** → 07
- **"I want to generate (or understand) the LSM-vs-InMemory comparison reports"** → 10 (db-sync) or 13 (cardano-node) → 11 (internals, shared)
- **"I just need a query for peak RAM / disk size / row counts from the stats DB"** → 12
- **"I want the full theoretical context"** → read everything in order

The docs deliberately overlap with the main README in a few places - the docs explain *why* a thing exists; the README explains *how to use it*. When you need both, the docs reference the relevant README section.

## Post-mortems

Write-ups of notable bugs - what broke, why, and what now prevents a recurrence. Read these to understand standing invariants the code relies on.

| Date | Write-up |
|:---|:---|
| 2026-06-06 | [Empty disk plot and invisible RTS data](postmortems/2026-06-06-empty-disk-and-invisible-rts.md) - a fixed gap-break threshold vs. a slower collector, a mistyped version label the picker couldn't surface, and a stale `rename-version.py`. Establishes the `VERSION_KEYED_TABLES` single-source-of-truth invariant. |
| 2026-06-08 | [Collectors crashing on "database is locked"](postmortems/2026-06-08-database-is-locked-collector-crash.md) - several collectors share one per-env SQLite file, WAL serializes writers, and the default 5s busy timeout plus an uncaught `OperationalError` let one lost race kill a multi-day run. Establishes the `connect_writer` / survive-a-transient-`OperationalError` invariant. |
