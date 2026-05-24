# 04 — Statistics primer

The math we actually use, motivated by examples from this project's data. Aimed at someone who can read a graph but might not have thought hard about *which* summary statistic to compute or which mistake the obvious computation makes.

## Mean vs median vs percentile

These three answer the question "what's a typical value?" but they answer it differently. The choice matters most when the underlying distribution has long tails — and ours often does.

### Mean (arithmetic average)

`mean = sum / count`. The textbook average. Sensitive to outliers — one transaction that's 100 KB pulls the mean noticeably.

When it's right: you genuinely want the total quantity per item. "Average bytes per transaction" tells you "if all transactions were the same size, this is what each would be" — useful for capacity planning where total bytes is what matters.

When it misleads: when you want to characterize "typical" behaviour and the distribution is skewed. Mean tx size on a chain with a few giant Plutus transactions can be 2× the median because the giants pull it up.

### Median (50th percentile)

The middle value when you sort all observations. Insensitive to outliers — adding a single 100 KB transaction to a list of thousands of 200-byte transactions barely moves the median.

When it's right: when you want "typical" and the distribution has tails.

When it misleads: when you actually care about the heavy users. A median that says "typical tx is 200 bytes" hides the fact that some transactions are huge — and those huge ones drive much of the system's load.

### p95 (95th percentile)

The value below which 95% of observations fall. Captures "what does the slow-ish end of the distribution look like?" without being thrown off by a handful of extreme outliers (those land in the top 5%, above p95).

When it's right: latency-style metrics where the worst-case matters. "p95 request latency" tells you "95% of users had a good experience; the other 5% had something at least this bad."

For us, **p95 tx size is the right answer to "how big are the chunky transactions?"** A small change in average could mean "everything got slightly bigger" *or* "rare big ones got bigger." p95 disambiguates: it tracks the "rare big ones" specifically.

### Worked example

Suppose an epoch has 100,000 transactions with these sizes (bytes):

- 99,500 transactions of size 200 (small UTXO transfers).
- 400 transactions of size 1,500 (multi-asset transactions).
- 100 transactions of size 18,000 (Plutus script transactions).

Computed summaries:

- **Mean**: ~244 bytes. Pulled up by the long tail.
- **Median**: 200 bytes. The middle of the sorted list is in the dense low-size cluster.
- **p95**: ~1,500 bytes. The 95th percentile lands in the multi-asset cluster.
- **Max**: 18,000 bytes. One Plutus outlier.

Each tells a different story:

- "Most transactions are tiny" — median.
- "The chunky ones are around 1.5 KB" — p95.
- "Outliers can be 90× the median" — max.
- "Total bytes if you needed to provision storage" — mean × count.

Why we compute p95 in the report: it catches Plutus-driven size inflation that mean and median both hide. Why we don't compute p99 or max: they're more outlier-driven and less stable epoch-to-epoch (one giant tx in one epoch and not another makes p99 jump around). p95 is the right balance for "long-term comparison across epochs."

## Why averaging the per-epoch averages is wrong

You have per-epoch average tx size. You want overall average tx size across all epochs. The temptation: `mean(epoch_averages)`. This is wrong.

Each epoch's average is `sum_tx_size_in_epoch / tx_count_in_epoch`. Different epochs have very different tx counts. Averaging the per-epoch means treats each epoch equally regardless of how many transactions it had — but you wanted to treat each transaction equally.

The right computation: `total_sum_tx_size / total_tx_count`, summed across epochs first.

```
WRONG:                              RIGHT:
mean([                              total_sum = sum(epoch.sum_tx_size)
  e1.sum_tx_size / e1.tx_count,     total_count = sum(epoch.tx_count)
  e2.sum_tx_size / e2.tx_count,     overall_mean = total_sum / total_count
  ...
])
```

The report avoids this trap by computing the summary in `build_summary` from `sum(epoch_df.total_fees)`, `sum(epoch_df.tx_count)`, etc. — never from `mean(epoch_df.avg_tx_size)`.

This is one of those statistical mistakes that's easy to make and impossible to notice from the output alone. The number always looks plausible.

## Derivatives: rate from a cumulative counter

We track cumulative counters (`max_block_no`, `max_tx_id`) and report rates (blocks/sec, tx/sec). Computing the rate is a finite-difference derivative:

```
rate_at_sample_i = (counter_i - counter_{i-1}) / (ts_i - ts_{i-1})
```

This is essentially `Δy/Δx`, the same operation as taking a slope between two points on a curve. With uniform-interval sampling (10 seconds between samples), the denominator is approximately 10 every time, so the rate is roughly `Δcounter / 10`.

### What this measures honestly

The instantaneous rate over the just-elapsed interval. If db-sync processed 1,000 blocks in 10 seconds, the rate at that sample is 100 blocks/sec.

### What it doesn't capture

A rate computed this way assumes the work was evenly distributed across the 10 seconds. If db-sync was idle for 8 seconds, then processed 1,000 blocks in 2 seconds, our finite-difference rate still reports 100/sec. The peak burst rate (500/sec) is invisible.

This matters mostly when monitoring batched operations. A "burst then idle" pattern looks like a steady rate on our chart. If you suspect this, lower `--interval` (but you pay for it in monitor overhead) or instrument db-sync itself.

### Spikes from intervals slightly off

Occasionally a sample is taken 12 seconds after the previous one (the monitor's iteration ran slowly). The computed rate divides by 12 instead of 10, giving a slightly lower number even if the actual ingestion rate was the same. On the chart this looks like a small dip. Smooth it mentally; it's not real.

### Spikes from db-sync's batch commits

db-sync occasionally commits a batch of blocks at once rather than one at a time. The counter doesn't increment between sub-batch operations; it jumps when the batch commits. If the monitor samples mid-batch and again post-commit, the post-commit sample shows the entire batch as having "happened in one interval" — a huge rate spike.

Visually: occasional skyward spikes on the block/tx rate plot. These are sampling artifacts of bursty commits, not real CPU spikes. The cumulative counter is correct; only the instantaneous rate is misleading.

If you want to see *smoothed* rates, average over multiple intervals — a 5-sample (50-second) rolling mean filters out the burst noise while preserving the medium-scale trend.

## Sampling assumptions — what uniform-interval breaks under

We assume samples are taken at uniform intervals, and downstream tools (rate computation, gap detection, averaging) lean on that.

What breaks the assumption:

1. **The monitor's iteration takes longer than the interval.** If a postgres query stalls for 30 seconds, the next sample is 30 seconds late. Subsequent samples are uniformly spaced again, but that one interval is wide.
2. **The monitor is killed and restarted.** Long gap; gap-aware plotting (see [01](01-time-series-fundamentals.md#why-naive-line-plots-mislead--the-gap-problem)) breaks the line across it rather than fabricating values.
3. **System sleep/hibernate.** Wall-clock advances; the monitor process doesn't. On resume, the next sample is wildly late.
4. **NTP clock jumps.** If the host's clock is corrected mid-run, `ts` values around the correction don't line up with reality. Rare; usually a sign of a misconfigured host.

The first two are handled (gap detection threshold = 5× interval). The last two we don't currently handle specifically — they're rare enough that we accept some noise rather than build elaborate detection.

## Aggregation in a comparison

When comparing two versions, the goal is to isolate "what's different between them" from "what's different about the conditions they ran in." Statistical control means:

- **Same chain content** — both versions should sync the same range of slots from the same network. The project ensures this via `--env`.
- **Same hardware** — comparing version A on a fast SSD against version B on a spinning disk gives you nothing about the versions.
- **Same monitoring overhead** — if you run the monitor for A but not for B, A's metrics include the (small) monitor overhead and B's don't. Always monitor both runs.
- **Same configuration** — don't change `--interval`, don't change db-sync config flags, don't change postgres settings between runs.

When all these match, the headline comparison number (e.g., "B is 27% faster") is meaningful. When they don't, the number includes noise from whichever variable wasn't controlled.

### Per-era as the granularity that matters

Aggregating to a single overall percentage often hides the real story. Two versions that are equal overall might be 30% different in one era and identical in others. The era-bar comparison is what tells you "the regression lives in Conway specifically" — actionable information rather than just a leaderboard.

The general principle: when you have an aggregate, also have its breakdown. The breakdown is what makes the aggregate interpretable.

## Confidence and noise

A single run gives you one number. That number has noise in it from:

- Host load variability (other processes, disk activity, network).
- Postgres autovacuum / autoanalyze timing (background work can slow specific intervals).
- Chain content variability (slightly different sets of transactions on slightly different epoch ranges).
- Monitor sampling granularity.

Two runs of *the same version* on *the same host* will give slightly different numbers. How different? Depends on workload, but typically a few percent for total sync time and a fraction of a percent for total DB size.

When you compare two versions and the headline difference is in the same range as the run-to-run noise (a few percent), don't conclude. Run again. If the difference persists, it's real. If it averages out, it was noise.

We don't compute confidence intervals or do formal statistical tests in this project. For a research-grade conclusion you'd run each version 3–5 times and report `mean ± std`. For routine "did the new build regress" checks, a single run with the era-bar breakdown is enough — a real regression usually shows up plainly in at least one era.

## What we deliberately don't do

A few things you might expect from a "monitoring" project that we don't include:

- **Alerting / thresholds.** This is for offline analysis, not live alerting. If you want alerts on tip lag, point a real monitoring system (Prometheus + Alertmanager, etc.) at cardano-node's metrics endpoint.
- **Persistent dashboards.** Each plot is a one-shot HTML file. For ongoing dashboards, the canonical Cardano answer is Grafana scraping cardano-node's Prometheus endpoint.
- **Long-term forecasting.** We measure what happened; we don't try to predict what will. ARIMA, Holt-Winters, etc. are off-topic.
- **Anomaly detection.** Same — outliers are flagged visually by you, the human reader, not algorithmically.

The scope is deliberately narrow: collect comparable time-series for A/B testing of cardano-node and cardano-db-sync versions. Everything in the math is in service of that.

## Recap

- Mean, median, p95 answer different questions. Pick the right one for the use case — we use mean for totals, p95 for long-tailed distributions like tx size.
- Don't mean-of-means when you wanted overall-mean. Recompute from totals.
- Rate from a cumulative counter is a finite-difference derivative; honest about the average but blind to within-interval bursts.
- Uniform sampling is fragile under stalls/restarts/clock jumps; gap-aware plotting handles the first two.
- A meaningful version comparison controls for chain content, hardware, monitoring overhead, and configuration. Era-bar breakdown shows where the difference lives.
- Single-run noise is real; treat differences of a few percent as inconclusive without repeated runs.

Next: [05 — Database internals](05-database-internals.md).
