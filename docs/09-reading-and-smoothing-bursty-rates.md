# 09 - Reading and smoothing bursty rates

The block-insert-rate and tx-insert-rate panels look jagged: a mostly-low line with occasional
spikes shooting far up. This doc explains, from first principles, **how to measure "how spiky is
this, really?"**, what each number means, and **which charting tricks actually make a spiky series
readable** (and which barely help). Every worked example uses real numbers measured from the mainnet
stats database, so you can reproduce them.

If you have not yet read why a rate is a *finite difference* of a counter, read the "Derivatives:
rate from a cumulative counter" section of [04 - Statistics primer](04-statistics-primer.md) first.
This doc picks up where that leaves off.

---

## 1. What "spiky" means, and why we should not just eyeball it

When we say a chart is "spiky" we mean two different things can be true, and they call for **opposite**
responses:

- **Noise**: random, meaningless wobble around the real value. The underlying thing is steady; the
  *measurement* jitters. You want to **smooth this away** so the real trend shows.
- **Signal**: real, meaningful change. The thing genuinely sped up or slowed down. Smoothing this is
  **destroying information** - you would be hiding something true.

The danger is treating signal as noise. So before reaching for a smoothing trick, we *measure* the
spikiness and ask where it comes from. The rest of this doc is the vocabulary for doing that.

> Analogy: a heart-rate monitor. If the trace jitters by one beat because the sensor slipped, that is
> noise - smooth it. If it spikes because the patient sprinted up the stairs, that is signal - do not
> erase it. Same-looking wiggle, completely different meaning.

---

## 2. The five numbers we use to describe a series

We will build these up one at a time with a tiny example, then show the real mainnet value. The tiny
example is this list of seven "blocks per second" readings:

```
10, 12, 11, 13, 9, 12, 200
```

Six readings cluster around 11; one is a 200 spike.

### 2.1 Mean (the average)

Add them up, divide by how many. `(10+12+11+13+9+12+200) / 7 = 38.1`.

Notice the problem already: the **mean is 38**, but **six of the seven readings are near 11**. One
spike dragged the average up to a value that describes *none* of the typical readings. The mean is
**sensitive to outliers**.

### 2.2 Standard deviation (how spread out)

Standard deviation (`std`) measures how far, on average, readings sit from the mean. Small `std` = the
points hug the mean (a thin band); large `std` = they are scattered widely. For the example the `std`
is about **70** - which is *larger than the mean*. That alone tells you the data is not a tidy band;
it is dominated by spread.

You do not need to compute `std` by hand; just read it as "the typical distance from the average".

### 2.3 Coefficient of variation (spread *relative to size*) - our main "spikiness" number

A `std` of 70 is huge for data centred on 11, but tiny for data centred on a million. So raw `std` is
not comparable across series of different scale. Divide it by the mean and you get a **scale-free**
spikiness number:

```
coefficient of variation (CV) = std / mean
```

- `CV` near **0** = almost flat (every reading nearly equal).
- `CV` around **0.2 - 0.5** = gently wavy.
- `CV` **above ~1** = very spiky; the spread is as big as the signal itself.

For the tiny example, `CV = 70 / 38 ~= 1.8` - confirming "very spiky". `CV` is the first number we
look at because it answers "spiky compared to what?" in one figure.

### 2.4 Percentiles (the shape of the tail): p50, p95, p99, max

A percentile is "the value below which this fraction of readings fall, once sorted". The **p50** (also
called the median) is the middle reading; **p95** is near the high end; **max** is the single biggest.

Why we need them: mean and `std` give two summary numbers, but they cannot tell you *whether* the
spread is a gentle wave or a flat line with a few rockets. Percentiles show the **shape**:

For the tiny example, sorted: `9, 10, 11, 12, 12, 13, 200`.
- **p50 (median) = 12** - the typical reading, **unmoved by the 200 spike** (it just sits at the
  middle of the sorted list).
- **max = 200**.

The gap between a modest median and a giant max is the fingerprint of a **heavy tail**: most readings
are small, a few are enormous. Hold onto that idea - it is the whole story for block rate.

### 2.5 Jumpiness (point-to-point choppiness)

`CV` tells you the spread, but not whether the series **wiggles fast** (up-down-up-down between
neighbours) or **drifts slowly** (a smooth ramp that happens to span a wide range). Two series can
have the same `CV` and look completely different. To separate them we measure the **average step
between neighbouring samples**, as a fraction of the typical value:

```
jumpiness = average of |reading_i - reading_(i-1)|  /  mean
```

(The numerator has a formal name, the "mean absolute successive difference"; do not worry about the
name.) Read it as: "from one sample to the next, the line typically moves this fraction of its own
height."

- Low jumpiness (near 0) = a smooth line; neighbours are close, change is gradual.
- High jumpiness = a saw-tooth; the line lurches between neighbours.

This is what tells you whether **short-window smoothing** can help: smoothing fixes *fast* wiggle
(high jumpiness), but cannot fix a wide-but-smooth drift.

---

## 3. The real mainnet numbers

Measured from `data/cardano-db-sync/mainnet.db`, across ~25,000 to ~28,000 ten-second samples per
version (cardano-db-sync 13.7.1.0 standard, and the LSM build). The two versions agree closely; the
standard build is shown.

| series | mean | std | CV | p50 | p95 | p99 | max | jumpiness |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **block_rate** (blocks/s) | 47.5 | 141.2 | **2.97** | 27.2 | 90.3 | **897** | **2094** | 0.26 |
| **tx_rate** (tx/s)        | 424  | 277  | **0.65** | 442  | 919  | 1288 | 2787 | 0.30 |

How to read this:

- **block_rate has `CV ~= 3`** - extremely spiky. And look at the tail: the **median is 27**, but the
  **p99 is 897 and the max is 2094**. That is a textbook heavy tail - the typical second adds ~27
  blocks, but a rare second adds 30x to 70x that. The single biggest spike is **77x the median**.
- **tx_rate has `CV ~= 0.65`** - moderately spiky, with a much gentler tail (p99 is ~3x the median,
  not 30x).
- **jumpiness is low-ish (~0.26 - 0.30) for both.** This is the crucial clue: the series are *not*
  fast saw-teeth. They are mostly smooth, with a few isolated giant excursions. That predicts (section
  5) that a short rolling average will barely help block_rate.

The sampling interval, by the way, is rock-steady (median 10.3 s, only one or two samples in 27,000
ran long). So the spikes are **not** caused by uneven sampling - the timing is honest. The spikiness
is in the data itself.

---

## 4. Where the spikes come from: two very different sources

We split block_rate by **position in the chain** (which is a proxy for which Cardano era db-sync was
ingesting, and therefore which phase of the sync we are in):

| chain position | what it is | mean block_rate | median | max |
|---|---|---:|---:|---:|
| block_no < 4.49M | **Byron era** (tiny old blocks, bulk-loaded) | **~1080 /s** | ~1080 | 2094 |
| block_no >= 4.49M | **Shelley to today** (normal-size blocks) | **32 /s** | 23 | 752 |

This is the heart of it. The giant spikes are **not random noise**. They are the **Byron bulk-load
phase**: early Cardano blocks are tiny, so db-sync pours them in at ~1080 per second, then drops to
~23 per second once it reaches modern blocks. That is a genuine, ~50x change in real ingest speed -
**signal, not noise**. Smoothing it would hide a true and interesting feature (the era transition).

On top of that real regime change, there is a second, smaller source *within* the modern phase: db-
sync **commits blocks in batches**. Between two samples it might commit nothing (rate 0), then commit
a batch (a spike). That part *is* a measurement artifact - the cumulative counter is always correct,
but the *instantaneous* rate over one 10-second window misrepresents a burst as if it were a steady
flow. (This is the "bursty commits" note in doc 04.)

So block_rate's spikiness is **mostly real signal (the era/phase regime change) plus a little genuine
noise (batched commits)**. That mix is exactly why the obvious fix does not work.

---

## 5. The presentation techniques, and when each actually helps

Now the useful part: the tricks for making a spiky series readable, what each one does, and - using
the numbers above - which ones help *here*.

### 5.1 Rolling mean (a.k.a. moving average)

**What it is.** Replace each point with the average of itself and its neighbours in a small window.
A "5-sample" window at 10 s per sample is a 50-second average.

Tiny example, smoothing `10, 12, 11, 13, 9, 12, 200` with a 3-wide mean: the point that was `13`
becomes `average(11, 13, 9) = 11`, and so on. The everyday wobble (10 vs 12 vs 11) blurs into a
steady ~11 line - good. **But** the `200` spike, averaged with its neighbours `9` and `12`, becomes
`(9 + 12 + 200)/3 = 74` - the mean **drags the spike sideways into its neighbours** instead of
removing it. One rocket becomes three small flares.

**Does it help our data?** We measured the `std` before and after a 5-sample rolling mean:

| series | std raw | std after 5-sample mean | reduction |
|---|---:|---:|---:|
| block_rate | 141.2 | 139.4 | **1%** (useless) |
| tx_rate    | 276.8 | 241.7 | **13%** (modest) |

This confirms the prediction from the low jumpiness. A short rolling mean only kills *fast* wiggle.
block_rate's spread is dominated by **isolated giant excursions** (the Byron regime and the rare
burst), which a 5-sample mean cannot touch - it just smears them. tx_rate, being gentler, gets a
modest 13% tidy-up. **Takeaway: a 5-sample rolling mean is the wrong tool for the worst panel.**

### 5.2 Rolling median

**What it is.** Same sliding window, but take the **median** (middle value) of the window instead of
the average.

Why it is different: the median **ignores** an outlier instead of averaging it in. Smooth
`10, 12, 11, 13, 9, 12, 200` with a 3-wide median and the window `9, 12, 200` yields `12` - the spike
is **dropped entirely**, not smeared. A rolling median flattens the everyday wobble *and* removes
isolated spikes without creating fake side-flares.

**When to use it.** Exactly our situation: a mostly-smooth line punctuated by rare giant spikes
(batched-commit bursts). A rolling median tames those far better than a rolling mean. The trade-off:
a median is slightly less faithful to genuine fast trends and costs a little more to compute - both
irrelevant at our data size.

### 5.3 Logarithmic y-axis

**What it is.** Instead of the axis going `0, 500, 1000, 1500, 2000` (equal *additions*), it goes
`1, 10, 100, 1000` (equal *multiplications*). Each step up the axis is "10x bigger", not "500 more".

**Why it is the real fix for block_rate.** block_rate spans ~23/s (modern) to ~1080/s (Byron) to a
2094 max - a **~50x to ~90x range**. On a linear axis sized to fit 2094, the entire modern phase
(23/s) is squashed into a flat smudge at the very bottom: you literally cannot see the detail that
matters most. A log axis gives the small values room to breathe *and* still shows the big ones,
because it cares about *ratios*, not *absolute size*.

Tiny example: plot `1, 10, 100, 1000` on a linear axis and the first three points are glued to the
floor under the 1000. On a log axis they are **four evenly spaced rungs** - all readable.

> Analogy: earthquakes. The Richter scale is logarithmic precisely because quakes range from
> imperceptible to catastrophic. A linear "energy released" axis would put every quake you can feel at
> the bottom and only the world-enders would be visible. Log scale lets you see the whole family.

A log axis is the single biggest readability win here, and - importantly - it **distorts nothing and
hides nothing**: every real point is still plotted, including the Byron regime and the spikes. It only
changes the *spacing* of the axis. That is why it is the right move when the spread is **real signal**
(a wide dynamic range) rather than noise.

### 5.4 Percentile clipping (capping the axis)

**What it is.** Cap the visible axis at, say, the p99 value, so the handful of monster spikes do not
set the scale. The spikes are still there; they just pile up at the top edge.

**When to use it.** A quick alternative to a log axis when you only care about the typical range and
are content to let the extreme few "max out" off the top. Cheaper to reason about than log scale, but
it *does* hide the true height of the outliers, so use it knowingly.

### 5.5 Smaller sampling interval (collect differently)

The batched-commit artifact (section 4) exists because one 10-second window can straddle a burst.
Sampling more often (`--interval 2`) narrows each window so a burst lands in fewer samples and the
rate is closer to instantaneous. The cost is more monitor overhead and a bigger stats DB. This fixes
the *cause* rather than the *display*, but it cannot help the Byron regime difference (that is real),
and it does not help data you have already collected.

---

## 6. A decision guide

Put it together as a recipe. Ask in order:

1. **Is the spread real signal or measurement noise?** Split the data (here, by chain position) and
   look. If a "spike region" corresponds to a real phase (Byron bulk-load), it is signal - do **not**
   smooth it away; reach for a **log axis** so you can see all phases at once.
2. **Is there leftover fast wiggle on top?** Check jumpiness, or just eyeball it. High jumpiness =
   genuine fast noise = a **rolling mean** helps. Low jumpiness with isolated spikes (our case) = a
   **rolling median** helps; a rolling mean does not.
3. **Do a few extreme outliers dominate the axis?** Use a **log axis** (keeps them visible) or
   **percentile clipping** (hides their height but frees the scale).
4. **Is the artifact in how you sampled?** If bursts straddle your interval, sample **more often** -
   but only for future runs.

For our two panels specifically, the evidence says: **block_rate** wants a **log y-axis** (to handle
the 50x era range) and optionally a **rolling median** (for the residual bursts); a 5-sample rolling
mean is nearly useless for it (1% reduction). **tx_rate** is mild enough that a small **rolling mean**
(13% reduction) is a reasonable light touch, though it hardly needs one.

And one rule above all, matching this project's "opt-in, default-off" convention: **any smoothing must
be optional and must not be the only view.** The raw series is the source of truth; a smoothed or
log-scaled view is a *reading aid* layered on top. Never let a presentation trick silently erase a
real feature of the data - the Byron ramp is a feature, not a flaw.

---

## 7. One-paragraph summary

A rate built as `Δcounter / Δtime` is honest but jagged. To judge the jaggedness, use the
**coefficient of variation** (`std / mean`, scale-free spikiness), **percentiles** (the tail shape -
a small median with a huge max means rare giant spikes), and **jumpiness** (fast wiggle vs slow
drift). On mainnet, block_rate is extremely spiky (`CV ~= 3`) but mostly because of a **real** 50x
speed difference between the Byron bulk-load and the modern chain - signal, not noise - so the right
fix is a **logarithmic y-axis** (and optionally a **rolling median** for the leftover batched-commit
bursts), **not** a short rolling mean, which we measured to cut block_rate's spread by only 1%.
