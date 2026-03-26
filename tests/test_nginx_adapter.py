from __future__ import annotations

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
        if command.startswith("cat /etc/nginx/nginx.conf"):
            return SSHResult(self.files["/etc/nginx/nginx.conf"], "", 0)

        if command.startswith("cp "):
            _, src, dst = command.split()
            self.files[dst] = self.files.get(src, "")
            return SSHResult("", "", 0)

        if command.startswith("cat > /tmp/nginx_new.conf << 'NGINX_CONF_EOF'\n"):
            content = command.split("\n", 1)[1]
            content = content.rsplit("NGINX_CONF_EOF", 1)[0]
            self.files["/tmp/nginx_new.conf"] = content
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
    initial = (
        "http {\n"
        "    server {\n"
        "        listen 80;\n"
        "    }\n"
        "}\n"
    )
    ssh = FakeSSH(initial, nginx_test_ok=True)
    adapter = _build_adapter(ssh)

    assert adapter._set_listen_backlog("1024") is True
    first = ssh.files["/etc/nginx/nginx.conf"]
    assert adapter._set_listen_backlog("1024") is True
    second = ssh.files["/etc/nginx/nginx.conf"]

    assert first == second


def test_set_listen_backlog_rolls_back_on_invalid_nginx_config():
    initial = (
        "http {\n"
        "    server {\n"
        "        listen 80;\n"
        "    }\n"
        "}\n"
    )
    ssh = FakeSSH(initial, nginx_test_ok=False)
    adapter = _build_adapter(ssh)

    assert adapter._set_listen_backlog("1024") is False
    assert ssh.files["/etc/nginx/nginx.conf"] == initial
