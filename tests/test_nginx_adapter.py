from __future__ import annotations

import pytest

from adapters.nginx import NginxAdapter, _rewrite_listen_backlog_line
from tools.ssh import SSHResult


class FakeSSH:
    def __init__(self, config_text: str, nginx_test_ok: bool = True):
        self.files = {
            "/etc/nginx/nginx.conf": config_text,
            "/tmp/nginx_new.conf": "",
        }
        self.nginx_test_ok = nginx_test_ok

    def execute(self, command: str, timeout: int | None = None) -> SSHResult:
        del timeout
        if command.startswith("cat /etc/nginx/"):
            path = command.split()[1]
            return SSHResult(self.files.get(path, ""), "", 0)

        if command.startswith("cp "):
            parts = command.split()
            if len(parts) >= 3:
                src, dst = parts[1], parts[2]
                self.files[dst] = self.files.get(src, "")
            return SSHResult("", "", 0)

        if command.startswith("cat > /tmp/nginx_") and "NGINX_CONF_EOF" in command:
            # Extract target path and content from heredoc
            first_line = command.split("\n", 1)[0]
            path = first_line.split("cat > ")[1].split(" <<")[0].strip()
            content = command.split("\n", 1)[1]
            content = content.rsplit("NGINX_CONF_EOF", 1)[0]
            self.files[path] = content
            return SSHResult("", "", 0)

        if command == "nginx -t 2>&1":
            if self.nginx_test_ok:
                return SSHResult("syntax is ok\ntest is successful\n", "", 0)
            return SSHResult("syntax error\n", "", 1)

        return SSHResult("", "", 0)


def _build_adapter(ssh: FakeSSH) -> NginxAdapter:
    return NginxAdapter(
        {
            "service": {
                "config_path": "/etc/nginx/nginx.conf",
                "benchmark": {},
            }
        },
        ssh,
    )


class FakeBench:
    def __init__(self, json_payload: str = ""):
        self.json_payload = json_payload
        self.commands: list[str] = []

    def execute(self, command: str, timeout: int | None = None) -> SSHResult:
        del timeout
        self.commands.append(command)
        if command.startswith("TARGET_HOST="):
            return SSHResult("ok", "", 0)
        if command.startswith("cat /root/hackathon-results/"):
            return SSHResult(self.json_payload, "", 0)
        if command.startswith("wrk2 "):
            return SSHResult("", "wrk2: command not found", 127)
        return SSHResult("", "", 0)


def test_rewrite_listen_line_updates_existing_backlog_and_keeps_comment():
    line = "    listen 443 ssl backlog=511; # tls listener"
    rewritten = _rewrite_listen_backlog_line(line, "2048")
    assert rewritten == "    listen 443 ssl backlog=2048; # tls listener"


def test_set_listen_backlog_updates_and_adds_only_in_server_block():
    initial = (
        "worker_processes auto;\n"
        "listen 9000;\n"
        "http {\n"
        "    server {\n"
        "        listen 80;\n"
        "        listen 443 ssl backlog=511; # tls\n"
        "    }\n"
        "}\n"
    )
    ssh = FakeSSH(initial, nginx_test_ok=True)
    adapter = _build_adapter(ssh)

    assert adapter._set_listen_backlog("1024") is True
    final = ssh.files["/etc/nginx/nginx.conf"]

    assert "listen 9000;" in final
    assert "listen 80 backlog=1024;" in final
    assert "listen 443 ssl backlog=1024; # tls" in final
    assert "backlog=511" not in final


def test_set_listen_backlog_is_idempotent():
    initial = "http {\n    server {\n        listen 80;\n    }\n}\n"
    ssh = FakeSSH(initial, nginx_test_ok=True)
    adapter = _build_adapter(ssh)

    assert adapter._set_listen_backlog("1024") is True
    first = ssh.files["/etc/nginx/nginx.conf"]
    assert adapter._set_listen_backlog("1024") is True
    second = ssh.files["/etc/nginx/nginx.conf"]

    assert first == second


def test_set_listen_backlog_rolls_back_on_invalid_nginx_config():
    initial = "http {\n    server {\n        listen 80;\n    }\n}\n"
    ssh = FakeSSH(initial, nginx_test_ok=False)
    adapter = _build_adapter(ssh)

    assert adapter._set_listen_backlog("1024") is False
    assert ssh.files["/etc/nginx/nginx.conf"] == initial


def test_apply_config_supports_open_file_cache_related_http_directives():
    initial = "http {\n    sendfile on;\n}\n"
    ssh = FakeSSH(initial, nginx_test_ok=True)
    adapter = _build_adapter(ssh)

    assert adapter.apply_config("open_file_cache_valid", "60s") is True
    assert adapter.apply_config("open_file_cache_min_uses", "2") is True

    final = ssh.files["/etc/nginx/nginx.conf"]
    assert "    open_file_cache_valid 60s;" in final
    assert "    open_file_cache_min_uses 2;" in final


def test_apply_config_removes_server_block_duplicates_for_http_directives():
    initial = (
        "http {\n"
        "    sendfile on;\n"
        "    server {\n"
        "        sendfile off;\n"
        "    }\n"
        "}\n"
    )
    ssh = FakeSSH(initial, nginx_test_ok=True)
    adapter = _build_adapter(ssh)

    assert adapter.apply_config("sendfile", "off") is True

    final = ssh.files["/etc/nginx/nginx.conf"]
    # Server-block duplicate removed; only http-level directive remains
    assert final.count("sendfile off;") == 1
    assert "    sendfile off;" in final


def test_apply_config_returns_false_when_target_context_block_is_missing():
    initial = "worker_processes auto;\n"
    ssh = FakeSSH(initial, nginx_test_ok=True)
    adapter = _build_adapter(ssh)

    assert adapter.apply_config("sendfile", "on") is False


def test_benchmark_uses_hackathon_runner_when_configured(monkeypatch):
    monkeypatch.setenv("DUT_HOST", "127.0.0.1")
    payload = (
        '{"results":{"requests":{"per_sec":431193.8},'
        '"latency":{"percentiles":{"p50":"1.2ms","p99":"9.2ms"}},"duration":60}}'
    )
    bench = FakeBench(payload)
    adapter = NginxAdapter(
        {
            "service": {
                "config_path": "/etc/nginx/nginx.conf",
                "benchmark": {
                    "tool": "hackathon",
                    "script": "/root/hackathon-tools/benchmark.sh",
                    "contestant_name": "slaymetrics",
                    "target_host_env": "DUT_HOST",
                    "small_file_url": "http://127.0.0.1/1kb.html",
                },
            }
        },
        FakeSSH("http {}\n"),
        bench=bench,
    )

    result = adapter.benchmark(url="http://127.0.0.1/1kb.html")

    assert result.requests_per_sec == 431193.8
    assert result.latency_p99_ms == 9.2
    assert any("/root/hackathon-tools/benchmark.sh" in cmd for cmd in bench.commands)
    assert any(cmd.endswith("_small.json 2>/dev/null") for cmd in bench.commands if cmd.startswith("cat "))


def test_benchmark_raises_when_wrk2_command_fails():
    adapter = NginxAdapter(
        {
            "service": {
                "config_path": "/etc/nginx/nginx.conf",
                "benchmark": {
                    "threads": 4,
                    "connections": 100,
                    "rate": 1000,
                    "small_file_url": "http://localhost/1kb.html",
                },
            }
        },
        FakeSSH("http {}\n"),
        bench=FakeBench(),
    )

    with pytest.raises(RuntimeError, match="wrk2 benchmark failed"):
        adapter.benchmark(url="http://localhost/1kb.html")
