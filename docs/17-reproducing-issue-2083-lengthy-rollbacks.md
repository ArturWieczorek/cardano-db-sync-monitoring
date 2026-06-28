# 17 - Reproducing issue #2083 (lengthy rollbacks)

A recipe for reproducing, confirming, and tracking the "lengthy rollback"
regression reported in
[cardano-db-sync #2083](https://github.com/IntersectMBO/cardano-db-sync/issues/2083),
and how this project's rollback tools relate to it.

Read [16 - Compare rollback performance step by step](16-compare-rollback-performance-step-by-step.md)
first for the benchmark mechanics; this doc is the issue-specific overlay.

---

## What the bug was

On large (mainnet-scale) databases, db-sync's rollback runs "find the minimum
id" queries of this shape, one per affected table:

```sql
SELECT id FROM tx       WHERE block_id >= $1 ORDER BY id ASC LIMIT 1;
SELECT id FROM tx_cbor  WHERE tx_id    >= $1 ORDER BY id ASC LIMIT 1;
SELECT id FROM datum    WHERE tx_id    >= $1 ORDER BY id ASC LIMIT 1;
SELECT id FROM tx_metadata WHERE tx_id >= $1 ORDER BY id ASC LIMIT 1;
```

The Postgres planner sometimes chooses to walk the **primary-key** index (id
order) and *filter* on `block_id`/`tx_id`, instead of using the range index
(`idx_tx_block_id`, `idx_..._tx_id`). When the predicate matches few or zero rows
(exactly the rollback boundary case), the PK walk scans a huge portion of a
billion-row table - turning a millisecond lookup into minutes. Reporters saw
rollbacks go from <1 min to **7-10 min**, almost entirely in "Querying minimum
transaction ID" and "Deleting block data".

It is a **planner / statistics** problem, not a simple code bug: `VACUUM ANALYZE`
did not reliably fix it, and the same query could pick the good plan on one
database and the bad plan on another. That's why the real fix (below) rewrote the
queries to **force** the right index rather than depend on the planner's
estimate.

## Affected and fixed versions

| Version | Status |
|:---|:---|
| 13.6.0.5 | Susceptible (reporters saw the same bad plan / multi-minute rollbacks) |
| 13.7.0.0 - 13.7.0.1 | **Buggy** (the version in the issue report) |
| **13.7.0.2 and later** (incl. 13.7.1.0, 13.7.2.1) | **Fixed** - CHANGELOG: "Fix slow rollbacks caused by suboptimal query plans on large tables [#2083]" |

So a current binary (13.7.x >= 0.2) and a database it synced will roll back
*fast*. Reproducing the slow behaviour means deliberately recreating the
conditions that made the planner mis-choose.

## What it takes to reproduce (all three needed)

1. **Mainnet scale.** The bad plan only hurts on tables with tens to hundreds of
   millions of rows. Preview/preprod stay fast on either plan.
2. **The pre-fix query, or the conditions that fool the planner.** Either run the
   **13.7.0.1** binary (whose query text predates the fix), *or* recreate the
   statistics conditions that make even the old query mis-plan - most reliably
   **freshly (re)built indexes with stats that haven't settled** (this is exactly
   the "restore snapshot -> rebuild indexes -> start db-sync -> rollbacks within
   20 minutes" sequence reporters described), or a large table the planner has no
   good stats for.
3. **The affected tables populated.** The slowest query in the report was on
   **`tx_cbor`**, which only has data if synced with `tx_cbor` enabled. The
   project's standard config disables it, so a stock DB has an empty `tx_cbor`
   and won't show that query. `tx`, `datum`, `tx_metadata` are populated under
   the standard config and are also candidates.

> The node version is irrelevant to reproduction: the reliable path uses
> `cardano-db-tool rollback`, which needs only a Postgres connection - no
> cardano-node and no running db-sync. So "does 13.7.0.1 run with node 11.0.1"
> does not arise.

---

## Method A - confirm the root cause directly (fast, version-independent)

### The easy way: `rollback-plan-check.py`

A one-shot script checks all four affected tables at once. It runs only `EXPLAIN`
(plans, never executes), so it's read-only and instant, and it prints a verdict
per table:

```bash
python3 scripts/rollback-plan-check.py --pg-dbname mainnet-dbsync-...
```

```
  table                    rows  index used             verdict
  -------------- --------------  ---------------------- -------
  tx                121,072,744  idx_tx_block_id        GOOD (range index)
  tx_cbor                     0  -                      empty (no data)
  datum              34,859,144  idx_datum_tx_id        GOOD (range index)
  tx_metadata       137,036,032  idx_tx_metadata_tx_id  GOOD (range index)

OK: every populated table uses a range-index plan for the rollback min-id query.
```

It exits `0` if every populated table picks a good (range-index) plan, `1` if any
picks the bad (PK + Filter) plan - so you can use it as a quick pre-upgrade or CI
gate. `--show-plans` prints the full EXPLAIN for each table. (`PGHOST`/`PGPORT`/
`PGUSER` env vars or the `--pg-host/--pg-port/--pg-user` flags select the server.)

### The manual way (one table, to see the raw plan)

You don't need the buggy binary or the script to see the planner problem - just
ask Postgres which plan it would use, with `EXPLAIN` (plan only, instant):

```bash
DB=mainnet-dbsync-...           # your mainnet db-sync database
MX=$(psql -d "$DB" -tAc "SELECT max(block_id) FROM tx;")
psql -d "$DB" -c "EXPLAIN SELECT id FROM tx WHERE block_id >= $((MX+100)) ORDER BY id ASC LIMIT 1;"
```

**Reading the plan:**
- **Good:** `Index Scan using idx_tx_block_id ... Index Cond: (block_id >= ...)`.
- **Bad (the bug):** `Index Scan using tx_pkey ... Filter: (block_id >= ...)` - the
  PK index with a *Filter* instead of an *Index Cond*. On a populated table this
  is the multi-minute plan.

Repeat for `datum` / `tx_metadata` (filter column `tx_id`, index `idx_..._tx_id`)
and `tx_cbor` if you sync it. Only escalate to `EXPLAIN ANALYZE` (which actually
runs the query - cap it with `SET statement_timeout='90s';` first) once `EXPLAIN`
shows a bad plan, since the bad plan is the slow one.

### Worked example (observed on this machine, 2026-06)

On a 13.7.1.0-synced, well-analyzed 505 GB mainnet DB:

| Table | Rows | Plan chosen | Time |
|:---|---:|:---|---:|
| `tx` | ~121M | `idx_tx_block_id` (good) | 0.06 ms |
| `datum` | ~35M | `idx_datum_tx_id` (good) | 0.08 ms |
| `tx_metadata` | ~137M | `idx_tx_metadata_tx_id` (good) | 0.07 ms |
| `tx_cbor` | 0 (empty) | `tx_cbor_pkey` + Filter (**bad shape**) | instant (empty) |

Interpretation: this database does **not** currently reproduce the slow rollback
- the planner picks the good index on every populated table, consistent with
being on a fixed version with fresh statistics. The only bad-shaped plan was on
the **empty, never-analyzed** `tx_cbor`, which is harmless precisely because it's
empty; on a populated `tx_cbor` that same plan is the regression. This is the
whole point of the bug being statistics-dependent.

---

## Method B - reproduce the symptom end to end (and prove the fix)

This measures an actual rollback's deletion time, the way #2083 was felt.

1. Get a **mainnet** database synced with the affected tables populated (include
   `tx_cbor` if you want the worst case). Note its tip slot `S`.
2. Obtain the **pre-fix `cardano-db-tool` 13.7.0.1** binary. (Caveat: the fix was
   also back-patched onto the 13.7.0.1 branch, so a freshly downloaded "13.7.0.1"
   may already be fixed - use the original pre-fix release artifact, or build from
   a commit before the fix.)
3. Run the benchmark on a snapshot, rolling back a realistic depth, and compare to
   a fixed binary (13.7.0.2+). This is the doc-16 procedure, with the two binaries
   being pre-fix vs post-fix:

```bash
# pre-fix (expect minutes if conditions reproduce the bad plan)
python3 scripts/db-sync-rollback-benchmark.py \
  --env mainnet --db-sync-ver 13.7.0.1-prefix \
  --db-tool /path/to/13.7.0.1/cardano-db-tool \
  --from-slot S --to-slot TARGET --reps 3 \
  --pgpassfile config/pgpass-bench \
  --restore-cmd 'dropdb --if-exists bench && createdb bench --template snap'

# fixed (expect seconds)
python3 scripts/db-sync-rollback-benchmark.py \
  --env mainnet --db-sync-ver 13.7.2.1 \
  --db-tool /path/to/13.7.2.1/cardano-db-tool \
  --from-slot S --to-slot TARGET --reps 3 \
  --pgpassfile config/pgpass-bench \
  --restore-cmd 'dropdb --if-exists bench && createdb bench --template snap'
```

A large gap between the two medians reproduces #2083 and demonstrates the fix.

> Note on statistics: a `createdb --template` snapshot copies the source's
> statistics, so it inherits whatever plan the source would pick. To reproduce the
> *stale-stats* trigger, recreate the reporters' condition - restore, rebuild the
> relevant indexes, and run **before** an `ANALYZE` settles - rather than relying
> on a clean template clone.

---

## Method C - watch a live db-sync (full diagnostic detail)

`cardano-db-tool` runs with logging off, so Method B gives the total deletion
time but not the per-step breakdown. To see the granular "Querying minimum
transaction ID... / Deleting block data..." steps the issue log shows, run a
**live** db-sync of the affected version with this project's monitor tailing its
log (see [doc 15, Case 2](15-tracking-and-benchmarking-rollbacks.md#case-2---also-capture-exact-deletion-timing--per-table-counts)),
and/or enable Postgres slow-query logging to catch the offending statements
directly:

```sql
ALTER SYSTEM SET log_min_duration_statement = 1000;  -- log queries over 1s
SELECT pg_reload_conf();
```

The monitor records each rollback's deletion duration and per-table delete counts
into `rollback_events` / `rollback_table_deletes`, so a multi-minute deletion
shows up directly.

---

## How this project's tools relate to #2083

- **Detection / regression-tracking:** the benchmark and monitor are exactly the
  instruments for this class of bug. If a future version reintroduced a slow
  rollback query, the doc-16 cross-version benchmark would show its deletion
  median jump, and the live monitor would record long `delete_duration_sec`. That
  is the reason this feature exists.
- **They do not, by themselves, recreate the planner's bad mood.** Reproducing the
  *slow* behaviour needs the data scale, the pre-fix query, and the statistics
  conditions above. The tools then *measure* it.
- **Confirming the specific cause** (planner picking the PK index) is a one-line
  `EXPLAIN` (Method A), independent of these tools and of the db-sync version.
