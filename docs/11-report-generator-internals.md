# 11 - Report generator internals

This doc explains *how the report generator works inside* - the code, step by
step, in plain language. It's for someone with basic Python who wants to
understand, debug, or extend it. If you just want to *use* the tool, read
[10 - Generating reports](10-generating-reports.md) instead.

We'll build the picture in four passes: the bird's-eye flow, the three files and
why they're separate, a step-by-step walk through one run, and then the "why" of
each design choice.

---

## Bird's-eye view: data flows one way

A report is produced by a small pipeline. Data starts as rows in a SQLite file
and ends as images and HTML on disk. Each arrow is "turned into":

```
   CLI flags (--env, --lsm, --inmemory, --compare-to, --format, …)
        │
        ▼
   resolve version tokens  ──►  full labels   (e.g. "13.7.1.0-node-11.0.1"
        │                                       ►  "cardano-db-sync 13.7.1.0-node-11.0.1 preprod")
        ▼
   load_*()    SQLite rows  ──►  pandas DataFrame      (one per metric)
        │
        ▼
   build_*()   DataFrame    ──►  Plotly Figure         (the chart, in memory)
        │
        ├──►  render_png()      Figure ──► PNG file        (full resolution)
        └──►  assemble_html()   Figure ──► one HTML page   (interactive, embedded)
        │
        ▼
   assemble_markdown() / write files  ──►  reports/cardano-db-sync/<env>/
```

The key idea: **a `Figure` is built once, then either saved as a static PNG or
embedded interactively in HTML.** Nothing flows backward.

---

## The three files, and why they're separate

| File | Role | Analogy |
|:---|:---|:---|
| `scripts/db-sync-plot.py` | Knows how to read the stats DB (`load_*`) and turn a DataFrame into a specific chart (`build_*`). | The *kitchen* - it knows the recipes. |
| `scripts/_report.py` | Generic, role-agnostic report machinery: render a Figure to PNG, embed Figures in HTML, assemble Markdown. Knows nothing about db-sync specifically. | The *printing press* - give it figures, it prints a document. |
| `scripts/db-sync-stats-report.py` | The orchestrator. Reads the CLI flags, decides which charts to build, calls the kitchen and the press in the right order, writes the files. | The *head chef* - coordinates everything. |

Why split them?

- The `build_*` functions live in `db-sync-plot.py` because **both** the plot
  CLI *and* the report need the exact same charts. Putting them in one place
  means the report can never drift from what `db-sync-plot.py` draws. (Each
  `plot_*` in that file is now just `build_*` + "write it to an HTML file".)
- `_report.py` is deliberately db-sync-agnostic so the **cardano-node** report
  (`node-stats-report.py`) reuses it unchanged - it only deals in "figures" and
  "sections".

---

## One run, step by step

Follow a single invocation from top to bottom. Function names match the source.

### 1. `main()` - figure out what to do
(`scripts/db-sync-stats-report.py`)

- Calls `parse_args()`. It enforces "pass at least one of `--inmemory`/`--lsm`".
- Expands `--env`: `all` becomes every environment; otherwise it splits a
  comma-separated list.
- For each environment it computes the DB path
  (`data/cardano-db-sync/<env>.db`), skips it if the file is missing, and calls
  `build_env_report(...)` then `write_env_report(...)`.
- The whole loop is wrapped so that if PNG export hits a missing-`kaleido`
  situation, it exits with one clear message instead of a stack trace.

### 2. `build_env_report(db, env, args)` - decide the sections
Returns a list of `ReportSection` objects (the in-memory shape of the report),
or `None` if nothing usable was found.

- `load_all_versions(db, VERSION_KEYED_TABLES["db-sync"])` (from `_common.py`)
  lists every version label present in that env's DB.
- `_resolve(token, available)` turns each token you passed into a full label,
  using `resolve_versions` from `_common.py`. A token that isn't there is
  reported and treated as "absent" (the run continues with what it can).
- It then assembles, in order:
  1. `build_build_section(...)` for the InMemory build (if present),
  2. `build_build_section(...)` for the LSM build (if present),
  3. `build_comparison_section(...)` for **LSM vs InMemory** (only if *both*
     builds resolved),
  4. `build_comparison_section(...)` for **this vs previous** (only if
     `--compare-to` resolved).

### 3. `build_build_section(...)` - one build, every chart
For the three report metrics - `cpu_ram`, `ingest`, `tables` - on **both** the
`slot` and `time` axes, it calls `build_fig(...)` and wraps each resulting
figure in a `ReportImage` (caption + the PNG file name it will get + the figure
itself). Six images per build.

### 4. `build_comparison_section(...)` - two builds overlaid
Same idea, but it passes **a list of labels** to `build_fig` so the chart draws
one line per build, and it always uses the **`slot`** axis (two runs only line
up by chain position - see doc 10). It does this for the headline metrics
(`cpu_ram`, `ingest`).

### 5. `build_fig(db, versions, env, kind, x_axis)` - the load+build bridge
This is the single place that connects the report to `db-sync-plot.py`:

- `cpu_ram` → `load_cpu_ram` then `build_cpu_ram`
- `ingest`  → `load_ingest`  then `build_ingest`
- `tables`  → `load_rowcounts` then `build_rowcounts`

If the data can't be plotted on the requested axis (for example a very old DB
with no timestamps, which makes `load_cpu_ram` bail on the time axis), or a
metric simply has no rows, `build_fig` catches it, prints a one-line note, and
returns `None`. The caller skips that one image - **one bad panel never kills
the whole report.**

### 6. `write_env_report(env, sections, args)` - turn sections into files
- First it **clears its own old outputs** in the env folder (`*.png`,
  `report.md`, `report.html`) so a re-run can't leave mismatched leftovers.
- If the format includes `md`: for each image it calls `render_png(fig, path)`
  (printing `[env] rendering PNG i/N`), then `assemble_markdown(...)` writes
  `report.md` referencing those PNG file names.
- If the format includes `html`: `assemble_html(...)` builds one self-contained
  page and writes `report.html`, then prints its size.

### 7. The `_report.py` helpers it calls

- **`render_png(fig, path, …)`** - saves a Figure as a PNG via Plotly's
  `write_image` (the `kaleido` backend). Responsive charts carry no fixed height,
  so it supplies a sensible default. If `kaleido` is missing it raises
  `KaleidoMissingError` with an install hint instead of a cryptic error.
- **`assemble_markdown(title, sections, …)`** - plain string building: a heading
  per section and an `![caption](file.png)` per image.
- **`assemble_html(title, sections, …, max_points)`** - emits one HTML page,
  embedding each Figure as an interactive chart. Plotly's JavaScript is inlined
  **once** (in the first chart) and reused by the rest, so the file works offline
  without bundling the library many times. If `max_points` is set, each figure is
  passed through `downsample_figure` first (HTML only).
- **`downsample_figure(fig, max_points)`** - returns a *copy* of the figure with
  each line thinned (every Nth point) to at most `max_points`. The original is
  untouched, so the PNGs stay full resolution.

### The in-memory model: two small dataclasses

`_report.py` defines exactly two record types, and the whole report is just a
list of them:

```python
ReportImage   = (caption, png_name, fig)        # one chart
ReportSection = (title, images: [ReportImage])  # a titled group of charts
```

Everything upstream produces these; everything downstream consumes them. That's
the entire "data model".

---

## Design choices, and why

- **`build_*` split out from the writers.** So the report and the plot CLI draw
  identical charts from one source. (This was a deliberate refactor: `plot_*` =
  `build_*` + write-to-HTML.)
- **Two dataclasses as the model.** A report is "sections of captioned figures".
  Keeping that explicit means Markdown and HTML are just two renderers over the
  same structure - add a third (PDF, say) without touching the build logic.
- **PNGs full-res, HTML optionally thinned.** A static image benefits from full
  detail; an interactive page just needs enough points to look smooth, and small
  files travel better. So downsampling applies to HTML only.
- **Responsive in standalone, pinned in the report.** Some figures (e.g.
  cpu_ram) carry no fixed height so the *standalone* `db-sync-plot.py` HTML fills
  the browser viewport. But a height-less figure renders as `height:100%`, which
  collapses to nothing when many figures are stacked in the *report* page. So
  `assemble_html` pins a generous height (`HTML_FALLBACK_HEIGHT`, 900px - these
  are the few-panel headline charts, so they get more room than the dense plots)
  on any figure that lacks one - on a copy, so the original (also used for the
  PNG) isn't mutated. `render_png` applies its own fallback for static images.
- **Comparisons on the slot axis.** Two runs ran at different wall-clock times;
  only the chain position (slot) is shared, so only slot lines them up.
- **Clear the output folder each run.** Renaming an axis or changing
  `--compare-to` would otherwise leave orphan images behind and make it unclear
  which files belong to the current report.
- **Per-panel graceful skip.** Real data is uneven (a metric missing for one
  build, a pre-timestamp DB). Skipping one figure with a note is far friendlier
  than aborting the whole report.
- **`kaleido` is optional.** Many people only want the interactive HTML, which
  needs no extra package. PNG export is gated behind the `[report]` extra, and
  the error is normalized into one actionable sentence.
- **Loading a hyphen-named module.** `db-sync-stats-report.py` imports
  `db-sync-plot.py`, but Python can't `import db-sync-plot` (hyphens aren't
  allowed in module names), so it loads it by file path with `importlib`.
  **Gotcha:** a module loaded this way must be registered in `sys.modules`
  *before* it executes, or dataclasses defined with `from __future__ import
  annotations` fail to resolve their own module. The loaders do this
  registration - keep it if you touch them.

---

## How to extend it

- **Add a metric to each build's section** - add a `(kind, caption, infix)` entry
  to `METRICS` in `db-sync-stats-report.py` and make `build_fig` handle that
  `kind` (load it, then call its `build_*`). Note `tables`/`ingest`/`cpu_ram`
  already exist in `db-sync-plot.py`.
- **Add a metric to the comparison charts** - add it to `HEADLINE`.
- **The cardano-node report** - this already exists as `node-stats-report.py`: it
  reuses `_report.py` as-is and imports `node-plot.py`'s `build_*` functions the same
  way this one imports `db-sync-plot.py` (that's the whole point of keeping
  `_report.py` generic). It differs only in its metric set - `cpu_ram`, `ingest`,
  `disk`, `rts` instead of `cpu_ram`, `ingest`, `tables` - and in plotting `ingest`
  on an epoch axis. See [13 - Generating node reports](13-node-report-generation.md).

---

## See also

- [10 - Generating reports](10-generating-reports.md) - the user-facing guide.
- [08 - Data access and databases](08-data-access-and-databases.md) - how the
  `load_*` functions read SQLite in the first place.
- [03 - Graph catalog](03-graph-catalog.md) - what each chart shows.
- [Post-mortem: empty disk plot and invisible RTS data](postmortems/2026-06-06-empty-disk-and-invisible-rts.md)
  - why version-keyed tables are registered once in `_common.VERSION_KEYED_TABLES`
  (which `load_all_versions` reads here).
