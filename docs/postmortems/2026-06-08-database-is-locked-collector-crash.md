# Post-mortem: collectors crashing on "database is locked"

**Date:** 2026-06-08
**Severity:** availability — long-running collectors died mid-sync (no data corrupted)
**Components:** `node-resource-monitor.py`, `node-rts-monitor.py`, `_disk_size.py`, `db-sync-resource-monitor.py`, `_common` (new `connect_writer`), `rename-version.py`

## Summary

While monitoring a mainnet node sync, two collectors writing the **same** SQLite
file (`data/cardano-node/mainnet.db`) crashed within the same window with an
unhandled `sqlite3.OperationalError: database is locked`. The resource collector
(`node-resource-monitor.py`) died on an `INSERT INTO memory_metrics`, and the RTS collector
(`node-rts-monitor.py`) died on an `INSERT INTO rts_metrics`. Both had been running
fine for hours; each was killed by a single lost write-lock race, ending the
collection for that run.

No data on disk was damaged — WAL crashes are atomic, and everything written
before the crash was intact. The cost was the *future* samples we stopped
collecting until the processes were restarted.

## Impact

- Any env where two or more node collectors run together
  (`node-resource-monitor` + `node-rts-monitor` + `node-db-size-monitor`) was exposed; the
  same applies to db-sync (`db-sync-resource-monitor` + `db-sync-ledger-size-monitor`).
- A crash is silent unless someone is watching the terminal — a multi-day sync
  could lose most of its run before anyone noticed the collector was gone.
- The plots show the outage as a gap (correctly broken by `insert_gap_breaks`),
  but the data for that period is simply absent.

## Timeline / how it was found

A user pasted two tracebacks from a live mainnet sync (Epoch 377, Babbage, ~59%):

```
Slot 77635934 | ... | RSS 12.4 GiB
Traceback (most recent call last):
  File ".../scripts/node-resource-monitor.py", line 237, in run
    conn.execute("INSERT INTO memory_metrics ...")
sqlite3.OperationalError: database is locked
```

```
Slot 77674686 | 38 rts metrics
Traceback (most recent call last):
  File ".../scripts/node-rts-monitor.py", line 210, in record
    conn.executemany("INSERT INTO rts_metrics ...")
sqlite3.OperationalError: database is locked
```

Two different collectors, two different tables, same error, same file — the shape
pointed straight at write-lock contention on the shared `mainnet.db`.

## Root cause

Three facts combined:

1. **One file, several writers.** `node-resource-monitor`, `node-rts-monitor`, and
   `node-db-size-monitor` all append to `data/cardano-node/<env>.db` (and the two
   db-sync collectors share `data/cardano-db-sync/<env>.db`). This is by design —
   keeping each run's series in one file is what makes the plots join up.

2. **WAL doesn't make SQLite multi-writer.** WAL mode (which we enable for
   reader/writer concurrency) still serializes *writers*: only one connection holds
   the write lock at a time. A second writer that arrives mid-write must wait.

3. **The default busy timeout is only 5s, and the exception was uncaught.** Python's
   `sqlite3.connect` waits 5 seconds for the lock, then raises `database is locked`.
   Normal inserts are sub-millisecond, so 5s is usually invisible — but it can be
   exhausted by a checkpoint on a 120 MB DB, or by a maintenance pass
   (`rename-version.py`, `backup-stats.py`) holding the write lock. The collector
   loops did not catch `OperationalError`, so the first time a writer lost the race
   the whole process exited.

In short: a known SQLite limitation (single writer) met an optimistic default
(5s timeout) met a missing guard (no try/except), and the combination turned a
transient, recoverable collision into a fatal crash.

## Fixes

- **A generous, shared busy timeout.** New `_common.connect_writer(db_file)` opens
  a connection with `WRITE_TIMEOUT_SEC = 30` seconds. Every writer now goes through
  it — the two crashed collectors, plus `_disk_size.py`, `db-sync-resource-monitor.py`,
  `_common.init_sqlite_schema`, and `rename-version.py`'s rewrite transaction (so a
  rename waits for a live monitor instead of racing it). Readers (plot/report/stats)
  keep plain `sqlite3.connect`; under WAL they never block on a writer.
- **Drop a sample, not the run.** Each collector's sample loop now wraps its write
  in `try/except sqlite3.OperationalError`: on a timeout it logs a warning and skips
  that one sample, then continues. This mirrors the existing "skip one sample on a
  Postgres connection blip" handling, so a transient lock can no longer end a
  multi-day collection.
- **Regression test.** `tests/test_rts_monitor.py::TestBusyResilience` drives the
  run loop with a write that raises `database is locked` and asserts the loop warns
  and exits cleanly instead of propagating.
- **Docs corrected.** `README.md` and `docs/08` both asserted "one writer per env" /
  "a single writer" as a justification — the exact assumption that broke. Both are
  fixed, and `docs/05` gains a *Multiple writers and `busy_timeout`* section.

## Standing invariant for contributors

> Any code that writes a shared per-env SQLite DB must open its connection through
> `_common.connect_writer` (not bare `sqlite3.connect`), and every collector sample
> loop must tolerate a transient `sqlite3.OperationalError` by skipping one sample
> rather than crashing. Readers may use plain `sqlite3.connect`.

## Lessons

- "We only ever have one writer" was true when the first collector shipped and
  quietly became false as `node-rts-monitor` and `node-db-size-monitor` were added —
  the *same drift pattern* as the [2026-06-06 post-mortem](2026-06-06-empty-disk-and-invisible-rts.md),
  where new collectors outran an assumption baked into older code (and docs).
- A library default tuned for the common case (5s busy timeout) is a latent
  failure mode under contention; make the timeout explicit and generous when you
  knowingly have multiple writers.
- A long-running collector should treat its datastore the way it already treats its
  upstream (Postgres): a single failed sample is a gap, not a fatal error. Crashing
  loses far more data than the one row it couldn't write.
