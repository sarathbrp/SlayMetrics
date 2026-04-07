from __future__ import annotations

import json
import sqlite3

import pytest

from memory.sqlite_store import SQLiteStore, _coerce_optional_float, from_config

# ── Test doubles ──────────────────────────────────────────────────────────────


class FakeEmbedder:
    def embed(self, text: str):
        return [0.1, len(text)]


def _store(tmp_path=None):
    db_path = str(tmp_path / "test.db") if tmp_path else ":memory:"
    cfg = {"memory": {"path": db_path}}
    return SQLiteStore(cfg, FakeEmbedder())


def _setup(tmp_path):
    """Create a connected store with an in-memory or tmp database."""
    store = _store(tmp_path)
    store.connect()
    return store


# ── Helper: coerce float ──────────────────────────────────────────────────────


def test_coerce_optional_float_none_and_empty():
    assert _coerce_optional_float(None) is None
    assert _coerce_optional_float("") is None


def test_coerce_optional_float_non_numeric_string():
    assert _coerce_optional_float("N/A") is None
    assert _coerce_optional_float("bad") is None


def test_coerce_optional_float_numeric_types():
    assert _coerce_optional_float(1) == 1.0
    assert _coerce_optional_float(3.14) == pytest.approx(3.14)
    assert _coerce_optional_float("3.14") == pytest.approx(3.14)
    assert _coerce_optional_float(True) == 1.0
    assert _coerce_optional_float("0") == 0.0


# ── Connection ────────────────────────────────────────────────────────────────


def test_connect_and_disconnect(tmp_path):
    store = _setup(tmp_path)
    assert store._conn is not None
    store.disconnect()
    assert store._conn is None


def test_disconnect_noop_when_not_connected():
    cfg = {"memory": {"path": ":memory:"}}
    store = SQLiteStore(cfg, FakeEmbedder())
    store.disconnect()  # should not raise


def test_from_config(tmp_path):
    result = from_config({"memory": {"path": str(tmp_path / "test.db")}}, FakeEmbedder())
    assert isinstance(result, SQLiteStore)


# ── System registry ───────────────────────────────────────────────────────────


def test_get_or_create_system_finds_existing(tmp_path):
    store = _setup(tmp_path)
    sid1 = store._get_or_create_system("host1", "nginx")
    sid2 = store._get_or_create_system("host1", "nginx")
    assert sid1 == sid2


def test_get_or_create_system_creates_new(tmp_path):
    store = _setup(tmp_path)
    sid = store._get_or_create_system("host1", "nginx")
    assert isinstance(sid, str) and len(sid) == 36


def test_get_system(tmp_path):
    store = _setup(tmp_path)
    sid = store._get_or_create_system("h1", "nginx")
    row = store.get_system(sid)
    assert row["host"] == "h1"


def test_get_system_returns_none_when_missing(tmp_path):
    store = _setup(tmp_path)
    assert store.get_system("missing") is None


def test_get_system_by_host(tmp_path):
    store = _setup(tmp_path)
    store._get_or_create_system("h1", "nginx")
    row = store.get_system_by_host("h1", "nginx")
    assert row["service"] == "nginx"


def test_update_system_filters_to_system_fields(tmp_path):
    store = _setup(tmp_path)
    sid = store._get_or_create_system("h1", "nginx")
    store.update_system(sid, cpu_cores=8, ram_gb=16, unknown_field="x")
    row = store.get_system(sid)
    assert row["cpu_cores"] == 8
    assert row["ram_gb"] == 16


def test_update_system_json_encodes_tuning_state(tmp_path):
    store = _setup(tmp_path)
    sid = store._get_or_create_system("h1", "nginx")
    store.update_system(sid, tuning_state={"worker_connections": "65536"})
    row = store.get_system(sid)
    assert json.loads(row["tuning_state"]) == {"worker_connections": "65536"}


def test_update_system_noop_when_no_valid_fields(tmp_path):
    store = _setup(tmp_path)
    sid = store._get_or_create_system("h1", "nginx")
    store.update_system(sid, not_a_field="x")  # should not raise


# ── Sessions ──────────────────────────────────────────────────────────────────


def test_create_session_returns_system_id(tmp_path):
    store = _setup(tmp_path)
    sys_id = store.create_session("sess-1", "nginx", "host1", "gpt-4")
    assert isinstance(sys_id, str) and len(sys_id) == 36
    assert store._system_id_cache["sess-1"] == sys_id


def test_complete_session_calculates_rps_delta(tmp_path):
    store = _setup(tmp_path)
    store.create_session("sess-1", "nginx", "host1", "gpt-4")
    store.complete_session("sess-1", total_tokens=1000, fixes_applied=3, rps_start=100.0, rps_end=150.0)
    session = store.get_session("sess-1")
    assert session["rps_delta_pct"] == pytest.approx(50.0)
    assert session["total_tokens"] == 1000


def test_complete_session_without_rps_delta_is_none(tmp_path):
    store = _setup(tmp_path)
    store.create_session("sess-1", "nginx", "host1", "gpt-4")
    store.complete_session("sess-1")
    session = store.get_session("sess-1")
    assert session["rps_delta_pct"] is None


def test_session_exists(tmp_path):
    store = _setup(tmp_path)
    assert store.session_exists("sess-1") is False
    store.create_session("sess-1", "nginx", "host1", "gpt-4")
    assert store.session_exists("sess-1") is True


def test_get_latest_session_for_host(tmp_path):
    store = _setup(tmp_path)
    store.create_session("sess-1", "nginx", "host1", "gpt-4")
    result = store.get_latest_session_for_host("host1")
    assert result == "sess-1"


def test_get_latest_session_for_host_not_found(tmp_path):
    store = _setup(tmp_path)
    assert store.get_latest_session_for_host("host1") is None


# ── Profile (backward-compatible facade) ──────────────────────────────────────


def test_get_profile(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    profile = store.get_profile("s1")
    assert profile["host"] == "h1"


def test_update_profile(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    store.update_profile("s1", baseline_rps=1.2, status="completed")
    profile = store.get_profile("s1")
    assert profile["status"] == "completed"


# ── Knowledge ─────────────────────────────────────────────────────────────────


def test_save_fact_basic(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    kid = store.save_fact("s1", "finding", "worker_connections", "too low")
    assert isinstance(kid, str) and len(kid) == 36


def test_save_fact_reverted_maps_to_deprecated(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    kid = store.save_fact("s1", "finding", "param", "reason", status="reverted")
    facts = store.get_facts("s1")
    assert any(f["status"] == "deprecated" for f in facts)


def test_save_fact_fix_type_creates_validation(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    kid = store.save_fact("s1", "fix", "worker_connections", "raise limit",
                          before_rps=100.0, after_rps=150.0)
    sys_id = store._system_id_for_session("s1")
    validations = store.get_validations(kid)
    assert len(validations) > 0


def test_save_fact_coerces_impact_pct(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    kid = store.save_fact("s1", "fix", "param", "reason", impact_pct="N/A (reset)")
    facts = store.get_facts("s1")
    matching = [f for f in facts if f["id"] == kid]
    assert matching[0]["impact_pct"] is None


def test_get_facts_filtered(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    store.save_fact("s1", "fix", "param1", "reason1")
    store.save_fact("s1", "finding", "param2", "reason2")
    fixes = store.get_facts("s1", "fix")
    assert all(f["type"] == "fix" for f in fixes)


def test_get_facts_all(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    store.save_fact("s1", "fix", "p1", "r1")
    store.save_fact("s1", "finding", "p2", "r2")
    result = store.get_facts("s1")
    assert len(result) == 2


def test_promote_knowledge(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    kid = store.save_fact("s1", "finding", "param", "reason")
    store.promote_knowledge(kid, "service_type")
    # Verify by querying
    cur = store._cursor()
    cur.execute("SELECT scope FROM knowledge WHERE id = ?", (kid,))
    row = cur.fetchone()
    assert row["scope"] == "service_type"


# ── Validations ───────────────────────────────────────────────────────────────


def test_save_validation(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    kid = store.save_fact("s1", "fix", "param", "reason")
    sys_id = store._system_id_for_session("s1")
    vid = store.save_validation(kid, "s1", sys_id, "confirmed",
                                before_rps=100.0, after_rps=150.0, impact_pct=50.0)
    assert isinstance(vid, str) and len(vid) == 36


# ── Benchmarks ────────────────────────────────────────────────────────────────


def test_save_benchmark_returns_id(tmp_path):
    store = _setup(tmp_path)
    store.create_session("sess-1", "nginx", "h1", "gpt-4")
    bid = store.save_benchmark("sess-1", 1, "baseline", "small", rps=1000.0)
    assert isinstance(bid, str) and len(bid) == 36


def test_get_benchmarks_no_filters(tmp_path):
    store = _setup(tmp_path)
    store.create_session("sess-1", "nginx", "h1", "gpt-4")
    store.save_benchmark("sess-1", 1, "baseline", "small", rps=1000.0)
    results = store.get_benchmarks()
    assert len(results) == 1


def test_get_benchmark_comparison(tmp_path):
    store = _setup(tmp_path)
    store.create_session("sess-1", "nginx", "h1", "gpt-4")
    store.save_benchmark("sess-1", 0, "baseline", "small", rps=100.0, latency_p99_ms=10.0)
    store.save_benchmark("sess-1", 1, "final", "small", rps=150.0, latency_p99_ms=8.0)
    store.save_benchmark("sess-1", 0, "baseline", "medium", rps=50.0)
    store.save_benchmark("sess-1", 1, "final", "medium", rps=60.0)
    store.save_benchmark("sess-1", 0, "baseline", "large", rps=10.0)
    store.save_benchmark("sess-1", 1, "final", "large", rps=12.0)
    result = store.get_benchmark_comparison("sess-1")
    assert result["small"]["delta_pct"] == pytest.approx(50.0)


# ── Context ───────────────────────────────────────────────────────────────────


def test_save_context(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    cid = store.save_context("s1", "metric", "src", "content", "summary", iteration_num=3)
    assert isinstance(cid, str)


def test_save_context_telemetry_type_mapped(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    store.save_context("s1", "telemetry", "baseline:pre", "data")
    # telemetry maps to "metric" storage type, source gets "telemetry:" prefix
    # get_contexts with type="telemetry" fetches metric rows, filters by telemetry: prefix
    results = store.get_contexts("s1", "telemetry")
    assert len(results) > 0
    assert results[0]["type"] == "telemetry"
    assert results[0]["source"] == "baseline:pre"


def test_cleanup_context(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    for i in range(20):
        store.save_context("s1", "metric", f"src_{i}", f"content_{i}")
    deleted = store.cleanup_context("s1", keep_last_n=10)
    assert deleted == 10


# ── Hypothesis queue ──────────────────────────────────────────────────────────


def test_populate_queue(tmp_path):
    store = _setup(tmp_path)
    store.populate_queue("s1", [
        {"name": "h1", "priority": 1, "source": "knowledge", "knowledge_ref": "kid-1"},
        {"name": "h2", "priority": 2},
    ])
    queue = store.get_queue("s1")
    assert len(queue) == 2


def test_populate_queue_skips_when_already_populated(tmp_path):
    store = _setup(tmp_path)
    store.populate_queue("s1", [{"name": "h1", "priority": 1}])
    store.populate_queue("s1", [{"name": "h2", "priority": 2}])
    queue = store.get_queue("s1")
    assert len(queue) == 1  # second populate was skipped


def test_next_hypothesis(tmp_path):
    store = _setup(tmp_path)
    store.populate_queue("s1", [{"name": "h1", "priority": 1}])
    result = store.next_hypothesis("s1")
    assert result["name"] == "h1"


def test_mark_hypothesis(tmp_path):
    store = _setup(tmp_path)
    store.populate_queue("s1", [{"name": "h1", "priority": 1}])
    store.mark_hypothesis("s1", "h1", "done", "ok")
    result = store.next_hypothesis("s1")
    assert result is None  # no more pending


def test_pending_count(tmp_path):
    store = _setup(tmp_path)
    store.populate_queue("s1", [{"name": "h1", "priority": 1}, {"name": "h2", "priority": 2}])
    assert store.pending_count("s1") == 2


# ── Token history ─────────────────────────────────────────────────────────────


def test_get_token_history(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    store.complete_session("s1", total_tokens=5000)
    hist = store.get_token_history()
    assert hist[0]["session_id"] == "s1"


# ── Semantic search ───────────────────────────────────────────────────────────


def test_semantic_search(tmp_path):
    store = _setup(tmp_path)
    store.create_session("s1", "nginx", "h1", "gpt-4")
    store.save_fact("s1", "knowledge", "worker_connections", "increase for throughput",
                    scope="universal")
    results = store.semantic_search("connections", "s1", top_k=3)
    assert len(results) > 0
    assert results[0]["parameter"] == "worker_connections"


# ── Knowledge promotion pipeline ─────────────────────────────────────────────


def test_run_knowledge_promotion_returns_empty_when_no_candidates(tmp_path):
    store = _setup(tmp_path)
    promoted = store.run_knowledge_promotion()
    assert promoted == []
