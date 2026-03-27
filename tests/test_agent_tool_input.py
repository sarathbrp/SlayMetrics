from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import agents.agent as diagnosis_agent
from adapters.base import BenchmarkResult
from agents import TokenCounter
from agents.agent import DiagnosisOutput, build
from tools.ssh import SSHResult


class FakeMemory:
    def __init__(self):
        self.saved_facts: list[dict] = []
        self.saved: list[tuple] = []

    def save_context(self, *args, **kwargs) -> None:
        del kwargs
        self.saved.append(args)

    def save_fact(self, **kwargs) -> None:
        self.saved_facts.append(kwargs)

    def get_profile(self, session_id):
        del session_id
        return {"baseline_rps": 100.0}

    def get_contexts(self, session_id, type=None, source_prefix=None, limit=None):
        del session_id, type, limit
        if source_prefix == "baseline_small":
            return [
                {
                    "content": json.dumps({"rps": 100.0, "p99": 2.0, "error_rate": 0.0}),
                }
            ]
        if source_prefix == "baseline:":
            return [
                {
                    "source": "baseline:series",
                    "content": json.dumps(
                        {
                            "summary": {
                                "run_queue_max": 5,
                                "rx_drop_delta": 10,
                                "rx_drop_rate_per_sec": 2.5,
                            },
                            "last_sample": {"tcp_established": 1200},
                        }
                    ),
                }
            ]
        return []


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

    def benchmark(self, duration: int = 30, url: str = "") -> BenchmarkResult:
        return BenchmarkResult(150.0, 1.0, 1.5, 0.0, duration, url=url, cpu_pct=10.0, mem_mb=20.0)


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
    agent = build("model")
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    ctx = _ctx()

    result = asyncio.run(
        tool(
            ctx,
            '{"sendfile":"on","keepalive_requests":1000}',
        )
    )

    assert result["reload"] == "OK"
    assert result["failed"] == []
    assert ("sendfile", "on") in ctx.deps.adapter.applied
    assert ("keepalive_requests", "1000") in ctx.deps.adapter.applied


def test_inspect_irq_distribution_uses_telemetry_window_summary():
    agent = build("model")
    tool = agent._function_toolset.tools["inspect_irq_distribution"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx))

    assert "possible_irq_or_worker_core_lock" in result["needs_investigation"]
    assert result["current"]["telemetry_run_queue_max"] == 5
    assert result["current"]["telemetry_rx_drop_delta"] == 10


def test_apply_nginx_tuning_invalid_json_returns_structured_error():
    agent = build("model")
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, '{"sendfile":"on"'))

    assert result["reload"] == "FAILED"
    assert "invalid JSON" in result["error"]
    assert result["applied"] == []
    assert result["failed"] == []


def test_apply_system_tuning_invalid_json_returns_structured_error():
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, '{"net.core.somaxconn":65535'))

    assert result["applied"] == {}
    assert "_input" in result["failed"]
    assert "invalid JSON" in result["failed"]["_input"]


def test_apply_nginx_tuning_rejects_unsupported_directives():
    agent = build("model")
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, {"upstream_read_timeout": "5s", "sendfile": "on"}))

    assert result["reload"] == "OK"
    assert result["applied"] == ["sendfile"]
    assert result["failed"] == ["upstream_read_timeout"]
    assert "ignored unsupported nginx directives" in result["warning"]
    assert ctx.deps.adapter.applied == [("sendfile", "on")]


def test_apply_nginx_tuning_accepts_top_level_kwargs_shape():
    agent = build("model")
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, access_log="off", sendfile="on"))

    assert result["reload"] == "OK"
    assert "sendfile" in result["applied"]
    assert "access_log" in result["failed"] or "access_log" in result["applied"]


def test_apply_system_tuning_accepts_top_level_kwargs_shape():
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, transparent_hugepage="never"))

    assert result["applied"].get("transparent_hugepage") == "never"


def test_apply_nginx_tuning_strips_leading_dot_from_keys():
    agent = build("model")
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, **{".sendfile": "on"}))

    assert result["reload"] == "OK"
    assert result["applied"] == ["sendfile"]


def test_apply_nginx_tuning_applies_supported_subset_and_reports_unsupported():
    agent = build("model")
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
    agent = build("model")
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function

    result = asyncio.run(tool(ctx, {"sendfile": "on", "keepalive_requests": "1000"}))

    assert result["reload"] == "FAILED"
    assert result["applied"] == ["sendfile"]
    assert result["failed"] == ["keepalive_requests"]
    assert "failed to apply nginx directive" in result["error"]
    assert ctx.deps.ssh.files["/etc/nginx/nginx.conf"] == "good config\n"


def test_save_findings_coerces_non_numeric_impact_pct():
    agent = build("model")
    tool = agent._function_toolset.tools["save_findings"].function
    ctx = _ctx()

    result = asyncio.run(
        tool(
            ctx,
            [
                {
                    "parameter": "reset_timedout_connection",
                    "before_value": "off",
                    "after_value": "on",
                    "before_rps": "0",
                    "after_rps": "0.0",
                    "impact_pct": "N/A (reset)",
                }
            ],
        )
    )

    assert result is True
    assert ctx.deps.memory.saved_facts[0]["before_rps"] == 0.0
    assert ctx.deps.memory.saved_facts[0]["after_rps"] == 0.0
    assert ctx.deps.memory.saved_facts[0]["impact_pct"] is None


def test_save_findings_derives_run_level_delta_when_model_omits_it():
    agent = build("model")
    tool = agent._function_toolset.tools["save_findings"].function
    ctx = _ctx()
    agent._slaymetrics_state["after_rps"] = 150.0

    result = asyncio.run(
        tool(
            ctx,
            [
                {
                    "parameter": "worker_connections",
                    "before_value": "1024",
                    "after_value": "65536",
                }
            ],
        )
    )

    assert result is True
    assert ctx.deps.memory.saved_facts[0]["before_rps"] == 100.0
    assert ctx.deps.memory.saved_facts[0]["after_rps"] == 150.0
    assert ctx.deps.memory.saved_facts[0]["impact_pct"] == 50.0


def test_diagnosis_output_coerces_granite_friendly_shapes():
    output = DiagnosisOutput(
        nginx_applied=True,
        system_applied=True,
        after_rps="1918406.5",
        improvement_pct=None,
        notes=[
            {"parameter": "nginx.access_log", "before_value": "/var/log/nginx/access.log main"},
        ],
    )

    assert output.after_rps == 1918406.5
    assert output.improvement_pct == 0.0
    assert output.notes.startswith("[")
    assert output.rca_records == []


def test_save_rca_persists_structured_records():
    agent = build("model")
    tool = agent._function_toolset.tools["save_rca"].function
    ctx = _ctx()

    result = asyncio.run(
        tool(
            ctx,
            [
                {
                    "symptom": "High small-file p99 latency",
                    "root_cause": "Backlog and worker limits are below target",
                    "confidence": 0.9,
                    "recommendation": "Raise worker and socket limits",
                    "evidence": ["p99 > 1000ms", "somaxconn=4096"],
                }
            ],
        )
    )

    assert result is True
    saved = ctx.deps.memory.saved[-1]
    assert saved[1] == "rca"
    assert "Backlog and worker limits" in saved[4]


def test_save_recommendations_persists_human_readable_items():
    agent = build("model")
    tool = agent._function_toolset.tools["save_recommendations"].function
    ctx = _ctx()

    result = asyncio.run(
        tool(
            ctx,
            [
                {
                    "title": "Raise connection limits",
                    "recommendation": "Increase worker_connections and somaxconn",
                    "rationale": "Current values are below proven targets",
                    "expected_benefit": "Higher small-file throughput",
                    "risk_level": "low",
                    "validation": "Re-run small workload and compare p99/RPS",
                    "scope": "nginx",
                    "changes": {"worker_connections": "65536"},
                }
            ],
        )
    )

    assert result is True
    saved = ctx.deps.memory.saved[-1]
    assert saved[1] == "recommendation"
    assert "Raise connection limits" in saved[4]


def test_save_recommendations_skips_non_nginx_performance_changes():
    agent = build("model")
    tool = agent._function_toolset.tools["save_recommendations"].function
    ctx = _ctx()

    result = asyncio.run(
        tool(
            ctx,
            [
                {
                    "title": "Change unrelated setting",
                    "recommendation": "Touch upstream timeout",
                    "rationale": "not relevant",
                    "expected_benefit": "none",
                    "risk_level": "low",
                    "validation": "n/a",
                    "scope": "nginx",
                    "changes": {"upstream_read_timeout": "5s"},
                }
            ],
        )
    )

    assert result is True
    assert any(entry[2] == "planner_recommendations_raw" for entry in ctx.deps.memory.saved)
    rejected = [entry for entry in ctx.deps.memory.saved if entry[2] == "recommendation_rejected_1"]
    assert rejected
    assert "no allowed performance changes" in rejected[0][4]


def test_run_builds_diagnosis_output_from_tool_state(monkeypatch):
    deps = SimpleNamespace(
        memory=SimpleNamespace(get_profile=lambda session_id: {"baseline_rps": 100.0}),
        session_id="s1",
        token_counter=TokenCounter(),
    )

    class FakeRunResult:
        output = "Applied tuning successfully."

        def usage(self):
            return SimpleNamespace(input_tokens=1, output_tokens=2)

        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "nginx_applied": True,
            "system_applied": True,
            "after_rps": 150.0,
            "findings": [{"parameter": "nginx.access_log"}],
        }

        async def run(self, prompt, deps):
            return FakeRunResult()

    monkeypatch.setattr(diagnosis_agent, "build", lambda model: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))

    assert output.nginx_applied is True
    assert output.system_applied is True
    assert output.after_rps == 150.0
    assert output.improvement_pct == 50.0
    assert output.notes == "Applied tuning successfully."


def test_run_does_not_double_count_usage(monkeypatch):
    deps = SimpleNamespace(
        memory=SimpleNamespace(get_profile=lambda session_id: {"baseline_rps": 100.0}),
        session_id="s1",
        token_counter=TokenCounter(),
    )

    class FakeRunResult:
        output = "Applied tuning successfully."

        def usage(self):
            return SimpleNamespace(input_tokens=11, output_tokens=7)

        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "nginx_applied": True,
            "system_applied": True,
            "after_rps": 150.0,
            "findings": [],
        }

        async def run(self, prompt, deps):
            return FakeRunResult()

    monkeypatch.setattr(diagnosis_agent, "build", lambda model: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    asyncio.run(diagnosis_agent.run("model", deps, "ctx"))

    assert deps.token_counter.input_tokens == 11
    assert deps.token_counter.output_tokens == 7


def test_run_uses_debate_planner_mode(monkeypatch):
    deps = SimpleNamespace(
        memory=SimpleNamespace(get_profile=lambda session_id: {"baseline_rps": 100.0}),
        session_id="s1",
        token_counter=TokenCounter(),
        config={"agent": {"planner_mode": "debate", "max_phase": 3}},
    )

    class FakeAgent:
        _slaymetrics_state = {
            "nginx_applied": False,
            "system_applied": False,
            "after_rps": 0.0,
            "findings": [],
            "rca_records": [],
            "recommendations": [],
        }

    class FakeRunResult:
        output = "Debate complete."

        def usage(self):
            return SimpleNamespace(input_tokens=4, output_tokens=3)

        def all_messages(self):
            return []

    monkeypatch.setattr(diagnosis_agent, "build", lambda model: FakeAgent())
    monkeypatch.setattr(
        diagnosis_agent,
        "_run_debate_planner",
        lambda agent, model, deps, context_prompt: asyncio.sleep(0, result=FakeRunResult()),
    )
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))

    assert output.notes == "Debate complete."
    assert deps.token_counter.input_tokens == 4
    assert deps.token_counter.output_tokens == 3


def test_run_applies_saved_recommendations(monkeypatch):
    deps = _ctx().deps

    class FakeRunResult:
        output = "Plan complete."

        def usage(self):
            return SimpleNamespace(input_tokens=3, output_tokens=2)

        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "nginx_applied": False,
            "system_applied": False,
            "after_rps": 0.0,
            "findings": [],
            "recommendations": [{"title": "x"}],
        }

        async def run(self, prompt, deps):
            return FakeRunResult()

        def _apply_from_recommendations(self, deps):
            self._slaymetrics_state["nginx_applied"] = True
            self._slaymetrics_state["system_applied"] = True
            self._slaymetrics_state["after_rps"] = 160.0
            self._slaymetrics_state["findings"] = [{"parameter": "nginx.sendfile"}]
            self._slaymetrics_state["recommendations"] = [{"title": "Raise connection limits"}]
            return {}

    monkeypatch.setattr(diagnosis_agent, "build", lambda model: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))

    assert output.nginx_applied is True
    assert output.system_applied is True
    assert output.after_rps == 160.0
    assert output.recommendations[0]["title"] == "Raise connection limits"


def test_recommendation_guardrail_records_negative_on_regression():
    ctx = _ctx()
    ctx.deps.adapter.benchmark = lambda duration=30, url="": BenchmarkResult(
        90.0, 1.0, 3.0, 0.0, duration, url=url, cpu_pct=10.0, mem_mb=20.0
    )
    agent = build("model")
    agent._slaymetrics_state["recommendations"] = [
        {
            "title": "Enable sendfile",
            "recommendation": "Enable sendfile",
            "rationale": "recommended",
            "scope": "nginx",
            "changes": {"sendfile": "on"},
        }
    ]
    agent._slaymetrics_state["nginx_inspection"] = {"current": {"sendfile": "off"}}

    agent._apply_from_recommendations(ctx.deps)

    assert any(fact["type"] == "negative" for fact in ctx.deps.memory.saved_facts)
    assert not any(fact["type"] == "fix" for fact in ctx.deps.memory.saved_facts)
    assert "RPS regressed" in agent._slaymetrics_state["guardrail_failure"]
