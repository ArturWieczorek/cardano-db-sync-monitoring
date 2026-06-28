# 15 - Tracking and benchmarking rollbacks (how-to, per case)

A task-oriented guide to the two rollback tools. Pick the case that matches what
you're trying to do; each has the exact commands and what you get back. For what
the resulting graphs mean, see
[14 - Reading the rollback graphs](14-reading-the-rollback-graphs.md).

The two tools:

- **`db-sync-rollback-monitor.py`** - watches a *running* db-sync and records
  rollbacks as they happen (passive; no impact on db-sync).
- **`db-sync-rollback-benchmark.py`** - *triggers* rollbacks against a copy of a
  database with `cardano-db-tool` and times them, for controlled cross-version
  comparison (no node / running db-sync needed).

Both only ever create and write their own tables (`rollback_samples`,
`rollback_events`, `rollback_table_deletes`, `rollback_benchmarks`) - your
existing sync-monitoring data is never touched. Run scripts from the repo root
with the venv active (`source .venv/bin/activate`) or via `.venv/bin/python`.

---

## Quick chooser

| You want to... | Case |
|:---|:---|
| Watch a live db-sync and log rollbacks as they occur | [1](#case-1---watch-a-running-db-sync-metrics-only) / [2](#case-2---also-capture-exact-deletion-timing--per-table-counts) |
| See / plot what was captured | [3](#case-3---see-what-was-captured-plot--queries) |
| Get a repeatable rollback time for one version | [4](#case-4---benchmark-one-versions-rollback-on-demand) |
| Compare two db-sync versions fairly | [5](#case-5---compare-two-versions-fairly-the-headline-case) |
| Confirm a rollback didn't corrupt data | [6](#case-6---confirm-data-equivalence-after-a-rollback) |

---

## Case 1 - Watch a running db-sync (metrics only)

**When:** you have a db-sync running and want to record any rollbacks it does,
with the least setup.

**Command:**

```bash
python3 scripts/db-sync-rollback-monitor.py \
  --env preprod --db-sync-ver 13.7.1.0-node-11.0.1
```

It scrapes db-sync's Prometheus endpoint (default `http://127.0.0.1:8080/` -
**note the root path**, not `/metrics`; the port is the `PrometheusPort` in your
db-sync config) every 2 seconds and appends to `rollback_samples` in
`data/cardano-db-sync/<env>.db`, tagged with the `--db-sync-ver` label.

**What you get:** the queue-length and node-vs-db tip series. Rollbacks are
detected from the tip going backwards and reconstructed at plot time. Leave it
running alongside your normal collectors; it stops cleanly on Ctrl-C.

**Tips:**
- Match `--db-sync-ver` to the label you give the other monitors so the series
  group together.
- Different endpoint/port: `--prometheus-url http://127.0.0.1:8081/`.
- Write somewhere isolated for a one-off: `--sqlite-db /tmp/rb-test.db`.

---

## Case 2 - Also capture exact deletion timing + per-table counts

**When:** you want the *precise* deletion duration and which tables dominate a
rollback - not just the metric picture.

**Command:** same as Case 1, plus point at the db-sync log:

```bash
python3 scripts/db-sync-rollback-monitor.py \
  --env preprod --db-sync-ver 13.7.1.0-node-11.0.1 \
  --log-file /var/log/cardano-db-sync.log
```

If db-sync logs to stdout, tee it to a file and point `--log-file` at that file:

```bash
cardano-db-sync ... | tee -a db-sync.log     # in db-sync's pane
python3 scripts/db-sync-rollback-monitor.py --env preprod \
  --db-sync-ver 13.7.1.0-node-11.0.1 --log-file db-sync.log
```

**What you get:** each completed rollback is written to `rollback_events` with the
exact deletion duration (read from the log's own timestamps) and its per-table
delete counts to `rollback_table_deletes`. The monitor starts reading the log
from its current end, so it only captures rollbacks that happen while it runs.

---

## Case 3 - See what was captured (plot + queries)

**Plot** (prefer the time axis for reading rollbacks):

```bash
python3 scripts/db-sync-plot.py --env preprod --metrics rollback --x-axis time \
  --versions 13.7.1.0-node-11.0.1
```

Output: `plots/cardano-db-sync/preprod/preprod_<version>_rollback_by_time.html`.
How to read the three panels: [doc 14](14-reading-the-rollback-graphs.md).

**Raw numbers** straight from SQLite:

```bash
sqlite3 data/cardano-db-sync/preprod.db \
  "SELECT event_start_ts, depth_blocks, delete_duration_sec, to_slot, source
   FROM rollback_events ORDER BY event_start_ts;"

sqlite3 data/cardano-db-sync/preprod.db \
  "SELECT table_name, deleted_rows FROM rollback_table_deletes
   ORDER BY deleted_rows DESC LIMIT 15;"
```

---

## Case 4 - Benchmark one version's rollback on demand

**When:** you want a repeatable, isolated measurement of how long a specific
rollback takes for one db-sync version - no node, no running db-sync.

**How it works:** the benchmark runs that version's `cardano-db-tool rollback
--slot N` (the same delete code db-sync uses) against a Postgres database, times
the deletion, samples peak RSS/CPU, and repeats. Because the rollback is
destructive, you give it a `--restore-cmd` that resets the database between runs.

**Prerequisites:**
1. A Postgres database already synced to some slot `S` (the "before" state).
2. The matching `cardano-db-tool` binary for the version under test.
3. A snapshot to restore from between repetitions (e.g. a `pg_dump`, or a
   template database you clone).
4. A pgpass file pointing `cardano-db-tool` at the database under test.

**Pick a rollback target slot** (e.g. roll back ~200 blocks from tip):

```bash
psql -d <db> -tAc \
  "SELECT slot_no FROM block WHERE block_no = (SELECT max(block_no)-200 FROM block);"
```

**Run it** (3 repetitions, restoring a cloned template DB between each):

```bash
python3 scripts/db-sync-rollback-benchmark.py \
  --env preprod --db-sync-ver 13.7.1.0-node-11.0.1 \
  --db-tool /path/to/13.7.1.0/cardano-db-tool \
  --from-slot <S> --to-slot <S-target> --reps 3 \
  --pgpassfile config/pgpass-bench \
  --restore-cmd 'dropdb --if-exists bench && createdb bench --template snap_at_S'
```

**What you get:** one row per repetition in `rollback_benchmarks` (deletion
duration, peak RSS/CPU, depth) and a printed summary with median / min / max /
stdev.

**Tips:**
- If the database was synced with the address tx_out variant, add
  `--tool-arg --use-tx-out-address` so the tool deletes the right tables. Keep
  this identical across versions you compare.
- `--restore-cmd` is required for meaningful `--reps > 1` (the rollback is
  destructive). It runs before *every* repetition, so they all start identical.
- Recovery-phase timing (re-applying blocks to the tip) needs a live node +
  db-sync and is out of scope for the benchmark - use the monitor (Case 2) for that.

---

## Case 5 - Compare two versions fairly (the headline case)

**The problem:** a newer db-sync is usually further along the chain than an older
one, so "roll each back by D" isn't apples-to-apples.

**The fix - normalize to one starting point:**

1. Sync **one** database to slot `S` (or take the lower of the two tips) on the
   target host, with identical db-sync config.
2. **Snapshot** it once: `pg_dump -Fc <db> -f snap_at_S.dump` (or keep it as a
   template database to clone).
3. For **each** version, run the benchmark against a fresh restore of that
   snapshot, rolling back to the same `S - D`:

```bash
# version A
python3 scripts/db-sync-rollback-benchmark.py \
  --env preprod --db-sync-ver 13.6.0.5 \
  --db-tool /path/to/13.6.0.5/cardano-db-tool \
  --from-slot <S> --to-slot <S-D> --reps 5 \
  --pgpassfile config/pgpass-bench \
  --restore-cmd 'dropdb --if-exists bench && createdb bench --template snap_at_S'

# version B - identical, only --db-sync-ver and --db-tool change
python3 scripts/db-sync-rollback-benchmark.py \
  --env preprod --db-sync-ver 13.7.1.0 \
  --db-tool /path/to/13.7.1.0/cardano-db-tool \
  --from-slot <S> --to-slot <S-D> --reps 5 \
  --pgpassfile config/pgpass-bench \
  --restore-cmd 'dropdb --if-exists bench && createdb bench --template snap_at_S'
```

Identical starting bytes + identical depth make the db-sync version the only
variable. Compare the two printed summaries (or query `rollback_benchmarks` by
`version`). **Version B is a regression if** its median deletion duration is
meaningfully higher for the same depth.

**Fairness caveats** (matter for resolving *small* differences):
- Run on the same host / Postgres config with no other load.
- Postgres cache state matters - a fresh `pg_restore` is cold; keep the reset
  identical across versions, and consider a consistent cache warm-up if you need
  fine resolution.
- Keep `--tool-arg` flags identical across versions (or treat the difference as
  the deliberate variable).

---

## Case 6 - Confirm data equivalence after a rollback

**When:** you want to check that a version's rollback produced the *same* data as
a reference (e.g. it didn't drop or mangle rows).

Add `--compare-cmd` - any command that exits `0` when the two databases match.
The natural choice is the separate `db-sync-compare` tool:

```bash
python3 scripts/db-sync-rollback-benchmark.py \
  --env preprod --db-sync-ver 13.7.1.0 \
  --db-tool /path/to/cardano-db-tool \
  --from-slot <S> --to-slot <S-D> --reps 1 \
  --pgpassfile config/pgpass-bench \
  --restore-cmd 'dropdb --if-exists bench && createdb bench --template snap_at_S' \
  --compare-cmd 'db-sync-compare --db1 bench --db2 reference_db ...'
```

The exit status is recorded per repetition in `rollback_benchmarks.equivalence_ok`
(`1` = matched, `0` = differed, NULL = no compare run).

---

## Gotchas (learned the hard way)

- **db-sync's Prometheus metrics are at the root path `/`**, not `/metrics`
  (the cardano-*node* endpoint uses `/metrics` - different server). The monitor
  defaults to the root; only override `--prometheus-url` if your port differs.
- **A forced rollback via restarting db-sync (`--rollback-to-slot`) won't show in
  the metric panels** - the endpoint is down during the restart, so the dip falls
  in a sampling gap. The `--log-file` path still captures it. On a natural reorg
  (no restart) the metrics stay continuous. (See doc 14.)
- **`cardano-db-tool` prints little** (it runs with a null tracer), so the
  benchmark measures the deletion by wall-clock time and `getrusage`, not by
  parsing tool output. `depth_blocks` from the tool may be NULL - that's expected.
- **The db-sync ledger-state dir must match the database tip.** If you start a
  db-sync against a database whose matching ledger snapshot is missing, it will
  roll the database back to whatever snapshot it can find - keep the
  `--state-dir` paired with the database.
