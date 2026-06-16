"""Tests for node-rts-monitor.py - the cardano-node Prometheus/RTS scraper.

Pure parsing/selection/extraction is unit-tested directly; the HTTP fetch is
exercised via a monkeypatched urlopen (no network); insertion is tested by
feeding raw Prometheus text to `record()` against a tmp SQLite DB.

Metric names vary across node versions / tracing backends, so the collector
matches a substring allowlist rather than exact names - the tests use a mix of
new-tracing (`cardano_node_metrics_RTS_*`) and old-EKG (`rts_gc_*`) shapes.
"""

import importlib.util
import sqlite3
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rts = _load("node_rts_monitor", "node-rts-monitor.py")

SAMPLE = """\
# HELP cardano_node_metrics_RTS_gcMajorNum_int Major GCs
# TYPE cardano_node_metrics_RTS_gcMajorNum_int gauge
cardano_node_metrics_RTS_gcMajorNum_int 42
cardano_node_metrics_RTS_gcMinorNum_int 1000
cardano_node_metrics_RTS_gcLiveBytes_int 4.2e9
cardano_node_metrics_RTS_gcHeapBytes_int 8589934592
rts_gc_allocated_bytes 1234567
cardano_node_metrics_slotNum_int 1234567
cardano_node_metrics_txsInMempool_int 17
labeled_metric{quantile="0.5",foo="bar"} 3.14
not_a_metric_line_without_value
nan_metric NaN
inf_metric +Inf
metric_with_timestamp 99 1700000000000
"""


# --- parse_prometheus_text -------------------------------------------------


class TestParse:
    def test_parses_plain_metrics(self):
        m = rts.parse_prometheus_text(SAMPLE)
        assert m["cardano_node_metrics_RTS_gcMajorNum_int"] == 42.0
        assert m["cardano_node_metrics_RTS_gcMinorNum_int"] == 1000.0
        assert m["cardano_node_metrics_RTS_gcLiveBytes_int"] == 4.2e9
        assert m["rts_gc_allocated_bytes"] == 1234567.0

    def test_skips_comments_and_blank(self):
        m = rts.parse_prometheus_text(SAMPLE)
        assert not any(k.startswith("#") for k in m)

    def test_strips_labels(self):
        m = rts.parse_prometheus_text(SAMPLE)
        assert m["labeled_metric"] == 3.14  # labels dropped from the key

    def test_skips_unparseable_and_nonfinite(self):
        m = rts.parse_prometheus_text(SAMPLE)
        assert "not_a_metric_line_without_value" not in m
        assert "nan_metric" not in m
        assert "inf_metric" not in m

    def test_ignores_trailing_timestamp(self):
        m = rts.parse_prometheus_text(SAMPLE)
        assert m["metric_with_timestamp"] == 99.0

    def test_empty_text(self):
        assert rts.parse_prometheus_text("") == {}


# --- select_metrics --------------------------------------------------------


class TestSelect:
    def test_default_allowlist_matches_gc_and_mempool(self):
        m = rts.parse_prometheus_text(SAMPLE)
        sel = rts.select_metrics(m, list(rts.DEFAULT_INCLUDE))
        assert "cardano_node_metrics_RTS_gcMajorNum_int" in sel
        assert "cardano_node_metrics_RTS_gcLiveBytes_int" in sel
        assert "rts_gc_allocated_bytes" in sel
        assert "cardano_node_metrics_txsInMempool_int" in sel  # via "mempool" substring

    def test_excludes_unrelated(self):
        m = rts.parse_prometheus_text(SAMPLE)
        sel = rts.select_metrics(m, list(rts.DEFAULT_INCLUDE))
        assert "labeled_metric" not in sel

    def test_case_insensitive(self):
        sel = rts.select_metrics({"Cardano_RTS_GcMinorNum": 5.0}, ["gc"])
        assert sel == {"Cardano_RTS_GcMinorNum": 5.0}

    def test_custom_include(self):
        sel = rts.select_metrics({"a_density": 1.0, "b_gc": 2.0}, ["density"])
        assert sel == {"a_density": 1.0}


# --- extract_slot ----------------------------------------------------------


class TestExtractSlot:
    def test_finds_slotnum(self):
        m = rts.parse_prometheus_text(SAMPLE)
        assert rts.extract_slot(m) == 1234567

    def test_none_when_absent(self):
        assert rts.extract_slot({"cardano_node_metrics_RTS_gcMajorNum_int": 1.0}) is None


# --- fetch_metrics (monkeypatched urlopen, no network) ---------------------


class TestFetch:
    def test_success(self, monkeypatch):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"foo 1\n"

        monkeypatch.setattr(rts, "urlopen", lambda url, timeout: FakeResp())
        assert rts.fetch_metrics("http://x/metrics", 5) == "foo 1\n"

    def test_failure_returns_none(self, monkeypatch):
        def boom(url, timeout):
            raise OSError("connection refused")

        monkeypatch.setattr(rts, "urlopen", boom)
        assert rts.fetch_metrics("http://x/metrics", 5) is None  # must not raise


# --- schema + record -------------------------------------------------------


def _mon(tmp_path, **kw):
    defaults = dict(
        env="mainnet",
        node_ver="LSM-11.0.1",
        url="http://x/metrics",
        includes=list(rts.DEFAULT_INCLUDE),
        interval=1,
        timeout=5,
        emit_json=False,
        sqlite_db=str(tmp_path / "n.db"),
    )
    defaults.update(kw)
    return rts.NodeRtsMonitor(**defaults)


class TestSchemaAndRecord:
    def test_init_creates_table_in_wal(self, tmp_path):
        m = _mon(tmp_path)
        with sqlite3.connect(m.db_file) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert "rts_metrics" in tables
        assert mode.lower() == "wal"

    def test_run_label(self, tmp_path):
        assert _mon(tmp_path).run_label == "cardano-node LSM-11.0.1 mainnet"

    def test_default_db_path(self, tmp_path):
        m = rts.NodeRtsMonitor(
            env="preprod",
            node_ver="v",
            url="u",
            includes=["gc"],
            interval=1,
            timeout=1,
            emit_json=False,
            sqlite_db=None,
        )
        assert m.db_file.endswith("data/cardano-node/preprod.db")

    def test_record_inserts_selected_with_slot_and_label(self, tmp_path):
        m = _mon(tmp_path)
        result = m.record(SAMPLE)
        assert result is not None
        _ts, slot, _selected = result
        assert slot == 1234567  # slot pulled from slotNum even though it's not in the allowlist
        with sqlite3.connect(m.db_file) as conn:
            rows = conn.execute("SELECT metric, value, slot_no, version FROM rts_metrics ORDER BY metric").fetchall()
        names = {r[0] for r in rows}
        assert "cardano_node_metrics_RTS_gcMajorNum_int" in names
        assert "cardano_node_metrics_txsInMempool_int" in names
        assert "labeled_metric" not in names  # not in allowlist
        assert all(r[2] == 1234567 for r in rows)
        assert all(r[3] == "cardano-node LSM-11.0.1 mainnet" for r in rows)

    def test_record_none_when_nothing_matches(self, tmp_path):
        m = _mon(tmp_path, includes=["this-matches-nothing"])
        assert m.record(SAMPLE) is None
        with sqlite3.connect(m.db_file) as conn:
            assert conn.execute("SELECT COUNT(*) FROM rts_metrics").fetchone()[0] == 0

    def test_report_existing_counts(self, tmp_path, capsys):
        m = _mon(tmp_path)
        m.report_existing()
        assert "no existing rts samples" in capsys.readouterr().out
        m.record(SAMPLE)
        m.report_existing()
        assert "already has" in capsys.readouterr().out


class TestBusyResilience:
    """A `database is locked` from a colliding writer must not kill the collector.

    Several node collectors write to the same per-env DB; under WAL only one may
    write at a time. connect_writer's busy timeout absorbs normal contention, but
    if a writer still loses the race the run loop must warn and drop the scrape
    rather than propagate OperationalError and end a multi-day run.
    """

    def test_run_survives_db_locked(self, tmp_path, monkeypatch, capsys):
        m = _mon(tmp_path)
        monkeypatch.setattr(rts, "fetch_metrics", lambda url, timeout: SAMPLE)

        def boom(_text):
            m.running = False  # one iteration, then exit the loop cleanly
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(m, "record", boom)
        m.run()  # must not raise
        assert "DB busy" in capsys.readouterr().err
