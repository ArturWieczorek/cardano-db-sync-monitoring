# 12 - Useful queries (SQLite stats DBs)

A lookup-style cookbook of the SQL you actually reach for day to day against the
**stats databases** the monitors write. Copy a query, swap in your version
label, run it. Every query that returns bytes or MiB is already converted to
**GiB** so you don't do mental math.

> Prefer a one-shot formatted summary over running these by hand?
> `python3 scripts/stats-summary.py --env <env> [--version <token>]` runs the
> headline queries below and prints them in GiB - see the README's
> `# stats-summary.py` section. This doc is the underlying raw SQL for when you
> need something custom.

This is for the **SQLite stats DBs**, not the Postgres db-sync database:

```
data/cardano-db-sync/<env>.db      # written by db-sync-resource-monitor.py & friends
data/cardano-node/<env>.db         # written by node-resource-monitor.py & friends
```

> These files are **read-only as far as you're concerned** - the monitors own
> them. To remove or rename a run's data, don't hand-edit; use the recipes in the
> README (`# Removing / renaming version stats`) or `scripts/rename-version.py`.

## Before you start

Open a DB and turn on readable output:

```bash
sqlite3 data/cardano-db-sync/preprod.db
sqlite> .headers on
sqlite> .mode column
```

Or run one query straight from the shell:

```bash
sqlite3 -header -column data/cardano-node/mainnet.db "SELECT ..."
```

**Every run is tagged with a version label** of the form
`cardano-db-sync <ver> <env>` or `cardano-node <ver> <env>` - e.g.
`cardano-db-sync 13.7.1.0-node-11.0.1 preprod`. You filter on the **full label**
in `WHERE version = '...'`. (See [10 - Generating reports](10-generating-reports.md)
and the [glossary](06-glossary.md) for the label model; don't know which labels
exist? See the first query below.)

**Units stored in the tables** (so you know what to divide by):

| Column(s) | Stored as | To GiB |
|:---|:---|:---|
| `rss`, `vms`, `uss`, `pss`, `swap`, `shared` (memory_metrics) | MiB | `/ 1024.0` |
| `total_bytes`, `lsm_bytes` (disk_metrics) | bytes | `/ 1024.0/1024/1024` |
| `db_size_bytes` (ingest_metrics, db-sync) | bytes | `/ 1024.0/1024/1024` |
| `cpu_percent` | percent (can exceed 100 = multi-core) | - |
| `value` (rts_metrics) | depends on the metric (bytes for `*Bytes*`, etc.) | `/ 1024.0/1024/1024` for byte metrics |

---

## Listing & housekeeping

**Which version labels are in this DB?**

```sql
SELECT DISTINCT version FROM memory_metrics ORDER BY version;
```
> `memory_metrics` exists in both roles. A run collected *only* by the disk or
> RTS collector won't have memory rows - check `disk_metrics` / `rts_metrics`
> too, or use `python3 scripts/db-sync-plot.py --env <env> --list`.

**How many samples, and over what span / wall-clock duration?**

```sql
SELECT COUNT(*)                                    AS samples,
       MIN(ts)                                     AS first_sample,
       MAX(ts)                                     AS last_sample,
       ROUND((julianday(MAX(ts)) - julianday(MIN(ts))) * 24, 2) AS hours
FROM memory_metrics
WHERE version = 'cardano-db-sync 13.7.1.0-node-11.0.1 preprod';
```
> `julianday()` turns the ISO timestamps into days; `* 24` gives hours. Example:
> `5677 samples ... 8.09 hours`.

---

## Memory & CPU (both roles)

These tables are identical in the node and db-sync DBs, so the same queries work
against either file.

**Peak RAM (MiB and GiB)** - the headline number:

```sql
SELECT ROUND(MAX(rss), 1)        AS peak_mib,
       ROUND(MAX(rss) / 1024.0, 2) AS peak_gib
FROM memory_metrics
WHERE version = 'cardano-db-sync 13.7.1.0-node-11.0.1 mainnet';
```
> Example: `18726.3 MiB → 18.29 GiB`.

**RAM start vs end vs peak** (a quick growth / leak check):

```sql
SELECT
  (SELECT ROUND(rss/1024.0, 2) FROM memory_metrics WHERE version = :v ORDER BY ts ASC  LIMIT 1) AS start_gib,
  (SELECT ROUND(rss/1024.0, 2) FROM memory_metrics WHERE version = :v ORDER BY ts DESC LIMIT 1) AS end_gib,
  (SELECT ROUND(MAX(rss)/1024.0, 2) FROM memory_metrics WHERE version = :v)                     AS peak_gib;
```
> Replace `:v` with the full label in all three places. If `end ≈ peak` and both
> are far above `start`, memory grew and stayed up; if `end` falls back below the
> peak, it was released.

**Other memory facets (peak), in GiB:**

```sql
SELECT ROUND(MAX(rss)/1024.0, 2)    AS rss_gib,
       ROUND(MAX(uss)/1024.0, 2)    AS uss_gib,   -- unique to the process
       ROUND(MAX(pss)/1024.0, 2)    AS pss_gib,   -- proportional (shared split)
       ROUND(MAX(swap)/1024.0, 2)   AS swap_gib
FROM memory_metrics
WHERE version = 'cardano-db-sync 13.7.1.0-node-11.0.1 preprod';
```

**Peak and average CPU:**

```sql
SELECT ROUND(MAX(cpu_percent), 1) AS peak_cpu_pct,
       ROUND(AVG(cpu_percent), 1) AS avg_cpu_pct
FROM cpu_metrics
WHERE version = 'cardano-db-sync 13.7.1.0-node-11.0.1 preprod';
```
> `cpu_percent` can exceed 100 (it's summed across cores).

---

## On-disk size (both roles)

`disk_metrics` is written by `node-db-size-monitor.py` (node DB dir) and
`db-sync-ledger-size-monitor.py` (db-sync ledger-state dir). `total_bytes` is the
whole measured directory; `lsm_bytes` is its `lsm/` subdir (0 on in-memory
builds). Note `disk_metrics.slot_no` is always NULL (the collector doesn't query
for a slot); the plot scripts derive a slot from `memory_metrics` by timestamp
only for the `--x-axis slot` view, so SQL here keys off `ts`.

**Peak total and LSM size, in GiB:**

```sql
SELECT ROUND(MAX(total_bytes)/1024.0/1024/1024, 2) AS peak_total_gib,
       ROUND(MAX(lsm_bytes)  /1024.0/1024/1024, 2) AS peak_lsm_gib
FROM disk_metrics
WHERE version = 'cardano-node LSM-11.0.1 mainnet';
```
> Example: `peak_total 220.35 GiB, peak_lsm 9.21 GiB`.

**Final (most recent) size, plus which directory was measured:**

```sql
SELECT ROUND(total_bytes/1024.0/1024/1024, 2) AS final_total_gib,
       ROUND(lsm_bytes  /1024.0/1024/1024, 2) AS final_lsm_gib,
       path
FROM disk_metrics
WHERE version = 'cardano-node LSM-11.0.1 mainnet'
ORDER BY ts DESC LIMIT 1;
```
> Peak ≠ final when the directory shrank after compaction (e.g.
> `final_total 218.97 GiB, final_lsm 4.42 GiB` vs the 9.21 GiB lsm peak).

---

## db-sync: ingest & table sizes

**Final DB size, max block / tx, UTXO count:**

```sql
SELECT ROUND(db_size_bytes/1024.0/1024/1024, 2) AS db_gib,
       max_block_no, max_tx_id, utxo_count
FROM ingest_metrics
WHERE version = 'cardano-db-sync 13.7.1.0-node-11.0.1 preprod'
  AND db_size_bytes IS NOT NULL
ORDER BY ts DESC LIMIT 1;
```
> `utxo_count` is only populated when UTXO tracking was enabled during
> collection; otherwise it's blank.

**Tip lag (how far behind the chain tip), summary:**

```sql
SELECT ROUND(MIN(tip_lag_sec), 1) AS min_lag_s,
       ROUND(MAX(tip_lag_sec), 1) AS max_lag_s,
       ROUND(AVG(tip_lag_sec), 1) AS avg_lag_s
FROM ingest_metrics
WHERE version = 'cardano-db-sync 13.7.1.0-node-11.0.1 preprod';
```

**Latest row count per hot table (largest first):**

```sql
SELECT table_name, MAX(row_count) AS latest_rows
FROM table_rowcounts
WHERE version = 'cardano-db-sync 13.7.1.0-node-11.0.1 preprod'
GROUP BY table_name
ORDER BY latest_rows DESC;
```
> Example top rows: `ma_tx_out 21.8M, tx_out 20.2M, tx 5.8M, ...`.

---

## node: sync state & RTS / runtime

**Latest epoch / era / sync progress:**

```sql
SELECT epoch_no, era, ROUND(sync_progress, 2) AS sync_progress
FROM node_ingest_metrics
WHERE version = 'cardano-node LSM-11.0.1 mainnet'
  AND epoch_no IS NOT NULL
ORDER BY ts DESC LIMIT 1;
```
> Example: `epoch 635, Conway, 100.0`.

**What RTS metrics were collected?**

```sql
SELECT DISTINCT metric FROM rts_metrics
WHERE version = 'cardano-node LSM-11.0.1 mainnet'
ORDER BY metric;
```

**Peak heap / live bytes (GHC RTS), in GiB:**

```sql
SELECT metric, ROUND(MAX(value)/1024.0/1024/1024, 2) AS peak_gib
FROM rts_metrics
WHERE version = 'cardano-node LSM-11.0.1 mainnet'
  AND metric IN ('cardano_node_metrics_RTS_gcLiveBytes_int',
                 'cardano_node_metrics_RTS_gcHeapBytes_int')
GROUP BY metric;
```
> Example: `gcHeapBytes 5.42 GiB, gcLiveBytes 3.80 GiB`. (Only byte-valued metrics
> should be divided to GiB - GC *counts* and millisecond timings are raw; list
> the names with the query above.)

---

## Comparing two builds (LSM vs InMemory)

**Peak RAM, both builds in one result:**

```sql
SELECT version, ROUND(MAX(rss)/1024.0, 2) AS peak_gib
FROM memory_metrics
WHERE version IN ('cardano-db-sync 13.7.1.0-node-11.0.1 preprod',
                  'cardano-db-sync LSM-13.7.1.0-node-11.0.1 preprod')
GROUP BY version;
```
> Example: InMemory `6.94 GiB` vs LSM `2.31 GiB` - the kind of headline the
> report's comparison section visualises.

**Final disk size, both builds (node DB):**

```sql
SELECT version,
       ROUND(MAX(total_bytes)/1024.0/1024/1024, 2) AS peak_total_gib,
       ROUND(MAX(lsm_bytes)  /1024.0/1024/1024, 2) AS peak_lsm_gib
FROM disk_metrics
WHERE version IN ('cardano-node 11.0.1 mainnet',
                  'cardano-node LSM-11.0.1 mainnet')
GROUP BY version;
```

---

## Housekeeping pointers

**Count a version's rows across every version-keyed table** (before deleting):

```sql
SELECT 'memory_metrics' t, COUNT(*) n FROM memory_metrics WHERE version = :v
UNION ALL SELECT 'cpu_metrics',        COUNT(*) FROM cpu_metrics        WHERE version = :v
UNION ALL SELECT 'disk_metrics',       COUNT(*) FROM disk_metrics       WHERE version = :v
UNION ALL SELECT 'ingest_metrics',     COUNT(*) FROM ingest_metrics     WHERE version = :v
UNION ALL SELECT 'table_rowcounts',    COUNT(*) FROM table_rowcounts    WHERE version = :v;
```
> (db-sync example - for a node DB swap in `node_ingest_metrics`, `rts_metrics`,
> `node_version`.) The authoritative table list per role is
> `VERSION_KEYED_TABLES` in `scripts/_common.py`.

**To rename or delete a version**, use `scripts/rename-version.py` (it touches
every table in one transaction and takes a backup) - see the README. Don't
hand-edit the labels.

**Spot a collection gap** (the monitor was down for a while):

```sql
SELECT ts,
       ROUND((julianday(ts) - julianday(LAG(ts) OVER (ORDER BY ts))) * 86400, 0) AS gap_seconds
FROM memory_metrics
WHERE version = 'cardano-db-sync 13.7.1.0-node-11.0.1 preprod'
ORDER BY gap_seconds DESC
LIMIT 5;
```
> The biggest `gap_seconds` are your outages (this is the same idea the plots use
> to break their lines - see [03 - Graph catalog](03-graph-catalog.md)).

---

## See also

- [10 - Generating reports](10-generating-reports.md) - the version-label model in depth.
- [05 - Database internals](05-database-internals.md) - SQLite WAL and the *why* behind the schema.
- [06 - Glossary](06-glossary.md) - what each table and column means.
