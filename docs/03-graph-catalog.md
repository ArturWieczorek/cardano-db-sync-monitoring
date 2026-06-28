# 03 - Graph catalog

A reference for every chart the project produces. For each one: where the data comes from, what the axes mean, what healthy looks like, what a regression looks like, and the common mistakes when reading it.

For the *how to produce them* part, see the [main README](../README.md). This doc is about *interpretation*.

## Conventions used throughout

- **Source**: which SQLite table or Postgres query the chart reads from.
- **X / Y**: what the axes show.
- **Healthy shape**: what you expect to see when everything is fine.
- **Regression signal**: what to look for when comparing two versions or investigating a problem.
- **Common misreads**: shapes that look meaningful but aren't.

---

## Resource graphs (`*-monitor.py` output → `--metrics cpu_ram`)

### RSS (Resident Set Size) over time

- **Source**: `memory_metrics.rss` from the SQLite stats DB.
- **X**: `slot_no` (default) or `ts` (with `--x-axis time`).
- **Y**: RSS in MB. Auto-promoted to GB for display in the monitor's live status line; the chart always uses MB.
- **Healthy shape (db-sync)**: monotonically increasing during catch-up sync (the ledger state grows as more blocks are processed and validated). Plateaus when bulk-load mode ends; may then *drop* significantly after NearTip migrations + GC, then settle into a steady-state band at tip.
- **Healthy shape (cardano-node)**: similar but typically smaller working set; plateaus once the ledger state is fully loaded.
- **Regression signal**: a comparison plot where version B's RSS curve sits 200+ MB above version A's *throughout* - the new build is heavier across the board. Or a curve that grows during the at-tip phase when the old one didn't - possible memory leak.
- **Common misreads**:
  - "RSS dropped 2 GB instantly" - almost always a process restart, not a real release. The new process's first sample is its post-warmup baseline, which is much lower than where the old process ended up. The drop is sampling-gap, not behaviour. See [01 §gap problem](01-time-series-fundamentals.md#why-naive-line-plots-mislead--the-gap-problem).
  - "RSS stays high after at-tip" - usually correct: GHC (Haskell's runtime) is reluctant to return heap to the OS. The process *uses* less memory but holds onto the address space it allocated at peak. A restart releases it; running production usually doesn't.

### CPU %

- **Source**: `cpu_metrics.cpu_percent` from psutil (process-level, not host-level).
- **X**: `slot_no` or `ts`.
- **Y**: percent. Above 100% means multi-threaded use (a value of 250% means 2.5 cores worth of work).
- **Healthy shape**: high during catch-up (often 100-300%), dropping to near zero at tip with brief spikes when a block arrives.
- **Regression signal**:
  - **At tip, sustained non-trivial CPU.** If a version that used to idle at <5% now sits at 30%, it's doing background work it shouldn't be - or the chain has gotten busier.
  - **During catch-up, lower CPU%.** Surprising regression: maybe the new build is *better* (less work per block), or maybe it's *worse* (CPU-idle because it's waiting on disk).
- **Common misreads**:
  - "CPU% dropped" can mean "the bottleneck moved." A drop in CPU% without a rise in throughput means the process is now waiting for something else (disk, postgres, network).
  - psutil's `cpu_percent(interval=None)` requires a first call to "prime" the sampler; the very first sample reads ~0%. The monitor primes correctly, so this only affects manual psutil usage if you experiment.

---

## Ingest progress graphs (db-sync `--metrics ingest`)

### Tip lag

- **Source**: `ingest_metrics.tip_lag_sec` from the SQLite stats DB. Computed in the monitor as `wall_clock_now - block.time` (UTC-correctly, after the timezone bug fix).
- **X**: `slot_no` or `ts`.
- **Y**: seconds. Auto-formatted in the live status line (`12d`, `2.5h`, `45s`) - the chart shows raw seconds.
- **Healthy shape**: huge at sync start (years of chain history, so hundreds of millions of seconds for a fresh mainnet sync), monotonically decreasing during catch-up, asymptoting to ~slot duration (≈ 20s on Cardano) at tip. Then a sawtooth: rises by ~1s per second until the next block arrives, then drops back to a few seconds.
- **Regression signal**: a slower catch-up rate shows as a less-steep descending curve. **The fundamental version-comparison signal lives here** - if you're answering "which build syncs faster," compare the slopes of the tip_lag curves.
- **Common misreads**:
  - **"Tip lag is exactly 2h and not closing"** - that was the timezone bug. If you see this on current builds, it's a real bug; the regression test in `tests/test_time.py` should catch it.
  - **"Tip lag plateaus 30 minutes from tip"** - most often, cardano-node hasn't caught up either. db-sync can only ingest blocks cardano-node has fetched. Check `cardano-cli query tip --testnet-magic <N>` on the node.

### DB size over time

- **Source**: `ingest_metrics.db_size_bytes`, sampled from `pg_database_size(current_database())`. Cheap to sample (catalog lookup).
- **X**: `slot_no` or `ts`.
- **Y**: bytes. Auto-formatted; chart displays MB.
- **Healthy shape**: monotonically increasing during catch-up. Slope varies by era - Mary onward steeper (multi-asset, Plutus), Conway can be steeper still (governance).
- **Regression signal**: a comparison where one version's curve sits higher *and grows faster* - the new schema is storing more per byte of chain content. Worth investigating which tables are responsible (`--metrics tables`).
- **Common misreads**:
  - **Sudden plateaus or small drops** can happen during postgres VACUUM or autovacuum activity. Not real "data was deleted" events. The curve continues upward shortly after.
  - **A late-stage upward kink** is often "NearTip migrations creating client-only indexes." Logs from db-sync confirm this; the chart shows it as a step in the size curve.

### Block insert rate and tx insert rate

- **Source**: computed at plot time from `ingest_metrics.max_block_no` and `ingest_metrics.max_tx_id` as `Δcounter / Δts_seconds` between consecutive samples, per-version.
- **X**: `slot_no` or `ts`.
- **Y**: blocks/sec or tx/sec.
- **Healthy shape**: high (hundreds to low thousands per second) during catch-up, dropping to ~0.05 blocks/sec (≈ one block per 20s) at tip.
- **Regression signal**:
  - A version with a *lower* sustained catch-up rate is slower.
  - A version with the same rate but more variability (lots of zero-rate samples) is stalling more often - pages of pending postgres writes, perhaps.
- **Common misreads**:
  - **Spikes to extreme values** can be artifacts of batch inserts: db-sync ingests N blocks in a burst, the monitor catches the post-burst snapshot, and divides by the inter-sample gap. The instantaneous rate is fictitious; if you see a single 5000-block-per-second spike, smooth it mentally. But note that *most* of the block-rate spread on mainnet is **real signal**, not this artifact: the Byron bulk-load runs ~50x faster (~1080 blocks/sec) than the modern chain (~23 blocks/sec), which is why the panel reads best on a **logarithmic y-axis**. For how to measure the spikiness and which smoothing actually helps (a rolling median, not a short rolling mean), see [09 - Reading and smoothing bursty rates](09-reading-and-smoothing-bursty-rates.md).
  - **At tip the rate is near zero** - not a bug. There's nothing for db-sync to do until the next block arrives.

### UTXO set size

- **Source**: `ingest_metrics.utxo_count`, sampled only when db-sync has `consumed_by_tx_id` populated.
- **X**: `slot_no` or `ts`.
- **Y**: row count.
- **Healthy shape**: monotonically increasing during catch-up (output rate exceeds consumption rate by a small margin). Curve shape reflects actual chain dynamics, not db-sync's processing.
- **Regression signal**: not version-dependent in practice - UTXO is a property of the chain, not of db-sync. But it's a sanity check: if a version's UTXO count is *different* from another's at the same slot, something is broken.
- **Common misreads**: **all-zeros or missing line** means `consumed_by_tx_id` wasn't populated. Not a bug, just configuration. The monitor prints a `UTXO tracking: DISABLED` notice at startup that flags this; the plot omits the panel if no data exists.

---

## Hot-table row counts (db-sync `--metrics tables`)

### Row count per hot table over time

- **Source**: `table_rowcounts` from SQLite, written by the monitor sampling `pg_class.reltuples` (the postgres planner's row-count estimate, cheap to query and accurate to within a few percent).
- **X**: `slot_no` or `ts`.
- **Y**: row count (log scale, because table sizes span ~5 orders of magnitude).
- **Tables sampled**: `block`, `tx`, `tx_out`, `ma_tx_out`, `ma_tx_mint`, `multi_asset`, `datum`, `redeemer`, `script`.
- **Healthy shape**: each table's curve grows monotonically. `block` and `tx` have the smoothest curves (one row per block / per tx). `redeemer` and `datum` are step-function-ish - they grow only when Plutus transactions are processed, otherwise flat.
- **Regression signal**: a version where one table grows substantially faster suggests a schema change or extra writes you didn't expect. Conversely, *slower* growth of a table that should track 1:1 with chain content suggests db-sync isn't writing some rows it used to.
- **Common misreads**:
  - **reltuples is an estimate, not exact.** Postgres updates it during ANALYZE (run periodically by autovacuum). Between ANALYZE runs the count can lag. A stair-stepped curve where the steps coincide with ANALYZE timing is normal; it's not real bursty growth.
  - **Log y-axis flattens small differences.** A 5% gap between two version's `tx_out` curves looks tiny on a log scale; on a linear scale it would be obvious. Switch axis type mentally when comparing.

---

## Rollback performance graphs (db-sync `--metrics rollback`)

Three stacked panels from `db-sync-rollback-monitor.py`. A rollback (chain reorg) makes db-sync delete blocks it had stored and re-apply the new ones; these panels show how that costs time. Each detected rollback is also drawn as a dashed vertical line across all panels, labelled with its depth. For the plain-language, step-by-step version see [14 - Reading the rollback graphs](14-reading-the-rollback-graphs.md).

### DB event queue length

- **Source**: `rollback_samples.queue_length` (the `cardano_db_sync_db_queue_length` gauge), scraped from the db-sync Prometheus endpoint (root path, default port 8080).
- **X**: `slot_no` (= db slot height) or `ts`.
- **Y**: number of items in db-sync's internal work queue.
- **Healthy shape**: 0-1 at the tip; tens during catch-up sync.
- **Regression signal**: for the same rollback, a version whose queue spikes higher or drains slower is doing more work / clearing it more slowly.
- **Common misreads**: a high flat value isn't a rollback - it's normal catch-up backlog. The rollback signal is the *spike* relative to a calm baseline.

### Node-DB block-height gap

- **Source**: `rollback_samples.node_block_height - rollback_samples.db_block_height`.
- **X**: `slot_no` or `ts`. **Y**: blocks behind (0 = caught up).
- **Healthy shape**: flat near 0 at tip. A rollback drives it up, and the slope back down to 0 is the recovery.
- **Regression signal**: a version whose gap takes longer to return to 0 after the same rollback recovers more slowly.
- **Common misreads**: a forced rollback done by restarting db-sync leaves a sampling gap (the endpoint is down during the restart), so the dip may be missing here - the log-sourced event still captures it. See [01 §gap problem](01-time-series-fundamentals.md#why-naive-line-plots-mislead--the-gap-problem).

### Rollback event duration

- **Source**: `rollback_events` - `delete_duration_sec` for log-sourced events (the `x` markers, `source='log'`) and `recovery_duration_sec` for metrics-derived events (round markers, `source='metrics'`). Depth is `depth_blocks`.
- **X**: event position (start time, or target slot on the slot axis). **Y**: seconds.
- **Healthy shape**: occasional low dots (sub-second to a few seconds on testnets).
- **Regression signal**: the regression-prone number is the **deletion** dot - a version that deletes the same depth in noticeably more time, or trends to minutes, is the "lengthy rollback" failure mode this feature exists to catch. The per-table detail is in `rollback_table_deletes`.
- **Common misreads**: an empty panel usually means no rollback was detected in the window (not an error); a rollback captured only via the log shows a `Deletion` dot but no `Recovery` dot.

---

## Sync time by era and per epoch (node and db-sync, different sources)

### Sync time by era (bar chart)

- **Source (node side)**: `node_ingest_metrics`, aggregated. For each (version, epoch_no), `max(ts) - min(ts)` gives a per-epoch wall-clock duration; sum within each era's `protocol_major`-derived bucket.
- **Source (db-sync side)**: `epoch_sync_time.seconds` joined with `epoch_param.protocol_major`. db-sync writes its own per-epoch duration measurements; we just sum them per era.
- **X**: era name (categorical).
- **Y**: total wall-clock seconds.
- **Healthy shape**: tall bars in eras that produced more content (Babbage, Conway typically). Short bars in early eras (Byron, Shelley) because their chain content is sparse.
- **Regression signal**: this is the single most valuable A/B comparison view. A version that's 8% slower overall might be 30% slower in Conway and even with the old build in Byron - the era bar reveals where the regression lives. Conversely, an optimization targeted at Plutus would show up as a shorter Alonzo+Babbage bar.
- **Common misreads**:
  - **"Conway is much shorter than Babbage."** Probably correct: Conway is younger; fewer total epochs have elapsed in Conway than in Babbage. The bar reflects total time, not per-epoch time. For per-epoch comparison, look at the per-epoch line below.
  - **Phantom Conway bar on a chain still in Babbage.** Was the `block.proto_major` vs `epoch_param.protocol_major` bug we hit during development; current code uses the right source. If you see this, file a bug.
  - **Byron bar shows fewer epochs than expected** (node-side only). Byron-era epochs were 21,600 slots = ~12 hours of chain time, but on testnets they often pass in 1-2 seconds of wall-clock during catch-up - faster than the default 10-second sample interval. The monitor can sample epoch N then 10s later find the node is already in epoch N+3, never observing N+1 and N+2. The chart's title carries a sub-line caveat about this; lower `--interval` to 1-2s if you need every Byron epoch captured. For A/B comparison the under-count cancels across runs and contributes negligibly to totals.

### Sync duration per epoch (line chart)

- **Source**: same as the era bar, but un-aggregated.
- **X**: epoch number.
- **Y**: wall-clock seconds for that epoch.
- **Healthy shape**: 
  - In catch-up sync: low values (the node ingests faster than chain time). The line wobbles with chain content - heavier epochs take longer.
  - At tip: the duration is approximately 432,000 seconds (one epoch = 5 days of wall-clock).
- **Regression signal**: a single epoch that's an outlier (10× the surrounding ones) usually points to a specific event - a hard fork, a stake snapshot, an unusual chain event. Compare the two versions on that epoch in detail.
- **Common misreads**:
  - **The first and last epochs are partial.** The monitor may have started mid-epoch (so the first epoch's duration is just the back half) or stopped mid-epoch (last epoch's duration is just what was observed). For A/B comparison this cancels because both runs have the same artifact.
  - **A flat-zero line for some range** usually means the monitor wasn't running during those epochs. The gap-break logic would help here on the resource plots, but the per-epoch chart doesn't apply gap breaks (it'd be confusing across discrete epoch numbers); the value is just genuinely missing.
  - **Jumps in epoch_no between consecutive points** (e.g. epoch 0 → epoch 4 in the line) mean the monitor missed the intervening epochs entirely. Same root cause as the era-bar under-count: catch-up rate exceeded sample rate. See the era-bar entry above for the fix.

---

## Per-epoch chain stats (db-sync `db-sync-epoch-report.py`)

These come from Postgres directly, not from the SQLite stats DB. The report queries `cardano-db-sync`'s own schema (`block`, `tx`, `redeemer`, `ma_tx_mint`, `epoch_sync_time`, `epoch_param`, etc.).

### Block count per epoch

- **Source**: `COUNT(DISTINCT block.id)` grouped by `block.epoch_no`.
- **Healthy shape**: ~21,600 blocks per epoch is the theoretical maximum on Cardano (5% of 432,000 slots), but actual numbers are more like 17,000-20,000 because of stake-distribution mechanics. Stable from epoch to epoch within a network.
- **Regression signal**: not version-dependent - this is purely chain content, same across any honest sync.
- **Use**: sanity check that db-sync is recording every block. If the count drops sharply for a range of epochs, something went wrong with sync.

### Transaction count per epoch

- **Source**: `COUNT(tx.id)` joined with block, grouped by `block.epoch_no`.
- **Healthy shape**: highly variable across epochs (depends on user activity). Long-term trend is upward. Network campaigns or DeFi launches show as spikes.
- **Use**: contextualize "this epoch took longer to sync" - was it because the chain had more transactions, or was the version slow on that epoch?

### Total fees per epoch (lovelace)

- **Source**: `SUM(tx.fee)` per epoch.
- **Healthy shape**: tracks transaction count and average fee. Generally growing as activity increases.
- **Use**: economic activity indicator. Useful for cross-referencing with explorer data.

### Total output value per epoch

- **Source**: `SUM(tx.out_sum)` per epoch.
- **Healthy shape**: very high values (billions of lovelace per epoch on mainnet), but this measures **gross** outputs, not net new ADA. Most outputs are just unspent change from previous transactions.
- **Common misread**: "ADA was created." No - total output equals total input plus rewards (paid by treasury). The number is large because of UTXO accounting where every transaction's outputs sum to roughly the same as its inputs.

### Plutus tx fraction

- **Source**: per-epoch, `COUNT(DISTINCT tx with at least one redeemer) / COUNT(DISTINCT tx)`.
- **Healthy shape**: 0% before Alonzo (epoch ~290 on mainnet, much earlier on preprod), then climbing. On mainnet currently ~5-15%.
- **Use**: see when Plutus adoption took off in the chain you're analyzing.

### MA mint events per epoch and cumulative distinct assets

- **Source**:
  - Mint events: `COUNT(ma_tx_mint.id)` per epoch.
  - Cumulative distinct: per-epoch `COUNT(DISTINCT ident)` of "first-seen" assets, then `cumsum` in pandas.
- **Healthy shape**: mint events spike sporadically; cumulative line grows monotonically with a knee at the Mary era (first multi-asset support).
- **Use**: chain-content context. Heavy NFT minting on a specific epoch range explains a sync-duration bump there.

### Avg tx size (and p95 with `--with-p95`)

- **Source**: `AVG(tx.size)` and `PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY tx.size)` per epoch.
- **Healthy shape**: 
  - Avg is in the hundreds of bytes (most transactions are small).
  - p95 is much higher - single-kilobyte transactions are not rare, and a few are tens of kilobytes (Plutus script-bearing transactions).
- **Why both**: average alone hides the long-tail distribution. A small change in average could mean "all transactions got slightly bigger" or "a few transactions got much bigger" - p95 disambiguates. See [04 - Statistics primer §percentiles](04-statistics-primer.md#mean-vs-median-vs-percentile).
- **Why p95 is gated behind `--with-p95`**: PERCENTILE_CONT has to sort all `tx.size` values per epoch group. On mainnet with ~100M tx rows, that's 5-20 minutes of CPU. Default-skip; opt in when you actually want it.

### Reward count, stake count

- **Source**: row counts of `reward` and `epoch_stake` grouped by epoch.
- **Healthy shape**: reward count nonzero only in epochs where rewards crystallize (every epoch on Shelley+ chains). Stake count is roughly the size of the active stake set (~1M on mainnet).
- **Use**: confirms db-sync recorded the per-epoch state correctly. A version that produced fewer reward rows for the same epoch range is *not* syncing all of them - that's a regression.

### Conway governance: voting procedures, drep registrations

- **Source**: row counts in `voting_procedure` and `drep_registration` joined to `block.epoch_no`. Guarded with `to_regclass()` - if the tables don't exist (pre-Conway db-sync), the panels are omitted.
- **Healthy shape**: zero before Conway HFC; rising afterwards as governance participation grows.
- **Use**: Conway activity tracking. For pre-Conway chains, this panel is missing - that's expected.

---

## Summary report - headline deltas (db-sync A/B comparison)

When you pass two `--pg-dbname` values to `db-sync-epoch-report.py`, the summary text file leads with a comparison table:

```
== Headline metrics ==
Metric                 dbA              dbB              Δ (b vs a)
Total sync time        4h 32m 15s       3h 18m 09s       -27.0%
Final DB size          450.2 GB         448.1 GB         -0.5%
Total blocks           1,234,567        1,234,567        -
Total transactions    12,345,678       12,345,678        -
Plutus tx fraction     12.4%            13.1%            +0.7pp
UTXO set size            420,000          420,000        -
```

- **Total blocks / total transactions** should be `-` (no delta). Both syncs covered the same chain content; if they differ, sync is incomplete on one side or schema differs.
- **Total sync time** is the headline number for the comparison. Negative is faster.
- **Final DB size** can differ slightly across versions for the same chain - schema changes between db-sync releases. A change > 5% is worth investigating which tables grew or shrank.
- **Plutus tx fraction** in percentage points, not relative percent. `+0.7pp` means "version B saw 0.7 percentage points more Plutus transactions" - which probably just means version B happened to sync slightly more recent chain content, not that the version itself changed Plutus behaviour.
- **UTXO set size** - same caveat as the time-series chart. Only populated when both versions have `consumed_by_tx_id` enabled.

---

## Cross-cutting note: gap-aware plotting

Every time-series chart in this project applies `insert_gap_breaks` to its data before plotting. If you see a chart with a visible empty span in the middle - neither line nor data points - that's the monitor not having run for that period. Not missing data due to a bug; explicitly indicated absence.

This is most relevant for:
- Multi-session syncs (the monitor was restarted).
- Long lab tests where the monitor died and was restarted later.
- Resumed syncs after a process crash.

The threshold is adaptive: `5 ×` each series' own median sample interval (≈50s for the 10s CPU/RAM/RTS collectors, ≈300s for the 60s disk collector), falling back to 50s when a series has too few samples to measure. Implemented in `_common.insert_gap_breaks`.

---

Next: [04 - Statistics primer](04-statistics-primer.md).
