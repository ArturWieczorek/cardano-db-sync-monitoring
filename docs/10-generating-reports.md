# 10 - Generating reports (cardano-db-sync)

This doc explains how to use `scripts/db-sync-stats-report.py` - the tool that
turns the time-series plots into a tidy, per-environment **comparison report**.
It is written for someone who can run a command in a terminal but has not used
this tool before. Every term is explained in plain words. For *how the tool
works inside*, see [11 - Report generator internals](11-report-generator-internals.md).

> **Two tools, similar names - don't mix them up.**
> - `db-sync-epoch-report.py` talks to **PostgreSQL** and produces a per-epoch /
>   size / summary report about the chain data itself.
> - `db-sync-stats-report.py` (this doc) reads the **SQLite stats database**
>   that the monitors wrote, and assembles the **resource/ingest plots** into a
>   report. It never touches Postgres.

---

## What it is, and why it exists

When you run a sync and collect stats, you end up with plots: CPU & RAM, ingest
metrics, table row counts. To compare two builds of db-sync (an **LSM** build
vs an **InMemory** build) you used to:

1. open each plot's HTML file in a browser,
2. click Plotly's little camera icon to save a PNG,
3. paste that PNG into a document by hand,
4. repeat for every plot, every build, every environment.

That is slow and easy to get wrong. `db-sync-stats-report.py` does all of it
automatically: it builds every plot, saves the images, and writes a finished
report - one per environment - in the exact shape of
`db-sync-report-template.md`.

---

## The mental model (read this once)

Every sample the monitor records is stamped with a **version label** - a single
string that says *which run this row belongs to*. It has three parts:

```
cardano-db-sync   13.7.1.0-node-11.0.1   preprod
└─ fixed prefix   └─ the version token   └─ the environment
```

- The **prefix** is always `cardano-db-sync`.
- The **token** is the bit *you* chose when you collected the data (the monitor's
  `--db-sync-ver`). It names the build.
- The **environment** is `mainnet`, `preprod`, or `preview`.

An **LSM build** and an **InMemory build** of the same version are simply *two
labels that differ only by an `LSM-` prefix on the token*:

```
cardano-db-sync 13.7.1.0-node-11.0.1     preprod   ← InMemory build
cardano-db-sync LSM-13.7.1.0-node-11.0.1 preprod   ← LSM build
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
--inmemory 13.7.1.0-node-11.0.1
--lsm      LSM-13.7.1.0-node-11.0.1
```

You do **not** pass `cardano-db-sync` or the environment - the tool adds those
itself (it already knows the env from `--env`).

### These are the SQLite stats labels - NOT your Postgres database names

This matters. The names you gave your **Postgres** databases, like:

```
lsm-mainnet-dbsync-13.7.1.0-node-11.0.1
fix-port-preprod-dbsync-13.7.1.0-node-11.0.1
```

are **not** what this tool uses. It uses the **version token** that was stored
in the SQLite stats DB when you ran the monitor. In the examples above the token
is `13.7.1.0-node-11.0.1` (and `LSM-13.7.1.0-node-11.0.1` for the LSM build) -
the chain-version part, not the Postgres database name. If you collected a
special build (a `fix`, a pre-release, etc.), whatever token you gave the
monitor at collection time is what you pass here.

### Don't know the exact token? List them

Ask the plotting tool what tokens exist in an environment's stats DB:

```bash
python3 scripts/db-sync-plot.py --env mainnet --list
```

It prints the full labels; the token is the middle part. For example, mainnet
currently has these tokens:

```
LSM-13.7.1.0-node-11.0.1
13.7.1.0-node-11.0.1
13.7.0.4-with-offchain-data
13.7.0.3
13.6.0.5-hasql
13.6.0.5-standard-disable-ledger
13.6.0.5-consumed-all-options-on
13.6.0.5-genesis
```

### The mistake that produces "unrecognized arguments"

If you paste the **whole label** (which contains spaces) without quotes:

```bash
# WRONG - the shell splits this into three words
--compare-to cardano-db-sync 13.6.0.5-genesis mainnet
```

your shell hands the tool three separate words. It reads `cardano-db-sync` as
the value of `--compare-to`, then sees `13.6.0.5-genesis` and `mainnet` as stray
arguments it doesn't understand, and prints:

```
error: unrecognized arguments: 13.6.0.5-genesis mainnet
```

**The fix** is to pass just the token (no spaces, nothing to quote):

```bash
# RIGHT
--compare-to 13.6.0.5-genesis
```

If you ever *want* to pass a full label, you have to wrap it in quotes so the
shell treats it as one argument: `--compare-to 'cardano-db-sync 13.6.0.5-genesis mainnet'`.
But the token is shorter and is what you'll normally use.

---

## Quickstart

Compare the LSM and InMemory builds of one version in one environment:

```bash
python3 scripts/db-sync-stats-report.py --env preprod \
    --inmemory 13.7.1.0-node-11.0.1 \
    --lsm      LSM-13.7.1.0-node-11.0.1
```

That writes `reports/cardano-db-sync/preprod/` with a Markdown report (plus PNG
images) and a self-contained interactive HTML.

## More examples (all using real tokens)

```bash
# Every environment at once:
python3 scripts/db-sync-stats-report.py --env all \
    --inmemory 13.7.1.0-node-11.0.1 --lsm LSM-13.7.1.0-node-11.0.1

# Add a "this version vs a previous version" comparison:
python3 scripts/db-sync-stats-report.py --env preprod \
    --inmemory 13.7.1.0-node-11.0.1 --lsm LSM-13.7.1.0-node-11.0.1 \
    --compare-to 13.6.0.5-genesis

# Interactive HTML only (no image dependency needed - see "Choosing a format"):
python3 scripts/db-sync-stats-report.py --env preview --format html \
    --inmemory 13.7.1.0-node-11.0.1 --lsm LSM-13.7.1.0-node-11.0.1

# Mainnet, comparing against an older build, with a smaller HTML file:
python3 scripts/db-sync-stats-report.py --env mainnet --html-max-points 4000 \
    --inmemory 13.7.1.0-node-11.0.1 --lsm LSM-13.7.1.0-node-11.0.1 \
    --compare-to 13.7.0.3

# Only one build? That's fine - pass just one. Comparisons are skipped.
python3 scripts/db-sync-stats-report.py --env preprod --lsm LSM-13.7.1.0-node-11.0.1
```

### All the flags

| Flag | Meaning |
|:---|:---|
| `--env` | `mainnet`, `preprod`, `preview`, a comma-separated list, or `all`. Picks `data/cardano-db-sync/<env>.db`. **Required.** |
| `--inmemory <token>` | Version token of the InMemory build. |
| `--lsm <token>` | Version token of the LSM build. (Pass at least one of `--inmemory`/`--lsm`.) |
| `--compare-to <token>` | Optional. Adds a "this vs previous" section comparing your build(s) against this earlier version. |
| `--format md\|html\|both` | What to write. Default `both`. |
| `--outdir <dir>` | Where reports go. Default `reports/cardano-db-sync`. |
| `--sqlite-db <path>` | Use a specific stats DB file (only with a single `--env`). |
| `--scale <n>` | PNG resolution multiplier. Default `2`. |
| `--html-max-points <N>` | Shrink the interactive HTML by thinning each line to ≤ N points (see below). |

---

## What you get

For each environment, a folder `reports/cardano-db-sync/<env>/` containing:

```
reports/cardano-db-sync/preprod/
├── report.md                  ← the Markdown report
├── report.html               ← the interactive report (one self-contained file)
└── *.png                      ← one image per plot (referenced by report.md)
```

The report is laid out in four sections (the same shape as the template):

1. **InMemory version** - CPU & RAM, Ingest Metrics, Table Row Counts, each on
   both the slot axis and the time axis.
2. **LSM version** - the same plots for the LSM build.
3. **LSM vs InMemory Comparison** - the two builds overlaid on one chart so you
   can see the difference directly (slot axis - see below).
4. **This vs Previous** - only if you passed `--compare-to`.

While it runs, it prints progress so you can see it's working, e.g.
`[mainnet] rendering PNG 7/16: …` and the final HTML size.

> The output folder is **wiped of its own old files** (`*.png`, `report.md`,
> `report.html`) at the start of each run, so a re-run never leaves stale or
> mismatched images behind.

---

## Choosing a format

| Format | What it is | Good for | Notes |
|:---|:---|:---|:---|
| `md` | A `report.md` plus PNG images | Pasting into wikis / Confluence / GitHub | Needs the `kaleido` image backend (see below). It's a *folder* (the `.md` plus its images), not one file. |
| `html` | One self-contained interactive page | Exploring (zoom, hover); emailing a single file | No extra dependency. Can be **large** (mainnet ~94 MiB) because it embeds every data point. |
| `both` | Both of the above | The default | |

### Installing the image backend (for PNG / Markdown)

PNG export uses a package called **kaleido**. Install it once:

```bash
pip install '.[report]'      # or: pip install kaleido
```

If it's missing and you ask for PNGs, the tool tells you exactly this. HTML
output needs nothing extra.

### Making the HTML smaller: `--html-max-points`

The interactive HTML embeds every sample, so a multi-day run gets big. A screen
is only ~1500 pixels wide, so you don't need 100,000 points to see a smooth
curve. `--html-max-points N` thins each line to at most `N` points **in the HTML
only** (the PNGs stay full resolution), and it's still one self-contained file.

`~4000` is a good balance - visually identical for these curves, and roughly
halves the file:

| | default | `--html-max-points 4000` |
|:---|:---|:---|
| mainnet `report.html` | ~94 MiB | ~25 MiB |
| preprod `report.html` | ~20 MiB | ~11 MiB |

Lower values barely help beyond this - the leftover size is the number of panels
plus Plotly's library, not the points. For a genuinely small artifact on
mainnet, the Markdown+PNG format (a folder, but each PNG is well under 1 MiB) is
the lighter option.

---

## Why the comparison charts use the slot axis

A single build's plots offer two x-axes: **slot** (chain position) and **time**
(wall clock). For looking at *one* run, time is fine.

But the **comparison** charts overlay *two different runs*, and those runs
happened at different wall-clock times (the InMemory run in the morning, the LSM
run in the afternoon, say). On a time axis they'd sit in separate parts of the
chart and never line up - useless for comparison. The **slot number** is the
*same chain position* in both runs, so plotting against slot lines the two builds
up point-for-point. That's why every comparison section uses the slot axis.

---

## Troubleshooting

| Symptom | Cause & fix |
|:---|:---|
| `error: unrecognized arguments: 13.6.0.5-genesis mainnet` | You passed a full label with spaces, unquoted. Pass just the **token** (`--compare-to 13.6.0.5-genesis`), or quote the whole label. See [Naming](#naming-the-part-that-trips-people-up). |
| `PNG export needs the 'kaleido' package…` | Install it: `pip install '.[report]'`. Or use `--format html`, which needs nothing extra. |
| "It looks like it's hanging." | It isn't - mainnet takes ~25-60 s and prints `[env] rendering PNG i/N` as it goes. The quiet stretch is the final HTML write (mainnet's file is large). |
| The `report.html` is huge / slow to open | Use `--html-max-points 4000`, or `--format md` for small images. |
| A section is missing or its plots are empty | That build/axis has no rows in the stats DB for that token. Check the token with `db-sync-plot.py --env <env> --list`. If only one build exists, comparisons are skipped on purpose. |
| Old images from a previous run linger | They shouldn't - the output folder is cleaned each run. If you see leftovers, you're looking at a different `--outdir` or an old copy. |

---

## See also

- [11 - Report generator internals](11-report-generator-internals.md) - how the
  code works, step by step.
- [03 - Graph catalog](03-graph-catalog.md) - what each plot means and how to
  read it.
- [12 - Useful queries](12-useful-queries.md) - pull the same numbers (peak RAM,
  disk size, row counts) straight from the stats DB with SQL.
- The main [README](../README.md) `# db-sync-stats-report.py` section - the quick
  command reference.
