"""Tests for pure helper functions and impl methods in agents/agent.py.

Targets uncovered lines: _extract_changes_from_action, _extract_changes_from_commands,
_coerce_records, _coerce_recommendations, _normalize_synthesized_recommendation,
_coerce_float, _coerce_notes, _sanitize_debug_text, _extract_final_text,
_aggregate_usage, _save_planner_artifact, _extract_json_dict, _resolve_model_name,
_hypothesis_enabled, inspect_nginx_impl, inspect_system_tuning,
apply_system_impl, apply_from_recommendations, save_rca_impl, and more.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import agents.agent as diagnosis_agent
from agents import TokenCounter
from agents.agent import (
    DiagnosisOutput,
    _aggregate_usage,
    _coerce_float,
    _coerce_notes,
    _coerce_recommendations,
    _coerce_records,
    _extract_final_text,
    _normalize_synthesized_recommendation,
    _sanitize_debug_text,
    _extract_json_dict,
    _resolve_model_name,
    _extract_changes_from_action,
    _extract_changes_from_commands,
    _save_planner_artifact,
    _hypothesis_enabled,
    build,
)
from adapters.base import BenchmarkResult
from tools.ssh import SSHResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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
            return [{"content": json.dumps({"rps": 100.0, "p99": 2.0, "error_rate": 0.0})}]
        if source_prefix == "baseline:":
            return [
                {
                    "source": "baseline:series",
                    "content": json.dumps({
                        "summary": {"run_queue_max": 5, "rx_drop_delta": 10, "rx_drop_rate_per_sec": 2.5},
                        "last_sample": {"tcp_established": 1200},
                    }),
                }
            ]
        return []

    def semantic_search(self, symptom, session_id, top_k=5):
        return [{"symptom": symptom}]


class FakeAdapter:
    def __init__(self):
        self.applied: list[tuple[str, str]] = []
        self.ALLOWED_BATCH_DIRECTIVES = {
            "sendfile", "keepalive_requests", "tcp_nodelay",
            "open_file_cache_valid", "open_file_cache_min_uses",
        }

    def apply_config(self, parameter: str, value: str) -> bool:
        self.applied.append((parameter, value))
        return True

    def reload(self) -> bool:
        return True

    def benchmark(self, duration: int = 30, url: str = "") -> BenchmarkResult:
        return BenchmarkResult(150.0, 1.0, 1.5, 0.0, duration, url=url, cpu_pct=10.0, mem_mb=20.0)


class FakeSSH:
    """SSH mock that returns plausible output for inspect commands."""
    def __init__(self, outputs=None):
        self.files = {"/etc/nginx/nginx.conf": "good config\n"}
        self._outputs = outputs or {}

    def execute(self, command: str, timeout: int | None = None) -> SSHResult:
        del timeout
        # Return custom outputs if configured
        for pattern, result in self._outputs.items():
            if pattern in command:
                return result
        if command.startswith("cp "):
            parts = command.split()
            if len(parts) >= 3:
                self.files[parts[2]] = self.files.get(parts[1], "")
            return SSHResult("", "", 0)
        if command == "nginx -t 2>&1":
            return SSHResult("syntax is ok\ntest is successful\n", "", 0)
        if command.startswith("nginx -T"):
            return SSHResult(
                "worker_connections 1024;\n"
                "worker_rlimit_nofile 65535;\n"
                "access_log /var/log/nginx/access.log main;\n"
                "tcp_nodelay on;\n"
                "listen 80 backlog=511;\n",
                "", 0,
            )
        if command.startswith("sysctl -n"):
            key = command.split("sysctl -n ")[1].split()[0]
            defaults = {
                "net.core.somaxconn": "4096",
                "net.ipv4.tcp_max_syn_backlog": "1024",
                "net.core.netdev_max_backlog": "1000",
                "net.ipv4.tcp_tw_reuse": "0",
                "net.ipv4.tcp_max_tw_buckets": "65536",
                "net.ipv4.ip_local_port_range": "32768\t60999",
                "net.core.rmem_max": "212992",
                "net.core.wmem_max": "212992",
            }
            return SSHResult(defaults.get(key, "0") + "\n", "", 0)
        if "transparent_hugepage/enabled" in command and "echo" not in command:
            return SSHResult("[always] madvise never\n", "", 0)
        if "getenforce" in command:
            return SSHResult("Enforcing\n", "", 0)
        if "scaling_governor" in command and "echo" not in command:
            return SSHResult("powersave\n", "", 0)
        if "ulimit -Sn" in command:
            return SSHResult("1024\n", "", 0)
        if "irqbalance" in command and "systemctl is-active" in command:
            return SSHResult("inactive\n", "", 0)
        if command.startswith("sysctl -w"):
            return SSHResult("", "", 0)
        if "setenforce" in command:
            return SSHResult("", "", 0)
        if "tee" in command and "scaling_governor" in command:
            return SSHResult("", "", 0)
        if "echo" in command and "transparent_hugepage" in command:
            return SSHResult("", "", 0)
        if "systemctl" in command:
            return SSHResult("", "", 0)
        if "sed -i" in command or "limits.conf" in command or "daemon-reload" in command:
            return SSHResult("", "", 0)
        if "cat /proc/interrupts" in command:
            return SSHResult("", "", 0)
        if "ps -eo" in command:
            return SSHResult("", "", 0)
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
            "service": {
                "benchmark": {"duration": 30, "small_file_url": "http://localhost/test"},
                "config_path": "/etc/nginx/nginx.conf",
            },
        },
    )
    return SimpleNamespace(deps=deps)


# ===========================================================================
# _coerce_float
# ===========================================================================

def test_coerce_float_none():
    assert _coerce_float(None) == 0.0

def test_coerce_float_empty_string():
    assert _coerce_float("") == 0.0

def test_coerce_float_bool_true():
    assert _coerce_float(True) == 1.0

def test_coerce_float_bool_false():
    assert _coerce_float(False) == 0.0

def test_coerce_float_int():
    assert _coerce_float(42) == 42.0

def test_coerce_float_string():
    assert _coerce_float("3.14") == 3.14

def test_coerce_float_string_with_spaces():
    assert _coerce_float("  99.5 ") == 99.5

def test_coerce_float_invalid_string():
    assert _coerce_float("not a number") == 0.0

def test_coerce_float_list():
    assert _coerce_float([1, 2]) == 0.0


# ===========================================================================
# _coerce_notes
# ===========================================================================

def test_coerce_notes_none():
    assert _coerce_notes(None) == ""

def test_coerce_notes_string():
    assert _coerce_notes("hello") == "hello"

def test_coerce_notes_list():
    result = _coerce_notes(["a", "b"])
    assert result == '["a", "b"]'

def test_coerce_notes_dict():
    result = _coerce_notes({"key": "val"})
    assert result == '{"key": "val"}'

def test_coerce_notes_int():
    assert _coerce_notes(42) == "42"


# ===========================================================================
# _sanitize_debug_text
# ===========================================================================

def test_sanitize_debug_text_normalizes_unicode():
    result = _sanitize_debug_text("hello\u2011world\u2013foo\u2014bar")
    # \u2013 and \u2014 are replaced by "-", \u2011 is NFKC-normalized
    assert "world-foo-bar" in result
    assert "\u2013" not in result
    assert "\u2014" not in result

def test_sanitize_debug_text_replaces_narrow_nbsp():
    result = _sanitize_debug_text("hello\u202fworld\xa0end")
    assert result == "hello world end"

def test_sanitize_debug_text_collapses_whitespace():
    result = _sanitize_debug_text("hello   \n  world")
    assert result == "hello world"

def test_sanitize_debug_text_truncates():
    result = _sanitize_debug_text("a" * 5000, limit=100)
    assert len(result) == 100
    assert result.endswith("...")

def test_sanitize_debug_text_short_no_truncation():
    result = _sanitize_debug_text("short text")
    assert result == "short text"


# ===========================================================================
# _extract_final_text
# ===========================================================================

def test_extract_final_text_from_string_content():
    msg = SimpleNamespace(content="Final answer")
    assert _extract_final_text([msg]) == "Final answer"

def test_extract_final_text_from_list_content():
    msg = SimpleNamespace(content=[{"type": "text", "text": "Part A"}, {"type": "text", "text": " Part B"}])
    assert _extract_final_text([msg]) == "Part A Part B"

def test_extract_final_text_empty_messages():
    assert _extract_final_text([]) == ""

def test_extract_final_text_skips_empty_content():
    msg1 = SimpleNamespace(content="")
    msg2 = SimpleNamespace(content="Real answer")
    assert _extract_final_text([msg2, msg1]) == "Real answer"

def test_extract_final_text_list_content_no_text_type():
    msg = SimpleNamespace(content=[{"type": "image", "url": "http://example.com"}])
    result = _extract_final_text([msg])
    # Falls through to empty since no text parts
    assert result == ""

def test_extract_final_text_picks_last_non_empty():
    msg1 = SimpleNamespace(content="First")
    msg2 = SimpleNamespace(content="  ")
    msg3 = SimpleNamespace(content="Third")
    assert _extract_final_text([msg1, msg2, msg3]) == "Third"


# ===========================================================================
# _aggregate_usage
# ===========================================================================

def test_aggregate_usage_no_messages():
    result = _aggregate_usage([])
    assert result == {"input_tokens": 0, "output_tokens": 0}

def test_aggregate_usage_with_input_output_tokens():
    msg1 = SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 5})
    msg2 = SimpleNamespace(usage_metadata={"input_tokens": 20, "output_tokens": 15})
    result = _aggregate_usage([msg1, msg2])
    assert result == {"input_tokens": 30, "output_tokens": 20}

def test_aggregate_usage_with_prompt_completion_tokens():
    msg = SimpleNamespace(usage_metadata={"prompt_tokens": 8, "completion_tokens": 3})
    result = _aggregate_usage([msg])
    assert result == {"input_tokens": 8, "output_tokens": 3}

def test_aggregate_usage_skips_messages_without_usage():
    msg1 = SimpleNamespace(usage_metadata=None)
    msg2 = SimpleNamespace(usage_metadata={"input_tokens": 5, "output_tokens": 2})
    result = _aggregate_usage([msg1, msg2])
    assert result == {"input_tokens": 5, "output_tokens": 2}

def test_aggregate_usage_empty_dict():
    msg = SimpleNamespace(usage_metadata={})
    result = _aggregate_usage([msg])
    assert result == {"input_tokens": 0, "output_tokens": 0}

def test_aggregate_usage_with_input_token_count():
    msg = SimpleNamespace(usage_metadata={"input_token_count": 12, "output_token_count": 6})
    result = _aggregate_usage([msg])
    assert result == {"input_tokens": 12, "output_tokens": 6}


# ===========================================================================
# _extract_json_dict
# ===========================================================================

def test_extract_json_dict_empty():
    assert _extract_json_dict("") == {}

def test_extract_json_dict_valid():
    assert _extract_json_dict('{"key": "value"}') == {"key": "value"}

def test_extract_json_dict_with_fences():
    text = '```json\n{"key": "value"}\n```'
    assert _extract_json_dict(text) == {"key": "value"}

def test_extract_json_dict_no_braces():
    assert _extract_json_dict("just plain text") == {}

def test_extract_json_dict_invalid_json():
    assert _extract_json_dict("{invalid json}") == {}

def test_extract_json_dict_returns_empty_for_list():
    assert _extract_json_dict("[1, 2, 3]") == {}

def test_extract_json_dict_extracts_from_surrounding_text():
    text = 'Here is the result: {"answer": 42} end.'
    assert _extract_json_dict(text) == {"answer": 42}

def test_extract_json_dict_fences_without_newline():
    text = '```{"key": "val"}```'
    assert _extract_json_dict(text) == {"key": "val"}


# ===========================================================================
# _resolve_model_name
# ===========================================================================

def test_resolve_model_name_from_model_name_attr():
    model = SimpleNamespace(model_name="gpt-4")
    assert _resolve_model_name(model) == "gpt-4"

def test_resolve_model_name_from_model_attr():
    model = SimpleNamespace(model="granite-8b")
    assert _resolve_model_name(model) == "granite-8b"

def test_resolve_model_name_fallback_class_name():
    class MyModel:
        pass
    assert _resolve_model_name(MyModel()) == "MyModel"

def test_resolve_model_name_skips_empty():
    model = SimpleNamespace(model_name="", model="  ", _model="real-model")
    assert _resolve_model_name(model) == "real-model"


# ===========================================================================
# _extract_changes_from_action
# ===========================================================================

def test_extract_action_set_directive():
    result = _extract_changes_from_action("Set worker_connections 65536", "nginx")
    assert result == {"worker_connections": "65536"}

def test_extract_action_enable():
    result = _extract_changes_from_action("Enable tcp_nodelay", "nginx")
    assert result == {"tcp_nodelay": "on"}

def test_extract_action_disable():
    result = _extract_changes_from_action("Disable access_log", "nginx")
    assert result == {"access_log": "off"}

def test_extract_action_aio_threads():
    result = _extract_changes_from_action("Set aio threads", "nginx")
    assert result == {"aio": "threads"}

def test_extract_action_no_scope():
    result = _extract_changes_from_action("Set keepalive_requests 10000", "")
    assert result == {"keepalive_requests": "10000"}

def test_extract_action_system_scope_no_match():
    result = _extract_changes_from_action("Set worker_connections 65536", "system")
    # System scope should not match nginx-style action
    assert result == {}

def test_extract_action_configure():
    result = _extract_changes_from_action("Configure sendfile on", "nginx")
    assert result == {"sendfile": "on"}

def test_extract_action_reduce():
    result = _extract_changes_from_action("Reduce keepalive_timeout 30", "nginx")
    assert result == {"keepalive_timeout": "30"}

def test_extract_action_worker_cpu_affinity_auto():
    result = _extract_changes_from_action("Enable worker_cpu_affinity auto", "nginx")
    # "Enable" with value "auto"
    assert result == {"worker_cpu_affinity": "auto"}

def test_extract_action_no_match():
    result = _extract_changes_from_action("Do something weird", "nginx")
    assert result == {}


# ===========================================================================
# _extract_changes_from_commands
# ===========================================================================

def test_extract_commands_sysctl():
    result = _extract_changes_from_commands(["sysctl -w net.core.somaxconn=65535"])
    assert result == {"net.core.somaxconn": "65535"}

def test_extract_commands_transparent_hugepage():
    result = _extract_changes_from_commands(
        ["echo never > /sys/kernel/mm/transparent_hugepage/enabled"]
    )
    assert result == {"transparent_hugepage": "never"}

def test_extract_commands_setenforce():
    result = _extract_changes_from_commands(["setenforce 0"])
    assert result == {"selinux": "permissive"}

def test_extract_commands_setenforce_enforcing():
    result = _extract_changes_from_commands(["setenforce 1"])
    assert result == {"selinux": "enforcing"}

def test_extract_commands_ulimit():
    result = _extract_changes_from_commands(["ulimit -n 65536"])
    assert result == {"nofile": "65536"}

def test_extract_commands_nofile_limits_conf():
    result = _extract_changes_from_commands(
        ["echo '* soft nofile 65536' >> /etc/security/limits.conf"]
    )
    assert result == {"nofile": "65536"}

def test_extract_commands_irqbalance():
    result = _extract_changes_from_commands(["systemctl enable --now irqbalance"])
    assert result == {"irqbalance": "enabled"}

def test_extract_commands_empty_list():
    result = _extract_changes_from_commands([])
    assert result == {}

def test_extract_commands_empty_string():
    result = _extract_changes_from_commands([""])
    assert result == {}

def test_extract_commands_mixed():
    result = _extract_changes_from_commands([
        "sysctl -w net.core.somaxconn=65535",
        "setenforce 0",
        "echo never > /sys/kernel/mm/transparent_hugepage/enabled",
    ])
    assert result == {
        "net.core.somaxconn": "65535",
        "selinux": "permissive",
        "transparent_hugepage": "never",
    }

def test_extract_commands_tcp_tw_reuse():
    result = _extract_changes_from_commands(
        ["sysctl -w net.ipv4.tcp_tw_reuse=1"]
    )
    assert result == {"net.ipv4.tcp_tw_reuse": "1"}

def test_extract_commands_sysctl_quoted():
    result = _extract_changes_from_commands(
        ['sysctl -w net.ipv4.ip_local_port_range="1024 65535"']
    )
    assert result == {"net.ipv4.ip_local_port_range": "1024 65535"}

def test_extract_commands_irqbalance_start():
    result = _extract_changes_from_commands(["systemctl start irqbalance"])
    assert result == {"irqbalance": "enabled"}

def test_extract_commands_unrecognized():
    result = _extract_changes_from_commands(["ls -la /tmp"])
    assert result == {}


# ===========================================================================
# _normalize_synthesized_recommendation
# ===========================================================================

def test_normalize_with_changes_already_set():
    item = {"title": "test", "changes": {"sendfile": "on"}}
    result = _normalize_synthesized_recommendation(item)
    assert result["changes"] == {"sendfile": "on"}

def test_normalize_setting_value_single():
    item = {"setting": "worker_connections", "value": "65536", "description": "Raise connections"}
    result = _normalize_synthesized_recommendation(item)
    assert result["changes"] == {"worker_connections": "65536"}
    assert result["title"] == "Raise connections"

def test_normalize_setting_value_lists():
    item = {
        "setting": ["somaxconn", "backlog"],
        "value": ["65535", "65535"],
        "description": "Raise limits",
    }
    result = _normalize_synthesized_recommendation(item)
    assert result["changes"] == {"somaxconn": "65535", "backlog": "65535"}

def test_normalize_directive_value():
    item = {"directive": "worker_connections", "value": "65536"}
    result = _normalize_synthesized_recommendation(item)
    assert result["changes"] == {"worker_connections": "65536"}
    assert result["scope"] == "nginx"

def test_normalize_action_based():
    item = {"action": "Set keepalive_requests 10000"}
    result = _normalize_synthesized_recommendation(item)
    assert result["changes"] == {"keepalive_requests": "10000"}

def test_normalize_command_based():
    item = {"command": "sysctl -w net.core.somaxconn=65535"}
    result = _normalize_synthesized_recommendation(item)
    assert result["changes"] == {"net.core.somaxconn": "65535"}
    assert result["scope"] == "system"

def test_normalize_commands_list_based():
    item = {
        "action": "Tune kernel",
        "commands": [
            "sysctl -w net.core.somaxconn=65535",
            "setenforce 0",
        ],
    }
    result = _normalize_synthesized_recommendation(item)
    assert result["changes"] == {"net.core.somaxconn": "65535", "selinux": "permissive"}
    assert result["scope"] == "system"

def test_normalize_type_sets_scope():
    item = {"type": "system", "setting": "net.core.somaxconn", "value": "65535"}
    result = _normalize_synthesized_recommendation(item)
    assert result["scope"] == "system"

def test_normalize_type_irq_sets_system_scope():
    item = {"type": "irq", "setting": "irqbalance", "value": "active"}
    result = _normalize_synthesized_recommendation(item)
    assert result["scope"] == "system"

def test_normalize_no_extractable_changes():
    item = {"title": "something", "description": "no changes here"}
    result = _normalize_synthesized_recommendation(item)
    assert result.get("changes") is None or result.get("changes") == {}

def test_normalize_justification_fallback():
    item = {"setting": "sendfile", "value": "on", "justification": "faster I/O"}
    result = _normalize_synthesized_recommendation(item)
    assert result["rationale"] == "faster I/O"


# ===========================================================================
# _hypothesis_enabled
# ===========================================================================

def test_hypothesis_enabled_explicit_true():
    deps = SimpleNamespace(config={"agent": {"persist_hypotheses": True}})
    assert _hypothesis_enabled(deps) is True

def test_hypothesis_enabled_explicit_false():
    deps = SimpleNamespace(config={"agent": {"persist_hypotheses": False}})
    assert _hypothesis_enabled(deps) is False

def test_hypothesis_enabled_fallback_to_debug():
    deps = SimpleNamespace(config={"agent": {"debug_planner_payloads": True}})
    assert _hypothesis_enabled(deps) is True

def test_hypothesis_enabled_no_config():
    deps = SimpleNamespace(config={})
    assert _hypothesis_enabled(deps) is False

def test_hypothesis_enabled_none_config():
    deps = SimpleNamespace(config=None)
    assert _hypothesis_enabled(deps) is False


# ===========================================================================
# _save_planner_artifact
# ===========================================================================

def test_save_planner_artifact_stores_to_memory():
    ctx = _ctx()
    _save_planner_artifact(ctx.deps, "synthesizer", {"summary": "All clear", "rca": []})
    saved = [s for s in ctx.deps.memory.saved if s[2] == "synthesizer"]
    assert len(saved) == 1
    assert "synthesizer planner output" in saved[0][4]

def test_save_planner_artifact_file_map(tmp_path, monkeypatch):
    ctx = _ctx()
    ctx.deps.config["agent"]["persist_hypotheses"] = True
    monkeypatch.setattr(
        diagnosis_agent, "_hypothesis_dir",
        lambda deps: Path(tmp_path) / deps.session_id,
    )
    _save_planner_artifact(ctx.deps, "rhel_expert", {"summary": "Kernel OK"})
    saved_file = tmp_path / "s1" / "02_rhel_expert.md"
    assert saved_file.exists()
    text = saved_file.read_text()
    assert "Rhel Expert" in text
    assert "Kernel OK" in text

def test_save_planner_artifact_unknown_source(tmp_path, monkeypatch):
    ctx = _ctx()
    ctx.deps.config["agent"]["persist_hypotheses"] = True
    monkeypatch.setattr(
        diagnosis_agent, "_hypothesis_dir",
        lambda deps: Path(tmp_path) / deps.session_id,
    )
    _save_planner_artifact(ctx.deps, "custom_planner", {"summary": "Custom"})
    saved_file = tmp_path / "s1" / "custom_planner.md"
    assert saved_file.exists()


# ===========================================================================
# inspect_nginx_impl
# ===========================================================================

def test_inspect_nginx_impl():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["inspect_nginx_config"].function
    result = asyncio.run(tool(ctx))

    assert "needs_fixing" in result
    assert "already_ok" in result
    assert "current" in result
    # worker_connections is 1024, target is 65536 => needs fixing
    assert "worker_connections" in result["needs_fixing"]
    # tcp_nodelay is "on" => should be in already_ok
    assert "tcp_nodelay" in result["already_ok"]
    # access_log is set, target is "off" => needs fixing
    assert "access_log" in result["needs_fixing"]
    # listen_backlog parsed from "backlog=511"
    assert result["current"]["listen_backlog"] == "511"


# ===========================================================================
# inspect_system_impl (inspect_system_tuning)
# ===========================================================================

def test_inspect_system_impl():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["inspect_system_tuning"].function
    result = asyncio.run(tool(ctx))

    assert "needs_fixing" in result
    assert "already_ok" in result
    assert "current" in result
    # net.core.somaxconn is 4096, target 65535 => needs fixing
    assert "net.core.somaxconn" in result["needs_fixing"]
    # transparent_hugepage is "always", target "never" => needs fixing
    assert "transparent_hugepage" in result["needs_fixing"]
    # selinux is "enforcing", target "permissive" => needs fixing
    assert "selinux" in result["needs_fixing"]


# ===========================================================================
# apply_system_impl
# ===========================================================================

def test_apply_system_sysctl():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"net.core.somaxconn": "65535"}))
    assert result["applied"]["net.core.somaxconn"] == "65535"
    assert result["failed"] == {}

def test_apply_system_transparent_hugepage():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"transparent_hugepage": "never"}))
    assert result["applied"]["transparent_hugepage"] == "never"

def test_apply_system_selinux():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"selinux": "permissive"}))
    assert result["applied"]["selinux"] == "permissive"

def test_apply_system_cpu_governor():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"cpu_governor": "performance"}))
    assert result["applied"]["cpu_governor"] == "performance"

def test_apply_system_ip_local_port_range():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"net.ipv4.ip_local_port_range": "1024 65535"}))
    assert result["applied"]["net.ipv4.ip_local_port_range"] == "1024 65535"

def test_apply_system_nofile():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"nofile": "65536"}))
    assert result["applied"]["nofile"] == "65536"

def test_apply_system_irqbalance():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"irqbalance": "active"}))
    assert result["applied"]["irqbalance"] == "active"

def test_apply_system_multiple():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {
        "net.core.somaxconn": "65535",
        "selinux": "permissive",
        "transparent_hugepage": "never",
    }))
    assert len(result["applied"]) == 3
    assert result["failed"] == {}

def test_apply_system_failure():
    ctx = _ctx()
    ctx.deps.ssh = FakeSSH(outputs={
        "sysctl -w net.core.somaxconn": SSHResult("", "error", 1),
    })
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"net.core.somaxconn": "65535"}))
    assert "net.core.somaxconn" in result["failed"]

def test_apply_system_thp_failure():
    ssh = FakeSSH()
    ssh._outputs["transparent_hugepage/enabled"] = SSHResult("", "readonly fs", 1)
    ctx = _ctx()
    ctx.deps.ssh = ssh
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"transparent_hugepage": "never"}))
    assert "transparent_hugepage" in result["failed"]

def test_apply_system_irqbalance_failure():
    ssh = FakeSSH()
    ssh._outputs["systemctl enable --now irqbalance"] = SSHResult("", "failed", 1)
    ctx = _ctx()
    ctx.deps.ssh = ssh
    agent = build("model")
    tool = agent._function_toolset.tools["apply_system_tuning"].function
    result = asyncio.run(tool(ctx, {"irqbalance": "active"}))
    assert "irqbalance" in result["failed"]


# ===========================================================================
# save_rca_impl
# ===========================================================================

def test_save_rca_normalizes_evidence_string():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["save_rca"].function
    asyncio.run(tool(ctx, [
        {
            "symptom": "High latency",
            "root_cause": "backlog low",
            "confidence": "0.8",
            "recommendation": "raise",
            "evidence": "single string evidence",
        }
    ]))
    state = agent._slaymetrics_state
    assert state["rca_records"][0]["evidence"] == ["single string evidence"]

def test_save_rca_normalizes_evidence_non_list():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["save_rca"].function
    asyncio.run(tool(ctx, [
        {
            "symptom": "High p99",
            "root_cause": "somaxconn",
            "confidence": 0.7,
            "evidence": 42,
        }
    ]))
    state = agent._slaymetrics_state
    assert state["rca_records"][0]["evidence"] == ["42"]

def test_save_rca_defaults_for_missing_fields():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["save_rca"].function
    asyncio.run(tool(ctx, [{}]))
    state = agent._slaymetrics_state
    rec = state["rca_records"][0]
    assert rec["symptom"] == "unknown symptom"
    assert rec["root_cause"] == "unknown root cause"
    assert rec["recommendation"] == "no recommendation"
    assert rec["confidence"] == 0.0

def test_save_rca_truncates_evidence():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["save_rca"].function
    asyncio.run(tool(ctx, [
        {
            "symptom": "problem",
            "root_cause": "cause",
            "evidence": ["a", "b", "c", "d", "e", "f", "g", "h"],
        }
    ]))
    state = agent._slaymetrics_state
    assert len(state["rca_records"][0]["evidence"]) == 6


# ===========================================================================
# save_recommendations_impl — invalid scope
# ===========================================================================

def test_save_recommendations_rejects_invalid_scope():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["save_recommendations"].function
    asyncio.run(tool(ctx, [
        {
            "title": "Bad scope",
            "scope": "database",
            "changes": {"worker_connections": "65536"},
        }
    ]))
    state = agent._slaymetrics_state
    assert state["recommendations"] == []
    rejected = [s for s in ctx.deps.memory.saved if "rejected" in str(s[2])]
    assert len(rejected) == 1

def test_save_recommendations_rejects_invalid_scope_with_debug(monkeypatch):
    ctx = _ctx()
    ctx.deps.config["agent"]["debug_planner_payloads"] = True
    debug_lines = []
    monkeypatch.setattr(diagnosis_agent, "tool_result", lambda t, m: debug_lines.append((t, m)))
    agent = build("model")
    tool = agent._function_toolset.tools["save_recommendations"].function
    asyncio.run(tool(ctx, [
        {
            "title": "Bad scope",
            "scope": "database",
            "changes": {"worker_connections": "65536"},
        }
    ]))
    assert any("reject invalid scope" in m for _, m in debug_lines)


# ===========================================================================
# DiagnosisOutput edge cases
# ===========================================================================

def test_diagnosis_output_defaults():
    output = DiagnosisOutput(nginx_applied=False, system_applied=False)
    assert output.after_rps == 0.0
    assert output.improvement_pct == 0.0
    assert output.notes == ""
    assert output.rca_records == []
    assert output.recommendations == []

def test_diagnosis_output_none_rca():
    output = DiagnosisOutput(
        nginx_applied=True, system_applied=False,
        rca_records=None, recommendations=None,
    )
    assert output.rca_records == []
    assert output.recommendations == []


# ===========================================================================
# _coerce_records edge cases
# ===========================================================================

def test_coerce_records_non_list():
    assert _coerce_records("not a list") == []
    assert _coerce_records(None) == []

def test_coerce_records_drops_non_dict():
    result = _coerce_records(["string item", 42])
    assert result == []

def test_coerce_records_with_debug_drops_non_dict(monkeypatch):
    debug_lines = []
    monkeypatch.setattr(diagnosis_agent, "tool_result", lambda t, m: debug_lines.append((t, m)))
    deps = SimpleNamespace(config={"agent": {"debug_planner_payloads": True}})
    result = _coerce_records(["not a dict"], deps=deps)
    assert result == []
    assert any("dropped" in m for _, m in debug_lines)

def test_coerce_records_drops_empty_symptom_and_root_cause(monkeypatch):
    debug_lines = []
    monkeypatch.setattr(diagnosis_agent, "tool_result", lambda t, m: debug_lines.append((t, m)))
    deps = SimpleNamespace(config={"agent": {"debug_planner_payloads": True}})
    result = _coerce_records([{"symptom": "", "root_cause": ""}], deps=deps)
    assert result == []


# ===========================================================================
# _coerce_recommendations edge cases
# ===========================================================================

def test_coerce_recommendations_non_list():
    assert _coerce_recommendations("not a list") == []
    assert _coerce_recommendations(None) == []

def test_coerce_recommendations_drops_non_dict(monkeypatch):
    debug_lines = []
    monkeypatch.setattr(diagnosis_agent, "tool_result", lambda t, m: debug_lines.append((t, m)))
    deps = SimpleNamespace(config={"agent": {"debug_planner_payloads": True}})
    result = _coerce_recommendations(["string"], deps=deps)
    assert result == []
    assert any("dropped" in m for _, m in debug_lines)

def test_coerce_recommendations_drops_empty_changes_with_debug(monkeypatch):
    debug_lines = []
    monkeypatch.setattr(diagnosis_agent, "tool_result", lambda t, m: debug_lines.append((t, m)))
    deps = SimpleNamespace(config={"agent": {"debug_planner_payloads": True}})
    result = _coerce_recommendations([{"title": "empty", "changes": {}}], deps=deps)
    assert result == []
    assert any("dropped" in m for _, m in debug_lines)


# ===========================================================================
# apply_from_recommendations (integration)
# ===========================================================================

def test_apply_from_recommendations_nginx_and_system():
    ctx = _ctx()
    agent = build("model")
    agent._slaymetrics_state["recommendations"] = [
        {
            "title": "Enable sendfile",
            "scope": "nginx",
            "changes": {"sendfile": "on"},
            "rationale": "faster I/O",
        },
        {
            "title": "Tune kernel",
            "scope": "system",
            "changes": {"net.core.somaxconn": "65535"},
            "rationale": "raise limit",
        },
    ]
    result = agent._apply_from_recommendations(ctx.deps)
    assert "nginx" in result
    assert "system" in result
    assert "benchmark" in result
    assert "findings" in result

def test_apply_from_recommendations_skips_non_dict_changes():
    ctx = _ctx()
    agent = build("model")
    agent._slaymetrics_state["recommendations"] = [
        {"title": "bad", "scope": "nginx", "changes": "not a dict"},
    ]
    result = agent._apply_from_recommendations(ctx.deps)
    # Should produce no nginx changes
    assert result["nginx"]["reload"] == "SKIPPED"

def test_apply_from_recommendations_empty_list():
    ctx = _ctx()
    agent = build("model")
    agent._slaymetrics_state["recommendations"] = []
    result = agent._apply_from_recommendations(ctx.deps)
    assert result["nginx"]["reload"] == "SKIPPED"


# ===========================================================================
# run() function edge cases
# ===========================================================================

def test_run_generates_notes_from_findings_when_output_empty(monkeypatch):
    deps = _ctx().deps

    class FakeRunResult:
        output = ""
        def usage(self):
            return SimpleNamespace(input_tokens=1, output_tokens=1)
        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "nginx_applied": True,
            "system_applied": False,
            "after_rps": 120.0,
            "findings": [{"parameter": "nginx.sendfile"}, {"parameter": "nginx.access_log"}],
            "rca_records": [],
            "recommendations": [],
        }
        async def run(self, prompt, deps):
            return FakeRunResult()

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))
    assert "Applied findings" in output.notes
    assert "nginx.sendfile" in output.notes

def test_run_generates_default_notes_when_empty_and_no_findings(monkeypatch):
    deps = _ctx().deps

    class FakeRunResult:
        output = ""
        def usage(self):
            return SimpleNamespace(input_tokens=1, output_tokens=1)
        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "nginx_applied": False,
            "system_applied": False,
            "after_rps": 0.0,
            "findings": [],
            "rca_records": [],
            "recommendations": [],
        }
        async def run(self, prompt, deps):
            return FakeRunResult()

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))
    assert output.notes == "Diagnosis completed."

def test_run_appends_guardrail_failure(monkeypatch):
    deps = _ctx().deps

    class FakeRunResult:
        output = "Done."
        def usage(self):
            return SimpleNamespace(input_tokens=1, output_tokens=1)
        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "nginx_applied": False,
            "system_applied": False,
            "after_rps": 0.0,
            "findings": [],
            "rca_records": [],
            "recommendations": [],
            "guardrail_failure": "RPS regressed (90 < 100)",
        }
        async def run(self, prompt, deps):
            return FakeRunResult()

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))
    assert "Guardrail: RPS regressed" in output.notes

def test_run_with_tool_token_rows(monkeypatch):
    deps = _ctx().deps
    deps.token_counter.add_tool_tokens("inspect_nginx", calls=2, call_input=100, call_output=50)

    class FakeRunResult:
        output = "OK"
        def usage(self):
            return SimpleNamespace(input_tokens=10, output_tokens=5)
        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "nginx_applied": False,
            "system_applied": False,
            "after_rps": 0.0,
            "findings": [],
            "rca_records": [],
            "recommendations": [],
        }
        async def run(self, prompt, deps):
            return FakeRunResult()

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    log_calls = []
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: log_calls.append(a))

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))
    assert any("Tool token attribution" in str(a) for a in log_calls)

def test_run_handles_get_profile_exception(monkeypatch):
    deps = _ctx().deps

    def _failing_get_profile(session_id):
        raise RuntimeError("DB down")
    deps.memory.get_profile = _failing_get_profile

    class FakeRunResult:
        output = "Done."
        def usage(self):
            return SimpleNamespace(input_tokens=1, output_tokens=1)
        def all_messages(self):
            return []

    class FakeAgent:
        _slaymetrics_state = {
            "nginx_applied": False,
            "system_applied": False,
            "after_rps": 0.0,
            "findings": [],
            "rca_records": [],
            "recommendations": [],
        }
        async def run(self, prompt, deps):
            return FakeRunResult()

    monkeypatch.setattr(diagnosis_agent, "build", lambda model, config=None: FakeAgent())
    monkeypatch.setattr(diagnosis_agent, "llm_call", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "tokens", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_agent, "log", lambda *a, **k: None)

    output = asyncio.run(diagnosis_agent.run("model", deps, "ctx"))
    assert output.improvement_pct == 0.0


# ===========================================================================
# apply_nginx_impl edge cases
# ===========================================================================

def test_apply_nginx_all_unsupported():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    result = asyncio.run(tool(ctx, {"totally_fake": "1", "another_fake": "2"}))
    assert result["reload"] == "FAILED"
    assert "unsupported nginx directives" in result["error"]

def test_apply_nginx_syntax_check_fails():
    ctx = _ctx()
    ctx.deps.ssh = FakeSSH(outputs={
        "nginx -t 2>&1": SSHResult("nginx: configuration file test failed", "", 1),
    })
    agent = build("model")
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    result = asyncio.run(tool(ctx, {"sendfile": "on"}))
    assert result["reload"] == "FAILED"
    assert "nginx -t failed" in result["error"]

def test_apply_nginx_non_dict_payload():
    ctx = _ctx()
    agent = build("model")
    tool = agent._function_toolset.tools["apply_nginx_tuning"].function
    result = asyncio.run(tool(ctx, [1, 2, 3]))
    assert result["reload"] == "FAILED"
    assert "must be a dictionary" in result["error"]


# ===========================================================================
# _null_generation context manager
# ===========================================================================

def test_null_generation():
    from agents.agent import _null_generation
    with _null_generation() as val:
        assert val is None


# ===========================================================================
# add_messages fallback
# ===========================================================================

def test_add_messages_fallback():
    # The fallback add_messages just concatenates
    from agents.agent import add_messages
    result = add_messages([1, 2], [3, 4])
    assert result == [1, 2, 3, 4]
