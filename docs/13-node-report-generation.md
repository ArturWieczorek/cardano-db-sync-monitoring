# 13 - Generating reports (cardano-node)

This doc explains how to use `scripts/node-stats-report.py` - the tool that turns
the cardano-node time-series plots into a tidy, per-environment **comparison
report**. It is written for someone who can run a command in a terminal but has
not used this tool before. Every term is explained in plain words. For *how the
tool works inside*, see [11 - Report generator internals](11-report-generator-internals.md)
(the same machinery backs both the db-sync and the node report).

> **This is the cardano-node sibling of the db-sync report.**
> - `db-sync-stats-report.py` (see [doc 10](10-generating-reports.md)) reads the
>   db-sync SQLite stats DB and assembles the db-sync plots.
> - `node-stats-report.py` (this doc) does the same for **cardano-node**: it reads
>   the node SQLite stats database the monitors wrote and assembles the
>   **CPU & RAM, Ingest, On-disk Size, and RTS** plots into a report. It never
>   touches Postgres.

---

## What it is, and why it exists

When you run a node and collect stats, you end up with plots: CPU & RAM, sync
time by era / per epoch, on-disk database size, and RTS/runtime gauges. To compare
two builds of cardano-node (an **LSM** UTxO-HD build vs an **InMemory** build) you
used to:

1. open each plot's HTML file in a browser,
2. click Plotly's little camera icon to save a PNG,
3. paste that PNG into a document by hand,
4. repeat for every plot, every build, every environment.

That is slow and easy to get wrong. `node-stats-report.py` does all of it
automatically: it builds every plot, saves the images, and writes a finished
report - one per environment - in the shape of `node-report-template.md`.

---

## The mental model (read this once)

Every sample the monitor records is stamped with a **version label** - a single
string that says *which run this row belongs to*. It has three parts:

```
cardano-node   11.0.1   preprod
└─ fixed prefix └─ token └─ the environment
```

- The **prefix** is always `cardano-node`.
- The **token** is the bit *you* chose when you collected the data (the monitor's
  `--node-ver`). It names the build.
- The **environment** is `mainnet`, `preprod`, or `preview`.

An **LSM build** and an **InMemory build** of the same version are simply *two
labels that differ only by an `LSM-` prefix on the token*:

```
cardano-node 11.0.1     preprod   ← InMemory build
cardano-node LSM-11.0.1 preprod   ← LSM build
```

Both already live in the same environment's stats database, side by side. The
report tool just needs you to tell it which two tokens to compare.

---

## Naming: the part that trips people up

This is the single most common source of errors, so it gets its own section.

### The tool wants the *token*, not the whole label

The `--inmemory`, `--lsm`, and `--compare-to` flags each take **just the token**
- the middle part of the label:

```bash
--inmemory 11.0.1
--lsm      LSM-11.0.1
```

You do **not** pass `cardano-node` or the environment - the tool adds those itself
(it already knows the env from `--env`).

### Don't know the exact token? List them

Ask the plotting tool what tokens exist in an environment's stats DB:

```bash
python3 scripts/node-plot.py --env mainnet --list
```

It prints the full labels; the token is the middle part. For example:

```
cardano-node LSM-11.0.1 mainnet
cardano-node 11.0.1 mainnet
```

so the tokens are `LSM-11.0.1` and `11.0.1`.

### The mistake that produces "unrecognized arguments"

If you paste the **whole label** (which contains spaces) without quotes:

```bash
# WRONG - the shell splits this into three words
--compare-to cardano-node 10.1.4 mainnet
```

your shell hands the tool three separate words. It reads `cardano-node` as the
value of `--compare-to`, then sees `10.1.4` and `mainnet` as stray arguments it
doesn't understand, and prints `error: unrecognized arguments: 10.1.4 mainnet`.

**The fix** is to pass just the token (no spaces, nothing to quote):

```bash
# RIGHT
--compare-to 10.1.4
```

---

## Quickstart

Compare the LSM and InMemory builds of one version in one environment:

```bash
python3 scripts/node-stats-report.py --env preprod \
    --inmemory 11.0.1 \
    --lsm      LSM-11.0.1
```

That writes `reports/cardano-node/preprod/` with a Markdown report (plus PNG
images) and a self-contained interactive HTML.

## More examples

```bash
# Every environment at once:
python3 scripts/node-stats-report.py --env all \
    --inmemory 11.0.1 --lsm LSM-11.0.1

# Add a "this version vs a previous version" comparison:
python3 scripts/node-stats-report.py --env preprod \
    --inmemory 11.0.1 --lsm LSM-11.0.1 --compare-to 10.1.4

# Interactive HTML only (no image dependency needed - see "Choosing a format"):
python3 scripts/node-stats-report.py --env preview --format html \
    --inmemory 11.0.1 --lsm LSM-11.0.1

# Mainnet, comparing against an older build, with a smaller HTML file:
python3 scripts/node-stats-report.py --env mainnet --html-max-points 4000 \
    --inmemory 11.0.1 --lsm LSM-11.0.1 --compare-to 10.1.4

# Only one build? That's fine - pass just one. Comparisons are skipped.
python3 scripts/node-stats-report.py --env preprod --lsm LSM-11.0.1
```

### All the flags

| Flag | Meaning |
|:---|:---|
| `--env` | `mainnet`, `preprod`, `preview`, a comma-separated list, or `all`. Picks `data/cardano-node/<env>.db`. **Required.** |
| `--inmemory <token>` | Version token of the InMemory build. |
| `--lsm <token>` | Version token of the LSM build. (Pass at least one of `--inmemory`/`--lsm`.) |
| `--compare-to <token>` | Optional. Adds a "this vs previous" section comparing your build(s) against this earlier version. |
| `--format md\|html\|both` | What to write. Default `both`. |
| `--outdir <dir>` | Where reports go. Default `reports/cardano-node`. |
| `--sqlite-db <path>` | Use a specific stats DB file (only with a single `--env`). |
| `--scale <n>` | PNG resolution multiplier. Default `2`. |
| `--html-max-points <N>` | Shrink the interactive HTML by thinning each line to <= N points. |

---

## What you get

For each environment, a folder `reports/cardano-node/<env>/` containing:

```
reports/cardano-node/preprod/
├── report.md                  ← the Markdown report
├── report.html               ← the interactive report (one self-contained file)
└── *.png                      ← one image per plot (referenced by report.md)
```

The report is laid out in these sections (the same shape as the template):

1. **InMemory version** - CPU & RAM, Ingest, On-disk Size, RTS.
2. **LSM version** - the same plots for the LSM build.
3. **LSM vs InMemory Comparison** - the two builds overlaid on one chart.
4. **This vs Previous** - only if you passed `--compare-to`.

Within a build, the metrics behave like this:

| Metric | Axes shown | Notes |
|:---|:---|:---|
| CPU & RAM (RSS) | slot **and** time | RSS + CPU% over chain position or wall-clock. |
| Ingest Metrics | epoch only | Sync time by era (bar) + per-epoch duration (line). This figure is keyed on epoch, so there is one of it, not a slot/time pair. |
| On-disk DB Size | slot **and** time | Total directory + `lsm/` subdir. **Optional** - from `node-db-size-monitor.py`. |
| RTS / Runtime Metrics | slot **and** time | GHC GC / allocation / heap / mempool gauges. **Optional** - from `node-rts-monitor.py`. |

> **Disk and RTS are optional.** They come from separate collectors that most DBs
> won't have run. If an environment's stats DB has no `disk_metrics` /
> `rts_metrics` rows for your build, those plots are **silently skipped** - the
> rest of the report is still produced. So a report with no Disk/RTS sections just
> means those collectors weren't running for that build.

While it runs, it prints progress so you can see it's working, e.g.
`[mainnet] rendering PNG 7/17: …` and the final HTML size.

> The output folder is **wiped of its own old files** (`*.png`, `report.md`,
> `report.html`) at the start of each run, so a re-run never leaves stale or
> mismatched images behind.

---

## Choosing a format

| Format | What it is | Good for | Notes |
|:---|:---|:---|:---|
| `md` | A `report.md` plus PNG images | Pasting into wikis / Confluence / GitHub | Needs the `kaleido` image backend (see below). It's a *folder* (the `.md` plus its images), not one file. |
| `html` | One self-contained interactive page | Exploring (zoom, hover); emailing a single file | No extra dependency. Can be **large** because it embeds every data point - the RTS panels especially. |
| `both` | Both of the above | The default | |

### Installing the image backend (for PNG / Markdown)

PNG export uses a package called **kaleido**. Install it once:

```bash
pip install '.[report]'      # or: pip install kaleido
```

If it's missing and you ask for PNGs, the tool tells you exactly this. HTML output
needs nothing extra.

### Making the HTML smaller: `--html-max-points`

The interactive HTML embeds every sample, so a multi-day run gets big - and the
RTS collector samples a lot of metrics, so node reports can be larger than db-sync
ones. A screen is only ~1500 pixels wide, so you don't need 100,000 points to see a
smooth curve. `--html-max-points N` thins each line to at most `N` points **in the
HTML only** (the PNGs stay full resolution), and it's still one self-contained
file. `~4000` is a good balance - visually identical for these curves.

---

## Why the comparison charts use the slot (and epoch) axis

A single build's CPU & RAM / Disk / RTS plots offer two x-axes: **slot** (chain
position) and **time** (wall clock). For looking at *one* run, time is fine.

But the **comparison** charts overlay *two different runs*, and those runs happened
at different wall-clock times (the InMemory run in the morning, the LSM run in the
afternoon, say). On a time axis they'd sit in separate parts of the chart and never
line up - useless for comparison. The **slot number** is the *same chain position*
in both runs, so plotting against slot lines the two builds up point-for-point.
That's why the CPU & RAM and Disk comparison charts use the slot axis. The Ingest
comparison is keyed on **epoch**, which is likewise a shared chain position, so it
already lines the runs up.

The dense RTS plots are intentionally left out of the comparison overlays (they are
many panels and read better per-build); they still appear in each build's own
section.

---

## Troubleshooting

| Symptom | Cause & fix |
|:---|:---|
| `error: argument --inmemory: expected one argument` | You wrote the flag with **no value** after it. Each of `--inmemory` / `--lsm` / `--compare-to` needs the build's **token** right after it, e.g. `--inmemory 11.0.1`. Find the tokens with `node-plot.py --env <env> --list` (the token is the middle part of each label). |
| `error: unrecognized arguments: 10.1.4 mainnet` | You passed a full label with spaces, unquoted. Pass just the **token** (`--compare-to 10.1.4`), or quote the whole label. See [Naming](#naming-the-part-that-trips-people-up). |
| `PNG export needs the 'kaleido' package…` | Install it: `pip install '.[report]'`. Or use `--format html`, which needs nothing extra. |
| The `report.html` is huge / slow to open | Use `--html-max-points 4000`, or `--format md` for small images. |
| No Disk or RTS section appears | That build has no `disk_metrics` / `rts_metrics` rows (those optional collectors weren't run for it). The rest of the report is still correct. |
| A section is missing or its plots are empty | That build/axis has no rows in the stats DB for that token. Check the token with `node-plot.py --env <env> --list`. If only one build exists, comparisons are skipped on purpose. |

---

## See also

- [10 - Generating reports (cardano-db-sync)](10-generating-reports.md) - the
  db-sync sibling of this guide.
- [11 - Report generator internals](11-report-generator-internals.md) - how the
  code works, step by step (shared by both reports).
- [03 - Graph catalog](03-graph-catalog.md) - what each plot means and how to read it.
- [12 - Useful queries](12-useful-queries.md) - pull the same numbers (peak RAM,
  disk size, RTS heap) straight from the stats DB with SQL.
- The main [README](../README.md) `# node-stats-report.py` section - the quick
  command reference.
