# 08 - Data access and databases

This doc explains how the scripts in this repo actually talk to databases: how they connect, how
they read and write data, and the database words you keep bumping into (driver, cursor,
transaction, autocommit, isolation level, connection pool, WAL). It is written for a programmer
with basic Python knowledge who has not used `psycopg2` or `sqlite3`, and who has never had to
think about transactions or isolation levels before. No prior database theory is assumed; every
term is introduced in plain words before it is used, with a real-life analogy where one helps.

**How this relates to [05 - Database internals](05-database-internals.md):** think of it as two
halves of the same story.

- This doc (08) is the **plumbing**: how we open a connection and move data in and out.
- Doc 05 is the **tuning**: how we make individual queries fast (indexes, approximate counts,
  query plans).

So 08 comes first if you are new; read 05 next for the deep dives. Wherever a topic is already
explained in depth in 05, this doc gives you the one-sentence version and a pointer, instead of
repeating it.

---

## 1. Two databases, two completely different shapes

This project touches **two** databases, and they are different kinds of thing. Getting this
distinction straight up front makes everything else click.

**PostgreSQL ("Postgres") is a client/server database.** It is a separate, always-running server
program. Your script does not open the data files itself; it sends requests over a connection
(a local socket or a network port) and the server answers. Many programs can be connected at the
same time, and the server is the single referee that keeps them from stepping on each other.

> Analogy: Postgres is a **restaurant kitchen**. You (a client) do not walk in and cook. You phone
> in an order, the kitchen prepares it, and sends out the result. Many customers phone the same
> kitchen at once, and the kitchen keeps all the orders straight.

In this repo we do **not** own the Postgres database. It belongs to `cardano-db-sync`, which writes
the entire Cardano chain into it. Our monitor is a **guest reading over its shoulder** while it
works. That "we are only a reader, do not disturb the writer" idea drives several design choices
later (autocommit, isolation level).

**SQLite is an embedded database.** There is no server. SQLite is a *library* that runs inside your
own program, and the whole database is just one ordinary file on disk (for us, something like
`data/cardano-db-sync/preprod.db`). When your program ends, nothing keeps running.

> Analogy: SQLite is a **notebook on your own desk**. You open it, write a line, close it. No phone
> call, no kitchen, no other staff. Just you and a file.

In this repo we **own** the SQLite files completely. They are our private log: every few seconds the
monitor writes one row of measurements (CPU, memory, chain tip, table sizes) into SQLite, and later
the plot and report scripts read those rows back to draw graphs.

The data lifecycle, end to end, is therefore: **cardano-db-sync writes Postgres -> our monitor reads
Postgres and writes SQLite -> our plot/report scripts read SQLite and produce HTML and text.**
Section 11 walks through that in detail.

---

## 2. Drivers and cursors: how Python talks to a database

A Python program cannot speak a database's private protocol on its own. It needs a **driver**: a
library that knows how to open the connection, encode your request in the database's language, and
decode the reply back into Python values.

> Analogy: a driver is a **translator on a phone line**. You speak Python; the database speaks its
> own protocol; the translator sits in the middle so each side hears its own language.

This repo uses two drivers:

- **`psycopg2`** for Postgres. It is a third-party package (listed in `requirements.txt`). The "2"
  is just its major version; it is the most widely used Postgres driver for Python.
- **`sqlite3`** for SQLite. This one ships **inside Python's standard library**, because SQLite is
  small enough to bundle. You just `import sqlite3`; there is nothing extra to install.

Two objects show up every time you use either driver:

- A **connection** is the open line to the database. You get one by calling `psycopg2.connect(...)`
  or `sqlite3.connect("somefile.db")`.
- A **cursor** is the thing you actually run a query on and read results from. You create it from a
  connection, call `cursor.execute("SELECT ...")`, and then pull rows out.

> Analogy: if the connection is the open phone line, the cursor is the **order pad**. You write one
> request on it, hand it over, and read the answer back from the same pad.

How you read rows back:

- `cursor.fetchone()` returns the next single row (or `None` if there are no more). Used when you
  expect exactly one answer, like "what is the latest block?".
- `cursor.fetchall()` returns every remaining row as a list. Used for small result sets.
- `cursor.description` tells you the column names of the last query. The helper `query_df` in
  `scripts/_db_sync_queries.py` uses it to label a pandas DataFrame:

```python
with conn.cursor() as cur:
    cur.execute(sql, params)
    rows = cur.fetchall()
    cols = [desc[0] for desc in cur.description] if cur.description else []
```

A row comes back as a simple tuple, so `row[0]` is the first column. That is why you see patterns
like `return bool(row and row[0])` (in `table_exists`): "did we get a row, and is its first column
truthy?".

---

## 3. Connecting to Postgres

The helper `pg_connect` in `scripts/_db_sync_queries.py` (and the matching `_connect`/`_pg` methods
in `scripts/db-sync-resource-monitor.py`) open a Postgres connection. Two details are worth understanding.

**Where does it connect, and as whom?** If you do not pass a host, port, or user, `psycopg2` falls
back to the standard environment variables `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`. This is the
exact same convention the `psql` command-line tool uses, so if `psql` already works on your machine,
the monitor will connect the same way with no extra configuration. On a typical local setup this
means connecting over a Unix socket using **peer authentication** (Postgres trusts your OS user), so
there is no password to manage.

**Why is the database name always given explicitly?** Because this is an A/B testing toolkit: you
often have several databases side by side (for example one per `cardano-db-sync` version you are
comparing). The code never relies on a default database; it always says exactly which `dbname` to
open, so version A and version B never get mixed up.

```python
conn_kwargs: dict[str, Any] = {"dbname": pg_dbname}
if pg_host is not None:
    conn_kwargs["host"] = pg_host
# ... port, user added the same way ...
conn = psycopg2.connect(**conn_kwargs)
```

**One gotcha to know now (full detail in 05):** when you write
`with psycopg2.connect(...) as conn:`, psycopg2 commits or rolls back the transaction when the block
ends, but it does **not** close the connection. Over a long run that leaks connections. So this repo
wraps connections in its own `@contextmanager` with an explicit `finally: conn.close()`. See
[05 - Connection management](05-database-internals.md) for the worked explanation.

---

## 4. Connection lifecycle, and what "pooling" means

Opening a connection to a client/server database is not free: it is a network handshake plus, for
Postgres, the server starting a process for you. If the monitor opened a brand new connection for
**every** sample, every 10 seconds, for a sync that runs for days, that would be a lot of needless
handshakes.

So the monitor uses **two different strategies**, on purpose:

- **Setup-phase work uses short-lived connections.** The `_pg` context manager opens a connection,
  does one job, and closes it. This is used for things that happen rarely, like waiting for the
  database schema to exist before the sync has created its tables, or probing once whether a feature
  is enabled.

- **The steady-state sample loop reuses one long-lived connection.** `_ensure_loop_conn` opens a
  single connection the first time it is needed and hands that same connection back to all four
  per-sample queries (`get_tip`, `get_first_block_time`, `get_ingest_metrics`,
  `get_table_rowcounts`). The docstring says it plainly:

> "Reused across the four steady-state query methods ... so the monitor doesn't open ~3-4 new TCP
> connections per sample. Opened lazily on first call; reopened automatically if the connection has
> been dropped."

**Reconnect-on-drop.** Networks blip and databases restart. Each loop query catches the
connection-level errors (`psycopg2.OperationalError`, `psycopg2.InterfaceError`), throws the dead
connection away (`_drop_loop_conn`), and returns `None` so that one sample is simply skipped. The
next sample calls `_ensure_loop_conn` again, which notices the connection is gone and opens a fresh
one. The missing sample later shows up as a gap in the graph (section 12).

### The code, line by line

Three small methods in `scripts/db-sync-resource-monitor.py` implement everything above. They are short on
purpose; the value is in *why* each line is there.

**The short-lived connection (setup phase).** A `@contextmanager` is Python's way of guaranteeing
"do this at the end no matter what". Here the guaranteed end-step is `conn.close()`, so a setup query
can never leak a connection even if it raises:

```python
@contextmanager
def _pg(self, **kwargs):
    conn = self._connect(**kwargs)   # open a brand-new connection
    try:
        yield conn                   # hand it to the `with` block; run the one query
    finally:
        conn.close()                 # always close, success or error
```

You use it as `with self._pg() as conn: ...`. The moment the `with` block ends, the connection is
closed. That is exactly what you want for work that happens **once** (waiting for the schema) or
**rarely** (the one-time UTXO probe), where the handshake cost does not matter.

**The long-lived connection (sample loop).** The loop must not pay that handshake every 10 seconds,
so it keeps one connection on the instance (`self._loop_conn`) and reuses it:

```python
def _ensure_loop_conn(self):
    if self._loop_conn is None or self._loop_conn.closed:
        self._loop_conn = self._connect()   # first call, or after a drop: open one
        self._loop_conn.autocommit = True    # see below - critical for a long-lived conn
    return self._loop_conn                    # every later call: hand back the SAME one
```

Two details carry their weight:

- **`is None or .closed`** is the "open it lazily, and re-open if it died" logic in one line. On the
  very first sample `_loop_conn` is `None`, so we open one. On every healthy sample after that, the
  `if` is false and we just return the connection we already have - no new handshake.
- **`autocommit = True`** matters *because* the connection is long-lived. Without autocommit, Postgres
  opens an implicit transaction on your first query and holds it open until you commit. A transaction
  that stays open for days would pin an old snapshot of the database and **stop Postgres from cleaning
  up dead rows** (the `VACUUM` process), which bloats the very database we are trying to measure.
  Autocommit makes each tiny `SELECT` commit immediately, so we hold no long-running transaction.
  (Transactions and autocommit are section 5.)

**Throwing away a dead connection.** When a query hits a connection-level error, it calls this so the
*next* sample re-opens cleanly:

```python
def _drop_loop_conn(self):
    if self._loop_conn is not None:
        try:
            self._loop_conn.close()   # best-effort close; it may already be dead
        except Exception:
            pass                       # closing a dead socket can itself raise - ignore it
        self._loop_conn = None         # set to None so _ensure_loop_conn re-opens next time
```

**How a sample query ties it together.** Every steady-state query follows the same shape. `get_tip`
is the clearest example:

```python
def get_tip(self):
    try:
        conn = self._ensure_loop_conn()         # reuse (or lazily open) the shared connection
        with conn.cursor() as cur:
            cur.execute("SELECT slot_no, epoch_no, block_no, time FROM block "
                        "WHERE block_no IS NOT NULL ORDER BY block_no DESC LIMIT 1;")
            r = cur.fetchone()
        return (r[0], r[1], r[2], r[3]) if r else None
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        warn(f"Postgres connection lost during get_tip ({e}); will reconnect next sample.")
        self._drop_loop_conn()                  # connection is dead: discard it...
        return None                             # ...skip THIS sample; next one reconnects
    except Exception as e:
        warn(f"Postgres error: {e}")            # a non-connection error (e.g. bad SQL):
        return None                             # skip the sample but KEEP the connection
```

The key is the **two different `except` blocks**. A connection-level failure
(`OperationalError` / `InterfaceError`) means the socket itself is gone, so we drop it and let the
next sample rebuild it. Any *other* error (a transient query problem) is not the connection's fault,
so we keep the connection and just skip the one reading. Either way the method returns `None` rather
than crashing, and the run survives a database restart or a network blip as nothing worse than one
missing point on the graph.

That is the whole pattern: **open once, reuse, commit each read immediately, and rebuild only when the
socket actually dies.** It is the right amount of machinery for a single-process loop - and, as the
next part explains, why a full connection pool would be overkill here.

### What is connection pooling?

A **connection pool** is a set of already-open connections kept ready in advance. When code needs to
talk to the database, it **borrows** a connection from the pool and **returns** it when done, instead
of opening and closing a brand new one each time.

> Analogy: a pool is a **rank of taxis idling at the curb**. You take one, ride, and bring it back
> for the next person. Without a pool, every trip means phoning for a brand new cab and waiting for
> it to arrive, then sending it away forever.

Pools shine when **many requests happen concurrently** and each is short. The classic example is a
web server handling hundreds of overlapping requests per second: opening a fresh connection per
request would be far too slow, and a pool of, say, 20 shared connections serves them all.

**This project deliberately does not use a connection pool, and that is the right call.** A monitor
is a single process that makes one query at a time, in a slow loop. There is no concurrency to
manage, so a pool would add machinery for zero benefit; reusing one long-lived connection (above) is
simpler and already optimal. And SQLite has no notion of a connection pool at all: it is a local
file, opening it is nearly instant, so the SQLite side just opens a connection per write and closes
it. The lesson worth remembering: **pooling is a tool for concurrency and connection-setup cost; if
you have neither problem, you do not need it.**

---

## 5. Transactions, COMMIT, ROLLBACK, and autocommit

A **transaction** is a group of database statements treated as one all-or-nothing unit. Either every
statement in the group takes effect, or none of them do.

> Analogy: a **bank transfer**. "Subtract 100 from account A" and "add 100 to account B" must both
> happen or neither. You must never end up with the money gone from A but never arrived at B. Wrap
> both in one transaction and the database guarantees you never see that half-done state.

Two commands end a transaction:

- **COMMIT** makes all its changes permanent.
- **ROLLBACK** throws all its changes away, as if the transaction never happened.

By default, a database opens a transaction for you implicitly, and you decide when to COMMIT.

**Autocommit mode** is the opposite setting: every single statement is its own tiny transaction that
commits immediately, so there is never an open multi-statement transaction sitting around.

The monitor's loop connection turns **autocommit on**, and the reason is specific and important:

```python
self._loop_conn = self._connect()
self._loop_conn.autocommit = True
```

The docstring explains why:

> "Autocommit is on so each query commits immediately and doesn't hold a long-running transaction
> across the loop - important to let postgres clean up dead tuples (vacuum) without waiting on the
> monitor's snapshot."

Here is the plain-English version. When a transaction is open in Postgres, it pins a consistent
view of the data (a "snapshot", see section 7). Postgres cannot fully clean up old, deleted row
versions ("dead tuples", removed by a background job called autovacuum) while some open transaction
might still need to see them. Our monitor is a **guest** on a database that `cardano-db-sync` is
hammering with writes. If our long-running read held a snapshot open the whole time, we could get in
the way of the database's own housekeeping. Autocommit means each of our little read queries
finishes instantly and holds nothing open, so we observe without interfering.

**The opposite case, where we want one explicit transaction:** the `rename-version.py` tool renames a
version label across several SQLite tables at once. It wraps all the `UPDATE`s in one transaction so
the rename is atomic:

```python
conn.execute("BEGIN")
for t in tables:
    conn.execute(f"UPDATE {t} SET version = ? WHERE version = ?", (new, old))
conn.execute("COMMIT")   # or ROLLBACK if anything went wrong
```

Either every table gets renamed or none do. You can never end up with three tables renamed and two
not, which would leave the database in a confusing half-renamed state.

---

## 6. Isolation levels

This is the database concept newcomers have most often never met, so we build it up slowly.

**The problem.** When several transactions read and write the same data at the same time, what
exactly should a reader see of another transaction's not-yet-finished changes? Different answers give
different trade-offs between *correctness* and *concurrency*. The rules that pin this down are called
**isolation levels**. The classic problems they rule in or out are:

- **Dirty read:** you read another transaction's change before it has committed, and then that
  transaction rolls back, so you acted on data that never really existed.
- **Non-repeatable read:** you read a row, someone else commits a change to it, you read it again in
  the same transaction and get a different value.
- **Phantom read:** you run "all rows where X" twice in one transaction and the second run has extra
  rows because someone inserted matching data in between.

The SQL standard defines four levels, from loosest (most concurrency, fewest guarantees) to
strictest (most guarantees, least concurrency):

| Level | Roughly means |
|:---|:---|
| READ UNCOMMITTED | You may even see uncommitted ("dirty") changes. Fastest, least safe. |
| READ COMMITTED | You only see committed data, but each statement sees a fresh view. |
| REPEATABLE READ | Within one transaction, rows you have read will not change underneath you. |
| SERIALIZABLE | The result is as if transactions ran one after another, never overlapping. Safest, most contention. |

**Postgres uses READ COMMITTED by default,** and it implements it with a technique called **MVCC
(multi-version concurrency control)**. Instead of locking rows so readers and writers wait for each
other, Postgres keeps multiple versions of a row and shows each statement a consistent snapshot.

> Analogy: every time you ask a question, Postgres hands you a **photograph** of the data taken at
> that instant. While you study the photo, the real scene can keep changing; you simply do not see
> those changes until you take a new photo (run the next statement). The big practical payoff:
> **readers never block writers and writers never block readers.**

**Which level do these scripts use, and why?** They never set one, so they get the default,
READ COMMITTED. Combined with the autocommit choice from section 5, that is exactly what a sampler
wants:

- We take **independent point-in-time samples**. Each query should see the **latest committed**
  state of the chain at the moment it runs, which is precisely what READ COMMITTED gives.
- We explicitly do **not** want a long-lived snapshot that stays frozen for the whole run (that is
  what stricter levels inside a long transaction would create), because that is exactly what would
  interfere with the database's vacuuming.

So "use the default and keep transactions tiny" is a deliberate, correct design decision here, not an
oversight.

**SQLite's model is simpler:** it allows only **one writer at a time** for the whole database. Many
readers can read at once, and with WAL mode (next section) a reader sees a consistent snapshot even
while a write is happening. We do run several collectors against one env file, so they take turns on
that single write lock; a generous busy timeout (`connect_writer`, section 9) makes the waiting safe.

---

## 7. Postgres WAL versus SQLite WAL

You will see the term **WAL (write-ahead log)** for both databases, and it is easy to assume it means
the same thing. It does not, quite, so this section untangles it.

The shared idea: **write down what you are about to change in a sequential log first, then apply the
change to the main data.** If the power dies halfway, the database can replay or undo the log to get
back to a clean state.

> Analogy: before editing the master ledger, you jot the change in a **running diary**. If you are
> interrupted, the diary tells you exactly what was and was not finished, so you can finish or unwind
> it.

The two databases use this idea for different primary purposes:

| | Postgres WAL | SQLite WAL |
|:---|:---|:---|
| Main purpose | Durability and crash recovery (and feeding replication) | Concurrency: let readers read while a writer writes |
| Who turns it on | Always on; core to how Postgres works | We opt in with `PRAGMA journal_mode=WAL` |
| Who drives it here | `cardano-db-sync`'s heavy writes | Our monitor writing samples |
| Do we manage it? | No, it is the server's business | Yes, it is our file |

**Postgres WAL** is the mechanism that lets Postgres promise that once it says "committed", your data
survives a crash. We do not configure it; it just runs. It is relevant to us only indirectly: it is
part of why a heavily writing database produces a lot of disk I/O, which connects to the storage
story in [07 - System performance primer](07-system-performance-primer.md).

**SQLite WAL** is something we deliberately switch on (`PRAGMA journal_mode=WAL`) so that the plot and
report scripts can read the stats file **at the same time** the monitor is writing to it, without
either blocking the other. The full mechanics (the extra `-wal` and `-shm` files, and "checkpointing"
that folds the log back into the main file) are explained in depth in
[05 - SQLite WAL mode](05-database-internals.md); this doc just wants you to know *why* the two WALs
exist and that they are not the same feature.

---

## 8. Parameterized queries, and why string-built SQL is dangerous

Every query in this repo passes its values through **placeholders** rather than pasting them into the
SQL text. This is not a style preference; it prevents a whole class of bug and security hole called
**SQL injection**.

**The danger.** Imagine building SQL by string formatting with a value that came from outside:

```python
# NEVER do this
cur.execute(f"SELECT * FROM users WHERE name = '{name}'")
```

If `name` is the harmless `"Alice"`, fine. But if `name` is `"'; DROP TABLE users; --"`, the text you
just built becomes two commands, the second of which deletes the table. The database cannot tell that
the malicious part was supposed to be *data*; it reads it as *code*.

> This is the famous "Little Bobby Tables" problem: a value that is secretly a command, smuggled in
> because data and code were glued together as one string.

**The fix: placeholders.** You write the SQL with a marker where the value goes, and pass the value
separately. The driver then sends the query text and the value over the wire as two distinct things,
so the value can never be parsed as part of the command:

```python
# psycopg2 uses %s as the placeholder
cur.execute("SELECT to_regclass(%s) IS NOT NULL;", (name,))
cur.execute("... WHERE relname = ANY(%s);", (HOT_TABLES,))

# sqlite3 uses ? as the placeholder
conn.execute("UPDATE memory_metrics SET version = ? WHERE version = ?", (new, old))
```

(Note that `%s` is psycopg2's own placeholder; it is **not** Python string formatting. You never put
the value into the string yourself.)

**The one safe exception you will see in the code:** sometimes a *table or column name* is inserted
with an f-string, for example when iterating over tables to rename. That is acceptable here only
because those names come from a fixed internal allowlist defined in the source (`HOT_TABLES`,
`VERSION_TABLES`), never from user input. The rule of thumb: **values always go through
placeholders; identifiers may be interpolated only from constants you control.**

---

## 9. The SQLite stats store: how we create, grow, and back it up

This section is about the database we own.

**One file per environment and role.** The monitor writes to a file named after the network, like
`data/cardano-db-sync/preprod.db` or `data/cardano-node/mainnet.db`. Keeping environments in separate
files means there is never cross-talk between, say, preprod and mainnet data.

**A single file, but not a single writer.** Several collectors can append to one env file at the same
time: a node run may have `node-resource-monitor.py`, `node-rts-monitor.py`, and `node-db-size-monitor.py` all
writing `data/cardano-node/<env>.db`, and a db-sync run has `db-sync-resource-monitor.py` plus
`db-sync-ledger-size-monitor.py` on `data/cardano-db-sync/<env>.db`. SQLite still allows only one
writer at a time, so they take turns; a blocked writer waits up to a 30-second busy timeout for the
lock (via `_common.connect_writer`, see [05 - Multiple writers and `busy_timeout`](05-database-internals.md#multiple-writers-and-busy_timeout))
rather than failing immediately, and each loop drops a single sample if it ever times out.

**Creating the schema is idempotent.** Every time the monitor starts, it runs `CREATE TABLE IF NOT
EXISTS ...` for each table it needs. "Idempotent" means running it again changes nothing: if the
table already exists, the statement is a no-op.

> Analogy: "make this folder if it is not already there." Safe to say every single time you start,
> without worrying about whether it is the first run or the hundredth.

**Evolving the schema without a migration framework.** Sometimes a newer monitor adds a column an
older database file does not have. The code checks the existing columns with `PRAGMA table_info(...)`
and adds the missing one with `ALTER TABLE ... ADD COLUMN` only if needed:

```python
for tbl in ("memory_metrics", "cpu_metrics"):
    cols = {row[1] for row in c.execute(f"PRAGMA table_info({tbl})")}
    if "ts" not in cols:
        c.execute(f"ALTER TABLE {tbl} ADD COLUMN ts TEXT")
```

This lets an old stats file keep working with a new version of the tool, gaining the new column on
first run rather than forcing you to throw the file away.

**Wide tables versus long (narrow) tables.** Two table shapes appear, on purpose:

- A **wide** table has one row per sample with many columns. `ingest_metrics` is wide: each row holds
  one moment's tip lag, db size, block number, tx id, and so on. Good when the set of measurements is
  fixed and known.
- A **long/narrow** table has one row per sample *per item*. `table_rowcounts` is long: for each
  sample it writes one row for each hot table (`ts, slot_no, version, table_name, row_count`). This is
  the right shape when the set of things you measure can vary, because adding another table to watch
  means more rows, not a schema change.

**Commit cadence.** The monitor opens SQLite with `with connect_writer(...) as conn:` once per
sample and lets the context manager commit at the end of that block. So each sample is one small
committed transaction. Because SQLite is a local file, opening it per sample is cheap (no network
handshake), which is why the Postgres-style "reuse one connection" trick is unnecessary here.
`connect_writer` is just `sqlite3.connect` with a 30-second busy timeout, so when several collectors
write the same env file at once a blocked writer waits for the lock instead of erroring (see
[05 - Multiple writers and `busy_timeout`](05-database-internals.md#multiple-writers-and-busy_timeout)).
If a write does time out, the loop drops that one sample with a warning and keeps going - the same
"skip one sample" handling used for a Postgres connection blip above.

**Backing it up safely (the online backup API).** You might think you can back up a SQLite database
by copying the `.db` file with `cp`. Under WAL mode that is unsafe: recent changes may still live in
the separate `-wal` sidecar file and not yet be folded into the main `.db`. A plain copy of just the
`.db` would miss them. So `backup-stats.py` uses SQLite's **online backup API** through
`Connection.backup()`, which produces one clean, consistent `.db` with the WAL already merged in,
even while the monitor keeps writing:

```python
src = sqlite3.connect(str(src_path))
dst = sqlite3.connect(str(dst_path))
with dst:
    src.backup(dst)   # drains the WAL into a single consistent file
```

> Analogy: someone is writing the master ledger, but their latest notes are still on a separate
> scratch pad (the `-wal` file). Photocopying only the ledger would lose those notes. The backup API
> **merges the scratch pad into the ledger first**, then gives you one complete copy.

---

## 10. How data is obtained, end to end

Now we can put the whole pipeline together:

```
cardano-db-sync  ->  PostgreSQL  ->  monitor (samples every N seconds)  ->  SQLite (.db)
                                                                              |
                                                            plot / report scripts
                                                                              |
                                                                   HTML graphs + text reports
```

The monitor's job each tick is: ask Postgres a few cheap questions, and append the answers as one
row (or a few rows) to SQLite. Here is the map of the main read queries and where their answers land:

| Query method (Postgres) | What it asks for | Lands in SQLite table |
|:---|:---|:---|
| `get_tip` | the latest block (slot, epoch, block_no, time) | `ingest_metrics` |
| `get_first_block_time` | timestamp of the first block (for sync-rate math) | used in calculations |
| `get_ingest_metrics` | tip lag, db size, max block_no, max tx id, UTXO count | `ingest_metrics` |
| `get_table_rowcounts` | approximate row count of each hot table | `table_rowcounts` |
| `fetch_epoch_stats` (report) | per-epoch block/tx/fee aggregates | report output |

These read queries lean on several **tricks** to stay cheap even against a mainnet-sized database.
Each is summarized here in one line; **[05 - Database internals](05-database-internals.md) is the
deep dive** for all of them:

- **Reverse index scan for "the latest row":** `ORDER BY block_no DESC LIMIT 1` jumps straight to the
  newest block using the index, instead of scanning the whole `block` table.
- **Approximate counts instead of `COUNT(*)`:** reading `pg_class.reltuples` gives a cheap estimated
  row count, avoiding a full table scan when an exact number does not matter.
- **`EXISTS (SELECT 1 ... LIMIT 1)`:** to check "does any such row exist?" without scanning all of
  them, stop at the first hit.
- **`statement_timeout` as a guard:** before a probe that *might* accidentally scan a huge table, set
  a short timeout so the worst case fails fast instead of hanging.
- **`PERCENTILE_CONT` gated behind `--with-p95`:** the exact p95 calculation must sort every row in a
  group, which is minutes of work on mainnet, so it is off by default.
- **Era from the ledger, not from block signaling:** the era for an epoch is derived from
  `epoch_param.protocol_major` (what the ledger actually activated), not from `block.proto_major`
  (which a block producer can advertise many epochs early). Using the wrong one mislabels eras.
- **UTC pinning of naive timestamps:** Postgres returns timestamps without a timezone attached, but
  the values are UTC. Python would otherwise read them as *local* time and introduce a constant
  offset. The code pins `tz=UTC` before converting. This exact bug once showed up as a fake "tip lag"
  equal to the operator's timezone offset (the "`TipLag 2h`" bug).

---

## 11. The data-shaping algorithms, in plain language

Beyond reading and writing, the scripts run a handful of small algorithms to turn raw samples into
honest graphs. None of them is complicated once named.

**Uniform-interval sampling loop.** The monitor loops "take a sample, sleep N seconds, repeat" until
told to stop, and it installs handlers for the stop signals (`SIGINT` from Ctrl-C, `SIGTERM`) so it
can finish the current write cleanly rather than being killed mid-row.

**Gap-break insertion.** If the monitor was down for a while (a restart, a closed laptop, an ssh
disconnect), there is a hole in the data. A naive line chart would draw a single straight line across
that hole, implying a smooth trend that never happened. So `insert_gap_breaks` (in `_common.py`)
detects when two consecutive samples are more than 5× that series' own median sample interval apart
(so ~50s for the 10s collectors, ~300s for the 60s disk collector) and inserts a special marker row
holding `NaN` ("not a number") at the midpoint. Plotting libraries treat `NaN` as "lift the pen
here", so the line breaks instead of lying. (The time-series reasoning behind this is expanded in
[01 - Time-series fundamentals](01-time-series-fundamentals.md).)

> Analogy: if you stopped logging your car's mileage for a week, you would not draw a ruler-straight
> line across the gap and pretend you knew the mileage each missing day. You would leave a break.

**Per-epoch duration.** To find how long an epoch took, the code groups samples by epoch and computes
`max(timestamp) - min(timestamp)` within the group. The very first and last epochs of a run are
"partial": the monitor only saw part of them, so their durations are understood as lower bounds, not
full-epoch times.

**Rates from running totals (derivatives).** Some columns are cumulative, like "highest block number
seen". To get a rate such as blocks-per-second, the code takes the difference between consecutive
samples and divides by the time between them: `rate = delta(value) / delta(time)`. The first sample in
each series has no previous value to subtract, so its rate is `NaN`.

**Era lookup.** A small dictionary maps each protocol-major number to its era name (0 to 1 = Byron,
2 = Shelley, ... 9 to 10 = Conway), with unknown future numbers landing in a clearly labeled
"Unknown" bucket rather than crashing.

**Version resolution.** When you pass `--versions`, `resolve_versions` matches each input against the
versions actually present: an exact match wins; otherwise it tries a short token (like just the
version number); if that token matches exactly one available version it is used; if it matches none,
or more than one, the script stops with a clear error rather than guessing.

**Rejecting NaN and infinity on input.** When the RTS monitor scrapes metric numbers from the node's
Prometheus endpoint, it discards any value that is `NaN` or infinite, because a single bad value
would poison averages and stretch graph axes to uselessness.

---

## 12. Recap and cheat-sheet

- The repo touches **two databases**: Postgres (a client/server database owned by `cardano-db-sync`,
  which we only **read**) and SQLite (an embedded one-file database we **own** and write our samples
  into).
- A **driver** (`psycopg2`, `sqlite3`) is the translator library; a **connection** is the open line;
  a **cursor** is what you run a query on and read rows from.
- The monitor uses **one long-lived autocommit connection** for its sample loop and **short-lived
  connections** for setup, and reconnects automatically if the connection drops.
- A **transaction** is all-or-nothing; **COMMIT** keeps it, **ROLLBACK** discards it; **autocommit**
  makes each statement its own tiny transaction. The loop runs autocommit so it never holds a
  snapshot open and never interferes with the database it is observing.
- **Isolation levels** decide what one transaction sees of others' in-flight changes. Postgres
  defaults to **READ COMMITTED** with **MVCC** (each statement reads a consistent snapshot; readers
  and writers never block each other). The scripts keep that default on purpose.
- **Connection pooling** (a ready supply of reusable connections) is for concurrent, high-volume
  workloads. A single-process monitor does not need it, so it does not use one.
- **Always use placeholders** (`%s` for psycopg2, `?` for sqlite3) so values can never be executed as
  SQL. Only fixed internal constants are ever interpolated as identifiers.
- **WAL** means "write-ahead log" in both databases, but Postgres uses it for **durability** and we
  use SQLite's WAL for **concurrent reading while writing**.
- We back up SQLite with the **online backup API**, not a file copy, so nothing pending in the `-wal`
  file is lost.

**Which database, which tool, which guarantee:**

| You want to... | Use... |
|:---|:---|
| Read what cardano-db-sync produced | Postgres via `psycopg2` (read-only, autocommit, READ COMMITTED) |
| Store and later plot our own samples | SQLite via `sqlite3` (WAL mode; writers serialized, `connect_writer` busy timeout) |
| Avoid table scans on a huge DB | the query tricks in [05](05-database-internals.md) |
| Read the stats file while the monitor writes | SQLite WAL mode |
| Make a safe copy of the stats file | `backup-stats.py` (online backup API) |
| Understand why a chart breaks across a gap | gap-break insertion (section 11), and [01](01-time-series-fundamentals.md) |

For the query-optimization deep dives referenced throughout, read
[05 - Database internals](05-database-internals.md). For the time-series and plotting reasoning, read
[01 - Time-series fundamentals](01-time-series-fundamentals.md). For how disk, RAM, and I/O pressure
shape all of this at the system level, read
[07 - System performance primer](07-system-performance-primer.md).
