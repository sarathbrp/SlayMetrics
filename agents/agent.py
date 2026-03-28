from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict

from adapters.base import BenchmarkResult
from agents import AgentDeps
from core.log import llm_call, log, tokens, tool_call, tool_result
from telemetry import summarize_messages

try:
    from langgraph.graph.message import add_messages
except ModuleNotFoundError:

    def add_messages(left: list[Any], right: list[Any]) -> list[Any]:
        return [*left, *right]


class GraphState(TypedDict):
    messages: Annotated[list, add_messages]


@dataclass
class DiagnosisOutput:
    """Canonical internal diagnosis result built in Python after the graph run."""

    nginx_applied: bool
    system_applied: bool
    after_rps: float = 0.0
    improvement_pct: float = 0.0
    notes: str = ""
    rca_records: list[dict[str, Any]] | None = None
    recommendations: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.after_rps = _coerce_float(self.after_rps)
        self.improvement_pct = _coerce_float(self.improvement_pct)
        self.notes = _coerce_notes(self.notes)
        self.rca_records = list(self.rca_records or [])
        self.recommendations = list(self.recommendations or [])


SYSTEM_PROMPT = """\
You are SlayMetricsAgent.

Diagnostic sequence:
1. Inspect NGINX configuration first.
2. Inspect RHEL/kernel/system tuning second.
3. Inspect IRQ distribution and CPU affinity last.
4. Save structured RCA via save_rca after all three stages are reviewed.
5. Save transparent human-readable recommendations via save_recommendations.
6. Return one short plain-text summary sentence.

Do not benchmark or apply changes during planning. Python will do one combined apply
and one benchmark after planning is complete.

For apply_nginx_tuning and apply_system_tuning:
- Pass a structured object under the changes field
- Example: {"changes":{"access_log":"off","listen_backlog":"65535"}}
- If needed, you may also pass directive names directly as tool arguments instead of nesting them

For save_findings:
- Pass a structured list under the findings field
- Example: {"findings":[{"parameter":"nginx.access_log","before_value":"on","after_value":"off"}]}

For save_rca:
- Pass a structured list under the records field
- Each RCA record must include symptom, root_cause, confidence, recommendation
- evidence may be a list of short strings
- Example: {"records":[{"symptom":"High p99","root_cause":"backlog too low","confidence":0.9}]}

For save_recommendations:
- Pass a structured list under the recommendations field
- Each recommendation must include title, recommendation, rationale,
  expected_benefit, risk_level, validation, scope, changes
- scope must be nginx or system
- changes must be a structured object of parameter -> target value
- risk_level should be low, medium, or high
- Example: {"recommendations":[{"title":"Raise limit","scope":"nginx","changes":{"aio":"threads"}}]}

Use the inspect tool outputs as the source of truth for current values and candidate target values.
Do not invent target values outside the inspect outputs.

Do NOT apply gzip — test files are random binary data, compression wastes CPU.

Rules:
- Stage 1 must focus on NGINX configuration only.
- Stage 2 must focus on RHEL/kernel/system tuning only.
- Stage 3 must focus on IRQ distribution / CPU affinity evidence only.
- Only recommend IRQ-related action if the IRQ stage evidence supports it.
- Skip already-applied fixes, no packages, no reboot, no pre-fix benchmark.
- Do not call apply_nginx_tuning, apply_system_tuning, run_benchmark,
  or save_findings yourself; Python will execute saved recommendations
  after you finish planning.
"""


class DiagnosisWorkflow:
    def __init__(self, model, state: dict[str, Any], test_tools: dict[str, Any], tool_factory):
        self.model = model
        self._slaymetrics_state = state
        self._tool_factory = tool_factory
        self._function_toolset = SimpleNamespace(
            tools={name: SimpleNamespace(function=fn) for name, fn in test_tools.items()}
        )

    async def run(self, context_prompt: str, deps: AgentDeps):
        from langchain_core.messages import HumanMessage, SystemMessage
        from langgraph.graph import END, StateGraph
        from langgraph.prebuilt import ToolNode

        tools = self._tool_factory(deps)
        llm = self.model.bind_tools(tools)

        def call_model(state: GraphState):
            messages = [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
            _lf = getattr(deps, "langfuse", None)
            with (
                _lf.generation(
                    "single_planner_turn",
                    model=_resolve_model_name(self.model),
                    input={"messages": summarize_messages(messages)},
                    metadata={
                        "planner_mode": "single",
                        "session_id": deps.session_id,
                    },
                    model_parameters={"temperature": 0},
                )
                if _lf
                else _null_generation() as _
            ):
                response = llm.invoke(messages)
                usage = getattr(response, "usage_metadata", None) or {}
                if _lf:
                    _lf.update_generation(
                        output={
                            "content": _extract_final_text([response])[:2000],
                            "tool_calls": getattr(response, "tool_calls", None),
                        },
                        usage_details={
                            "prompt_tokens": int(
                                usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                            ),
                            "completion_tokens": int(
                                usage.get("output_tokens") or usage.get("completion_tokens") or 0
                            ),
                        },
                    )
            return {"messages": [response]}

        def route(state: GraphState):
            last = state["messages"][-1]
            return "tools" if getattr(last, "tool_calls", None) else "end"

        graph = StateGraph(GraphState)
        graph.add_node("agent", call_model)
        graph.add_node("tools", ToolNode(tools))
        graph.set_entry_point("agent")
        graph.add_conditional_edges("agent", route, {"tools": "tools", "end": END})
        graph.add_edge("tools", "agent")

        app = graph.compile()
        result = await app.ainvoke(
            {"messages": [HumanMessage(content=context_prompt)]},
            config={"recursion_limit": 25},
        )
        messages = result["messages"]
        output = _extract_final_text(messages)
        usage = _aggregate_usage(messages)
        return SimpleNamespace(
            output=output,
            usage=lambda: SimpleNamespace(
                input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]
            ),
            all_messages=lambda: messages,
        )


def build(model, config=None) -> DiagnosisWorkflow:
    state: dict[str, Any] = {
        "nginx_applied": False,
        "system_applied": False,
        "after_rps": 0.0,
        "after_p99_ms": 0.0,
        "after_error_rate": 0.0,
        "findings": [],
        "rca_records": [],
        "recommendations": [],
        "guardrail_failure": "",
    }
    memory_query_count = 0
    max_memory_queries = 3

    def _normalize_changes(
        raw_changes: dict[str, str] | str | None, tool_name: str
    ) -> tuple[dict[str, str] | None, str | None]:
        if raw_changes is None:
            return {}, None
        changes_obj = raw_changes
        if isinstance(changes_obj, str):
            try:
                changes_obj = json.loads(changes_obj)
            except json.JSONDecodeError as e:
                return None, f"{tool_name}: invalid JSON payload ({e.msg})"

        if not isinstance(changes_obj, dict):
            return None, f"{tool_name}: payload must be a dictionary"

        normalized: dict[str, str] = {}
        for key, value in changes_obj.items():
            key_str = str(key).lstrip(".")
            if isinstance(value, (dict, list)):
                normalized[key_str] = json.dumps(value, separators=(",", ":"))
            else:
                normalized[key_str] = str(value)
        return normalized, None

    def _debug_enabled(deps: AgentDeps) -> bool:
        cfg = getattr(deps, "config", {}) or {}
        return bool((cfg.get("agent") or {}).get("debug_planner_payloads", False))

    def _debug_planner(deps: AgentDeps, label: str, payload: Any) -> None:
        if not _debug_enabled(deps):
            return
        text = _sanitize_debug_text(
            json.dumps(payload, ensure_ascii=False)
            if isinstance(payload, (dict, list))
            else str(payload)
        )
        tool_result("debug", f"{label}: {text}")
        _persist_hypothesis_markdown(
            deps,
            filename=f"debug_{label}.md",
            title=f"Debug {label}",
            sections=[
                ("Summary", f"Planner debug artifact for `{label}`."),
                ("Payload", _markdown_json(payload)),
            ],
        )

    def _langfuse_event(
        deps: AgentDeps,
        name: str,
        *,
        input: Any = None,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
    ) -> None:
        client = getattr(deps, "langfuse", None)
        if client:
            with client.tool_span(name, input=input, metadata=metadata):
                client.update_span(
                    output=output if level != "ERROR" else {"error": output},
                    metadata={"level": level} if level else None,
                )

    def _coerce_tool_changes(
        raw_changes: dict[str, str] | str | None,
        extra_changes: dict[str, Any],
        tool_name: str,
    ) -> tuple[dict[str, str] | None, str | None]:
        normalized_changes, parse_error = _normalize_changes(raw_changes, tool_name)
        if parse_error:
            return None, parse_error

        merged = dict(normalized_changes or {})
        for key, value in extra_changes.items():
            if key == "changes":
                continue
            key_str = str(key).lstrip(".")
            if isinstance(value, (dict, list)):
                merged[key_str] = json.dumps(value, separators=(",", ":"))
            else:
                merged[key_str] = str(value)
        return merged, None

    _tuning_cfg = (config or {}).get("tuning") or {}
    nginx_targets: dict[str, str] = {
        "worker_connections": "65536",
        "worker_rlimit_nofile": "200000",
        "worker_cpu_affinity": "auto",
        "open_file_cache": "max=200000 inactive=60s",
        "open_file_cache_valid": "30s",
        "open_file_cache_min_uses": "2",
        "access_log": "off",
        "tcp_nodelay": "on",
        "keepalive_requests": "10000",
        "keepalive_timeout": "30",
        "reset_timedout_connection": "on",
        "listen_backlog": "65535",
        "aio": "threads",
        **{str(k): str(v) for k, v in (_tuning_cfg.get("nginx_targets") or {}).items()},
    }
    system_targets: dict[str, str] = {
        "net.core.somaxconn": "65535",
        "net.ipv4.tcp_max_syn_backlog": "65535",
        "net.core.netdev_max_backlog": "65535",
        "net.core.rmem_max": "16777216",
        "net.core.wmem_max": "16777216",
        "net.ipv4.tcp_tw_reuse": "1",
        "net.ipv4.tcp_max_tw_buckets": "2000000",
        "net.ipv4.ip_local_port_range": "1024 65535",
        "transparent_hugepage": "never",
        "selinux": "permissive",
        "cpu_governor": "performance",
        "nofile": "65536",
        "irqbalance": "active",
        **{str(k): str(v) for k, v in (_tuning_cfg.get("system_targets") or {}).items()},
    }

    def inspect_irq_impl(deps: AgentDeps) -> dict:
        tool_call("inspect", "irq distribution — checking for IRQ lock and CPU spread")
        ssh = deps.ssh
        interrupts = ssh.execute(
            "cat /proc/interrupts | grep -E 'eth|ens|eno|virtio' | head -n 20"
        ).stdout
        workers = ssh.execute("ps -eo pid,psr,comm | grep nginx").stdout
        telemetry_rows = deps.memory.get_contexts(
            deps.session_id, type="telemetry", source_prefix="baseline:", limit=6
        )

        worker_cores: list[int] = []
        for line in workers.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "nginx":
                try:
                    worker_cores.append(int(parts[1]))
                except ValueError:
                    continue

        latest_series = {}
        for row in telemetry_rows:
            if row.get("source") != "baseline:series":
                continue
            try:
                latest_series = json.loads(row.get("content", "{}"))
            except (TypeError, json.JSONDecodeError):
                latest_series = {}
            break

        summary = latest_series.get("summary", {}) if isinstance(latest_series, dict) else {}
        latest_sample = (
            latest_series.get("last_sample", {}) if isinstance(latest_series, dict) else {}
        )
        current = {
            "irq_lines": interrupts.strip()[:2000] or "no ethernet IRQs found",
            "nginx_worker_cores": sorted(set(worker_cores)),
            "worker_core_spread": len(set(worker_cores)),
            "telemetry_run_queue_max": summary.get("run_queue_max", 0),
            "telemetry_rx_drop_delta": summary.get("rx_drop_delta", 0),
            "telemetry_rx_drop_rate_per_sec": summary.get("rx_drop_rate_per_sec", 0),
            "telemetry_tcp_established": latest_sample.get("tcp_established", 0),
        }
        needs_investigation = []
        if not interrupts.strip():
            needs_investigation.append("no_nic_irq_visibility")
        if current["worker_core_spread"] <= 2 and current["telemetry_rx_drop_delta"]:
            needs_investigation.append("possible_irq_or_worker_core_lock")
        if current["telemetry_run_queue_max"] and current["telemetry_run_queue_max"] >= 4:
            needs_investigation.append("run_queue_pressure_during_load")

        result = {
            "needs_investigation": needs_investigation,
            "current": current,
        }
        _langfuse_event(
            deps,
            "tool.inspect_irq_distribution",
            input={"telemetry_rows": len(telemetry_rows)},
            output=result,
        )
        deps.token_counter.tool_calls += 1
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            "inspect_irq",
            str(result),
            (
                f"irq: {len(needs_investigation)} signals, "
                f"worker_spread={current['worker_core_spread']}"
            ),
        )
        tool_result(
            "inspect",
            (
                f"irq: {len(needs_investigation)} signals, "
                f"worker_spread={current['worker_core_spread']}"
            ),
        )
        state["irq_inspection"] = result
        return result

    def inspect_nginx_impl(deps: AgentDeps) -> dict:
        tool_call("inspect", "nginx config — stage 1 analysis")
        raw = deps.ssh.execute("nginx -T 2>/dev/null").stdout
        current: dict[str, str] = {}
        for directive in nginx_targets:
            if directive == "listen_backlog":
                match = re.search(r"listen\s+.*backlog=(\d+)", raw)
                current[directive] = match.group(1) if match else "not set"
            else:
                match = re.search(rf"^\s*{directive}\s+(.+?);", raw, re.MULTILINE)
                current[directive] = match.group(1).strip() if match else "not set"

        needs_fixing = {}
        already_ok = []
        for parameter, target in nginx_targets.items():
            cur = current.get(parameter, "not set")
            if cur == "not set" or cur != target:
                needs_fixing[parameter] = {"current": cur, "target": target}
            else:
                already_ok.append(parameter)

        result = {"needs_fixing": needs_fixing, "already_ok": already_ok, "current": current}
        _langfuse_event(
            deps,
            "tool.inspect_nginx_config",
            input={"directive_count": len(nginx_targets)},
            output=result,
        )
        deps.token_counter.tool_calls += 1
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            "inspect_nginx",
            str(result),
            f"nginx: {len(needs_fixing)} need fixing, {len(already_ok)} ok",
        )
        tool_result(
            "inspect", f"nginx: {len(needs_fixing)} need fixing, {len(already_ok)} already ok"
        )
        state["nginx_inspection"] = result
        return result

    def inspect_system_impl(deps: AgentDeps) -> dict:
        tool_call("inspect", "rhel/kernel tuning — stage 2 analysis")
        ssh = deps.ssh
        current = {}
        for key in [
            "net.core.somaxconn",
            "net.ipv4.tcp_max_syn_backlog",
            "net.core.netdev_max_backlog",
            "net.ipv4.tcp_tw_reuse",
            "net.ipv4.tcp_max_tw_buckets",
            "net.ipv4.ip_local_port_range",
            "net.core.rmem_max",
            "net.core.wmem_max",
        ]:
            sysctl_r = ssh.execute(f"sysctl -n {key} 2>/dev/null")
            current[key] = sysctl_r.stdout.strip()

        thp = ssh.execute("cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null")
        match = re.search(r"\[(\w+)\]", thp.stdout)
        current["transparent_hugepage"] = match.group(1) if match else "unknown"
        current["selinux"] = (
            ssh.execute("getenforce 2>/dev/null || echo Disabled").stdout.strip().lower()
        )
        governor = ssh.execute(
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null"
        )
        current["cpu_governor"] = governor.stdout.strip() or "not available"
        nofile_r = ssh.execute("ulimit -Sn 2>/dev/null || echo unknown")
        current["nofile"] = nofile_r.stdout.strip()
        irq_r = ssh.execute("systemctl is-active irqbalance 2>/dev/null || echo inactive")
        current["irqbalance"] = irq_r.stdout.strip()

        needs_fixing = {}
        already_ok = []
        for parameter, target in system_targets.items():
            cur = current.get(parameter, "unknown")
            if cur != target:
                needs_fixing[parameter] = {"current": cur, "target": target}
            else:
                already_ok.append(parameter)

        result = {"needs_fixing": needs_fixing, "already_ok": already_ok, "current": current}
        _langfuse_event(
            deps,
            "tool.inspect_system_tuning",
            input={"parameter_count": len(system_targets)},
            output=result,
        )
        deps.token_counter.tool_calls += 1
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            "inspect_system",
            str(result),
            f"system: {len(needs_fixing)} need fixing, {len(already_ok)} ok",
        )
        tool_result(
            "inspect", f"system: {len(needs_fixing)} need fixing, {len(already_ok)} already ok"
        )
        state["system_inspection"] = result
        return result

    def apply_nginx_impl(
        deps: AgentDeps, changes: dict[str, str] | str | None, **kwargs: Any
    ) -> dict:
        normalized_changes, parse_error = _coerce_tool_changes(changes, kwargs, "apply_nginx")
        if parse_error:
            tool_call("apply_nginx", "invalid input payload")
            tool_result("apply_nginx", f"FAILED: {parse_error}")
            _langfuse_event(
                deps,
                "tool.apply_nginx_tuning",
                input={"changes": changes, "kwargs": kwargs},
                output={"error": parse_error},
                level="ERROR",
            )
            deps.token_counter.tool_calls += 1
            return {"applied": [], "failed": [], "reload": "FAILED", "error": parse_error}

        changes_dict = normalized_changes or {}
        allowed = getattr(deps.adapter, "ALLOWED_BATCH_DIRECTIVES", None)
        unsupported: list[str] = []
        if allowed is not None:
            unsupported = [key for key in changes_dict if key not in allowed]
            changes_dict = {key: value for key, value in changes_dict.items() if key in allowed}
            if unsupported and not changes_dict:
                error = f"unsupported nginx directives: {', '.join(unsupported)}"
                tool_call("apply_nginx", "unsupported directives")
                tool_result("apply_nginx", f"FAILED: {error}")
                _langfuse_event(
                    deps,
                    "tool.apply_nginx_tuning",
                    input={"changes": changes_dict, "unsupported": unsupported},
                    output={"error": error},
                    level="ERROR",
                )
                deps.token_counter.tool_calls += 1
                return {"applied": [], "failed": unsupported, "reload": "FAILED", "error": error}

        tool_call("apply_nginx", f"{len(changes_dict)} changes: {', '.join(changes_dict.keys())}")

        config_path = deps.config["service"]["config_path"]
        batch_backup = f"/tmp/slay_nginx_batch_{deps.session_id}.conf"
        deps.ssh.execute(f"cp {config_path} {batch_backup}")

        applied = []
        failed = list(unsupported)
        for param, value in changes_dict.items():
            success = deps.adapter.apply_config(param, value)
            if success:
                applied.append(param)
            else:
                failed.append(param)
                deps.ssh.execute(f"cp {batch_backup} {config_path}")
                result = {
                    "applied": applied,
                    "failed": failed,
                    "reload": "FAILED",
                    "error": f"failed to apply nginx directive: {param}",
                }
                deps.token_counter.tool_calls += 1
                deps.memory.save_context(
                    deps.session_id,
                    "command_output",
                    f"apply_nginx:{','.join(changes_dict.keys())}"[:250],
                    f"applied={applied} failed={failed}",
                    "nginx batch apply",
                )
                tool_result("apply_nginx", f"FAILED: {result['error']}")
                _langfuse_event(
                    deps,
                    "tool.apply_nginx_tuning",
                    input={"changes": changes_dict},
                    output=result,
                    level="ERROR",
                )
                return result

        test = deps.ssh.execute("nginx -t 2>&1")
        if "syntax is ok" not in test.stdout and "test is successful" not in test.stdout:
            deps.ssh.execute(f"cp {batch_backup} {config_path}")
            error_msg = test.stdout.strip()[:200]
            result = {
                "applied": applied,
                "failed": failed,
                "reload": "FAILED",
                "error": f"nginx -t failed: {error_msg}",
            }
            deps.token_counter.tool_calls += 1
            deps.memory.save_context(
                deps.session_id,
                "command_output",
                f"apply_nginx:{','.join(changes_dict.keys())}"[:250],
                f"applied={applied} failed={failed}",
                "nginx batch apply failed",
            )
            tool_result("apply_nginx", f"FAILED: {error_msg}")
            _langfuse_event(
                deps,
                "tool.apply_nginx_tuning",
                input={"changes": changes_dict},
                output=result,
                level="ERROR",
            )
            return result

        reload_ok = deps.adapter.reload()
        result = {"applied": applied, "failed": failed, "reload": "OK" if reload_ok else "FAILED"}
        if unsupported:
            result["warning"] = f"ignored unsupported nginx directives: {', '.join(unsupported)}"

        deps.token_counter.tool_calls += 1
        state["nginx_applied"] = state["nginx_applied"] or bool(applied and reload_ok)
        summary = f"applied={applied} failed={failed} reload={'OK' if reload_ok else 'FAILED'}"
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            f"apply_nginx:{','.join(changes_dict.keys())}"[:250],
            summary,
            "nginx batch apply",
        )
        _langfuse_event(
            deps,
            "tool.apply_nginx_tuning",
            input={"changes": changes_dict},
            output=result,
        )
        tool_result("apply_nginx", summary)
        return result

    def apply_system_impl(
        deps: AgentDeps, changes: dict[str, str] | str | None, **kwargs: Any
    ) -> dict:
        normalized_changes, parse_error = _coerce_tool_changes(changes, kwargs, "apply_system")
        if parse_error:
            tool_call("apply_system", "invalid input payload")
            tool_result("apply_system", f"FAILED: {parse_error}")
            _langfuse_event(
                deps,
                "tool.apply_system_tuning",
                input={"changes": changes, "kwargs": kwargs},
                output={"error": parse_error},
                level="ERROR",
            )
            deps.token_counter.tool_calls += 1
            return {"applied": {}, "failed": {"_input": parse_error}}

        changes_dict = normalized_changes or {}
        tool_call("apply_system", f"{len(changes_dict)} changes: {', '.join(changes_dict.keys())}")

        ssh = deps.ssh
        applied = {}
        failed = {}
        for param, value in changes_dict.items():
            if param == "transparent_hugepage":
                result = ssh.execute(
                    f"echo {value} > /sys/kernel/mm/transparent_hugepage/enabled 2>&1"
                )
                if result.ok:
                    applied[param] = value
                else:
                    failed[param] = result.stderr.strip()
            elif param == "selinux":
                mode = "0" if value.lower() in ("permissive", "0") else "1"
                result = ssh.execute(f"setenforce {mode} 2>&1")
                # Persist across reboots
                if value.lower() in ("permissive", "0"):
                    ssh.execute(
                        "sed -i 's/^SELINUX=enforcing/SELINUX=permissive/'"
                        " /etc/selinux/config 2>/dev/null || true"
                    )
                # Verify it actually changed
                verify = ssh.execute("getenforce 2>/dev/null")
                actual = verify.stdout.strip().lower()
                expected = "permissive" if mode == "0" else "enforcing"
                if actual == expected:
                    applied[param] = value
                else:
                    failed[param] = f"setenforce ran but getenforce={actual}"
            elif param == "cpu_governor":
                result = ssh.execute(
                    f"echo {value} | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>&1"
                )
                if result.ok:
                    applied[param] = value
                else:
                    failed[param] = result.stderr.strip()
            elif param == "net.ipv4.ip_local_port_range":
                result = ssh.execute(f'sysctl -w net.ipv4.ip_local_port_range="{value}" 2>&1')
                if result.ok:
                    applied[param] = value
                else:
                    failed[param] = result.stderr.strip()
            elif param == "nofile":
                cmds = [
                    "sed -i '/nofile/d' /etc/security/limits.conf 2>/dev/null || true",
                    f"echo '* soft nofile {value}' >> /etc/security/limits.conf",
                    f"echo '* hard nofile {value}' >> /etc/security/limits.conf",
                    f"systemctl set-property nginx.service LimitNOFILE={value} 2>/dev/null || true",
                    "systemctl daemon-reload && systemctl restart nginx 2>&1 || true",
                ]
                for cmd in cmds:
                    ssh.execute(cmd)
                applied[param] = value
            elif param == "irqbalance":
                result = ssh.execute("systemctl enable --now irqbalance 2>&1")
                if result.ok:
                    applied[param] = value
                else:
                    failed[param] = result.stderr.strip() or "irqbalance enable failed"
            else:
                result = ssh.execute(f"sysctl -w {param}={value} 2>&1")
                if result.ok:
                    applied[param] = value
                else:
                    failed[param] = result.stderr.strip()

        deps.token_counter.tool_calls += 1
        state["system_applied"] = state["system_applied"] or bool(applied)
        summary = f"applied={list(applied.keys())} failed={list(failed.keys())}"
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            f"apply_system:{','.join(changes_dict.keys())}"[:250],
            summary,
            "system batch apply",
        )
        _langfuse_event(
            deps,
            "tool.apply_system_tuning",
            input={"changes": changes_dict},
            output={"applied": applied, "failed": failed},
        )
        tool_result("apply_system", summary)
        return {"applied": applied, "failed": failed}

    def run_benchmark_impl(deps: AgentDeps, duration: int = 30) -> dict:
        bench_cfg = deps.config["service"]["benchmark"]
        url = bench_cfg.get("small_file_url", "http://localhost/")
        bench_tool = bench_cfg.get("tool", "wrk2")
        benchmark_label = "benchmark.sh" if bench_tool == "hackathon" else "wrk2"
        tool_call("benchmark", f"{benchmark_label} {duration}s -> {url}")
        result: BenchmarkResult = deps.adapter.benchmark(duration, url)
        tool_result(
            "benchmark",
            f"{result.requests_per_sec:.1f} RPS, "
            f"p99={result.latency_p99_ms:.1f}ms, CPU={result.cpu_pct:.1f}%",
        )
        deps.memory.save_context(
            deps.session_id,
            "benchmark",
            url,
            (
                f"RPS={result.requests_per_sec:.1f} "
                f"p99={result.latency_p99_ms:.1f}ms "
                f"CPU={result.cpu_pct:.1f}%"
            ),
            "post-fix benchmark",
        )
        deps.token_counter.tool_calls += 1
        state["after_rps"] = float(result.requests_per_sec)
        state["after_p99_ms"] = float(result.latency_p99_ms)
        state["after_error_rate"] = float(result.error_rate)
        _langfuse_event(
            deps,
            "tool.run_benchmark",
            input={"duration": duration, "url": url, "backend": bench_tool},
            output=result.__dict__,
        )
        return result.__dict__

    def query_memory_impl(deps: AgentDeps, symptom: str) -> list[dict]:
        nonlocal memory_query_count
        memory_query_count += 1
        if memory_query_count > max_memory_queries:
            tool_call("memory", f"BLOCKED — limit reached ({max_memory_queries})")
            tool_result("memory", "limit reached; returning no additional memory results")
            deps.token_counter.tool_calls += 1
            return []
        tool_call("memory", f"query {memory_query_count}/{max_memory_queries}: {symptom}")
        results = deps.memory.semantic_search(symptom, deps.session_id, top_k=5)
        tool_result("memory", f"{len(results)} results found")
        _langfuse_event(
            deps,
            "tool.query_memory",
            input={"symptom": symptom, "query_index": memory_query_count},
            output={"result_count": len(results), "top_results": results[:3]},
        )
        deps.token_counter.tool_calls += 1
        return results

    def save_findings_impl(deps: AgentDeps, findings: list[dict[str, Any]]) -> bool:
        tool_call("save", f"{len(findings)} findings")

        def _coerce_optional_float(value: Any) -> float | None:
            if value is None or value == "":
                return None
            if isinstance(value, bool):
                return float(value)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value.strip())
                except ValueError:
                    return None
            return None

        try:
            profile = deps.memory.get_profile(deps.session_id) or {}
        except Exception:
            profile = {}

        baseline_rps = _coerce_optional_float(profile.get("baseline_rps")) or 0.0
        run_after_rps = _coerce_optional_float(state.get("after_rps")) or 0.0
        derived_impact = (
            ((run_after_rps - baseline_rps) / baseline_rps * 100)
            if baseline_rps and run_after_rps
            else 0.0
        )

        for finding in findings:
            param = finding.get("parameter", "unknown")
            before_rps = _coerce_optional_float(finding.get("before_rps"))
            after_rps = _coerce_optional_float(finding.get("after_rps"))
            impact_pct = _coerce_optional_float(finding.get("impact_pct"))

            if before_rps is None and baseline_rps:
                before_rps = baseline_rps
            if after_rps is None and run_after_rps:
                after_rps = run_after_rps
            if impact_pct is None and before_rps and after_rps:
                impact_pct = (after_rps - before_rps) / before_rps * 100
            elif impact_pct is None and derived_impact:
                impact_pct = derived_impact

            deps.memory.save_fact(
                session_id=deps.session_id,
                type="fix",
                parameter=param,
                reasoning=finding.get("reasoning", "proven fix applied"),
                before_value=finding.get("before_value", ""),
                after_value=finding.get("after_value", ""),
                before_rps=before_rps,
                after_rps=after_rps,
                impact_pct=impact_pct,
            )
            impact_label = "n/a" if impact_pct is None else f"{impact_pct:+.1f}%"
            tool_result("save", f"{param} ({impact_label})")
        deps.token_counter.tool_calls += 1
        state["findings"] = findings
        _langfuse_event(
            deps,
            "tool.save_findings",
            input={"count": len(findings)},
            output={"saved": findings[:10]},
        )
        return True

    def save_rca_impl(deps: AgentDeps, records: list[dict[str, Any]]) -> bool:
        tool_call("rca", f"{len(records)} records")
        _debug_planner(deps, "raw_rca_records", records[:3])
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            "planner_rca_raw",
            json.dumps(records, ensure_ascii=True),
            f"raw rca payload ({len(records)} records)",
        )
        normalized_records: list[dict[str, Any]] = []
        for idx, record in enumerate(records, start=1):
            symptom = str(record.get("symptom", "")).strip() or "unknown symptom"
            root_cause = str(record.get("root_cause", "")).strip() or "unknown root cause"
            recommendation = str(record.get("recommendation", "")).strip() or "no recommendation"
            confidence = _coerce_float(record.get("confidence", 0.0))
            evidence = record.get("evidence", [])
            if isinstance(evidence, str):
                evidence_list = [evidence]
            elif isinstance(evidence, list):
                evidence_list = [str(item) for item in evidence[:6]]
            else:
                evidence_list = [str(evidence)]
            normalized = {
                "symptom": symptom,
                "root_cause": root_cause,
                "confidence": confidence,
                "recommendation": recommendation,
                "evidence": evidence_list,
            }
            if _debug_enabled(deps) and (
                symptom == "unknown symptom" or root_cause == "unknown root cause"
            ):
                tool_result(
                    "debug",
                    (
                        f"rca_{idx} malformed: raw={json.dumps(record, ensure_ascii=True)[:500]} "
                        f"normalized={json.dumps(normalized, ensure_ascii=True)[:300]}"
                    ),
                )
            deps.memory.save_context(
                deps.session_id,
                "rca",
                f"rca_{idx}",
                json.dumps(normalized),
                f"{symptom} -> {root_cause} (conf={confidence:.2f})",
            )
            normalized_records.append(normalized)
            tool_result("rca", f"{symptom} -> {root_cause} ({confidence:.2f})")
        deps.token_counter.tool_calls += 1
        state["rca_records"] = normalized_records
        _persist_hypothesis_markdown(
            deps,
            filename="04_rca.md",
            title="RCA",
            sections=[
                ("Summary", f"Accepted RCA records: {len(normalized_records)}"),
                ("Records", _markdown_json(normalized_records)),
            ],
        )
        _langfuse_event(
            deps,
            "tool.save_rca",
            input={"count": len(records)},
            output={"normalized_records": normalized_records[:10]},
        )
        return True

    def save_recommendations_impl(deps: AgentDeps, recommendations: list[dict[str, Any]]) -> bool:
        tool_call("recommend", f"{len(recommendations)} recommendations")
        _debug_planner(deps, "raw_recommendations", recommendations[:3])
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            "planner_recommendations_raw",
            json.dumps(recommendations, ensure_ascii=True),
            f"raw recommendation payload ({len(recommendations)} items)",
        )
        normalized_items: list[dict[str, Any]] = []
        for idx, item in enumerate(recommendations, start=1):
            scope = str(item.get("scope", "nginx")).strip().lower() or "nginx"
            if scope not in {"nginx", "system"}:
                if _debug_enabled(deps):
                    tool_result(
                        "debug",
                        (
                            f"recommendation_{idx} reject invalid scope: "
                            f"raw={json.dumps(item, ensure_ascii=True)[:500]}"
                        ),
                    )
                deps.memory.save_context(
                    deps.session_id,
                    "command_output",
                    f"recommendation_rejected_{idx}",
                    json.dumps(
                        {
                            "reason": "invalid scope",
                            "scope": scope,
                            "raw": item,
                        },
                        ensure_ascii=True,
                    ),
                    f"recommendation_{idx} rejected: invalid scope {scope}",
                )
                _persist_hypothesis_markdown(
                    deps,
                    filename="06_rejections.md",
                    title="Rejected Recommendations",
                    sections=[
                        (
                            "Rejection",
                            _markdown_json(
                                {
                                    "index": idx,
                                    "reason": "invalid scope",
                                    "scope": scope,
                                    "raw": item,
                                }
                            ),
                        )
                    ],
                    append=True,
                )
                tool_result("recommend", f"skipped recommendation_{idx}: invalid scope {scope}")
                continue
            changes, parse_error = _normalize_changes(item.get("changes"), "recommendation")
            if parse_error:
                changes = {}
            allowed_params = set(nginx_targets) if scope == "nginx" else set(system_targets)
            filtered_changes = {
                key: value for key, value in (changes or {}).items() if key in allowed_params
            }
            if not filtered_changes:
                if _debug_enabled(deps):
                    tool_result(
                        "debug",
                        (
                            f"recommendation_{idx} reject no allowed changes: "
                            f"title={str(item.get('title', ''))[:80]!r} "
                            f"scope={scope} "
                            f"raw_changes="
                            f"{json.dumps(item.get('changes', {}), ensure_ascii=True)[:300]} "
                            f"normalized={json.dumps(changes or {}, ensure_ascii=True)[:300]}"
                        ),
                    )
                deps.memory.save_context(
                    deps.session_id,
                    "command_output",
                    f"recommendation_rejected_{idx}",
                    json.dumps(
                        {
                            "reason": "no allowed performance changes",
                            "scope": scope,
                            "raw": item,
                            "normalized_changes": changes or {},
                            "allowed_params": sorted(allowed_params),
                        },
                        ensure_ascii=True,
                    ),
                    f"recommendation_{idx} rejected: no allowed performance changes",
                )
                _persist_hypothesis_markdown(
                    deps,
                    filename="06_rejections.md",
                    title="Rejected Recommendations",
                    sections=[
                        (
                            "Rejection",
                            _markdown_json(
                                {
                                    "index": idx,
                                    "reason": "no allowed performance changes",
                                    "scope": scope,
                                    "raw": item,
                                    "normalized_changes": changes or {},
                                    "allowed_params": sorted(allowed_params),
                                }
                            ),
                        )
                    ],
                    append=True,
                )
                tool_result(
                    "recommend", f"skipped recommendation_{idx}: no allowed performance changes"
                )
                continue
            normalized = {
                "title": str(item.get("title", "")).strip() or f"recommendation_{idx}",
                "recommendation": str(item.get("recommendation", "")).strip()
                or "no recommendation",
                "rationale": str(item.get("rationale", "")).strip() or "no rationale",
                "expected_benefit": str(item.get("expected_benefit", "")).strip()
                or "no expected benefit",
                "risk_level": str(item.get("risk_level", "medium")).strip().lower() or "medium",
                "validation": str(item.get("validation", "")).strip()
                or "manual verification required",
                "scope": scope,
                "changes": filtered_changes,
            }
            deps.memory.save_context(
                deps.session_id,
                "recommendation",
                f"recommendation_{idx}",
                json.dumps(normalized),
                f"{normalized['title']} [{normalized['risk_level']}]",
            )
            normalized_items.append(normalized)
            tool_result("recommend", f"{normalized['title']} [{normalized['risk_level']}]")
        deps.token_counter.tool_calls += 1
        state["recommendations"] = normalized_items
        _persist_hypothesis_markdown(
            deps,
            filename="05_recommendations.md",
            title="Recommendations",
            sections=[
                ("Summary", f"Accepted recommendations: {len(normalized_items)}"),
                ("Recommendations", _markdown_json(normalized_items)),
            ],
        )
        _langfuse_event(
            deps,
            "tool.save_recommendations",
            input={"count": len(recommendations)},
            output={"accepted": normalized_items[:10]},
        )
        return True

    def _evaluate_nginx_guardrails(
        deps: AgentDeps, benchmark_result: dict[str, Any]
    ) -> dict[str, Any]:
        def _load_baseline_benchmark() -> dict[str, Any]:
            for source in ("baseline_small", "baseline_homepage"):
                try:
                    rows = deps.memory.get_contexts(
                        deps.session_id, type="benchmark", source_prefix=source, limit=1
                    )
                except Exception:
                    rows = []
                if not rows:
                    continue
                content = rows[0].get("content", "")
                if not isinstance(content, str) or not content.strip():
                    continue
                try:
                    parsed = json.loads(content)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
            return {}

        try:
            profile = deps.memory.get_profile(deps.session_id) or {}
        except Exception:
            profile = {}
        baseline_benchmark = _load_baseline_benchmark()

        baseline_rps = _coerce_float(profile.get("baseline_rps")) or _coerce_float(
            baseline_benchmark.get("rps")
        )
        baseline_p99 = _coerce_float(baseline_benchmark.get("p99"))
        baseline_error_rate = _coerce_float(baseline_benchmark.get("error_rate"))
        after_rps = _coerce_float(benchmark_result.get("requests_per_sec"))
        after_p99 = _coerce_float(benchmark_result.get("latency_p99_ms"))
        after_error_rate = _coerce_float(benchmark_result.get("error_rate"))
        reasons: list[str] = []

        if baseline_rps and after_rps < baseline_rps * 0.95:
            reasons.append(f"RPS regressed ({after_rps:.1f} < {baseline_rps:.1f})")
        if baseline_p99 and after_p99 > baseline_p99 * 1.10:
            reasons.append(f"p99 regressed ({after_p99:.1f}ms > {baseline_p99:.1f}ms)")
        if after_error_rate > baseline_error_rate:
            reasons.append(
                f"error rate regressed ({after_error_rate:.3f} > {baseline_error_rate:.3f})"
            )

        impact_pct = ((after_rps - baseline_rps) / baseline_rps * 100) if baseline_rps else 0.0
        return {
            "ok": not reasons,
            "summary": "validated for nginx performance" if not reasons else "; ".join(reasons),
            "baseline_rps": baseline_rps,
            "after_rps": after_rps,
            "impact_pct": impact_pct,
        }

    def apply_saved_recommendations_impl(deps: AgentDeps) -> dict[str, Any]:
        recommendations = list(state.get("recommendations") or [])
        nginx_changes: dict[str, str] = {}
        system_changes: dict[str, str] = {}
        recommendation_by_param: dict[tuple[str, str], dict[str, Any]] = {}

        for item in recommendations:
            scope = str(item.get("scope", "nginx")).lower()
            changes = item.get("changes", {})
            if not isinstance(changes, dict):
                continue
            for param, value in changes.items():
                if scope == "system":
                    system_changes[str(param)] = str(value)
                    recommendation_by_param[("system", str(param))] = item
                else:
                    nginx_changes[str(param)] = str(value)
                    recommendation_by_param[("nginx", str(param))] = item

        nginx_result = {"applied": [], "failed": [], "reload": "SKIPPED"}
        system_result: dict[str, Any] = {"applied": {}, "failed": {}}
        if nginx_changes:
            nginx_result = apply_nginx_impl(deps, nginx_changes)
        if system_changes:
            system_result = apply_system_impl(deps, system_changes)

        bench_duration = int(deps.config["service"]["benchmark"].get("duration", 30))
        benchmark_result = run_benchmark_impl(deps, bench_duration)

        nginx_current = ((state.get("nginx_inspection") or {}).get("current")) or {}
        system_current = ((state.get("system_inspection") or {}).get("current")) or {}
        findings: list[dict[str, Any]] = []
        for param in nginx_result.get("applied", []):
            rec = recommendation_by_param.get(("nginx", param), {})
            findings.append(
                {
                    "parameter": f"nginx.{param}",
                    "before_value": nginx_current.get(param, ""),
                    "after_value": nginx_changes.get(param, ""),
                    "reasoning": rec.get("rationale")
                    or rec.get("recommendation")
                    or "recommended change applied",
                }
            )
        for param, value in system_result.get("applied", {}).items():
            rec = recommendation_by_param.get(("system", param), {})
            findings.append(
                {
                    "parameter": f"system.{param}",
                    "before_value": system_current.get(param, ""),
                    "after_value": value,
                    "reasoning": rec.get("rationale")
                    or rec.get("recommendation")
                    or "recommended change applied",
                }
            )

        guardrail = _evaluate_nginx_guardrails(deps, benchmark_result)
        deps.memory.save_context(
            deps.session_id,
            "metric",
            "guardrail_validation",
            json.dumps(guardrail),
            guardrail["summary"],
        )

        if findings and guardrail["ok"]:
            state["guardrail_failure"] = ""
            save_findings_impl(deps, findings)
        elif findings:
            state["guardrail_failure"] = guardrail["summary"]
            state["nginx_applied"] = False
            state["system_applied"] = False
            state["findings"] = []
            for finding in findings:
                deps.memory.save_fact(
                    session_id=deps.session_id,
                    type="negative",
                    parameter=finding.get("parameter", "unknown"),
                    reasoning=guardrail["summary"],
                    before_value=finding.get("before_value", ""),
                    after_value=finding.get("after_value", ""),
                    before_rps=guardrail["baseline_rps"],
                    after_rps=guardrail["after_rps"],
                    impact_pct=guardrail["impact_pct"],
                    status="regressed",
                )
                tool_result(
                    "guardrail",
                    f"blocked {finding.get('parameter', 'unknown')}: {guardrail['summary']}",
                )

        return {
            "nginx": nginx_result,
            "system": system_result,
            "benchmark": benchmark_result,
            "findings": findings,
        }

    async def inspect_nginx_config(ctx) -> dict:
        return inspect_nginx_impl(ctx.deps)

    async def inspect_system_tuning(ctx) -> dict:
        return inspect_system_impl(ctx.deps)

    async def inspect_irq_distribution(ctx) -> dict:
        return inspect_irq_impl(ctx.deps)

    async def apply_nginx_tuning(
        ctx, changes: dict[str, str] | str | None = None, **kwargs: Any
    ) -> dict:
        return apply_nginx_impl(ctx.deps, changes, **kwargs)

    async def apply_system_tuning(
        ctx, changes: dict[str, str] | str | None = None, **kwargs: Any
    ) -> dict:
        return apply_system_impl(ctx.deps, changes, **kwargs)

    async def run_benchmark(ctx, duration: int = 30) -> dict:
        return run_benchmark_impl(ctx.deps, duration)

    async def query_memory(ctx, symptom: str) -> list[dict]:
        return query_memory_impl(ctx.deps, symptom)

    async def save_findings(ctx, findings: list[dict[str, Any]]) -> bool:
        return save_findings_impl(ctx.deps, findings)

    async def save_rca(ctx, records: list[dict[str, Any]]) -> bool:
        return save_rca_impl(ctx.deps, records)

    async def save_recommendations(ctx, recommendations: list[dict[str, Any]]) -> bool:
        return save_recommendations_impl(ctx.deps, recommendations)

    def tool_factory(deps: AgentDeps):
        from langchain_core.tools import tool

        @tool
        def inspect_nginx_config() -> dict:
            """Inspect nginx config and return only what needs fixing vs proven targets."""
            return inspect_nginx_impl(deps)

        @tool
        def inspect_system_tuning() -> dict:
            """Stage 2: inspect RHEL/kernel/system tuning and return candidate fixes."""
            return inspect_system_impl(deps)

        @tool
        def inspect_irq_distribution() -> dict:
            """Stage 3: inspect IRQ distribution, worker CPU spread, and IRQ lock signals."""
            return inspect_irq_impl(deps)

        @tool
        def apply_nginx_tuning(changes: dict[str, Any] | str | None = None) -> dict:
            """Apply multiple nginx config changes from a structured changes object."""
            return apply_nginx_impl(deps, changes)

        @tool
        def apply_system_tuning(changes: dict[str, Any] | str | None = None) -> dict:
            """Apply multiple system tuning changes from a structured changes object."""
            return apply_system_impl(deps, changes)

        @tool
        def run_benchmark(duration: int = 30) -> dict:
            """Run the post-fix benchmark."""
            return run_benchmark_impl(deps, duration)

        @tool
        def query_memory(symptom: str) -> list[dict]:
            """Search the knowledge base and past findings."""
            return query_memory_impl(deps, symptom)

        @tool
        def save_findings(findings: list[dict[str, Any]]) -> bool:
            """Save all findings from a structured findings list."""
            return save_findings_impl(deps, findings)

        @tool
        def save_rca(records: list[dict[str, Any]]) -> bool:
            """Save structured root-cause analysis records before remediation."""
            return save_rca_impl(deps, records)

        @tool
        def save_recommendations(recommendations: list[dict[str, Any]]) -> bool:
            """Save transparent human-readable recommendations before remediation."""
            return save_recommendations_impl(deps, recommendations)

        return [
            inspect_nginx_config,
            inspect_system_tuning,
            inspect_irq_distribution,
            save_rca,
            save_recommendations,
            query_memory,
        ]

    test_tools = {
        "inspect_nginx_config": inspect_nginx_config,
        "inspect_system_tuning": inspect_system_tuning,
        "inspect_irq_distribution": inspect_irq_distribution,
        "save_rca": save_rca,
        "save_recommendations": save_recommendations,
        "apply_nginx_tuning": apply_nginx_tuning,
        "apply_system_tuning": apply_system_tuning,
        "run_benchmark": run_benchmark,
        "query_memory": query_memory,
        "save_findings": save_findings,
    }
    workflow = DiagnosisWorkflow(model, state, test_tools, tool_factory)
    workflow._apply_from_recommendations = apply_saved_recommendations_impl  # type: ignore[attr-defined]
    return workflow


async def run(model, deps: AgentDeps, context_prompt: str) -> DiagnosisOutput:
    """Run the diagnosis workflow and derive the final structured result in Python."""
    llm_call("agent", "Starting diagnosis — sending context to LLM...")
    agent = build(model, config=getattr(deps, "config", None))
    state = getattr(agent, "_slaymetrics_state", {})
    config = getattr(deps, "config", {}) or {}
    planner_mode = str((config.get("agent") or {}).get("planner_mode", "debate")).strip().lower()
    if planner_mode == "debate":
        result = await _run_debate_planner(agent, model, deps, context_prompt)
    else:
        result = await agent.run(context_prompt, deps=deps)
    apply_from_recommendations = getattr(agent, "_apply_from_recommendations", None)
    max_phase = int((config.get("agent") or {}).get("max_phase", 4))
    if callable(apply_from_recommendations) and max_phase >= 4:
        apply_from_recommendations(deps)
    inp, out = result.usage().input_tokens or 0, result.usage().output_tokens or 0
    deps.token_counter.input_tokens += int(inp)
    deps.token_counter.output_tokens += int(out)
    deps.token_counter.tool_calls += 1
    llm_call("agent", f"LLM finished — {inp:,} in / {out:,} out tokens")
    tokens("agent", inp, out, deps.token_counter.summary())

    rows = deps.token_counter.tool_token_rows()
    if rows:
        top = ", ".join(
            f"{row['tool']}={row['total_tokens']:,}t/{row['calls']}c"
            for row in sorted(rows, key=lambda item: item["total_tokens"], reverse=True)[:5]
        )
        log("agent", f"Tool token attribution: {top}", "result")

    try:
        profile = deps.memory.get_profile(deps.session_id) or {}
    except Exception:
        profile = {}

    baseline_rps = float(profile.get("baseline_rps") or 0.0)
    after_rps = float(state.get("after_rps") or 0.0)
    improvement_pct = ((after_rps - baseline_rps) / baseline_rps * 100) if baseline_rps else 0.0
    notes = str(result.output).strip()
    if not notes:
        findings = state.get("findings") or []
        if findings:
            params = ", ".join(str(finding.get("parameter", "unknown")) for finding in findings[:4])
            notes = f"Applied findings: {params}."
        else:
            notes = "Diagnosis completed."
    guardrail_failure = str(state.get("guardrail_failure") or "").strip()
    if guardrail_failure:
        notes = f"{notes} Guardrail: {guardrail_failure}"

    return DiagnosisOutput(
        nginx_applied=bool(state.get("nginx_applied", False)),
        system_applied=bool(state.get("system_applied", False)),
        after_rps=after_rps,
        improvement_pct=improvement_pct,
        notes=notes,
        rca_records=list(state.get("rca_records") or []),
        recommendations=list(state.get("recommendations") or []),
    )


async def _run_debate_planner(agent, model, deps: AgentDeps, context_prompt: str):
    ctx = SimpleNamespace(deps=deps)
    inspect_nginx = agent._function_toolset.tools["inspect_nginx_config"].function
    inspect_system = agent._function_toolset.tools["inspect_system_tuning"].function
    inspect_irq = agent._function_toolset.tools["inspect_irq_distribution"].function
    save_rca = agent._function_toolset.tools["save_rca"].function
    save_recommendations = agent._function_toolset.tools["save_recommendations"].function

    nginx_inspection = await inspect_nginx(ctx)
    system_inspection = await inspect_system(ctx)
    irq_inspection = await inspect_irq(ctx)

    nginx_prompt = (
        "You are an NGINX performance expert. Review only nginx config evidence. "
        "Do not recommend kernel or IRQ changes. Return strict JSON with keys "
        "summary, rca_records, recommendations, counterpoints.\n\n"
        f"Shared Context:\n{context_prompt}\n\n"
        f"NGINX Inspection:\n{json.dumps(nginx_inspection, ensure_ascii=True)}"
    )
    rhel_prompt = (
        "You are a RHEL Linux performance expert. "
        "Review only kernel/system/IRQ evidence. "
        "Do not recommend nginx-only config changes "
        "unless they directly depend on system evidence. "
        "Return strict JSON with keys summary, rca_records, recommendations, counterpoints.\n\n"
        f"Shared Context:\n{context_prompt}\n\n"
        f"System Inspection:\n{json.dumps(system_inspection, ensure_ascii=True)}\n\n"
        f"IRQ Inspection:\n{json.dumps(irq_inspection, ensure_ascii=True)}"
    )

    (nginx_analysis, nginx_usage), (rhel_analysis, rhel_usage) = await asyncio.gather(
        asyncio.to_thread(_invoke_json_planner, model, "planner.nginx_expert", nginx_prompt, deps),
        asyncio.to_thread(_invoke_json_planner, model, "planner.rhel_expert", rhel_prompt, deps),
    )
    if _planner_debug_enabled(deps):
        tool_result(
            "debug",
            f"nginx_expert raw: {json.dumps(nginx_analysis, ensure_ascii=False)}",
        )
        tool_result(
            "debug",
            f"rhel_expert raw: {json.dumps(rhel_analysis, ensure_ascii=False)}",
        )

    synth_prompt = (
        "You are the synthesis arbiter between an NGINX expert "
        "and a RHEL Linux performance expert. "
        "Merge their outputs into one final plan. "
        "Keep only grounded recommendations supported by evidence. "
        "Prefer nginx fixes first, system fixes second, IRQ fixes only if clearly justified. "
        "Return strict JSON with keys summary, rca_records, recommendations.\n\n"
        f"NGINX Expert:\n{json.dumps(nginx_analysis, ensure_ascii=True)}\n\n"
        f"RHEL Expert:\n{json.dumps(rhel_analysis, ensure_ascii=True)}"
    )
    synthesis, synth_usage = _invoke_json_planner(model, "planner.synthesizer", synth_prompt, deps)
    if _planner_debug_enabled(deps):
        tool_result("debug", f"synthesizer raw: {json.dumps(synthesis, ensure_ascii=False)}")

    _save_planner_artifact(deps, "nginx_expert", nginx_analysis)
    _save_planner_artifact(deps, "rhel_expert", rhel_analysis)
    _save_planner_artifact(deps, "synthesizer", synthesis)

    rca_records = _coerce_records(synthesis.get("rca_records"), deps=deps)
    recommendations = _coerce_recommendations(synthesis.get("recommendations"), deps=deps)
    if rca_records:
        await save_rca(ctx, rca_records)
    if recommendations:
        await save_recommendations(ctx, recommendations)

    total_in = (
        nginx_usage["input_tokens"] + rhel_usage["input_tokens"] + synth_usage["input_tokens"]
    )
    total_out = (
        nginx_usage["output_tokens"] + rhel_usage["output_tokens"] + synth_usage["output_tokens"]
    )
    return SimpleNamespace(
        output=str(synthesis.get("summary") or "Debate planning completed.").strip(),
        usage=lambda: SimpleNamespace(input_tokens=total_in, output_tokens=total_out),
        all_messages=lambda: [],
    )


@contextmanager
def _null_generation():
    yield None


def _resolve_model_name(model: Any) -> str:
    for attr in ("model_name", "model", "_model", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return model.__class__.__name__


def _invoke_json_planner(
    model, name: str, prompt: str, deps: AgentDeps | None = None
) -> tuple[dict[str, Any], dict[str, int]]:
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(
            content=(
                f"You are {name}. Return JSON only. No markdown fences. "
                "If unsure, return empty arrays instead of prose."
            )
        ),
        HumanMessage(content=prompt),
    ]
    langfuse = getattr(deps, "langfuse", None) if deps else None
    with (
        langfuse.generation(
            name,
            model=_resolve_model_name(model),
            input={"messages": summarize_messages(messages)},
            metadata={
                "planner_name": name,
                "session_id": getattr(deps, "session_id", ""),
                "planner_mode": "debate",
            },
            model_parameters={"temperature": 0},
        )
        if langfuse
        else _null_generation()
    ):
        response = model.invoke(messages)
    text = _extract_final_text([response])
    payload = _extract_json_dict(text)
    usage = getattr(response, "usage_metadata", None) or {}
    if langfuse:
        langfuse.update_generation(
            output={"text": text[:4000], "payload": payload},
            usage_details={
                "prompt_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                "completion_tokens": int(
                    usage.get("output_tokens") or usage.get("completion_tokens") or 0
                ),
            },
        )
    return payload, {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
    }


def _extract_json_dict(text: str) -> dict[str, Any]:
    if not text:
        return {}
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if "\n" in candidate:
            candidate = candidate.split("\n", 1)[1]
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _planner_debug_enabled(deps: AgentDeps) -> bool:
    cfg = getattr(deps, "config", {}) or {}
    return bool((cfg.get("agent") or {}).get("debug_planner_payloads", False))


def _sanitize_debug_text(value: str, limit: int = 4000) -> str:
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    text = text.replace("\u202f", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = f"{text[: limit - 3]}..."
    return text


def _coerce_records(value: Any, deps: AgentDeps | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, Any]] = []
    for idx, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            if deps and _planner_debug_enabled(deps):
                tool_result("debug", f"synthesized rca_{idx} dropped: non-dict item {item!r}")
            continue
        issue = str(item.get("issue", "")).strip()
        cause = str(item.get("cause", "")).strip()
        description = str(item.get("description", "")).strip()
        impact = str(item.get("impact", "")).strip()
        symptom = str(item.get("symptom", "")).strip() or issue or cause or description
        root_cause = str(item.get("root_cause", "")).strip()
        if not root_cause:
            if impact:
                root_cause = impact
            else:
                root_cause = cause or description
        recommendation = str(item.get("recommendation", "")).strip()
        if not recommendation:
            recommendation = impact or "no recommendation"
        if not symptom or not root_cause:
            if deps and _planner_debug_enabled(deps):
                tool_result(
                    "debug",
                    (
                        f"synthesized rca_{idx} dropped: missing required fields "
                        f"raw={json.dumps(item, ensure_ascii=True)[:500]}"
                    ),
                )
            continue
        records.append(
            {
                "symptom": symptom,
                "root_cause": root_cause,
                "confidence": _coerce_float(item.get("confidence", 0.0)),
                "recommendation": recommendation or "no recommendation",
                "evidence": item.get("evidence", []),
            }
        )
    return records


def _coerce_recommendations(value: Any, deps: AgentDeps | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    recommendations: list[dict[str, Any]] = []
    for idx, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            if deps and _planner_debug_enabled(deps):
                tool_result(
                    "debug",
                    f"synthesized recommendation_{idx} dropped: non-dict item {item!r}",
                )
            continue
        normalized_item = _normalize_synthesized_recommendation(item)
        changes = normalized_item.get("changes", {})
        if not isinstance(changes, dict) or not changes:
            if deps and _planner_debug_enabled(deps):
                tool_result(
                    "debug",
                    (
                        f"synthesized recommendation_{idx} dropped: empty/invalid changes "
                        f"raw={json.dumps(item, ensure_ascii=True)[:500]}"
                    ),
                )
            continue
        recommendations.append(
            {
                "title": str(normalized_item.get("title", "")).strip() or f"recommendation_{idx}",
                "recommendation": str(normalized_item.get("recommendation", "")).strip()
                or "no recommendation",
                "rationale": str(normalized_item.get("rationale", "")).strip() or "no rationale",
                "expected_benefit": str(normalized_item.get("expected_benefit", "")).strip()
                or "no expected benefit",
                "risk_level": str(normalized_item.get("risk_level", "medium")).strip().lower()
                or "medium",
                "validation": str(normalized_item.get("validation", "")).strip()
                or "manual verification required",
                "scope": str(normalized_item.get("scope", "nginx")).strip().lower() or "nginx",
                "changes": changes,
            }
        )
    return recommendations


def _normalize_synthesized_recommendation(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    changes = item.get("changes")
    if isinstance(changes, dict) and changes:
        return normalized

    rec_type = str(item.get("type", "")).strip().lower()
    if rec_type in {"nginx", "system", "irq"}:
        normalized["scope"] = "system" if rec_type in {"system", "irq"} else "nginx"

    setting = item.get("setting")
    value = item.get("value")
    if isinstance(setting, list) and isinstance(value, list) and len(setting) == len(value):
        normalized["changes"] = {str(k): str(v) for k, v in zip(setting, value)}
        normalized["title"] = (
            normalized.get("title") or str(item.get("description", "")).strip() or "batch setting"
        )
        normalized["recommendation"] = (
            normalized.get("recommendation")
            or str(item.get("description", "")).strip()
            or "apply configuration changes"
        )
        normalized["rationale"] = (
            normalized.get("rationale") or normalized.get("justification") or ""
        )
        return normalized

    if setting and value not in (None, ""):
        normalized["changes"] = {str(setting): str(value)}
        normalized["title"] = (
            normalized.get("title") or str(item.get("description", "")).strip() or f"Set {setting}"
        )
        normalized["recommendation"] = (
            normalized.get("recommendation")
            or str(item.get("description", "")).strip()
            or f"Set {setting} to {value}"
        )
        normalized["rationale"] = (
            normalized.get("rationale") or normalized.get("justification") or ""
        )
        return normalized

    directive_raw = item.get("directive")
    value = item.get("value")

    # Parse directive(s) into changes — handles both string and list forms
    directive_changes: dict[str, str] = {}
    directives = (
        directive_raw
        if isinstance(directive_raw, list)
        else [directive_raw]
        if isinstance(directive_raw, str) and directive_raw.strip()
        else []
    )
    for d in directives:
        d_str = str(d).strip().rstrip(";").strip()
        if not d_str:
            continue
        if value not in (None, ""):
            # Explicit value provided separately
            directive_changes[d_str] = str(value)
        else:
            # Parse "directive_name value" from the string itself
            parts = d_str.split(None, 1)
            if len(parts) == 2:
                directive_changes[parts[0]] = parts[1].rstrip(";").strip()
            elif len(parts) == 1:
                # Single word like "on" or "off" — can't determine key/value
                pass
    if directive_changes:
        normalized["scope"] = normalized.get("scope") or "nginx"
        normalized["changes"] = directive_changes
        normalized["title"] = (
            normalized.get("title")
            or str(item.get("action", "")).strip()
            or f"Set {', '.join(directive_changes)}"
        )
        normalized["recommendation"] = (
            normalized.get("recommendation")
            or str(item.get("action", "")).strip()
            or f"Apply directives: {', '.join(directive_changes)}"
        )
        normalized["rationale"] = (
            normalized.get("rationale") or normalized.get("justification") or ""
        )
        return normalized

    # Try config_snippet — synthesizer sometimes puts nginx directives here
    snippet = str(item.get("config_snippet", "")).strip()
    if snippet:
        snippet_changes: dict[str, str] = {}
        for line in snippet.replace("\\n", "\n").splitlines():
            line = line.strip().rstrip(";").strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                snippet_changes[parts[0]] = parts[1].rstrip(";").strip()
        if snippet_changes:
            normalized["scope"] = normalized.get("scope") or "nginx"
            normalized["changes"] = snippet_changes
            normalized["title"] = (
                normalized.get("title") or str(item.get("action", "")).strip() or "apply config"
            )
            normalized["recommendation"] = (
                normalized.get("recommendation") or str(item.get("action", "")).strip() or snippet
            )
            normalized["rationale"] = (
                normalized.get("rationale") or normalized.get("justification") or ""
            )
            return normalized

    action = str(item.get("action", "")).strip().rstrip(";")
    if action:
        action_changes = _extract_changes_from_action(action, normalized.get("scope", ""))
        if action_changes:
            normalized["changes"] = action_changes
            normalized["title"] = normalized.get("title") or action or "apply tuning"
            normalized["recommendation"] = normalized.get("recommendation") or action
            normalized["rationale"] = (
                normalized.get("rationale") or normalized.get("justification") or ""
            )
            return normalized

    command = item.get("command")
    if isinstance(command, str) and command.strip():
        extracted = _extract_changes_from_commands([command])
        if extracted:
            normalized["scope"] = normalized.get("scope") or "system"
            normalized["changes"] = extracted
            normalized["title"] = normalized.get("title") or action or "system tuning"
            normalized["recommendation"] = normalized.get("recommendation") or action or command
            normalized["rationale"] = (
                normalized.get("rationale") or normalized.get("justification") or ""
            )
            return normalized

    commands = item.get("commands")
    if isinstance(commands, list) and commands:
        extracted = _extract_changes_from_commands(commands)
        if extracted:
            normalized["scope"] = normalized.get("scope") or "system"
            normalized["changes"] = extracted
            normalized["title"] = (
                normalized.get("title") or str(item.get("action", "")).strip() or "system tuning"
            )
            normalized["recommendation"] = (
                normalized.get("recommendation")
                or str(item.get("action", "")).strip()
                or "apply system tuning"
            )
            normalized["rationale"] = (
                normalized.get("rationale") or normalized.get("justification") or ""
            )
            return normalized

    return normalized


def _extract_changes_from_action(action: str, scope: str) -> dict[str, str]:
    extracted: dict[str, str] = {}
    nginx_match = re.match(
        r"(?i)(?:set|enable|disable|configure|reduce)\s+([A-Za-z0-9_]+)\s*(.*)$", action
    )
    if nginx_match and (not scope or scope == "nginx"):
        key = nginx_match.group(1).strip()
        remainder = nginx_match.group(2).strip().strip(";")
        lowered = action.lower()
        if key and remainder:
            value = remainder
            if key == "aio" and value.lower() == "threads":
                extracted[key] = "threads"
            else:
                extracted[key] = value
        elif key and "disable" in lowered:
            extracted[key] = "off"
        elif key and "enable" in lowered:
            extracted[key] = "on"
        elif key == "worker_cpu_affinity" and "auto" in lowered:
            extracted[key] = "auto"
        return extracted

    return extracted


def _extract_changes_from_commands(commands: list[Any]) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for raw_command in commands:
        command = str(raw_command).strip()
        if not command:
            continue

        if "sysctl" in command and "-w" in command:
            # Extract all key=value pairs from multi-param sysctl commands
            sysctl_pairs = re.findall(r"([A-Za-z0-9_.]+)=('[^']*'|\"[^\"]*\"|[^\s]+)", command)
            if sysctl_pairs:
                for key, value in sysctl_pairs:
                    extracted[key] = value.strip().strip('"').strip("'")
                continue

        if "transparent_hugepage/enabled" in command and "echo never" in command:
            extracted["transparent_hugepage"] = "never"
            continue

        if command.startswith("setenforce "):
            mode = command.split(None, 1)[1].strip()
            extracted["selinux"] = "permissive" if mode in {"0", "permissive"} else "enforcing"
            continue

        if "tcp_tw_reuse" in command and "sysctl -w" in command:
            tw_match = re.search(r"tcp_tw_reuse=(.+)$", command)
            if tw_match:
                extracted["net.ipv4.tcp_tw_reuse"] = tw_match.group(1).strip().strip('"').strip("'")
            continue

        ulimit_match = re.search(r"ulimit\s+-n\s+(\d+)", command)
        if ulimit_match:
            extracted["nofile"] = ulimit_match.group(1)
            continue

        nofile_match = re.search(r"nofile\s+(\d+)", command)
        if nofile_match and "limits.conf" in command and "soft" in command:
            extracted["nofile"] = nofile_match.group(1)
            continue

        if "irqbalance" in command and any(v in command for v in ("enable", "start")):
            extracted["irqbalance"] = "enabled"
            continue

    return extracted


def _save_planner_artifact(deps: AgentDeps, source: str, payload: dict[str, Any]) -> None:
    deps.memory.save_context(
        deps.session_id,
        "command_output",
        source,
        json.dumps(payload, ensure_ascii=True),
        f"{source} planner output",
    )
    file_map = {
        "nginx_expert": "01_nginx_expert.md",
        "rhel_expert": "02_rhel_expert.md",
        "synthesizer": "03_synthesizer.md",
    }
    _persist_hypothesis_markdown(
        deps,
        filename=file_map.get(source, f"{source}.md"),
        title=source.replace("_", " ").title(),
        sections=[
            ("Summary", str(payload.get("summary", "")).strip() or "No summary returned."),
            ("Payload", _markdown_json(payload)),
        ],
    )


def _hypothesis_enabled(deps: AgentDeps) -> bool:
    cfg = getattr(deps, "config", {}) or {}
    agent_cfg = cfg.get("agent") or {}
    if "persist_hypotheses" in agent_cfg:
        return bool(agent_cfg.get("persist_hypotheses"))
    return bool(agent_cfg.get("debug_planner_payloads", False))


def _hypothesis_dir(deps: AgentDeps) -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "hypothesis" / str(getattr(deps, "session_id", "unknown"))


def _markdown_json(payload: Any) -> str:
    return f"```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```"


def _persist_hypothesis_markdown(
    deps: AgentDeps,
    *,
    filename: str,
    title: str,
    sections: list[tuple[str, str]],
    append: bool = False,
) -> None:
    if not _hypothesis_enabled(deps):
        return
    path = _hypothesis_dir(deps)
    path.mkdir(parents=True, exist_ok=True)
    target = path / filename
    chunks: list[str] = []
    if not append or not target.exists():
        chunks.append(f"# {title}")
    for heading, body in sections:
        chunks.append(f"## {heading}")
        chunks.append(body)
    text = "\n\n".join(chunks).strip() + "\n"
    if append and target.exists():
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n" + text)
    else:
        target.write_text(text, encoding="utf-8")


def _extract_final_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
            text = "".join(text_parts).strip()
            if text:
                return text
    return ""


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _coerce_notes(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=True)
    return str(value)


def _aggregate_usage(messages: list[Any]) -> dict[str, int]:
    input_tokens = 0
    output_tokens = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None) or {}
        if not usage:
            continue
        input_tokens += int(
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("input_token_count")
            or 0
        )
        output_tokens += int(
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("output_token_count")
            or 0
        )
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}
