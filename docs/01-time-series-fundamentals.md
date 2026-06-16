# 01 - Time-series fundamentals

## What this project actually does

Two cardano components run on a host:

- **`cardano-node`** - talks to the Cardano network, validates blocks, hands them to db-sync.
- **`cardano-db-sync`** - receives blocks from cardano-node, writes structured rows to a Postgres database.

Both are long-running processes. Both have characteristic resource profiles (CPU, memory, disk) that change as they sync the chain. We want to **measure those profiles** so that:

1. You can compare two versions of the same component fairly ("does the new db-sync use less memory than the old one?").
2. You can spot anomalies during a sync (a sudden jump in RSS, a long stall on one epoch).
3. You can correlate behaviour with chain content (does Conway slow things down? does Plutus tx volume push memory up?).

**The mechanism is sampling.** Once every 10 seconds (or whatever `--interval` is), the monitor wakes up, reads the process's current resource use plus a few cheap chain-state numbers, and writes one row to SQLite. After hours or days of this, you have a *time-series* - a sequence of numeric observations, each tagged with the moment it was taken.

## Why time-series and not snapshots

A snapshot - "at the end of sync, the process used 4.6 GB of RSS" - tells you the destination but not the journey.

A time-series - "RSS climbed from 100 MB to 4.6 GB over 12 hours, with three distinct plateaus" - tells you the *shape*. The shape is where the diagnostic value lives.

Concrete examples of what a snapshot cannot tell you that a time-series can:

- **Where in the sync did memory spike?** Snapshot says "max 4.6 GB"; time-series says "spike was at slot 67M, just after Conway HFC, lasted 20 minutes, then settled back to 2.1 GB."
- **Did the two versions follow the same shape?** Snapshot says "A used 4.6 GB, B used 4.5 GB"; time-series says "B is consistently 200 MB lower throughout catch-up *except* in the Babbage range where they're equal."
- **When did the process stop catching up and start tracking tip?** Snapshot says "final sync 100%"; time-series shows the inflection point where tip-lag stops decreasing and starts oscillating around zero.

This is why every measurement in the project is timestamped and kept as a series. Aggregating to a single number is something you do *at analysis time*, after the data is collected - never at collection time, because you can always derive the snapshot from the series, but not vice versa.

## What "uniform-interval sampling" means and why we do it

Each sample is taken `--interval` seconds (default 10) after the last one. That uniformity matters for several downstream operations:

### 1. Rate calculations become trivial

To compute "blocks per second the monitor saw db-sync insert," you take the difference of two consecutive `max_block_no` values and divide by the wall-clock seconds between them. If your interval is 10 seconds, every consecutive pair tells you the rate at that point in time. No interpolation needed.

If samples were taken at arbitrary intervals (e.g. only when an event happened), you'd need to weight or interpolate to recover a comparable rate. Uniform intervals give you a free, honest rate metric.

### 2. Averaging across windows is meaningful

If you want "average CPU% over the last 5 minutes," with uniform 10-second samples that's just `mean(last 30 samples)`. With non-uniform sampling you'd need to weight each sample by the time slice it represents - and if some intervals are missing, the weighted mean can mislead.

### 3. Visual density is honest

On a time-axis plot, equally-spaced points produce equally-spaced visual marks. A cluster of dots on the plot really means "many real measurements at that time," not "the monitor happened to sample more often there." Without uniform sampling, density on the plot is an artifact of when we collected, not of what we measured.

### Cost of uniformity

The tradeoff: you spend a small amount of work every interval even when nothing interesting is happening. For our workload - a single process per env on a host that's already running cardano-node and db-sync - the monitor adds well under 1% CPU and a few megabytes of SQLite per day. Trivial.

## Slot axis vs time axis - the key distinction

This is the most important conceptual question we hit during development, and it deserves real attention.

We have **two natural x-axes** for plotting resource over time:

- **Slot number** (`slot_no`): position on the Cardano chain. Strictly increasing during a sync. The natural choice for "which block is db-sync on?"
- **Wall-clock time** (`ts`): real-world moment the sample was taken. Strictly increasing always.

They are *not* the same. Wall-clock time advances by one second per second. Slot number advances by however many slots the sync processed in that second - which varies enormously between catch-up sync (many slots/sec) and tip-following (one slot per ~20s on average).

### When slot-axis is right

Slot is right when you want to compare two versions on **the same chain content**. Two versions both sync from slot 0 to slot 124M. Comparing "RSS at slot 50M" between them shows the same point in the chain - apples to apples. You don't care that one version got there in 6 hours and the other in 8 hours; you care that at slot 50M, version A used 3 GB and version B used 3.5 GB.

### When time-axis is right

Time is right when you want to see **wall-clock dynamics**: how fast did sync progress per unit of real time? Is there a stall? Is the rate accelerating or decelerating? At what time of day did the spike happen?

Wall-clock time is also the right answer when you're comparing "how long did this version take to sync" - which is just `max(ts) - min(ts)`, computed in wall-clock seconds.

### The trap: stalls collapse on the slot axis

If db-sync stops processing for any reason - a long ledger calculation, an epoch boundary stake snapshot, a GC pause - slot_no doesn't advance even though wall-clock time does. You take 30 samples at 10-second intervals during a 5-minute stall: 30 samples, all at the *same* slot number.

Now plot RSS against slot_no:

```
Plotted point | slot_no   | rss
       1      | 1,000,000 | 2048 MB
       2      | 1,000,000 | 2560 MB   ← all 30 land on
       3      | 1,000,000 | 3100 MB     the same x-coordinate
      ...     |    ...    |  ...
      30      | 1,000,000 | 6100 MB
      31      | 1,000,001 | 6105 MB
```

On the slot-axis plot, you see *one dot* (or a vertical line if you connect them, depending on plotly settings). The 5-minute memory climb from 2 GB to 6 GB is invisible. You think nothing happened.

Plot the same data against `ts`:

```
ts            | slot_no   | rss
00:00:00 UTC  | 1,000,000 | 2048 MB
00:00:10 UTC  | 1,000,000 | 2560 MB
00:00:20 UTC  | 1,000,000 | 3100 MB
...
00:05:00 UTC  | 1,000,000 | 6100 MB   ← clearly a 5-minute climb
00:05:10 UTC  | 1,000,001 | 6105 MB
```

Now you see a 5-minute ramp ending in a tiny slot increment. Stall identified. Memory cost quantified.

**This is why both axes exist** and why the plot scripts default to slot-axis but offer `--x-axis time`. For routine A/B comparison: slot. For diagnosing "something weird happened": time.

### The other side: post-tip behaviour

After db-sync catches up, slot_no advances at roughly chain rate (one slot per ~20s on Cardano on average, but slots are 1s each - most slots simply don't produce a block). So at tip, **multiple samples land on the same slot number, just like during a stall** - except this is the steady state, not an anomaly.

On a slot-axis plot, the tail at tip looks dense (many y-values per x-coordinate). On a time-axis plot, it's a clean horizontal-ish line that grows by one second per second.

If you only ever plot against slot_no, you'd mistake the steady-state tip behaviour for ongoing activity. Time-axis makes the at-tip plateau obvious.

## Why naive line plots mislead - the gap problem

A monitoring process can be restarted. SSH disconnects, hosts reboot, you tweak a setting and restart. Every restart leaves a *gap* in the time-series - wall-clock seconds during which no samples were taken.

If you plot the resulting data with plotly's default `Scatter(mode="lines")`, the line connects the last sample of session 1 to the first sample of session 2. Visually, this looks like a continuous line - there is no gap.

But there *is* a gap. And depending on what happened during the gap, the connecting line can imply something that didn't happen:

- If db-sync grew RSS during the gap, the line slopes up across the gap, suggesting a smooth ramp where there was actually no observation.
- If db-sync dropped RSS during the gap (e.g. you restarted db-sync itself, and the fresh process has lower baseline memory), the line shows a near-vertical *cliff* that didn't happen - RSS didn't really drop instantly, you just stopped looking.

We hit this exact problem during development: a post-NearTip memory release (real, ~4 GB → 2 GB) was visualized as a near-vertical cliff because the monitor was restarted across the transition. The cliff was partly a real drop and partly a sampling artifact.

The fix in this project: **insert NaN-valued marker rows between consecutive samples whose wall-clock gap is more than 5× that series' own median sample interval**. Plotly's `Scatter` doesn't connect across NaN values, so the line breaks visibly. You see "no data here" rather than "line connecting two distant points."

The threshold is *adaptive*, computed per series, because the collectors don't all sample at the same rate: `node-resource-monitor.py` / `db-sync-resource-monitor.py` sample every ~10s (so the threshold lands near 50s), but `node-db-size-monitor.py` samples every 60s (threshold near 300s). A fixed threshold was a real bug - the 50s value tuned for the 10s collectors treated *every* normal 60s disk sample as a gap and broke the line at every point, rendering an empty disk plot. When a series' cadence can't be measured (fewer than two samples), it falls back to 50s.

This is implemented in `_common.insert_gap_breaks` and applied by all the load functions in the plot scripts. The 5× multiplier was chosen to be:
- Large enough to ignore the occasional slow sample (a sampling iteration that takes 12 seconds instead of 10).
- Small enough to catch a real outage (a restart that leaves several missed intervals of nothing).

You can read the implementation in [`scripts/_common.py`](../scripts/_common.py) - search for `insert_gap_breaks`.

## Aggregation: from samples to summary numbers

The raw time-series has thousands of rows. A summary report condenses them to a handful of numbers. The aggregation step is where lots of mistakes happen, so it's worth being deliberate:

### Per-epoch aggregation

For the node-side sync-duration-per-epoch plot, we group samples by `(version, epoch_no)` and compute `max(ts) − min(ts)`. That gives "wall-clock time the monitor saw samples for this epoch."

There's a subtle thing here: **first and last epochs of a sync are partial**. If the monitor started mid-epoch, we have samples only for the back half of epoch N - the duration we compute is the duration of the observation window, not the duration of the epoch. Same on the trailing end.

For diagnostic use (which epochs are heavy?) this rarely matters because the partial epochs are tiny outliers among hundreds of complete ones. For A/B comparison it doesn't matter at all because both runs have the same partial-epoch problem and the noise cancels.

### Per-era aggregation

Per-era totals come from summing per-epoch durations within each era. We sort eras by their canonical chronological order (Byron → Conway), not by epoch_no - so a comparison plot always shows Babbage before Conway even if the two versions reached Conway at different epochs.

### Means vs sums

"Total fees per epoch" is a sum - every transaction's fee adds up. Total over many epochs is meaningful.

"Average tx size per epoch" is a mean - total size divided by count. **Don't average the per-epoch means to get an overall mean.** That gives you the unweighted mean of epoch averages, which is not the same as the overall mean unless every epoch has the same tx count. For an overall mean, recompute from totals: `sum(sum_tx_size) / sum(tx_count)`.

This kind of subtlety is what [04 - Statistics primer](04-statistics-primer.md) covers in depth.

## Summary

- We collect time-series because the *shape* of a metric over time is more diagnostic than any single snapshot.
- Uniform-interval sampling makes derived metrics (rates, averages, comparisons) honest without interpolation.
- Slot-axis is right for chain-content comparison; time-axis is right for wall-clock dynamics. Both are needed, and the toolchain supports both via `--x-axis`.
- A naive line plot lies about gaps in monitoring. The project inserts NaN markers across gaps > 5× sample interval so the line breaks cleanly instead of fabricating values.
- Aggregation choices change meaning. Per-epoch and per-era summaries handle partial-window cases predictably; mean-of-means is a trap.

Next: [02 - Cardano domain primer](02-cardano-domain-primer.md).
