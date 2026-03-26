from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pydantic_ai.models.test import TestModel

from agents import TokenCounter
from agents.agent import build
from tools.ssh import SSHResult


class FakeMemory:
    def save_context(self, *args, **kwargs) -> None:
        del args, kwargs


class FakeAdapter:
    def __init__(self):
        self.applied: list[tuple[str, str]] = []
        self.ALLOWED_BATCH_DIRECTIVES = {
            "sendfile",
            "keepalive_requests",
            "tcp_nodelay",
            "open_file_cache_valid",
            "open_file_cache_min_uses",
        }

    def apply_config(self, parameter: str, value: str) -> bool:
        self.applied.append((parameter, value))
        return True

    def reload(self) -> bool:
        return True


class FakeSSH:
    def __init__(self):
        self.files = {"/etc/nginx/nginx.conf": "good config\n"}

    def execute(self, command: str, timeout: int | None = None) -> SSHResult:
        del timeout
        if command.startswith("cp "):
            _, src, dst = command.split()
            self.files[dst] = self.files.get(src, "")
            return SSHResult("", "", 0)
        if command == "nginx -t 2>&1":
            return SSHResult("syntax is ok\ntest is successful\n", "", 0)
        return SSHResult("", "", 0)


def _ctx():
    ssh = FakeSSH()
    adapter = FakeAdapter()
    deps = SimpleNamespace(
        adapter=adapter,
        ssh=ssh,
        memory=FakeMemory(),
        session_id="s1",
        token_counter=TokenCounter(),
        config={"service": {"benchmark": {}, "config_path": "/etc/nginx/nginx.conf"}},
    )
    return SimpleNamespace(deps=deps)


def test_apply_nginx_tuning_accepts_json_string_changes():
    agent = build(TestModel())
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    ctx = _ctx()

    result = asyncio.run(
        tool(
            ctx,
            '{"sendfile":"on","keepalive_requests":1000}',
            "test",
        )
    )

    assert result["reload"] == "OK"
    assert result["failed"] == []
    assert ("sendfile", "on") in ctx.deps.adapter.applied
    assert ("keepalive_requests", "1000") in ctx.deps.adapter.applied


def test_apply_nginx_tuning_invalid_json_returns_structured_error():
    agent = build(TestModel())
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, '{"sendfile":"on"', "test"))

    assert result["reload"] == "FAILED"
    assert "invalid JSON" in result["error"]
    assert result["applied"] == []
    assert result["failed"] == []


def test_apply_system_tuning_invalid_json_returns_structured_error():
    agent = build(TestModel())
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, '{"net.core.somaxconn":65535', "test"))

    assert result["applied"] == {}
    assert "_input" in result["failed"]
    assert "invalid JSON" in result["failed"]["_input"]


def test_apply_nginx_tuning_rejects_unsupported_directives():
    agent = build(TestModel())
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, {"upstream_read_timeout": "5s", "sendfile": "on"}, "test"))

    assert result["reload"] == "OK"
    assert result["applied"] == ["sendfile"]
    assert result["failed"] == ["upstream_read_timeout"]
    assert "ignored unsupported nginx directives" in result["warning"]
    assert ctx.deps.adapter.applied == [("sendfile", "on")]


def test_apply_nginx_tuning_applies_supported_subset_and_reports_unsupported():
    agent = build(TestModel())
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    ctx = _ctx()

    result = asyncio.run(
        tool(
            ctx,
            {
                "open_file_cache_valid": "60s",
                "upstream_read_timeout": "5s",
                "sendfile": "on",
            },
            "test",
        )
    )

    assert result["reload"] == "OK"
    assert result["applied"] == ["open_file_cache_valid", "sendfile"]
    assert result["failed"] == ["upstream_read_timeout"]
    assert "ignored unsupported nginx directives" in result["warning"]
    assert ("open_file_cache_valid", "60s") in ctx.deps.adapter.applied
    assert ("sendfile", "on") in ctx.deps.adapter.applied


def test_apply_nginx_tuning_restores_pre_batch_snapshot_on_failure():
    class FailingAdapter(FakeAdapter):
        def __init__(self, ssh: FakeSSH):
            super().__init__()
            self.calls = 0
            self._ssh = ssh

        def apply_config(self, parameter: str, value: str) -> bool:
            self.calls += 1
            self.applied.append((parameter, value))
            self._ssh.files["/etc/nginx/nginx.conf"] = f"mutated after {parameter}\n"
            if self.calls == 1:
                return True
            return False

    ctx = _ctx()
    ctx.deps.adapter = FailingAdapter(ctx.deps.ssh)
    agent = build(TestModel())
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function

    result = asyncio.run(tool(ctx, {"sendfile": "on", "keepalive_requests": "1000"}, "test"))

    assert result["reload"] == "FAILED"
    assert result["applied"] == ["sendfile"]
    assert result["failed"] == ["keepalive_requests"]
    assert "failed to apply nginx directive" in result["error"]
    assert ctx.deps.ssh.files["/etc/nginx/nginx.conf"] == "good config\n"
