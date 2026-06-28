#!/usr/bin/env python3
"""Check whether Postgres picks a good plan for db-sync's rollback min-id queries.

This is the one-shot diagnostic for cardano-db-sync issue #2083 ("lengthy
rollbacks"): on large databases the planner sometimes walks a table's PRIMARY
KEY (id order) and *filters* on `block_id`/`tx_id`, instead of using the range
index - turning a millisecond rollback lookup into minutes. This script asks
Postgres (via plain `EXPLAIN`, which only plans, never executes) which plan it
would choose for each affected table, and flags any that pick the bad plan.

Read-only and safe: it runs `EXPLAIN` (not `EXPLAIN ANALYZE`), so nothing is
executed against the tables and the database is never modified. It probes at a
key value just above each table's current maximum - the rollback boundary case,
where the bad plan is most likely to surface.

Exit status: 0 if every populated table picks a good plan, 1 if any picks the
bad (PK + Filter) plan, 2 on a connection/operational error - so it can gate CI
or a pre-upgrade check. See docs/17 for the full background.
"""

from __future__ import annotations

import argparse
import re
import sys

from _db_sync_queries import pg_connect, table_exists

# (table, the column the rollback filters on). These are the queries from #2083.
ROLLBACK_PROBE_TABLES: tuple[tuple[str, str], ...] = (
    ("tx", "block_id"),
    ("tx_cbor", "tx_id"),
    ("datum", "tx_id"),
    ("tx_metadata", "tx_id"),
)


def build_probe_sql(table: str, col: str, value: int) -> str:
    """The rollback min-id query for one table, at a boundary `value`. Wrapped in
    EXPLAIN so Postgres returns the plan without running it."""
    return f"EXPLAIN SELECT id FROM {table} WHERE {col} >= {value} ORDER BY id ASC LIMIT 1;"


def classify_plan(plan_text: str, col: str) -> str:
    """Classify an EXPLAIN plan as 'good', 'bad', or 'unknown' for a rollback
    min-id query on `col`.

    - good: an index provides the range bound directly - `Index Cond: (<col> >= ...)`.
    - bad:  a primary-key scan with the bound pushed down to a `Filter` - the
            issue #2083 plan (`Index Scan using <t>_pkey ... Filter: (<col> >= ...)`),
            which scans the table in id order until the filter matches.
    - unknown: anything else (e.g. a seq scan, or a shape we don't recognize).
    """
    c = re.escape(col)
    if re.search(rf"Index Cond:[^\n]*\b{c}\b", plan_text, re.IGNORECASE):
        return "good"
    pkey_scan = re.search(r"using\s+\w*_pkey\b", plan_text, re.IGNORECASE)
    filter_on_col = re.search(rf"Filter:[^\n]*\b{c}\b", plan_text, re.IGNORECASE)
    if pkey_scan and filter_on_col:
        return "bad"
    return "unknown"


def _index_used(plan_text: str) -> str:
    m = re.search(r"using\s+(\w+)", plan_text, re.IGNORECASE)
    return m.group(1) if m else "?"


def check_table(conn, table: str, col: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """Probe one table; returns a result dict with verdict and details."""
    if not table_exists(conn, table):
        return {"table": table, "verdict": "absent", "rows": 0, "index": "-", "plan": ""}
    with conn.cursor() as cur:
        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = %s", (table,))
        row = cur.fetchone()
        rows = int(row[0]) if row and row[0] is not None else 0
        cur.execute(f"SELECT max({col}) FROM {table}")
        mx = cur.fetchone()[0]
        if mx is None:
            return {"table": table, "verdict": "empty", "rows": max(rows, 0), "index": "-", "plan": ""}
        cur.execute(build_probe_sql(table, col, int(mx) + 100))
        plan = "\n".join(r[0] for r in cur.fetchall())
    return {
        "table": table,
        "verdict": classify_plan(plan, col),
        "rows": rows,
        "index": _index_used(plan),
        "plan": plan,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Check Postgres plans for db-sync rollback min-id queries (issue #2083)")
    p.add_argument("--pg-dbname", required=True, help="db-sync Postgres database name")
    p.add_argument("--pg-host", default=None, help="Postgres host (default: PGHOST / socket)")
    p.add_argument("--pg-port", default=None, help="Postgres port (default: PGPORT / 5432)")
    p.add_argument("--pg-user", default=None, help="Postgres user (default: PGUSER)")
    p.add_argument("--show-plans", action="store_true", help="Print the full EXPLAIN plan for each table")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with pg_connect(args.pg_host, args.pg_port, args.pg_user, args.pg_dbname) as conn:
            results = [check_table(conn, t, c) for t, c in ROLLBACK_PROBE_TABLES]
    except Exception as e:
        print(f"error: could not run the check against '{args.pg_dbname}': {e}", file=sys.stderr)
        return 2

    print(f"Rollback plan check for '{args.pg_dbname}' (issue #2083):\n")
    print(f"  {'table':<14} {'rows':>14}  {'index used':<22} verdict")
    print(f"  {'-' * 14} {'-' * 14}  {'-' * 22} -------")
    labels = {
        "good": "GOOD (range index)",
        "bad": "BAD (pkey + filter)",
        "empty": "empty (no data)",
        "absent": "absent",
        "unknown": "unknown plan",
    }
    for r in results:
        rows = f"{r['rows']:,}" if isinstance(r["rows"], int) and r["rows"] >= 0 else "?"
        print(f"  {r['table']:<14} {rows:>14}  {r['index']!s:<22} {labels.get(str(r['verdict']), r['verdict'])}")
    if args.show_plans:
        for r in results:
            if r["plan"]:
                print(f"\n--- {r['table']} ---\n{r['plan']}")

    bad = [r["table"] for r in results if r["verdict"] == "bad"]
    print()
    if bad:
        print(
            f"FAIL: {', '.join(str(b) for b in bad)} would use the slow PK-scan plan. "
            "On a populated table this is the issue #2083 lengthy-rollback regression. "
            "See docs/17 for what to do."
        )
        return 1
    print("OK: every populated table uses a range-index plan for the rollback min-id query.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
