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
| 06 | [Glossary](06-glossary.md) | Quick term reference — Cardano, db-sync, postgres, sqlite, python, project-specific. |

## Reading paths by goal

- **"I want to understand the A/B comparison output"** → 01 → 02 → 03
- **"I want to understand the statistics behind 'p95 tx size' and 'sync rate'"** → 04
- **"I want to understand why the SQL queries are written the way they are"** → 05
- **"I keep hitting terms I don't know"** → 06, used as a lookup
- **"I want the full theoretical context"** → read everything in order

The docs deliberately overlap with the main README in a few places — the docs explain *why* a thing exists; the README explains *how to use it*. When you need both, the docs reference the relevant README section.
