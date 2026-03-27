from __future__ import annotations

import sys
from types import SimpleNamespace

from telemetry.langfuse_client import LangfuseClient, summarize_messages


class _FakeContext:
    def __init__(self, owner, kind, kwargs):
        self.owner = owner
        self.kind = kind
        self.kwargs = kwargs

    def __enter__(self):
        self.owner.calls.append((self.kind, self.kwargs))
        return self

    def __exit__(self, *exc):
        return False


class _FakeLangfuseImpl:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[tuple] = []

    def start_as_current_observation(self, **kwargs):
        return _FakeContext(self, "observation", kwargs)

    def start_as_current_generation(self, **kwargs):
        return _FakeContext(self, "generation", kwargs)

    def update_current_generation(self, **kwargs):
        self.calls.append(("update_generation", kwargs))

    def update_current_span(self, **kwargs):
        self.calls.append(("update_span", kwargs))

    def create_event(self, **kwargs):
        self.calls.append(("event", kwargs))

    def get_trace_url(self):
        return "http://langfuse/project/p/traces/t"

    def flush(self):
        self.calls.append(("flush", {}))

    def shutdown(self):
        self.calls.append(("shutdown", {}))


def test_langfuse_client_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    client = LangfuseClient.from_env({"session_id": "s1"})

    assert client.enabled is False
    with client.trace("run"):
        pass


def test_langfuse_client_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    client = LangfuseClient.from_env({"session_id": "s1"}, enabled=False)

    assert client.enabled is False


def test_langfuse_client_traces_events_and_generations(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://langfuse.local")
    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(Langfuse=_FakeLangfuseImpl))

    client = LangfuseClient.from_env({"session_id": "s1", "planner_mode": "debate"})

    assert client.enabled is True
    with client.trace("slaymetrics_run", input={"cfg": "x"}):
        client.event("benchmark_evidence", output={"rps": 1.0})
        with client.generation("nginx_expert", model="gpt-oss-120b", input={"messages": []}):
            client.update_generation(output={"summary": "ok"}, usage_details={"prompt_tokens": 1})
        client.update_span(output={"done": True})
    client.flush()
    client.shutdown()

    fake = client._client
    assert fake.init_kwargs["base_url"] == "http://langfuse.local"
    assert fake.calls[0][0] == "observation"
    assert any(call[0] == "generation" for call in fake.calls)
    assert any(call[0] == "event" for call in fake.calls)
    assert any(call[0] == "update_generation" for call in fake.calls)
    assert client.last_trace_url == "http://langfuse/project/p/traces/t"


def test_langfuse_client_auth_check(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(Langfuse=_FakeLangfuseImpl))

    client = LangfuseClient.from_env({"session_id": "s1"})
    client._client.auth_check = lambda: True
    assert client.auth_check() is True


def test_summarize_messages_limits_content():
    messages = [
        SimpleNamespace(type="human", content="hello", tool_calls=None),
        SimpleNamespace(type="ai", content=["a", {"b": 1}], tool_calls=[{"name": "x"}]),
    ]

    summary = summarize_messages(messages)

    assert summary[0]["role"] == "human"
    assert summary[1]["tool_calls"] == [{"name": "x"}]
