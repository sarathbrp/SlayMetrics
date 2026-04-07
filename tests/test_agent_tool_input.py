from __future__ import annotations

# ruff: noqa: E402
import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault(
    "paramiko",
    types.SimpleNamespace(SSHClient=object, AutoAddPolicy=lambda: object()),
)

import agents.agent as diagnosis_agent
from adapters.base import BenchmarkResult
from agents import TokenCounter
from agents.agent import (
    DiagnosisOutput,
    _coerce_recommendations,
    _coerce_records,
    _run_observational_debate_eval,
    _save_planner_artifact,
    build,
)
from tools.ssh import SSHResult

_TEST_CONFIG = {
    "tuning": {
        "webserver_targets": {
            "worker_processes": "auto", "worker_connections": "65536",
            "worker_rlimit_nofile": "200000", "sendfile": "on",
            "tcp_nopush": "on", "tcp_nodelay": "on", "access_log": "off",
            "open_file_cache": "max=200000 inactive=60s",
            "open_file_cache_valid": "30s", "open_file_cache_min_uses": "2",
            "keepalive_requests": "10000", "keepalive_timeout": "30",
            "reset_timedout_connection": "on", "listen_backlog": "65535",
            "aio": "off",
        },
        "kernel_targets": {
            "net.core.somaxconn": "65535", "transparent_hugepage": "never",
            "selinux": "permissive",
        },
    },
}


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
        config={
            "agent": {"debug_planner_payloads": False, "persist_hypotheses": False},
            "service": {"benchmark": {}, "config_path": "/etc/nginx/nginx.conf"},
            "tuning": {
                "webserver_targets": {"sendfile": "on", "worker_connections": "65536"},
                "kernel_targets": {"net.core.somaxconn": "65535"},
                "resource_limits_targets": {},
                "network_targets": {},
                "storage_targets": {},
            },
        },
    )
    return SimpleNamespace(deps=deps)


def test_apply_service_tuning_accepts_json_string_changes():
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["apply_service_tuning"].function
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
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["inspect_irq_distribution"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx))

    assert "possible_irq_or_worker_core_lock" in result["needs_investigation"]
    assert result["current"]["telemetry_run_queue_max"] == 5
    assert result["current"]["telemetry_rx_drop_delta"] == 10


def test_apply_service_tuning_invalid_json_returns_structured_error():
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["apply_service_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, '{"sendfile":"on"'))

    assert result["reload"] == "FAILED"
    assert "invalid JSON" in result["error"]
    assert result["applied"] == []
    assert result["failed"] == []


def test_apply_system_tuning_invalid_json_returns_structured_error():
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, '{"net.core.somaxconn":65535'))

    assert result["applied"] == {}
    assert "_input" in result["failed"]
    assert "invalid JSON" in result["failed"]["_input"]


def test_apply_service_tuning_rejects_unsupported_directives():
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["apply_service_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, {"upstream_read_timeout": "5s", "sendfile": "on"}))

    assert result["reload"] == "OK"
    assert result["applied"] == ["sendfile"]
    assert result["failed"] == ["upstream_read_timeout"]
    assert "ignored unsupported nginx directives" in result["warning"]
    assert ctx.deps.adapter.applied == [("sendfile", "on")]


def test_apply_service_tuning_accepts_top_level_kwargs_shape():
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["apply_service_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, access_log="off", sendfile="on"))

    assert result["reload"] == "OK"
    assert "sendfile" in result["applied"]
    assert "access_log" in result["failed"] or "access_log" in result["applied"]


def test_apply_system_tuning_accepts_top_level_kwargs_shape():
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, transparent_hugepage="never"))

    assert result["applied"].get("transparent_hugepage") == "never"


def test_apply_service_tuning_strips_leading_dot_from_keys():
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["apply_service_tuning"].function
    ctx = _ctx()

    result = asyncio.run(tool(ctx, **{".sendfile": "on"}))

    assert result["reload"] == "OK"
    assert result["applied"] == ["sendfile"]


def test_apply_service_tuning_applies_supported_subset_and_reports_unsupported():
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["apply_service_tuning"].function
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


def test_apply_service_tuning_restores_pre_batch_snapshot_on_failure():
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
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["apply_service_tuning"].function

    result = asyncio.run(tool(ctx, {"sendfile": "on", "keepalive_requests": "1000"}))

    assert result["reload"] == "FAILED"
    assert result["applied"] == ["sendfile"]
    assert result["failed"] == ["keepalive_requests"]
    assert "failed to apply nginx directive" in result["error"]
    assert ctx.deps.ssh.files["/etc/nginx/nginx.conf"] == "good config\n"


def test_save_findings_coerces_non_numeric_impact_pct():
    agent = build("model", config=_TEST_CONFIG)
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
    agent = build("model", config=_TEST_CONFIG)
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
        service_applied=True,
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
    agent = build("model", config=_TEST_CONFIG)
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
    agent = build("model", config=_TEST_CONFIG)
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
    agent = build("model", config=_TEST_CONFIG)
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


def test_save_recommendations_debug_mode_keeps_same_filtering(monkeypatch):
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["save_recommendations"].function
    ctx = _ctx()
    ctx.deps.config["agent"]["debug_planner_payloads"] = True
    debug_lines = []
    monkeypatch.setattr(diagnosis_agent, "tool_result", lambda tool_name, message: debug_lines.append((tool_name, message)))

    result = asyncio.run(
        tool(
            ctx,
            [
                {
                    "title": "Bad shape",
                    "scope": "nginx",
                    "changes": {"not_supported": "1"},
                }
            ],
        )
    )

    assert result is True
    assert any(tool_name == "debug" for tool_name, _ in debug_lines)


def test_coerce_records_accepts_description_impact_shape():
    records = _coerce_records(
        [
            {
                "id": "RCA-01",
                "description": "worker_connections limited to 1024",
                "impact": "Restricts simultaneous client connections and raises latency.",
            }
        ]
    )

    assert records == [
        {
            "symptom": "worker_connections limited to 1024",
            "root_cause": "Restricts simultaneous client connections and raises latency.",
            "confidence": 0.0,
            "recommendation": "Restricts simultaneous client connections and raises latency.",
            "evidence": [],
        }
    ]


def test_coerce_recommendations_accepts_action_shape_for_nginx():
    recommendations = _coerce_recommendations(
        [
            {
                "type": "nginx",
                "action": "Set worker_connections 65536;",
                "justification": "Raise concurrent connection capacity.",
            },
            {
                "type": "nginx",
                "action": "Enable aio threads;",
                "justification": "Use async file I/O.",
            },
        ]
    )

    assert recommendations[0]["scope"] == "nginx"
    assert recommendations[0]["changes"] == {"worker_connections": "65536"}
    assert recommendations[1]["changes"] == {"aio": "threads"}


def test_coerce_recommendations_accepts_single_command_shape_for_system():
    recommendations = _coerce_recommendations(
        [
            {
                "type": "system",
                "action": "Temporarily set SELinux to permissive for benchmarking.",
                "command": "setenforce 0",
                "justification": "Isolate SELinux impact.",
            }
        ]
    )

    assert recommendations[0]["scope"] == "system"
    assert recommendations[0]["changes"] == {"selinux": "permissive"}


def test_save_planner_artifact_writes_hypothesis_markdown(tmp_path, monkeypatch):
    ctx = _ctx()
    ctx.deps.config["agent"]["persist_hypotheses"] = True
    monkeypatch.setattr(
        diagnosis_agent,
        "_hypothesis_dir",
        lambda deps: Path(tmp_path) / deps.session_id,
    )

    _save_planner_artifact(
        ctx.deps,
        "nginx_expert",
        {"summary": "summary text", "recommendations": [{"x": 1}]},
    )

    saved = (tmp_path / "s1" / "01_nginx_expert.md").read_text()
    assert "# Nginx Expert" in saved
    assert "summary text" in saved
    assert '"recommendations"' in saved


def test_rejected_recommendations_are_written_to_hypothesis_markdown(tmp_path, monkeypatch):
    agent = build("model", config=_TEST_CONFIG)
    tool = agent._function_toolset.tools["save_recommendations"].function
    ctx = _ctx()
    ctx.deps.config["agent"]["persist_hypotheses"] = True
    monkeypatch.setattr(
        diagnosis_agent,
        "_hypothesis_dir",
        lambda deps: Path(tmp_path) / deps.session_id,
    )

    result = asyncio.run(
        tool(
            ctx,
            [
                {
                    "title": "Bad shape",
                    "scope": "nginx",
                    "changes": {"not_supported": "1"},
                }
            ],
        )
    )

    assert result is True
    saved = (tmp_path / "s1" / "06_rejections.md").read_text()
    assert "# Rejected Recommendations" in saved
    assert "no allowed performance changes" in saved


def test_run_builds_diagnosis_output_from_tool_state(monkeypatch):
    deps = SimpleNamespace(
        memory=SimpleNamespace(get_profile=lambda session_id: {"baseline_rps": 100.0}),
        session_id="s1",
        token_counter=TokenCounter(),
        config={"agent": {"planner_mode": "custom"}},
    )

    class FakeRunResult:
        output = "Applied tuning successfully."

        def usage(self):
            return SimpleNamespace(input_tokens=1, output_tokens=2)

        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "service_applied": True,
            "system_applied": True,
            "after_rps": 150.0,
            "findings": [{"parameter": "nginx.access_log"}],
        }

        async def run(self, prompt, deps):
            return FakeRunResult()

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))

    assert output.service_applied is True
    assert output.system_applied is True
    assert output.after_rps == 150.0
    assert output.improvement_pct == 50.0
    assert output.notes == "Applied tuning successfully."


def test_run_does_not_double_count_usage(monkeypatch):
    deps = SimpleNamespace(
        memory=SimpleNamespace(get_profile=lambda session_id: {"baseline_rps": 100.0}),
        session_id="s1",
        token_counter=TokenCounter(),
        config={"agent": {"planner_mode": "custom"}},
    )

    class FakeRunResult:
        output = "Applied tuning successfully."

        def usage(self):
            return SimpleNamespace(input_tokens=11, output_tokens=7)

        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "service_applied": True,
            "system_applied": True,
            "after_rps": 150.0,
            "findings": [],
        }

        async def run(self, prompt, deps):
            return FakeRunResult()

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
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
            "service_applied": False,
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

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
    monkeypatch.setattr(
        diagnosis_agent,
        "_run_debate_planner",
        lambda agent, model, deps, context_prompt, agent_state=None: asyncio.sleep(0, result=FakeRunResult()),
    )
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))

    assert output.notes == "Debate complete."
    assert deps.token_counter.input_tokens == 4
    assert deps.token_counter.output_tokens == 3


def test_run_normalizes_single_planner_mode_to_deterministic(monkeypatch):
    deps = SimpleNamespace(
        memory=SimpleNamespace(get_profile=lambda session_id: {"baseline_rps": 100.0}),
        session_id="s1",
        token_counter=TokenCounter(),
        config={"agent": {"planner_mode": "single", "max_phase": 3}},
    )

    class FakeAgent:
        _slaymetrics_state = {
            "service_applied": False,
            "system_applied": False,
            "after_rps": 0.0,
            "findings": [],
            "rca_records": [],
            "recommendations": [],
        }

    class FakeRunResult:
        output = "Deterministic complete."

        def usage(self):
            return SimpleNamespace(input_tokens=5, output_tokens=1)

        def all_messages(self):
            return []

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
    monkeypatch.setattr(
        diagnosis_agent,
        "_run_rules_engine",
        lambda agent, model, deps, context_prompt, agent_state=None, hybrid=False: asyncio.sleep(0, result=FakeRunResult()),
    )
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))

    assert output.notes == "Deterministic complete."
    assert deps.token_counter.input_tokens == 5
    assert deps.token_counter.output_tokens == 1


def test_observational_debate_eval_logs_and_persists(monkeypatch):
    deps = SimpleNamespace(
        session_id="s1",
        memory=FakeMemory(),
        config={"agent": {"eval": {"enabled": True, "observational": True, "synth_timeout_sec": 42}}},
    )
    captured: dict[str, object] = {}

    def fake_evaluate_case_bundle(bundle, *, synth_judge=None):
        captured["bundle"] = bundle
        captured["judged"] = synth_judge({"session_id": bundle["session_id"]})
        return {
            "total_score": 0.62,
            "action": "recommended_improvements",
            "nginx_score": 0.4,
            "rhel_score": 0.7,
            "synthesizer_score": 1.0,
            "findings": [
                {
                    "agent": "nginx",
                    "rule_id": "nginx.fd_capacity",
                    "severity": "fail",
                    "correction": "Set worker_rlimit_nofile to 135168.",
                },
                {"agent": "rhel", "rule_id": "rhel.sysctl_range", "severity": "warn"},
                {
                    "agent": "synthesizer",
                    "rule_id": "synthesizer.critical_omission",
                    "severity": "fail",
                    "message": "Synthesizer omitted the critical nginx FD recommendation.",
                },
            ],
        }

    def fake_llm_synth_judge(model, payload, *, timeout_sec=0):
        captured["model"] = model
        captured["payload"] = payload
        captured["timeout_sec"] = timeout_sec
        return {"hallucination": {"pass": True}}

    monkeypatch.setattr("core.eval_harness.evaluate_case_bundle", fake_evaluate_case_bundle)
    monkeypatch.setattr("core.eval_harness.llm_synth_judge", fake_llm_synth_judge)
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(diagnosis_agent, "tool_result", lambda tool, msg: logged.append((tool, msg)))

    result = _run_observational_debate_eval(
        deps,
        "model",
        iteration=1,
        inspection={"system": {"os_cpu_count": 112, "ram_gb": 502}},
        service_analysis={"summary": "n"},
        rhel_analysis={"summary": "r"},
        synthesis={"summary": "s"},
    )

    assert result["action"] == "recommended_improvements"
    assert result["by_agent"]["nginx"]["verdict"] == "fail"
    assert result["by_agent"]["rhel"]["verdict"] == "warning"
    assert result["by_agent"]["synthesizer"]["verdict"] == "fail"
    assert result["by_agent"]["nginx"]["severity_counts"] == "1 fail"
    assert result["by_agent"]["rhel"]["severity_counts"] == "1 warn"
    assert result["by_agent"]["nginx"]["corrections"] == "Set worker_rlimit_nofile to 135168."
    assert "critical nginx FD recommendation" in result["by_agent"]["synthesizer"]["details"]
    assert captured["model"] == "model"
    assert captured["timeout_sec"] == 42
    assert captured["bundle"]["requested_format"] == "json"
    assert any(tool == "eval" and "score=0.62" in msg for tool, msg in logged)
    assert any(tool == "eval" and "nginx score=0.40" in msg and "counts=1 fail" in msg for tool, msg in logged)
    assert any(tool == "eval" and "rhel score=0.70" in msg and "counts=1 warn" in msg for tool, msg in logged)
    assert any(tool == "eval" and "synthesizer score=1.00" in msg and "counts=1 fail" in msg for tool, msg in logged)
    assert any(tool == "eval" and "nginx corrections=Set worker_rlimit_nofile to 135168." in msg for tool, msg in logged)
    assert any(tool == "eval" and "synthesizer details=Synthesizer omitted the critical nginx FD recommendation." in msg for tool, msg in logged)
    assert any(row[2] == "iter1_debate_eval" for row in deps.memory.saved)


def test_coerce_records_drops_malformed_synthesized_items():
    deps = SimpleNamespace(config={"agent": {"debug_planner_payloads": False}})

    records = diagnosis_agent._coerce_records(
        [
            {"symptom": "", "root_cause": "missing symptom"},
            {"symptom": "High p99", "root_cause": "backlog too low", "confidence": 0.8},
        ],
        deps=deps,
    )

    assert len(records) == 1
    assert records[0]["symptom"] == "High p99"


def test_coerce_recommendations_drops_empty_changes():
    deps = SimpleNamespace(config={"agent": {"debug_planner_payloads": False}})

    recommendations = diagnosis_agent._coerce_recommendations(
        [
            {"title": "bad", "scope": "nginx", "changes": {}},
            {"title": "good", "scope": "nginx", "changes": {"worker_connections": "65536"}},
        ],
        deps=deps,
    )

    assert len(recommendations) == 1
    assert recommendations[0]["title"] == "good"


def test_coerce_recommendations_accepts_nginx_directive_value_shape():
    recommendations = diagnosis_agent._coerce_recommendations(
        [
            {
                "directive": "worker_connections",
                "value": "65536",
                "justification": "Raise concurrency ceiling",
            }
        ]
    )

    assert len(recommendations) == 1
    assert recommendations[0]["changes"] == {"worker_connections": "65536"}
    assert recommendations[0]["scope"] == "nginx"
    assert recommendations[0]["rationale"] == "Raise concurrency ceiling"


def test_coerce_recommendations_extracts_system_changes_from_commands():
    recommendations = diagnosis_agent._coerce_recommendations(
        [
            {
                "action": "Increase socket backlog limits",
                "commands": [
                    "sysctl -w net.core.somaxconn=65535",
                    "sysctl -w net.ipv4.tcp_max_syn_backlog=65535",
                    "echo never > /sys/kernel/mm/transparent_hugepage/enabled",
                    "setenforce 0",
                ],
                "justification": "Reduce queue overflow",
            }
        ]
    )

    assert recommendations == [
        {
            "title": "Increase socket backlog limits",
            "recommendation": "Increase socket backlog limits",
            "rationale": "Reduce queue overflow",
            "expected_benefit": "no expected benefit",
            "risk_level": "medium",
            "validation": "manual verification required",
            "scope": "system",
            "changes": {
                "net.core.somaxconn": "65535",
                "net.ipv4.tcp_max_syn_backlog": "65535",
                "transparent_hugepage": "never",
                "selinux": "permissive",
            },
        }
    ]


def test_coerce_records_accepts_issue_and_cause_shapes():
    records = diagnosis_agent._coerce_records(
        [
            {"issue": "worker_connections set to 1024", "impact": "Queues requests under load."},
            {"cause": "Network backlog too low", "impact": "SYN drops raise p99 latency."},
        ]
    )

    assert records == [
        {
            "symptom": "worker_connections set to 1024",
            "root_cause": "Queues requests under load.",
            "confidence": 0.0,
            "recommendation": "Queues requests under load.",
            "evidence": [],
        },
        {
            "symptom": "Network backlog too low",
            "root_cause": "SYN drops raise p99 latency.",
            "confidence": 0.0,
            "recommendation": "SYN drops raise p99 latency.",
            "evidence": [],
        },
    ]


def test_coerce_recommendations_accepts_type_setting_value_shape():
    recommendations = diagnosis_agent._coerce_recommendations(
        [
            {
                "type": "nginx",
                "description": "Increase worker_connections to handle more simultaneous connections.",
                "setting": "worker_connections",
                "value": "65536",
                "rationale": "Matches the high core count and prevents request queuing.",
            }
        ]
    )

    assert recommendations == [
        {
            "title": "Increase worker_connections to handle more simultaneous connections.",
            "recommendation": "Increase worker_connections to handle more simultaneous connections.",
            "rationale": "Matches the high core count and prevents request queuing.",
            "expected_benefit": "no expected benefit",
            "risk_level": "medium",
            "validation": "manual verification required",
            "scope": "nginx",
            "changes": {"worker_connections": "65536"},
        }
    ]


def test_coerce_recommendations_accepts_setting_value_lists():
    recommendations = diagnosis_agent._coerce_recommendations(
        [
            {
                "type": "nginx",
                "description": "Set open_file_cache_valid and open_file_cache_min_uses.",
                "setting": ["open_file_cache_valid", "open_file_cache_min_uses"],
                "value": ["30s", "2"],
                "rationale": "Caches only frequently used files.",
            }
        ]
    )

    assert recommendations == [
        {
            "title": "Set open_file_cache_valid and open_file_cache_min_uses.",
            "recommendation": "Set open_file_cache_valid and open_file_cache_min_uses.",
            "rationale": "Caches only frequently used files.",
            "expected_benefit": "no expected benefit",
            "risk_level": "medium",
            "validation": "manual verification required",
            "scope": "nginx",
            "changes": {
                "open_file_cache_valid": "30s",
                "open_file_cache_min_uses": "2",
            },
        }
    ]


def test_run_applies_saved_recommendations(monkeypatch):
    deps = _ctx().deps
    deps.config["agent"]["planner_mode"] = "custom"

    class FakeRunResult:
        output = "Plan complete."

        def usage(self):
            return SimpleNamespace(input_tokens=3, output_tokens=2)

        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "service_applied": False,
            "system_applied": False,
            "after_rps": 0.0,
            "findings": [],
            "recommendations": [{"title": "x"}],
        }

        async def run(self, prompt, deps):
            return FakeRunResult()

        def _apply_from_recommendations(self, deps):
            self._slaymetrics_state["service_applied"] = True
            self._slaymetrics_state["system_applied"] = True
            self._slaymetrics_state["after_rps"] = 160.0
            self._slaymetrics_state["findings"] = [{"parameter": "nginx.sendfile"}]
            self._slaymetrics_state["recommendations"] = [{"title": "Raise connection limits"}]
            return {}

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))

    assert output.service_applied is True
    assert output.system_applied is True
    assert output.after_rps == 160.0
    assert output.recommendations[0]["title"] == "Raise connection limits"


def test_apply_from_recommendations_saves_findings_without_benchmark():
    """apply_saved_recommendations_impl applies changes and saves findings
    without running a benchmark — the iteration loop handles benchmarking."""
    ctx = _ctx()
    agent = build("model", config=_TEST_CONFIG)
    agent._slaymetrics_state["apply_plan"] = {
        "webserver": {"sendfile": "on"},
        "kernel": {},
    }
    agent._slaymetrics_state["inspection"] = {
        "webserver": {"current": {"sendfile": "off"}},
        "kernel": {"current": {}},
    }

    result = agent._apply_from_recommendations(ctx.deps)

    assert "results" in result
    assert "findings" in result
    assert any(fact["type"] == "fix" for fact in ctx.deps.memory.saved_facts)
