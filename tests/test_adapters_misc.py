from __future__ import annotations

from adapters import load_adapter
from adapters.base import BenchmarkResult
from adapters.postgres import PostgresAdapter, _parse_pgbench
from adapters.redis import RedisAdapter, _parse_redis_bench
from core import decision_engine
from memory.embeddings import LocalEmbeddings, from_config
from tools.ssh import LocalClient, SSHClient, SSHResult
from tools.ssh import from_config as ssh_from_config


class FakeSSH:
    def __init__(self, responses: dict[str, SSHResult] | None = None):
        self.responses = responses or {}
        self.commands: list[tuple[str, int | None]] = []

    def execute(self, command: str, timeout: int | None = None) -> SSHResult:
        self.commands.append((command, timeout))
        return self.responses.get(command, SSHResult("", "", 0))


class FakeMemory:
    def __init__(self):
        self.calls = []

    def populate_queue(self, session_id, hypotheses):
        self.calls.append(("populate", session_id, hypotheses))

    def next_hypothesis(self, session_id):
        self.calls.append(("next", session_id))
        return {"name": "x"}

    def mark_hypothesis(self, session_id, name, status, outcome):
        self.calls.append(("mark", session_id, name, status, outcome))

    def pending_count(self, session_id):
        self.calls.append(("pending", session_id))
        return 0


def test_load_adapter_constructs_named_adapter():
    cfg = {"service": {"name": "redis", "config_path": "/tmp/redis.conf"}}
    adapter = load_adapter(cfg, FakeSSH())
    assert isinstance(adapter, RedisAdapter)


def test_postgres_adapter_behaviors():
    ssh = FakeSSH(
        {
            "psql -U postgres -c 'SHOW ALL;' 2>/dev/null || cat /etc/postgresql.conf": SSHResult(
                "cfg", "", 0
            ),
            "sed -i 's/^#\\?max_connections\\s*=.*/max_connections = 200/' /etc/postgresql.conf": SSHResult(
                "", "", 0
            ),
            "pgbench -c10 -j2 -T60 postgres 2>&1": SSHResult(
                "tps = 123.4\nlatency average = 5.6 ms\n", "", 0
            ),
            'psql -U postgres -c "SELECT count(*) FROM pg_stat_activity;" 2>/dev/null': SSHResult(
                "42", "", 0
            ),
            "tail -10 /var/log/postgresql/postgresql.log": SSHResult("logline", "", 0),
            "systemctl reload postgresql.service": SSHResult("", "", 0),
        }
    )
    cfg = {
        "service": {
            "config_path": "/etc/postgresql.conf",
            "systemd_unit": "postgresql.service",
            "benchmark": {"args": "-c10 -j2 -T60"},
        }
    }
    adapter = PostgresAdapter(cfg, ssh)
    assert adapter.get_config() == {"raw": "cfg", "path": "/etc/postgresql.conf"}
    assert adapter.apply_config("max_connections", "200") is True
    bench = adapter.benchmark(60)
    assert bench.requests_per_sec == 123.4
    assert bench.latency_p50_ms == 5.6
    assert adapter.get_metrics()["pg_stat_activity"] == "42"
    adapter._cfg["log_path"] = "/var/log/postgresql/postgresql.log"
    assert adapter.get_logs(10) == "logline"
    assert adapter.reload() is True
    assert adapter.get_hypothesis_queue()[0]["name"] == "shared_buffers_tuned"


def test_parse_pgbench_handles_missing_values():
    bench = _parse_pgbench("no metrics", 30)
    assert bench == BenchmarkResult(0.0, 0.0, 0.0, 0.0, 30)


def test_redis_adapter_behaviors():
    ssh = FakeSSH(
        {
            "redis-cli CONFIG GETALL 2>/dev/null || cat /etc/redis.conf": SSHResult("cfg", "", 0),
            "redis-cli CONFIG SET maxmemory 1gb": SSHResult("", "boom", 1),
            "sed -i 's/^maxmemory.*/maxmemory 1gb/' /etc/redis.conf": SSHResult("", "", 0),
            "redis-benchmark -t get,set -n 100000 -q 2>&1": SSHResult(
                "SET: 1000.00 requests per second\nGET: 3000.00 requests per second", "", 0
            ),
            "redis-cli INFO stats 2>/dev/null": SSHResult("stats", "", 0),
            "tail -5 /var/log/redis/redis.log": SSHResult("rlog", "", 0),
            "systemctl reload redis.service": SSHResult("", "", 0),
        }
    )
    cfg = {
        "service": {
            "config_path": "/etc/redis.conf",
            "systemd_unit": "redis.service",
            "log_path": "/var/log/redis/redis.log",
        }
    }
    adapter = RedisAdapter(cfg, ssh)
    assert adapter.get_config()["raw"] == "cfg"
    assert adapter.apply_config("maxmemory", "1gb") is True
    assert adapter.benchmark(30).requests_per_sec == 2000.0
    assert adapter.get_metrics()["redis_info"] == "stats"
    assert adapter.get_logs(5) == "rlog"
    assert adapter.reload() is True
    assert adapter.get_hypothesis_queue()[0]["name"] == "cpu_governor_performance"


def test_parse_redis_bench_handles_missing_ops():
    assert _parse_redis_bench("n/a", 10).requests_per_sec == 0.0


def test_redis_adapter_live_config_set_success():
    ssh = FakeSSH(
        {
            "redis-cli CONFIG SET maxmemory 1gb": SSHResult("", "", 0),
        }
    )
    adapter = RedisAdapter({"service": {"config_path": "/etc/redis.conf"}}, ssh)
    assert adapter.apply_config("maxmemory", "1gb") is True


def test_decision_engine_delegates_to_memory():
    memory = FakeMemory()
    decision_engine.populate("s1", memory, [{"name": "x", "priority": 1}])
    assert decision_engine.next_hypothesis("s1", memory) == {"name": "x"}
    decision_engine.mark_done("s1", memory, "x", "good")
    decision_engine.mark_skipped("s1", memory, "y", "bad")
    assert decision_engine.is_exhausted("s1", memory) is True
    assert ("populate", "s1", [{"name": "x", "priority": 1}]) in memory.calls


def test_local_embeddings_and_factory():
    emb = LocalEmbeddings(dimensions=8)
    vec = emb.embed("hello hello world")
    assert len(vec) == 8
    assert round(sum(v * v for v in vec), 6) == 1.0
    assert isinstance(from_config({}), LocalEmbeddings)


def test_ssh_result_and_client_selection(monkeypatch):
    assert SSHResult("out", "", 0).ok is True
    assert str(SSHResult("out", "err", 1)) == "out\n[stderr]: err"
    assert str(SSHResult("", "err", 1)) == "err"
    assert isinstance(ssh_from_config({"target": {"host": "localhost"}}), LocalClient)
    remote = ssh_from_config(
        {
            "target": {
                "host": "1.2.3.4",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/id",
                "ssh_timeout": 9,
            }
        }
    )
    assert isinstance(remote, SSHClient)
    assert remote.timeout == 9

    class Result:
        stdout = "x"
        stderr = ""
        returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())
    assert LocalClient().execute("echo hi").stdout == "x"

    def boom(*args, **kwargs):
        import subprocess

        raise subprocess.TimeoutExpired("cmd", 1)

    monkeypatch.setattr("subprocess.run", boom)
    assert LocalClient().execute("sleep 2").exit_code == 124


def test_ssh_client_execute_and_disconnect():
    class FakeStdout:
        def __init__(self, data, exit_code):
            self._data = data
            self.channel = self
            self._exit = exit_code

        def read(self):
            return self._data

        def recv_exit_status(self):
            return self._exit

    class FakeStderr(FakeStdout):
        pass

    class FakeParamikoClient:
        def __init__(self):
            self.closed = False

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kwargs):
            self.kwargs = kwargs

        def exec_command(self, command, timeout):
            return None, FakeStdout(b"out", 0), FakeStderr(b"err", 0)

        def close(self):
            self.closed = True

    client = SSHClient("host", "user", "~/.ssh/id", 5)
    client._client = FakeParamikoClient()
    result = client.execute("ls", timeout=3)
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert client.execute_as("id", "root").stdout == "out"
    client.disconnect()
    assert client._client is None


def test_ssh_client_connect_and_context_manager(monkeypatch):
    created = {}

    class FakeParamikoClient:
        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kwargs):
            created["kwargs"] = kwargs

        def exec_command(self, command, timeout):
            class S:
                def __init__(self, data):
                    self._data = data
                    self.channel = self

                def read(self):
                    return self._data

                def recv_exit_status(self):
                    return 0

            return None, S(b"out"), S(b"")

        def close(self):
            created["closed"] = True

    monkeypatch.setattr("paramiko.SSHClient", lambda: FakeParamikoClient())
    monkeypatch.setattr("paramiko.AutoAddPolicy", lambda: object())
    with SSHClient("host", "user", "~/.ssh/id", 5) as client:
        assert client.execute("ls").stdout == "out"
    assert created["kwargs"]["hostname"] == "host"
    assert created["closed"] is True
