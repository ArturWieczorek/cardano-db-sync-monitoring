# Post-mortem: empty disk plot and invisible RTS data

**Date:** 2026-06-06
**Severity:** data correctness / silent data loss in plots (no data was lost on disk)
**Components:** `node-plot.py`, `_common.insert_gap_breaks`, `rename-version.py`

## Summary

Running `node-plot.py --env mainnet --metrics all` produced a **completely empty
disk plot** and **skipped the RTS plot entirely** ("no rts_metrics rows for the
selected versions"), even though both collectors had been running for days and
had written thousands of samples. The data was intact in SQLite the whole time;
three independent defects in the plotting/tooling layer hid it.

## Impact

- The on-disk DB-size chart was blank for any run collected at the default 60s
  cadence — i.e. every disk run.
- RTS/runtime metrics were unplottable whenever the run's `--node-ver` label
  didn't exactly match the resource monitor's label.
- `rename-version.py` — the tool you'd reach for to fix the label mismatch —
  would itself have silently left the RTS (and disk) rows behind.

No data was lost; everything was recoverable from the SQLite DB once the tooling
was fixed.

## Timeline / how it was found

A user reported the empty disk plot and the skipped RTS plot. Inspecting the
live `data/cardano-node/mainnet.db` showed all the rows present:

| table | version label | rows | cadence |
|---|---|---|---|
| cpu/memory/ingest | `cardano-node LSM-11.0.1 mainnet` | 20,457 | ~10s |
| `disk_metrics` | `cardano-node LSM-11.0.1 mainnet` | 3,482 | **60s** |
| `rts_metrics` | `cardano-node LSM-mainnet mainnet` | 783,674 | ~10s |

That table made all three root causes obvious at once.

## Root causes

### 1. Empty disk plot — a fixed gap-break threshold vs. a slower collector
`insert_gap_breaks` inserted a NaN "line break" between any two consecutive
samples more than **50s** apart (a constant tuned for the 10s resource
collector). The disk collector samples every **60s**, so a break was inserted
between *every* pair of disk points. With `mode="lines"` and
`connectgaps=False`, no segment ever connected two real points → a blank chart.

### 2. RTS invisible — a mistyped label the picker couldn't surface
All collectors build the row label as `cardano-node {node_ver} {env}`. The RTS
run was started with `--node-ver LSM-mainnet` (the env-*directory* name) while
everything else used `--node-ver LSM-11.0.1` (the *version*). So RTS rows landed
under a second label. The plot's version picker enumerated labels from the
`node_version` table only — which just `node-resource-monitor.py` writes — so the
RTS-only label was both invisible in the picker and unselectable, and selecting
`LSM-11.0.1` matched zero RTS rows. (Disk "worked" past the picker only because
the disk collector happened to reuse the `LSM-11.0.1` label.)

### 3. `rename-version.py` was stale — the fix tool had the same blind spot
Its node-side `VERSION_TABLES` listed only `memory_metrics`, `cpu_metrics`,
`node_version`, `node_ingest_metrics`. It omitted `disk_metrics` and
`rts_metrics`, so a rename would silently skip exactly the rows we needed to
relabel.

## The deeper cause: drift from no single source of truth

Root causes 2 and 3 are the *same mistake made twice*: `disk_metrics` shipped in
1.1.0 and `rts_metrics` in 1.2.0, and each time the new table was not propagated
to every place that must know about version-keyed tables. The list of those
tables was **duplicated** (in `rename-version.py` and inline in `node-plot.py`)
and hand-maintained, while the tables themselves are `CREATE`d across five files.
Nothing tied them together, so they drifted.

## Fixes

- **Adaptive gap threshold.** `insert_gap_breaks` now computes the threshold per
  series as `5 × median(inter-sample interval)` (≈50s for 10s collectors, ≈300s
  for the 60s disk collector), falling back to 50s when unmeasurable. Any
  collector cadence now works without a code change.
- **Picker unions all tables.** New `_common.load_all_versions` enumerates the
  union of labels across every version-keyed table; both plot pickers use it. A
  collector-only or mistyped label is now visible and selectable.
- **Single source of truth + drift guard (the real prevention).**
  `_common.VERSION_KEYED_TABLES` is now the one authoritative registry, consumed
  by `rename-version.py` and both pickers. `tests/test_version_tables_registry.py`
  scans every `CREATE TABLE` in `scripts/` for a `version` column and **fails CI
  if any such table is missing from the registry** — so this class of omission
  can't ship again.
- **Collector typo guard.** `node-db-size-monitor.py` and `node-rts-monitor.py`
  now warn at startup when a brand-new `--node-ver` label appears alongside
  existing ones, listing them — catching the mismatch at collection time.
- **Data remediation.** The mislabeled RTS rows were merged into the correct
  label with `rename-version.py --merge` (after fixing #3), taking an automatic
  backup.

## Standing invariant for contributors

> Every table with a `version` column must be registered in
> `_common.VERSION_KEYED_TABLES`. Adding a metric table means updating that dict
> (and the README rename recipe / test fixtures). `test_version_tables_registry`
> enforces it; the plot pickers and `rename-version.py` both read from it.

## Lessons

- A "graceful skip" (the RTS plot's `print(...); return`) is friendly for a
  genuinely-absent optional collector but dangerous when the real cause is a
  label mismatch — it turned a data bug into silence. Surfacing *all* labels in
  the picker makes the mismatch visible instead.
- When the same constant must be true in N places, encode it once and assert the
  rest against it. Two hand-maintained copies drifted within two releases.
- Defaults tuned for one collector ("5× the 10s interval") silently mislead when
  a second collector samples at a different rate; prefer values derived from the
  data over hardcoded ones.
