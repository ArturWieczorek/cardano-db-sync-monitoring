# 05 — Database internals

The "why is the SQL written that way" doc. Covers SQLite WAL deeper than the project README, Postgres index theory in the context of our queries, and the specific optimizations that made the mainnet-safe report possible.

## SQLite WAL mode

### The two journaling modes

SQLite has to maintain atomicity (a transaction either fully happens or doesn't). It uses a *journal* — a separate file that records enough information to undo or replay changes. Two modes are common:

**Rollback journal (default):**

1. Before changing the main DB file, write the original page contents to a journal file.
2. Modify the main DB file in place.
3. On commit: delete the journal (changes are now durable).
4. On crash/rollback: restore pages from the journal.

While a transaction is active, the journal is on disk and the main DB is being modified. **Readers need a snapshot of the main DB before changes — and writers are touching it — so readers and writers block each other.**

**Write-ahead log (WAL):**

1. Don't change the main DB during a transaction. Instead, append new page contents to a separate `.db-wal` file.
2. On commit: mark the WAL entry as final.
3. Readers consult the WAL for any pages newer than their snapshot point; they see a consistent snapshot, ignoring uncommitted entries.
4. Periodically, "checkpoint" — copy committed WAL pages into the main DB file.

**Writers don't touch the main DB during their transaction.** They only append to the WAL. **Readers don't touch the WAL during their query.** They use it read-only as needed. **No mutual exclusion.**

### Why we need WAL for this project

Concurrent reads and writes are our standard pattern:

- The monitor writes one or more rows every 10 seconds, indefinitely.
- The plot script reads the same DB for ad-hoc visualization, often mid-sync.
- The report script reads (a different DB but conceptually similar) — well, the report reads Postgres, not SQLite, but the same principle applies to anyone who wants to query SQLite while the monitor is running.

Under rollback journal mode, a 30-second `pd.read_sql_query` would block the monitor from inserting for 30 seconds. We'd lose 3 samples to nothing. The chart would have a 30-second gap *caused by the chart's own reading*.

WAL makes those operations independent. Hence `PRAGMA journal_mode=WAL` in `_common.init_sqlite_schema`.

### The three files

Once the DB is in WAL mode you'll see three files on disk per logical database:

| File | Role |
|:---|:---|
| `<name>.db` | Main database file. Holds checkpointed (durable) data. |
| `<name>.db-wal` | Write-ahead log. Holds pending committed writes that haven't been checkpointed yet. |
| `<name>.db-shm` | Shared memory file. Coordinates between readers and writers about WAL state. |

The README documents the operator-side implications (don't delete one of them while the monitor's running; use `sqlite3 .backup` for snapshots). Here we cover the mechanics.

### Checkpointing

A **checkpoint** moves WAL contents into the main DB file. SQLite does it automatically:

- **PASSIVE checkpoint** on every commit, opportunistically. Cheap. Merges what it can without disrupting readers. Doesn't wait.
- **FULL or RESTART checkpoint** when WAL size exceeds a threshold (default 1000 pages ≈ 4MB). Briefly waits for active readers to finish their snapshot, then merges everything.
- **TRUNCATE checkpoint** when the last connection closes. Merges, then resets the WAL file to 0 bytes.

A growing WAL means writes have outpaced checkpoints. For our workload this almost never happens (one tiny insert per 10 seconds vs. multi-MB checkpoint threshold), but if you were to write much faster, you'd see WAL inflation followed by automatic forced checkpoints.

Manual checkpoint if you ever need one (e.g., before backing up via filesystem `cp`):

```bash
sqlite3 data/cardano-node/preprod.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

After this, the `.db-wal` shrinks to 0 bytes and all data is in the main `.db`. A safe `cp` snapshot is then complete.

### When WAL isn't right

Mostly it's strictly better. The cases where rollback journal wins:

- **No concurrent readers ever.** Pure single-process write-only workloads. Rollback journal is slightly simpler and has fewer files. Not us.
- **Network filesystems.** WAL relies on shared memory mapping, which is broken on NFS. Not us — we're on local disk.
- **Very write-heavy with no need for read isolation.** Rollback journal's commits go straight to the main file; WAL needs an extra checkpoint step. Not us — our writes are infrequent and tiny.

For this project: WAL is unambiguously the right choice.

## Postgres index theory in our queries

We optimized several queries during development. The optimizations all come back to "use indexes; don't do work proportional to the table size when you only need a constant amount of data."

### Indexes 101

An **index** is a separate data structure (typically a B-tree) ordered by some column(s). Postgres can use it to find rows matching a predicate without scanning the whole table.

A primary key implies an index. Other indexes have to be created explicitly with `CREATE INDEX`.

When you write `SELECT * FROM block WHERE block_no = 12345`, Postgres has two options:

1. **Sequential scan**: read every row, check if `block_no == 12345`. O(N) work, where N is the total rows in the table.
2. **Index scan**: navigate the B-tree on `block_no` to find the matching row. O(log N) work.

For a tiny table the seq scan can be faster (no index navigation overhead). For a large table the index scan wins by orders of magnitude.

`EXPLAIN` shows you which one Postgres picked:

```sql
EXPLAIN SELECT * FROM block WHERE block_no = 12345;
-- Look for "Index Scan using ..." (good) or "Seq Scan" (bad on a big table)
```

### MAX(indexed_col) is special

Naively, `SELECT MAX(time) FROM block` requires scanning every row to find the largest `time`. Even with an index on `time`, Postgres might not use it (the planner has to recognize the optimization).

We use the equivalent indexed query:

```sql
SELECT time FROM block
WHERE block_no IS NOT NULL
ORDER BY block_no DESC LIMIT 1;
```

This works because `block_no` is monotonic with insertion order, so the latest block (max block_no) is also the latest in time (max time). The index on `block_no` lets Postgres find it in O(log N) — actually a single index leaf lookup.

### The MIN/MAX seq-scan bug we hit

The original monitor used `MIN(time)` and `MAX(time)` on the `block` table to compute `sync_percent`. There's no index on `time`, so Postgres seq-scanned `block` on every sample — 11M rows on mainnet, hundreds of MB of disk reads, every 10 seconds.

The fix: cache `first_block_time` once at startup, derive sync_percent from `tip_time - first_block_time` (and `now - first_block_time`) computed in Python. Two O(1) index lookups (the first time and the current tip), no seq scans.

This single change reduced monitor postgres load from "constantly hammering" to "negligible," which was important because the monitor was creating its own measurement bias: every read it did slowed down db-sync, inflating the very sync time we were measuring.

You can see the pattern in `db-sync-monitor.py`'s `get_tip()` and `get_first_block_time()` — both use the indexed two-hop pattern.

### `pg_class.reltuples` — fast approximate counts

`COUNT(*) FROM tx_out` on mainnet's 150M+ rows takes minutes (it has to scan the whole table). For the per-sample hot-table row counts, we use a much cheaper alternative:

```sql
SELECT relname, reltuples::bigint FROM pg_class
WHERE relkind = 'r' AND relname = ANY(%s);
```

`pg_class.reltuples` is a number Postgres keeps as part of the planner's statistics. It's updated during `ANALYZE` (which runs automatically via autovacuum, typically every few minutes for active tables). It's an estimate, not exact — it can lag the true count by a few percent and updates in steps rather than continuously.

For monitoring purposes (which tables are growing, do they grow at similar rates), reltuples is plenty good. For accuracy-critical "exactly how many rows" use COUNT(*), but accept the cost.

The trade-off in our context: 150M-row scan once per 10-second sample would be catastrophic. A pg_class lookup per sample is sub-millisecond. We chose accuracy-of-shape over accuracy-of-value, which is appropriate for a chart.

### PERCENTILE_CONT — when it's expensive

We compute p95 tx size in the report behind `--with-p95` because:

```sql
PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY t.size)
```

requires Postgres to sort *every* `tx.size` value in the group, take the value at the 95th-percentile position, interpolate. On mainnet with 100M tx rows, that's tens of millions of `tx.size` values to sort per epoch group — 5–20 minutes total.

There are sketch algorithms (t-digest, HDR histogram) that approximate percentiles in linear time without sorting. Postgres doesn't have them built-in (there's `tdigest` as an extension). We chose the exact computation gated behind a flag rather than approximations.

For the avg-tx-size which we always compute: it's just `SUM(size)/COUNT(*)`, both of which can use streaming aggregation — Postgres reads the rows once, accumulating sum and count, then divides. O(N) but with a tiny constant factor.

### `EXISTS (SELECT 1 ... LIMIT 1)`

Several places in the code probe "does this table have any row matching the predicate?" — the canonical fast idiom is:

```sql
SELECT EXISTS (SELECT 1 FROM tx_out WHERE consumed_by_tx_id IS NOT NULL);
```

The `EXISTS` short-circuits on the first matching row. If a row exists, Postgres stops searching. With an appropriate index (or a partial index on `consumed_by_tx_id IS NOT NULL`), this is sub-millisecond regardless of table size.

Without the index, EXISTS can degenerate to a seq scan looking for the first match. If you expect "no" 99% of the time and there's no index, this can be expensive — which leads us to the next topic.

### `statement_timeout` as a safety net

The UTXO-tracking probe is exactly the "EXISTS without index might be slow" case:

```sql
SELECT EXISTS (SELECT 1 FROM tx_out WHERE consumed_by_tx_id IS NOT NULL);
```

If `consumed_by_tx_id` is populated and indexed → instant.
If `consumed_by_tx_id` is populated but not indexed → fast (heap fetch on first matching row).
If `consumed_by_tx_id` is *not* populated and there's no index → seq scan through 150M rows looking for the first non-NULL.

The third case is the danger. We can't predict it from outside (the operator may or may not have the column populated; may or may not have an index).

Safety net: set `statement_timeout = 5000` on this specific query. If it takes more than 5 seconds, Postgres cancels it. We catch `QueryCanceled` in Python and treat it as "assume disabled."

This is a defensive pattern worth knowing: when you don't know a query's worst case, cap its time and fail gracefully.

### The `consumed_by_tx_id IS NULL` trap

For computing UTXO set size:

```sql
SELECT COUNT(*) FROM tx_out WHERE consumed_by_tx_id IS NULL;
```

Looks innocuous. But:

- If `consumed_by_tx_id` is **not populated at all**, every row has NULL — COUNT(*) returns the total `tx_out` count. **Meaningful-looking but wrong.** UTXO ≠ all outputs ever produced.
- If it **is populated** and you have a partial index `CREATE INDEX ON tx_out(consumed_by_tx_id) WHERE consumed_by_tx_id IS NULL`, this is fast.
- If it **is populated** without a partial index, Postgres scans tx_out (slow on mainnet).

Our code only runs this query if `utxo_tracking_enabled` returned True (i.e., we observed at least one non-NULL value). That rules out the "all NULL because the feature is off" case. We still depend on the operator having an appropriate index for the count to be fast at scale.

## Streaming aggregation vs sorting

A useful mental model for "is this query going to be expensive."

**Streaming aggregations** (SUM, COUNT, AVG, MIN, MAX) — process each row once, maintaining a small accumulator. O(N) time, O(1) memory.

**Sorting aggregations** (DISTINCT, PERCENTILE_CONT, ORDER BY without LIMIT) — need all values in memory or to sort them on disk. O(N log N) time, O(N) memory (or O(N log N) disk passes).

**Per-group aggregations** (GROUP BY) — Postgres can do this either way. If there are few groups, it hashes (O(N) time). If there are many groups, it may sort first (O(N log N)).

When you see a slow query, the question is which category it falls into. The `EXPLAIN ANALYZE` output tells you.

In our project:

- `fetch_epoch_stats` (per-epoch sums) — streaming over rows, grouped by `epoch_no`. Fast.
- `fetch_epoch_plutus` (per-epoch distinct counts) — Postgres has to deduplicate `t.id` per epoch. Internally it groups by (epoch, t.id), then counts the groups. Bigger working memory, slower.
- `fetch_epoch_stats` with p95 — adds the sort. Multiple-minutes slow.
- `fetch_epoch_distinct_assets` — needs to deduplicate `ident` across the whole table, then group by first epoch. Sorting + grouping. Slow.

## EXPLAIN ANALYZE

When in doubt, ask Postgres:

```sql
EXPLAIN ANALYZE
  SELECT epoch_no, COUNT(*) FROM block GROUP BY epoch_no;
```

Output (abbreviated):

```
HashAggregate  (cost=... rows=... loops=1)
  ->  Seq Scan on block  (cost=... rows=11000000)
Planning Time: 0.5 ms
Execution Time: 8.2 s
```

This tells you:
- The chosen plan (`HashAggregate` over `Seq Scan`).
- The estimated cost (used by the planner).
- Actual row counts and time.

If `Execution Time` is in the seconds when you expected milliseconds, look at what the plan is doing. Usually it's a seq scan when an index existed but wasn't usable for the predicate.

For our queries, `EXPLAIN ANALYZE` is the canonical way to verify "yes, this uses the index" vs "no, this scans everything."

## Postgres autovacuum

Worth mentioning briefly. Postgres's MVCC creates "dead tuples" — old row versions that need cleaning up after concurrent transactions finish with them. **Autovacuum** runs periodically to reclaim space and update statistics (`pg_class.reltuples` among them).

During autovacuum:
- Reads and writes still work (it's not blocking under normal conditions).
- IO and CPU are consumed in the background.
- Some queries can be momentarily slower.

If you see CPU/RSS spikes in your monitor that don't correspond to anything in db-sync's behaviour, autovacuum is one explanation. They're usually brief (seconds) and not a problem for steady-state monitoring.

## Connection management

We use `@contextmanager` decorators on both `_pg` (in db-sync-monitor.py) and `pg_connect` (in `_db_sync_queries.py`) to ensure connections close on scope exit.

A subtle gotcha: psycopg2's built-in `with psycopg2.connect(...) as conn:` block **commits or rolls back the transaction on exit but does NOT close the connection**. You can verify this is wrong by running a script that creates many `with` blocks and watching the count of open connections grow in Postgres.

The standard `contextmanager` pattern with explicit `try/finally: conn.close()` fixes this. The monitor opens many connections over a long run; without proper closing it would accumulate leaks.

## Summary

- SQLite WAL trades a few extra files for concurrent-reader-writer support — essential for our monitor + ad-hoc plot workflow.
- Postgres indexes let O(1)-ish lookups replace O(N) seq scans. Always check `EXPLAIN ANALYZE` if a query feels slow.
- `pg_class.reltuples` gives cheap approximate row counts; use it when shape matters more than exact value.
- `PERCENTILE_CONT` is exact but O(N log N); gate it behind a flag when running against big tables.
- `statement_timeout` caps the worst case of probes where you can't predict if an index will help.
- `consumed_by_tx_id IS NULL` is only safe to query *after* confirming the feature is populated; otherwise it returns a meaningful-looking but wrong number.
- psycopg2's `with conn:` doesn't close the connection — use an explicit context manager that does.

Next: [06 — Glossary](06-glossary.md).
