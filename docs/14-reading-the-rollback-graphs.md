# 14 - Reading the rollback graphs

A plain-language, step-by-step guide to the rollback performance plot
(`db-sync-plot.py --metrics rollback`). No prior monitoring experience assumed.

If you just want the one-line version: **a rollback is db-sync throwing away
blocks it already stored (because the chain changed under it) and then
re-adding them. This graph shows how often that happens, how deep it goes, how
long the throwing-away takes, and how long it takes to catch back up.** A new
db-sync version that does any of those noticeably slower is a regression.

---

## What a rollback actually is (30 seconds)

db-sync copies the blockchain into Postgres, block by block. Occasionally the
node tells it "the last N blocks you stored are no longer the real chain - a
different version won" (a *reorg*). db-sync then:

1. **Deletes** those N blocks and everything attached to them (transactions,
   outputs, etc.) from Postgres. This is the **deletion phase**.
2. **Re-applies** the new blocks from the node until it's caught up again. This
   is the **recovery phase**.

Both phases cost time. A deep rollback, or a slow deletion, is exactly the kind
of thing that can quietly get worse between versions - so we measure it.

---

## Opening the graph

It's an interactive HTML file (made with Plotly). Open it in any browser:

```
plots/cardano-db-sync/<env>/<env>_<version>_rollback_by_time.html
```

You can:
- **hover** any line or dot to see exact numbers,
- **drag** a box to zoom in, **double-click** to reset,
- **click a name in the legend** to show/hide that series.

There are two versions of the file: `_by_time` (x-axis = wall-clock time) and
`_by_slot` (x-axis = chain position). **For reading rollbacks, prefer
`_by_time`** - it spreads the samples out evenly. `_by_slot` is for lining two
runs up by chain position, but it bunches up when db-sync sits at the tip (many
samples at the same slot).

---

## The layout: three stacked charts

All three share the same horizontal axis, so a vertical slice through all three
is "the same moment." Read left to right = time (or chain) moving forward.

```
  Panel 1   DB Event Queue Length        (how busy db-sync's to-do list is)
  Panel 2   Node-DB Block-Height Gap     (how far behind db-sync is)
  Panel 3   Rollback Event Duration      (one dot per rollback, height = seconds)
```

A **dashed red vertical line** is drawn through all three panels at each
rollback, labelled with its depth (e.g. `rollback 5185 blk`) at the top. That's
your "a rollback happened *here*" marker.

**Where are the x-axis numbers?** They appear only on the **bottom** panel. The
top two panels deliberately hide their tick labels because all three share one
axis - showing the same numbers three times would just be clutter. So read the
slot/time off the bottom chart; a vertical position is the same moment in all
three. (Hovering any line also shows the exact value, including on the top
panels.) This is the same convention every multi-panel plot in this project
uses.

---

## Panel 1 - DB Event Queue Length

- **What it measures**: how many work items are waiting in db-sync's internal
  queue. Think of it as the length of its to-do list.
- **Calm**: 0-1. db-sync is keeping up with the chain.
- **Busy**: tens. db-sync has a backlog it's chewing through (normal during
  catch-up sync).
- **How a rollback shows**: a **spike** - the reorg suddenly dumps work on the
  queue. This is the "Queue Length" panel operators watch in Grafana.
- **Regression signal**: for the same rollback, a new version whose queue climbs
  **higher** or **stays elevated longer** is doing more work or clearing it more
  slowly.

## Panel 2 - Node-DB Block-Height Gap

- **What it measures**: how many blocks **behind** the node db-sync is
  (`node tip - db tip`). `0` means fully caught up.
- **Healthy**: flat near 0 when at the tip.
- **How a rollback shows**: the gap **jumps up** the instant the rollback throws
  the db backwards, then **slopes back down to 0** as db-sync re-applies blocks.
  That downward slope **is** the recovery - steeper slope = faster recovery.
- **Regression signal**: a new version whose gap takes **longer to slope back to
  0** after the same rollback recovers more slowly.

## Panel 3 - Rollback Event Duration (seconds)

This panel has **one dot per rollback**, positioned at the moment it happened,
with **height = how many seconds it took**. Hover a dot for the depth.

There are two dot types (see the legend):

- **`Deletion (log)`** (an `x` marker): how long the **deletion phase** took,
  read straight from db-sync's log. This is the regression-prone number.
- **`Recovery (metrics)`** (a round marker): how long the **recovery phase**
  took (db tip climbing back to where it was), derived from the metric series.

You may see only one type on a given rollback - that's expected, and the next
section explains why.

---

## Two ways a rollback is captured (and why a panel can be empty)

The monitor watches db-sync two ways at once:

1. **The metrics endpoint** (`:8080`) - sampled every couple of seconds. Good for
   the continuous picture (queue, gap) and for the **recovery** time.
2. **The db-sync log** (`--log-file`) - read line by line. Good for the **exact
   deletion duration** and the per-table delete counts.

Most of the time both see a rollback and you get both dots. But if a rollback
happens while the metrics endpoint is briefly down - most commonly when you
**restart** db-sync with `--rollback-to-slot` to force one, since the endpoint
isn't up during the restart - then only the **log** catches it. In that case you
see the `Deletion (log)` dot and the red marker line, but **no** `Recovery`
dot, and the gap/queue curves won't show the dip (it fell in the sampling gap).

That's not a bug - it's why we read the log as well as the metrics. The
authoritative deletion measurement is still captured. On a **naturally**
occurring reorg (no restart), the metrics stay continuous and you get the full
picture including the recovery dot.

---

## Worked example (from a real validation run)

A forced rollback on preview produced:

- A red marker line labelled **`rollback 5185 blk`** (5185 blocks rolled back).
- A `Deletion (log)` dot at **~22.8 seconds** - that's how long Postgres took to
  delete those 5185 blocks and everything attached.
- No `Recovery` dot (this was a forced restart, so the recovery fell in the
  metrics gap - see above).

The per-table breakdown (in `rollback_table_deletes`) showed where the time
goes: `ma_tx_out` 17005 rows, `tx_out` 13930, `tx_in` 12951, ... `block` 5185.
That table is the detail behind the single deletion number.

---

## How to spot a regression (the whole point)

Run the same rollback on two db-sync versions (see the controlled benchmark,
below) and compare. **Version B is worse if**, for the same rollback depth:

- its **`Deletion (log)`** dot is higher (deletion got slower), or
- its **gap** in Panel 2 takes longer to slope back to 0 (recovery got slower), or
- its **queue** in Panel 1 spikes higher or drains slower.

A small, occasional rollback is normal and healthy. A **lengthy** rollback - a
deletion that takes minutes instead of seconds - is the failure mode this whole
feature exists to catch early.

---

## Where the raw numbers live

The graph is the visual; the exact data is in the SQLite stats DB (and the
benchmark CSVs):

- `rollback_samples` - the raw time series behind Panels 1 and 2.
- `rollback_events` - one row per detected rollback (depth, deletion duration,
  recovery duration, target slot, source = `log` or `metrics`).
- `rollback_table_deletes` - per-table deleted-row counts for each rollback.
- `rollback_benchmarks` - results of the controlled cross-version benchmark
  (`db-sync-rollback-benchmark.py`), which times the deletion phase of a chosen
  rollback against one version's `cardano-db-tool`, repeated N times.

See the README sections "Track cardano-db-sync rollbacks" and "Benchmark a
controlled rollback" for how to collect this data, and
[03 - Graph catalog](03-graph-catalog.md#rollback-performance-graphs-db-sync---metrics-rollback)
for the terse per-panel reference.
