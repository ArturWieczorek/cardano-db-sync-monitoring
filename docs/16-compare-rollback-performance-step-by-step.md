# 16 - Compare two db-sync versions' rollback performance (detailed, step by step)

This is the full walkthrough for the headline task: **does a new db-sync version
roll back slower than the old one?** It starts from first principles - what the
problem actually is and why it's tricky to measure fairly - then gives an exact,
copy-paste, step-by-step procedure with "what you should see" at each step.

If you only want the commands, jump to [The procedure](#the-procedure-step-by-step).
For the terse per-flag reference see
[15 - Tracking and benchmarking rollbacks](15-tracking-and-benchmarking-rollbacks.md);
for what the resulting graphs mean, [14 - Reading the rollback graphs](14-reading-the-rollback-graphs.md).

---

## Part 1 - The problem we're facing

### What a rollback is

cardano-db-sync copies the Cardano blockchain into a Postgres database, block by
block. But the blockchain isn't final the instant a block appears: sometimes the
network briefly disagrees on the latest blocks and then settles on a different
version of recent history (a **reorg**, or **rollback**). When that happens, the
node tells db-sync "the last N blocks you stored are no longer real," and db-sync
must:

1. **Delete** those N blocks from Postgres, plus everything attached to them -
   transactions, inputs, outputs, multi-asset rows, scripts, governance rows, and
   so on. This is the **deletion phase**.
2. **Re-apply** the new blocks the node now considers correct, until the database
   is caught up again. This is the **recovery phase**.

Small rollbacks (a few blocks) happen routinely and are cheap. The danger is a
**lengthy rollback** - one that takes minutes instead of seconds - because while
db-sync is busy deleting and re-applying, the database is behind and consumers of
that data (explorers, wallets, dashboards) are serving stale or incomplete
results. This is a real operational pain point: the graphs that motivated this
whole feature were production dashboards showing rollback recovery times spiking
to tens of minutes.

### Why we want to compare versions

The deletion phase is exactly the kind of thing that can quietly get **worse**
between db-sync releases - a schema change adds a table to the cascade, an index
that made the delete fast gets dropped, a new feature writes extra rows that now
also have to be deleted. None of that shows up in a normal sync test (which only
measures going *forward*). So before shipping a new version we want to answer:
**for the same rollback, is the new version's deletion meaningfully slower?**

### Why a fair comparison is harder than it sounds

You can't just "roll back version A and version B and compare," because of four
traps:

1. **They're at different points on the chain.** A newer db-sync you've been
   running is usually further along than an older one. Rolling each back "by 200
   blocks from its own tip" means they delete *different* data - not comparable.
2. **Deletion cost depends on what's in those blocks.** A stretch of chain full
   of big multi-asset transactions deletes far more rows than a quiet stretch.
   The two versions must roll back the **same** range of chain.
3. **Postgres cache state changes the timing.** The same delete is much faster
   when the relevant pages are already in memory (warm cache) than cold. If one
   run is warm and the other cold, you're measuring cache, not db-sync.
4. **Background noise.** Other processes, autovacuum, disk contention - all add
   jitter. A single run can be misleading.

### Our approach, and why it's fair

We neutralize all four traps:

- **Normalize to one starting point.** We take **one** database, synced to a
  chosen tip slot `S`, and make a **snapshot** of it. Every measurement starts by
  restoring that identical snapshot. Same starting bytes, every time, for both
  versions (fixes traps 1 and 2).
- **Roll back to the same target slot** `S - D` in every run (fixes trap 2).
- **Repeat N times and look at the median + spread**, restoring the snapshot
  before *every* run so they all start from the same (cold) cache state (fixes
  traps 3 and 4 as far as is practical; see the caveats at the end).
- **Use `cardano-db-tool rollback`, not a running db-sync.** Each db-sync release
  ships a `cardano-db-tool` that runs the **exact same delete code path** db-sync
  uses internally, but needs only a database connection - no cardano-node, no
  running db-sync process. That makes the measurement isolated, fast, and
  perfectly repeatable, and the db-sync **version** becomes the only variable.

### What we measure - and what we don't

- We measure the **deletion phase**: wall-clock time for the rollback to complete,
  plus peak memory/CPU of the tool (from the kernel, so it's exact even for a
  sub-second rollback), plus the per-table count of deleted rows.
- We do **not** measure the **recovery phase** here. Re-applying blocks needs a
  live node feeding db-sync, which `cardano-db-tool` doesn't do. Recovery timing
  is the job of the passive monitor on a running db-sync (see doc 15, Case 2).
  The deletion phase is the regression-prone part, so it's the right thing to
  benchmark.

The benchmark writes results to its own `rollback_benchmarks` table and never
touches your existing sync-monitoring data; all work happens on **copies** of
your database, so the original is never modified.

---

## Part 2 - The procedure, step by step

### What you'll end up with

Two numbers you can put side by side, e.g.:

```
13.6.0.5  deletion: median 18.9s  (over 5 runs, stdev 0.6s)
13.7.1.0  deletion: median 22.8s  (over 5 runs, stdev 0.7s)   <- ~20% slower => investigate
```

### What you need (checklist)

- [ ] The **`cardano-db-tool`** binary for **each** version you're comparing (it
      ships in each cardano-db-sync release).
- [ ] **One** Postgres database already synced to some point (its tip slot is
      `S`). You can reuse an existing synced db-sync database - we only ever work
      on copies of it.
- [ ] Postgres reachable locally (if `psql -l` works, you're set).
- [ ] No db-sync currently connected to the database you'll snapshot (stop it
      first, so the copy is clean).

---

### Step 1 - Pick the two versions and locate their tools

Decide the two versions (example: `13.6.0.5` vs `13.7.1.0`) and note the full
path to each `cardano-db-tool`. Confirm a tool's version:

```bash
/path/to/cardano-db-tool version
```

**You should see** something like `cardano-db-tool 13.7.1.0 - linux-x86_64 ...`.
Why it matters: the tool's version must match the database that version synced,
because the set of tables it deletes is part of that version's schema.

### Step 2 - Snapshot the database (the reset point)

The rollback **deletes** data, so between runs we restore the database to its
"before" state. Locally, the fastest way is a **template copy**. Replace
`synced_db` with your real synced database name:

```bash
createdb snap --template synced_db
```

**You should see** no output (that means success). `snap` is now a frozen,
read-only-for-our-purposes copy we'll clone from. Nothing may be connected to
`synced_db` during this command - stop any db-sync using it first.

> `psql -l` lists databases and their sizes if you're unsure of the name. A
> 28 GB preview/preprod copy takes a couple of minutes; that's normal.

### Step 3 - Make a password file for the tool

`cardano-db-tool` connects via a pgpass file. Point one at our working copy
(which we'll call `bench`) and lock its permissions:

```bash
echo '/var/run/postgresql:5432:bench:*:*' > config/pgpass-bench
chmod 600 config/pgpass-bench
```

The `*:*` at the end are the username and password fields; on a local
trust-authenticated Postgres they can be wildcards.

### Step 4 - Choose the depth and find the target slot

Decide how far to roll back (example: 200 blocks from the tip) and ask the
database for the slot at that point. Replace `synced_db`:

```bash
psql -d synced_db -tAc \
  "SELECT max(slot_no) AS tip_slot, \
          (SELECT slot_no FROM block WHERE block_no=(SELECT max(block_no)-200 FROM block)) AS target_slot \
   FROM block;"
```

**You should see** two numbers, e.g. `116015507|115935123`. The first is your
tip slot `S`; the second is the `TARGET` slot (200 blocks earlier). Note both.
Why a slot and not a block count: db-sync's rollback API works in slots - it
deletes everything at or after the given slot.

### Step 5 - Benchmark version A

This restores a fresh `bench` copy before each run, rolls it back to `TARGET`
with version A's tool, times it, and repeats 5 times. Put your real numbers in
for `S` and `TARGET`:

```bash
python3 scripts/db-sync-rollback-benchmark.py \
  --env preprod --db-sync-ver 13.6.0.5 \
  --db-tool /path/to/13.6.0.5/cardano-db-tool \
  --from-slot S --to-slot TARGET --reps 5 \
  --pgpassfile config/pgpass-bench \
  --restore-cmd 'dropdb --if-exists bench && createdb bench --template snap'
```

**You should see** one line per run and then a summary:

```
rep 0: deleted to slot TARGET in 18.7s (peak RSS 73 MiB)
rep 1: deleted to slot TARGET in 19.1s (peak RSS 73 MiB)
...
=== cardano-db-sync 13.6.0.5 preprod | rollback to slot TARGET | n=5 ===
deletion duration: median 18.9s, min 18.1s, max 19.8s, stdev 0.6s
```

What's happening under the hood each run: `dropdb/createdb` resets `bench` from
`snap` (identical cold start), then `cardano-db-tool rollback --slot TARGET` runs
and is timed end to end.

### Step 6 - Benchmark version B

The **same command**, changing only `--db-sync-ver` and `--db-tool`. Keeping
everything else identical is the whole point:

```bash
python3 scripts/db-sync-rollback-benchmark.py \
  --env preprod --db-sync-ver 13.7.1.0 \
  --db-tool /path/to/13.7.1.0/cardano-db-tool \
  --from-slot S --to-slot TARGET --reps 5 \
  --pgpassfile config/pgpass-bench \
  --restore-cmd 'dropdb --if-exists bench && createdb bench --template snap'
```

### Step 7 - Read and interpret the result

Compare the two **median** lines. Lower is better.

- **Is the difference real or noise?** Look at the `stdev` and `min/max`. If the
  two medians differ by less than the run-to-run spread, it's probably noise -
  raise `--reps` (e.g. 10) for a tighter estimate. If version B's *whole* range
  (min..max) sits above version A's, the regression is real.
- **How big a difference matters?** A few percent is usually noise on a busy
  workstation. Tens of percent, or seconds turning into minutes, is a genuine
  regression worth chasing.

See both side by side from the stored data any time:

```bash
sqlite3 data/cardano-db-sync/preprod.db \
  "SELECT version, count(*) runs, round(avg(delete_duration_sec),1) avg_s,
          round(min(delete_duration_sec),1) min_s, round(max(delete_duration_sec),1) max_s
   FROM rollback_benchmarks GROUP BY version;"
```

**If version B is slower, localize *why*** with the per-table delete counts a
running db-sync records (doc 15, Case 2): the table with the largest count, or a
new table that only the newer version deletes, usually points straight at the
cause.

### Step 8 - Clean up

```bash
dropdb --if-exists bench
dropdb --if-exists snap
```

---

## Part 3 - Fairness caveats (read before trusting small differences)

The procedure controls the big confounds, but for resolving *small* differences
keep these in mind:

- **Same machine, quiet machine.** Deletion is dominated by disk and Postgres
  work, so run both versions on the same host with nothing else heavy running.
- **Cache state.** A freshly restored database has a cold Postgres cache. The
  procedure restores before every run so all runs are equally cold, which is
  consistent - but a template copy also repacks the data differently than a
  live-synced database would. For fine resolution, decide on one cache policy
  (e.g. a fixed `pg_prewarm`, or a forced checkpoint) and apply it identically to
  both versions.
- **Same tx_out layout.** If the database was synced with the "address table"
  variant, add `--tool-arg --use-tx-out-address` to **both** runs so the tool
  deletes the right set of tables. Check with:
  ```bash
  psql -d synced_db -tAc \
    "SELECT 1 FROM information_schema.columns \
     WHERE table_name='tx_out' AND column_name='address_id';"
  ```
  A returned `1` means the address variant is in use - add the flag. Whatever you
  choose, keep it identical across the two versions (or treat it as the
  deliberate variable).
- **`cardano-db-tool` prints almost nothing** (it runs with logging disabled), so
  the benchmark times the deletion by the clock and reads peak memory from the
  kernel rather than parsing tool output. A `NULL` `depth_blocks` in the results
  is therefore expected and harmless - the duration is what matters.
- **One depth tells you about one depth.** A version might regress only on deep
  rollbacks. If you care about that, repeat the whole procedure at a couple of
  depths (e.g. 50, 500, 2000 blocks).
