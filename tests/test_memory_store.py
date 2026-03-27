from __future__ import annotations

import json

from memory.tidb_store import TiDBStore, from_config


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


class FakeConn:
    def __init__(self):
        self.open = True
        self.closed = False
        self.executed = []
        self.fetchone_queue = []
        self.fetchall_queue = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def _store():
    cfg = {"memory": {"host": "h", "port": 4000, "user": "u", "database": "db"}}
    return TiDBStore(cfg, FakeEmbedder())


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
    store.create_session("s1", "nginx", "host1", "model")
    store.update_profile("s1", baseline_rps=1.2, status="done")
    conn.fetchone_queue.append({"session_id": "s1"})
    assert store.get_profile("s1") == {"session_id": "s1"}

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

    cid = store.save_context("s1", "metric", "src", "content", "summary")
    assert isinstance(cid, str)
    conn.fetchall_queue.append([{"type": "telemetry"}])
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

    conn.fetchall_queue.append(
        [
            {"content": json.dumps({"input_tokens": 1}), "created_at": "now", "session_id": "s1"},
            {"content": "bad", "created_at": "later", "session_id": "s2"},
        ]
    )
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

    store.save_fact(
        "s1",
        "fix",
        "reset_timedout_connection",
        "reason",
        before_rps="0",
        after_rps="0.0",
        impact_pct="N/A (reset)",
    )

    params = conn.executed[-1][1]
    assert params[6] == 0.0
    assert params[7] == 0.0
    assert params[8] is None
