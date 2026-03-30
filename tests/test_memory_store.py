from __future__ import annotations

import json

import pytest

from memory.tidb_store import TiDBStore, _coerce_optional_float, from_config

# ── Test doubles ──────────────────────────────────────────────────────────────


class FakeEmbedder:
    def embed(self, text: str):
        return [0.1, len(text)]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self.conn.executed.append((query, params))

    def fetchone(self):
        return self.conn.fetchone_queue.pop(0)

    def fetchall(self):
        return self.conn.fetchall_queue.pop(0)

    @property
    def rowcount(self):
        return self.conn._rowcount


class FakeConn:
    def __init__(self):
        self.open = True
        self.closed = False
        self.executed = []
        self.fetchone_queue = []
        self.fetchall_queue = []
        self._rowcount = 5

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def _store():
    cfg = {"memory": {"host": "h", "port": 4000, "user": "u", "database": "db"}}
    return TiDBStore(cfg, FakeEmbedder())


def _setup(monkeypatch):
    """Create a connected store with a fresh FakeConn."""
    conn = FakeConn()
    monkeypatch.setattr("pymysql.connect", lambda **kwargs: conn)
    store = _store()
    store.connect()
    return store, conn


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


def test_connect_and_disconnect(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.disconnect()
    assert conn.closed is True
    assert store._conn is None


def test_disconnect_noop_when_not_connected():
    store = _store()
    store.disconnect()  # should not raise


def test_cursor_reconnects_when_closed(monkeypatch):
    conn = FakeConn()
    conn.open = False
    monkeypatch.setattr("pymysql.connect", lambda **kwargs: conn)
    store = _store()
    store._conn = conn
    cur = store._cursor()
    assert isinstance(cur, FakeCursor)


def test_from_config():
    result = from_config(
        {"memory": {"host": "h", "port": 1, "user": "u", "database": "d"}}, FakeEmbedder()
    )
    assert isinstance(result, TiDBStore)


# ── System registry ───────────────────────────────────────────────────────────


def test_get_or_create_system_finds_existing(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"id": "sys-existing"})
    sid = store._get_or_create_system("host1", "nginx")
    assert sid == "sys-existing"
    queries = [q for q, _ in conn.executed]
    assert len(queries) == 1
    assert "SELECT id FROM systems" in queries[0]


def test_get_or_create_system_creates_new(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    sid = store._get_or_create_system("host1", "nginx")
    assert isinstance(sid, str) and len(sid) == 36
    queries = [q for q, _ in conn.executed]
    assert len(queries) == 2
    assert "INSERT INTO systems" in queries[1]


def test_get_system(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"id": "s1", "host": "h1"})
    row = store.get_system("s1")
    assert row == {"id": "s1", "host": "h1"}


def test_get_system_returns_none_when_missing(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    assert store.get_system("missing") is None


def test_get_system_by_host(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"id": "s1", "host": "h1", "service": "nginx"})
    row = store.get_system_by_host("h1", "nginx")
    assert row["service"] == "nginx"


def test_update_system_filters_to_system_fields(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.update_system("sys-1", cpu_cores=8, ram_gb=16, unknown_field="x")
    query, params = conn.executed[-1]
    assert "cpu_cores" in query
    assert "ram_gb" in query
    assert "unknown_field" not in query


def test_update_system_json_encodes_tuning_state(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.update_system("sys-1", tuning_state={"worker_connections": "65536"})
    _, params = conn.executed[-1]
    assert isinstance(params[0], str)
    assert json.loads(params[0]) == {"worker_connections": "65536"}


def test_update_system_noop_when_no_valid_fields(monkeypatch):
    store, conn = _setup(monkeypatch)
    initial = len(conn.executed)
    store.update_system("sys-1", not_a_field="x")
    assert len(conn.executed) == initial


# ── _system_id_for_session ────────────────────────────────────────────────────


def test_system_id_for_session_cache_hit(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["sess-1"] = "sys-1"
    result = store._system_id_for_session("sess-1")
    assert result == "sys-1"
    assert len(conn.executed) == 0


def test_system_id_for_session_cache_miss_populates_cache(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"system_id": "sys-2"})
    result = store._system_id_for_session("sess-2")
    assert result == "sys-2"
    assert store._system_id_cache["sess-2"] == "sys-2"


def test_system_id_for_session_not_found(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    result = store._system_id_for_session("missing")
    assert result is None
    assert "missing" not in store._system_id_cache


# ── Sessions ──────────────────────────────────────────────────────────────────


def test_create_session_returns_system_id_for_existing_system(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"id": "sys-99"})
    sys_id = store.create_session("sess-1", "nginx", "host1", "gpt-4")
    assert sys_id == "sys-99"
    assert store._system_id_cache["sess-1"] == "sys-99"


def test_create_session_creates_system_when_missing(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    sys_id = store.create_session("sess-1", "nginx", "host1", "gpt-4")
    assert isinstance(sys_id, str)
    queries = [q for q, _ in conn.executed]
    assert any("INSERT INTO systems" in q for q in queries)
    assert any("INSERT INTO sessions" in q for q in queries)


def test_complete_session_calculates_rps_delta(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.complete_session(
        "sess-1", total_tokens=1000, fixes_applied=3, rps_start=100.0, rps_end=150.0
    )
    _, params = conn.executed[-1]
    # params: (total_tokens, fixes_applied, rps_start, rps_end, delta, session_id)
    assert params[4] == pytest.approx(50.0)
    assert params[0] == 1000
    assert params[1] == 3


def test_complete_session_without_rps_delta_is_none(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.complete_session("sess-1")
    _, params = conn.executed[-1]
    assert params[4] is None


def test_complete_session_zero_rps_start_skips_delta(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.complete_session("sess-1", rps_start=0, rps_end=100.0)
    _, params = conn.executed[-1]
    assert params[4] is None


def test_get_session(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"id": "sess-1", "status": "running"})
    row = store.get_session("sess-1")
    assert row["status"] == "running"


def test_session_exists_true(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"cnt": 1})
    assert store.session_exists("sess-1") is True


def test_session_exists_false(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"cnt": 0})
    assert store.session_exists("sess-1") is False


def test_get_latest_session_for_host(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"session_id": "sess-99"})
    result = store.get_latest_session_for_host("host1")
    assert result == "sess-99"


def test_get_latest_session_for_host_not_found(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    assert store.get_latest_session_for_host("host1") is None


def test_get_latest_session_excludes_given_session(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"session_id": "sess-98"})
    store.get_latest_session_for_host("host1", exclude_session_id="sess-99")
    query, params = conn.executed[-1]
    assert "AND s.id <> %s" in query
    assert "sess-99" in params


# ── Profile (backward-compatible facade) ──────────────────────────────────────


def test_get_profile(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"session_id": "s1", "host": "h1"})
    profile = store.get_profile("s1")
    assert profile["host"] == "h1"


def test_update_profile_ignores_unknown_columns(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr("pymysql.connect", lambda **kwargs: conn)
    store = _store()
    store.connect()
    store._system_id_cache["s1"] = "sys-1"
    store.update_profile("s1", baseline_rps=1.2, baseline_p99=9.9, status="completed")
    query, params = conn.executed[-1]
    assert "rps_start = %s" in query
    assert "status = %s" in query
    assert "baseline_p99" not in query
    assert params[-1] == "s1"
    assert 1.2 in params
    assert "completed" in params


def test_update_profile_early_return_when_no_system(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    initial = len(conn.executed)
    store.update_profile("sess-missing", cpu_cores=8)
    # Only the system_id lookup, no UPDATE queries
    assert len(conn.executed) == initial + 1


def test_update_profile_session_only_fields(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["s1"] = "sys-1"
    store.update_profile("s1", status="completed", llm_profile="gpt-4")
    query, params = conn.executed[-1]
    assert "status = %s" in query
    assert "llm_profile = %s" in query


# ── Knowledge ─────────────────────────────────────────────────────────────────


def test_save_fact_basic(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["s1"] = "sys-1"
    conn.fetchone_queue.append({"service_type": "nginx", "service": "nginx"})
    kid = store.save_fact("s1", "finding", "worker_connections", "too low")
    assert isinstance(kid, str) and len(kid) == 36
    queries = [q for q, _ in conn.executed]
    assert any("INSERT INTO knowledge" in q for q in queries)


def test_save_fact_reverted_maps_to_deprecated(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["s1"] = "sys-1"
    conn.fetchone_queue.append({"service_type": None, "service": "nginx"})
    store.save_fact("s1", "finding", "param", "reason", status="reverted")
    insert = next(e for e in conn.executed if "INSERT INTO knowledge" in e[0])
    # k_status is at index 12: (kid, discovered_by, system_id, service_type, scope, type,
    #                            parameter, condition, before_value, after_value,
    #                            impact_pct, confidence, k_status, reasoning, embedding)
    assert insert[1][12] == "deprecated"


def test_save_fact_applied_status_maps_to_active(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    store.save_fact("s1", "finding", "param", "reason", status="applied")
    insert = next(e for e in conn.executed if "INSERT INTO knowledge" in e[0])
    assert insert[1][12] == "active"


def test_save_fact_fix_type_creates_validation(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["s1"] = "sys-1"
    conn.fetchone_queue.append({"service_type": "nginx", "service": "nginx"})
    conn.fetchone_queue.append(None)
    store.save_fact("s1", "fix", "worker_connections", "raise limit",
                    before_rps=100.0, after_rps=150.0)
    queries = [q for q, _ in conn.executed]
    assert any("INSERT INTO validations" in q for q in queries)
    assert any("UPDATE knowledge" in q for q in queries)


def test_save_fact_fix_confirmed_when_applied(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["s1"] = "sys-1"
    conn.fetchone_queue.append({"service_type": "nginx", "service": "nginx"})
    conn.fetchone_queue.append(None)
    store.save_fact("s1", "fix", "param", "reason", status="applied")
    insert = next(e for e in conn.executed if "INSERT INTO validations" in e[0])
    assert insert[1][4] == "confirmed"  # outcome


def test_save_fact_fix_contradicted_when_reverted(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["s1"] = "sys-1"
    conn.fetchone_queue.append({"service_type": "nginx", "service": "nginx"})
    conn.fetchone_queue.append(None)
    store.save_fact("s1", "fix", "param", "reason", status="reverted")
    insert = next(e for e in conn.executed if "INSERT INTO validations" in e[0])
    assert insert[1][4] == "contradicted"


def test_save_fact_non_fix_type_skips_validation(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["s1"] = "sys-1"
    conn.fetchone_queue.append({"service_type": None, "service": "nginx"})
    store.save_fact("s1", "finding", "param", "reason")
    queries = [q for q, _ in conn.executed]
    assert not any("INSERT INTO validations" in q for q in queries)


def test_save_fact_no_system_id_skips_validation(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    store.save_fact("s1", "fix", "param", "reason")
    queries = [q for q, _ in conn.executed]
    assert not any("INSERT INTO validations" in q for q in queries)


def test_save_fact_scope_and_confidence_stored(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    store.save_fact("s1", "knowledge", "param", "reason", scope="universal", confidence=0.9)
    insert = next(e for e in conn.executed if "INSERT INTO knowledge" in e[0])
    params = insert[1]
    assert params[4] == "universal"   # scope at index 4
    assert params[11] == 0.9          # confidence at index 11


def test_save_fact_coerces_impact_pct(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr("pymysql.connect", lambda **kwargs: conn)
    store = _store()
    store.connect()
    conn.fetchone_queue.append(None)
    conn.fetchone_queue.append(None)
    store.save_fact("s1", "fix", "param", "reason", impact_pct="N/A (reset)")
    insert = next(e for e in conn.executed if "INSERT INTO knowledge" in e[0])
    assert insert[1][10] is None  # impact_pct coerced to None


def test_save_fact_fix_reuses_existing_knowledge_row(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["s1"] = "sys-1"
    conn.fetchone_queue.append({"service_type": "nginx", "service": "nginx"})
    conn.fetchone_queue.append({"id": "kid-existing"})

    kid = store.save_fact(
        "s1",
        "fix",
        "worker_connections",
        "raise limit",
        before_value="512",
        after_value="65536",
        before_rps=100.0,
        after_rps=150.0,
        impact_pct=50.0,
    )

    assert kid == "kid-existing"
    queries = [q for q, _ in conn.executed]
    assert not any("INSERT INTO knowledge" in q for q in queries)
    assert any("UPDATE knowledge" in q for q in queries)
    assert any("INSERT INTO validations" in q for q in queries)


def test_get_facts_filtered(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"type": "fix"}])
    result = store.get_facts("s1", "fix")
    assert result == [{"type": "fix"}]
    query, _ = conn.executed[-1]
    assert "type = %s" in query


def test_get_facts_all(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"type": "fix"}, {"type": "finding"}])
    result = store.get_facts("s1")
    assert len(result) == 2


def test_get_all_fixes_for_host(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"parameter": "p"}])
    result = store.get_all_fixes_for_host("host1")
    assert result[0]["parameter"] == "p"


def test_get_knowledge_for_service(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"parameter": "worker_connections", "confidence": 0.8}])
    results = store.get_knowledge_for_service("nginx", min_confidence=0.5)
    assert results[0]["parameter"] == "worker_connections"
    query, params = conn.executed[-1]
    assert "service_type = %s" in query
    assert "nginx" in params
    assert 0.5 in params


def test_promote_knowledge(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.promote_knowledge("kid-1", "service_type")
    query, params = conn.executed[-1]
    assert "UPDATE knowledge SET scope = %s" in query
    assert params == ("service_type", "kid-1")


def test_supersede_knowledge(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.supersede_knowledge("old-id", "new-id")
    query, params = conn.executed[-1]
    assert "status = 'superseded'" in query
    assert params == ("new-id", "old-id")


# ── Validations ───────────────────────────────────────────────────────────────


def test_save_validation_inserts_and_updates_confidence(monkeypatch):
    store, conn = _setup(monkeypatch)
    vid = store.save_validation(
        "kid-1", "sess-1", "sys-1", "confirmed",
        before_rps=100.0, after_rps=150.0, impact_pct=50.0
    )
    assert isinstance(vid, str) and len(vid) == 36
    queries = [q for q, _ in conn.executed]
    assert any("INSERT INTO validations" in q for q in queries)
    assert any("UPDATE knowledge" in q for q in queries)


def test_save_validation_coerces_floats(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.save_validation("kid-1", "sess-1", "sys-1", "confirmed",
                          before_rps="bad", after_rps=None, impact_pct="N/A")
    insert = next(e for e in conn.executed if "INSERT INTO validations" in e[0])
    params = insert[1]
    assert params[5] is None   # before_rps
    assert params[6] is None   # after_rps
    assert params[7] is None   # impact_pct


def test_update_knowledge_confidence_confirmed(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._update_knowledge_confidence("kid-1", "confirmed")
    _, params = conn.executed[-1]
    assert params[0] == pytest.approx(0.1)


def test_update_knowledge_confidence_contradicted(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._update_knowledge_confidence("kid-1", "contradicted")
    _, params = conn.executed[-1]
    assert params[0] == pytest.approx(-0.15)


def test_update_knowledge_confidence_partial(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._update_knowledge_confidence("kid-1", "partial")
    _, params = conn.executed[-1]
    assert params[0] == pytest.approx(0.03)


def test_update_knowledge_confidence_unknown_outcome(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._update_knowledge_confidence("kid-1", "unknown_xyz")
    _, params = conn.executed[-1]
    assert params[0] == 0


def test_get_validations(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"id": "v1", "outcome": "confirmed"}])
    results = store.get_validations("kid-1")
    assert results[0]["outcome"] == "confirmed"
    query, params = conn.executed[-1]
    assert "knowledge_id = %s" in query


# ── Benchmarks ────────────────────────────────────────────────────────────────


def test_save_benchmark_returns_id(monkeypatch):
    store, conn = _setup(monkeypatch)
    store._system_id_cache["sess-1"] = "sys-1"
    bid = store.save_benchmark("sess-1", 1, "baseline", "small", rps=1000.0)
    assert isinstance(bid, str) and len(bid) == 36
    queries = [q for q, _ in conn.executed]
    assert any("INSERT INTO benchmarks" in q for q in queries)


def test_save_benchmark_coerces_floats(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    store.save_benchmark("sess-1", 1, "baseline", "small", rps="bad", latency_avg_ms="N/A")
    insert = next(e for e in conn.executed if "INSERT INTO benchmarks" in e[0])
    params = insert[1]
    assert params[6] is None   # rps
    assert params[7] is None   # latency_avg_ms


def test_get_benchmarks_no_filters(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"id": "b1", "rps": 1000}])
    results = store.get_benchmarks()
    assert len(results) == 1
    query, _ = conn.executed[-1]
    assert "SELECT * FROM benchmarks WHERE 1=1" in query


def test_get_benchmarks_with_all_filters(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"id": "b1"}])
    store.get_benchmarks(session_id="s1", system_id="sys-1", phase="baseline", payload_size="small")
    query, params = conn.executed[-1]
    assert "session_id = %s" in query
    assert "system_id = %s" in query
    assert "phase = %s" in query
    assert "payload_size = %s" in query
    assert "s1" in params
    assert "baseline" in params
    assert "small" in params


def test_get_benchmark_comparison_with_baseline_and_final(monkeypatch):
    store, conn = _setup(monkeypatch)
    for _ in range(3):
        conn.fetchall_queue.append([
            {"phase": "baseline", "rps": 100.0, "latency_p99_ms": 10.0,
             "cpu_pct": 50.0, "mem_pct": 40.0},
            {"phase": "final", "rps": 150.0, "latency_p99_ms": 8.0,
             "cpu_pct": 45.0, "mem_pct": 38.0},
        ])
    result = store.get_benchmark_comparison("sess-1")
    assert result["small"]["delta_pct"] == pytest.approx(50.0)
    assert result["small"]["baseline_rps"] == 100.0
    assert result["small"]["final_rps"] == 150.0
    assert result["small"]["baseline_p99"] == 10.0
    assert "medium" in result
    assert "large" in result


def test_get_benchmark_comparison_zero_baseline_no_divide(monkeypatch):
    store, conn = _setup(monkeypatch)
    for _ in range(3):
        conn.fetchall_queue.append([])
    result = store.get_benchmark_comparison("sess-1")
    assert result["small"]["delta_pct"] == 0
    assert result["small"]["baseline_rps"] == 0


def test_get_performance_trend(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"rps": 1000, "phase": "baseline"}])
    results = store.get_performance_trend("sys-1", "small")
    assert results[0]["rps"] == 1000
    query, params = conn.executed[-1]
    assert "sys-1" in params
    assert "small" in params


# ── Context ───────────────────────────────────────────────────────────────────


def test_save_context_with_iteration_num(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    cid = store.save_context("s1", "metric", "src", "content", "summary", iteration_num=3)
    assert isinstance(cid, str)
    insert = next(e for e in conn.executed if "INSERT INTO context" in e[0])
    # params: (cid, session_id, system_id, storage_type, source, content, summary, iteration_num)
    assert insert[1][7] == 3


def test_save_context_telemetry_type_mapped_and_prefixed(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    store.save_context("s1", "telemetry", "baseline:pre", "data")
    insert = next(e for e in conn.executed if "INSERT INTO context" in e[0])
    params = insert[1]
    assert params[3] == "metric"           # storage_type maps to metric
    assert params[4].startswith("telemetry:")  # source is prefixed


def test_save_context_native_type_no_prefix(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    store.save_context("s1", "metric", "cpu_usage", "data")
    insert = next(e for e in conn.executed if "INSERT INTO context" in e[0])
    params = insert[1]
    assert params[3] == "metric"
    assert not params[4].startswith("metric:")


def test_save_context_rca_type_prefixed(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append(None)
    store.save_context("s1", "rca", "finding_1", "some rca content")
    insert = next(e for e in conn.executed if "INSERT INTO context" in e[0])
    params = insert[1]
    assert params[3] == "command_output"   # rca maps to command_output
    assert params[4].startswith("rca:")


def test_cleanup_context_returns_rowcount(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn._rowcount = 7
    deleted = store.cleanup_context("sess-1", keep_last_n=10)
    assert deleted == 7
    query, params = conn.executed[-1]
    assert "DELETE FROM context" in query
    assert params[2] == 10


def test_get_contexts_with_recent_iterations(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([])
    store.get_contexts("s1", recent_iterations=2)
    query, params = conn.executed[-1]
    assert "iteration_num" in query
    assert 2 in params


def test_get_contexts_telemetry_strips_prefix(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"type": "metric", "source": "telemetry:baseline:pre"}])
    results = store.get_contexts("s1", "telemetry", "baseline:", 1)
    assert results[0]["type"] == "telemetry"
    assert results[0]["source"] == "baseline:pre"


# ── Hypothesis queue ──────────────────────────────────────────────────────────


def test_populate_queue_with_source_and_knowledge_ref(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"cnt": 0})
    store.populate_queue("s1", [
        {"name": "h1", "priority": 1, "source": "knowledge", "knowledge_ref": "kid-1"},
        {"name": "h2", "priority": 2},
    ])
    inserts = [e for e in conn.executed if "INSERT INTO hypothesis_queue" in e[0]]
    assert len(inserts) == 2
    assert "knowledge" in inserts[0][1]
    assert "kid-1" in inserts[0][1]
    assert "llm" in inserts[1][1]
    assert inserts[1][1][-1] is None   # knowledge_ref defaults to None


def test_populate_queue_skips_when_already_populated(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"cnt": 3})
    store.populate_queue("s1", [{"name": "h1", "priority": 1}])
    queries = [q for q, _ in conn.executed]
    assert not any("INSERT INTO hypothesis_queue" in q for q in queries)


def test_next_hypothesis(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"name": "h1", "status": "pending"})
    result = store.next_hypothesis("s1")
    assert result["name"] == "h1"


def test_mark_hypothesis(monkeypatch):
    store, conn = _setup(monkeypatch)
    store.mark_hypothesis("s1", "h1", "done", "ok")
    query, params = conn.executed[-1]
    assert "UPDATE hypothesis_queue" in query
    assert "done" in params
    assert "ok" in params


def test_pending_count(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchone_queue.append({"cnt": 4})
    assert store.pending_count("s1") == 4


def test_get_queue(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"name": "h1"}, {"name": "h2"}])
    result = store.get_queue("s1")
    assert len(result) == 2


# ── Token history ─────────────────────────────────────────────────────────────


def test_get_token_history(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([
        {"session_id": "s1", "total_tokens": 5000},
        {"session_id": "s2", "total_tokens": 3000},
    ])
    hist = store.get_token_history()
    assert hist[0]["session_id"] == "s1"
    query, _ = conn.executed[-1]
    assert "FROM sessions" in query
    assert "status = 'completed'" in query


# ── Semantic search ───────────────────────────────────────────────────────────


def test_semantic_search_with_session(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"parameter": "p", "reasoning": "r"}])
    results = store.semantic_search("query", "s1", top_k=3)
    assert results[0]["parameter"] == "p"
    query, _ = conn.executed[-1]
    assert "discovered_by = %s" in query


def test_semantic_search_without_session(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([{"parameter": "k"}])
    results = store.semantic_search("query", None, top_k=2)
    assert results[0]["parameter"] == "k"
    query, _ = conn.executed[-1]
    assert "discovered_by" not in query


# ── Knowledge promotion pipeline ─────────────────────────────────────────────


def test_run_knowledge_promotion_promotes_candidates(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([
        {"id": "kid-1", "parameter": "worker_connections", "unique_systems": 3},
        {"id": "kid-2", "parameter": "open_file_cache", "unique_systems": 4},
    ])
    promoted = store.run_knowledge_promotion(min_validations=3)
    assert "kid-1" in promoted
    assert "kid-2" in promoted
    update_queries = [e for e in conn.executed if "UPDATE knowledge SET scope" in e[0]]
    assert len(update_queries) == 2


def test_run_knowledge_promotion_returns_empty_when_no_candidates(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([])
    promoted = store.run_knowledge_promotion()
    assert promoted == []


def test_run_knowledge_promotion_min_validations_passed(monkeypatch):
    store, conn = _setup(monkeypatch)
    conn.fetchall_queue.append([])
    store.run_knowledge_promotion(min_validations=5)
    query, params = conn.executed[-1]
    assert "unique_systems >= %s" in query
    assert params[0] == 5


# ── Backward-compat: legacy test suite (fixed for new schema) ─────────────────


def test_tidb_store_methods(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr("pymysql.connect", lambda **kwargs: conn)
    store = _store()
    store.connect()
    store.disconnect()
    assert conn.closed is True

    conn = FakeConn()
    monkeypatch.setattr("pymysql.connect", lambda **kwargs: conn)
    store = _store()
    conn.fetchone_queue.append(None)   # _get_or_create_system: no existing system
    store.create_session("s1", "nginx", "host1", "model")
    store.update_profile("s1", baseline_rps=1.2, status="done")
    conn.fetchone_queue.append({"session_id": "s1"})
    assert store.get_profile("s1") == {"session_id": "s1"}

    conn.fetchone_queue.append({"service_type": None, "service": "nginx"})  # get_system in save_fact
    conn.fetchone_queue.append(None)  # _find_existing_fix_fact
    fid = store.save_fact(
        "s1", "fix", "param", "reason", before_value="a", after_value="b", impact_pct=1.5
    )
    assert isinstance(fid, str)
    conn.fetchall_queue.append([{"type": "fix"}])
    assert store.get_facts("s1", "fix") == [{"type": "fix"}]
    conn.fetchall_queue.append([{"type": "fix"}, {"type": "finding"}])
    assert len(store.get_facts("s1")) == 2
    conn.fetchall_queue.append([{"parameter": "p"}])
    assert store.get_all_fixes_for_host("host1")[0]["parameter"] == "p"

    # _system_id_for_session("s1") hits cache from create_session — no fetchone needed
    cid = store.save_context("s1", "metric", "src", "content", "summary")
    assert isinstance(cid, str)
    insert = next(e for e in conn.executed if "INSERT INTO context" in e[0])
    # params: (cid, session_id, system_id, storage_type, source, content, summary, iteration_num)
    assert insert[1][3] == "metric"
    assert not str(insert[1][4]).startswith("metric:")

    store.save_context("s1", "telemetry", "baseline:pre", "content", "summary")
    insert2 = [e for e in conn.executed if "INSERT INTO context" in e[0]][-1]
    assert insert2[1][3] == "metric"
    assert str(insert2[1][4]).startswith("telemetry:")

    conn.fetchall_queue.append([{"type": "metric", "source": "telemetry:baseline:pre"}])
    assert store.get_contexts("s1", "telemetry", "baseline:", 1)[0]["type"] == "telemetry"
    conn.fetchall_queue.append([{"parameter": "p", "reasoning": "r"}])
    assert store.semantic_search("query", "s1", 3)[0]["parameter"] == "p"
    conn.fetchall_queue.append([{"parameter": "k"}])
    assert store.semantic_search("query", None, 2)[0]["parameter"] == "k"

    conn.fetchone_queue.append({"cnt": 0})
    store.populate_queue("s1", [{"name": "h1", "priority": 1}])
    conn.fetchone_queue.append({"name": "h1"})
    assert store.next_hypothesis("s1") == {"name": "h1"}
    store.mark_hypothesis("s1", "h1", "done", "ok")
    conn.fetchone_queue.append({"cnt": 2})
    assert store.pending_count("s1") == 2
    conn.fetchall_queue.append([{"name": "h1"}])
    assert store.get_queue("s1") == [{"name": "h1"}]

    conn.fetchall_queue.append([
        {"session_id": "s1", "total_tokens": 1000},
        {"session_id": "s2", "total_tokens": 500},
    ])
    hist = store.get_token_history()
    assert hist[0]["session_id"] == "s1"

    conn.fetchone_queue.append({"cnt": 1})
    assert store.session_exists("s1") is True
    assert isinstance(
        from_config(
            {"memory": {"host": "h", "port": 1, "user": "u", "database": "d"}}, FakeEmbedder()
        ),
        TiDBStore,
    )


def test_tidb_store_reconnects_when_connection_closed(monkeypatch):
    conn = FakeConn()
    conn.open = False
    monkeypatch.setattr("pymysql.connect", lambda **kwargs: conn)
    store = _store()
    store._conn = conn
    cur = store._cursor()
    assert isinstance(cur, FakeCursor)


def test_save_fact_coerces_non_numeric_float_fields(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr("pymysql.connect", lambda **kwargs: conn)
    store = _store()
    store.connect()
    conn.fetchone_queue.append(None)   # _system_id_for_session → None (no system)
    store.save_fact(
        "s1",
        "fix",
        "reset_timedout_connection",
        "reason",
        before_rps="0",
        after_rps="0.0",
        impact_pct="N/A (reset)",
    )
    # With system_id=None, no validation is created; last exec is the knowledge INSERT
    insert = next(e for e in conn.executed if "INSERT INTO knowledge" in e[0])
    params = insert[1]
    # impact_pct is at index 10
    assert params[10] is None   # "N/A (reset)" → None
