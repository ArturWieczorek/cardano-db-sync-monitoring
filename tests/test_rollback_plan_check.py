"""Tests for scripts/rollback-plan-check.py - the issue #2083 plan diagnostic.

The DB-touching part needs Postgres; here we test the pure pieces: the EXPLAIN
probe SQL and the plan classifier, using real EXPLAIN output captured from a
mainnet db-sync database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("rollback_plan_check", SCRIPTS / "rollback-plan-check.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rpc = _load()

# Real plans captured on a 13.7.1.0 mainnet DB.
GOOD_TX = """\
 Limit  (cost=4.59..4.60 rows=1 width=8)
   ->  Sort  (cost=4.59..4.60 rows=1 width=8)
         Sort Key: id
         ->  Index Scan using idx_tx_block_id on tx  (cost=0.57..4.58 rows=1 width=8)
               Index Cond: (block_id >= 13546754)"""

BAD_TX_CBOR = """\
 Limit  (cost=0.15..0.33 rows=1 width=8)
   ->  Index Scan using tx_cbor_pkey on tx_cbor  (cost=0.15..62.88 rows=357 width=8)
         Filter: (tx_id >= 100)"""

SEQ_SCAN = """\
 Limit  (cost=0.00..1.00 rows=1 width=8)
   ->  Seq Scan on foo  (cost=0.00..1.00 rows=1 width=8)
         Filter: (bar >= 100)"""


class TestClassifyPlan:
    def test_index_cond_on_col_is_good(self) -> None:
        assert rpc.classify_plan(GOOD_TX, "block_id") == "good"

    def test_pkey_with_filter_on_col_is_bad(self) -> None:
        assert rpc.classify_plan(BAD_TX_CBOR, "tx_id") == "bad"

    def test_seq_scan_is_unknown(self) -> None:
        assert rpc.classify_plan(SEQ_SCAN, "bar") == "unknown"

    def test_pkey_filter_on_a_different_column_is_not_flagged_bad(self) -> None:
        # A PK scan filtering on some other column is not the #2083 pattern for `tx_id`.
        assert rpc.classify_plan(BAD_TX_CBOR, "block_id") == "unknown"


class TestBuildProbeSql:
    def test_shape(self) -> None:
        sql = rpc.build_probe_sql("tx", "block_id", 999)
        assert sql == "EXPLAIN SELECT id FROM tx WHERE block_id >= 999 ORDER BY id ASC LIMIT 1;"

    def test_probe_tables_cover_the_issue_queries(self) -> None:
        tables = {t for t, _ in rpc.ROLLBACK_PROBE_TABLES}
        assert {"tx", "tx_cbor", "datum", "tx_metadata"} <= tables
