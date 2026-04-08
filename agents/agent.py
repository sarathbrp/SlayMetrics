from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections import Counter
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

    service_applied: bool
    system_applied: bool
    after_rps: float = 0.0
    improvement_pct: float = 0.0
    notes: str = ""
    rca_records: list[dict[str, Any]] | None = None
    recommendations: list[dict[str, Any]] | None = None
    apply_results: dict[str, Any] | None = None
    guardrails_triggered: list[str] | None = None
    eval_results: dict[str, Any] | None = None
    # Expert state — passed between iterations
    _service_analysis: dict[str, Any] | None = None
    _rhel_analysis: dict[str, Any] | None = None
    _inspection: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.after_rps = _coerce_float(self.after_rps)
        self.improvement_pct = _coerce_float(self.improvement_pct)
        self.notes = _coerce_notes(self.notes)
        self.rca_records = list(self.rca_records or [])
        self.recommendations = list(self.recommendations or [])
        self.apply_results = dict(self.apply_results or {})
        self.guardrails_triggered = list(self.guardrails_triggered or [])
        self.eval_results = dict(self.eval_results or {})


SYSTEM_PROMPT = """\
You are SlayMetricsAgent.

Diagnostic sequence:
1. Inspect service configuration first.
2. Inspect RHEL/kernel/system tuning second.
3. Inspect IRQ distribution and CPU affinity last.
4. Save structured RCA via save_rca after all three stages are reviewed.
5. Save transparent human-readable recommendations via save_recommendations.
6. Return one short plain-text summary sentence.

Do not benchmark or apply changes during planning. Python will do one combined apply
and one benchmark after planning is complete.

For apply_service_tuning and apply_system_tuning:
- Pass a structured object under the changes field
- Example: {"changes":{"access_log":"off","listen_backlog":"65535"}}
- If needed, you may also pass directive names directly as tool arguments instead of nesting them

For save_findings:
- Pass a structured list under the findings field
- Example: {"findings":[{"parameter":"service.access_log","before_value":"on","after_value":"off"}]}

For save_rca:
- Pass a structured list under the records field
- Each RCA record must include symptom, root_cause, confidence, recommendation
- evidence may be a list of short strings
- Example: {"records":[{"symptom":"High p99","root_cause":"backlog too low","confidence":0.9}]}

For save_recommendations:
- Pass a structured list under the recommendations field
- Each recommendation must include title, recommendation, rationale,
  expected_benefit, risk_level, validation, scope, changes
- scope must be service or system
- changes must be a structured object of parameter -> target value
- risk_level should be low, medium, or high
- Example: {"recommendations":[{"title":"Raise limit","scope":"service","changes":{"aio":"threads"}}]}

Use the inspect tool outputs as the source of truth for current values and candidate target values.
Do not invent target values outside the inspect outputs.

Do NOT apply gzip — test files are random binary data, compression wastes CPU.

Rules:
- Stage 1 must focus on service configuration only.
- Stage 2 must focus on RHEL/kernel/system tuning only.
- Stage 3 must focus on IRQ distribution / CPU affinity evidence only.
- Only recommend IRQ-related action if the IRQ stage evidence supports it.
- Skip already-applied fixes, no packages, no reboot, no pre-fix benchmark.
- Do not call apply_service_tuning, apply_system_tuning, run_benchmark,
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
        "service_applied": False,
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
    # Build allowlists from new config categories (webserver_targets, kernel_targets)
    # with backward compat for old service_targets/system_targets
    service_targets: dict[str, str] = {
        str(k): str(v)
        for k, v in (
            _tuning_cfg.get("webserver_targets") or _tuning_cfg.get("service_targets") or {}
        ).items()
    }
    # Kernel-only targets used by inspect_system_tuning (backward-compatible behavior).
    system_targets: dict[str, str] = {
        str(k): str(v)
        for k, v in (
            _tuning_cfg.get("kernel_targets") or _tuning_cfg.get("system_targets") or {}
        ).items()
    }
    resource_targets: dict[str, str] = {
        str(k): str(v) for k, v in (_tuning_cfg.get("resource_limits_targets") or {}).items()
    }
    network_targets: dict[str, str] = {
        str(k): str(v) for k, v in (_tuning_cfg.get("network_targets") or {}).items()
    }
    storage_targets: dict[str, str] = {
        str(k): str(v) for k, v in (_tuning_cfg.get("storage_targets") or {}).items()
    }
    # Recommendation filtering should accept all non-webserver categories.
    system_allowed_params: set[str] = (
        set(system_targets) | set(resource_targets) | set(network_targets) | set(storage_targets)
    )

    # Alias map: LLMs use variant key names → normalize to config allowlist keys
    _param_aliases: dict[str, str] = {
        "selinux_mode": "selinux",
        "selinux": "selinux",
        "cgroup_IOWeight": "cgroup_io_weight",
        "cgroup_CPUWeight": "cgroup_cpu_weight",
        "cgroup_ioweight": "cgroup_io_weight",
        "cgroup_cpuweight": "cgroup_cpu_weight",
        "IOWeight": "cgroup_io_weight",
        "CPUWeight": "cgroup_cpu_weight",
        "tc_qdisc": "tc_rules",
        "defaultlimitnofile": "systemd_nofile",
        "DefaultLimitNOFILE": "systemd_nofile",
        "fs.file-max": "fs.file_max",
        "transparent_hugepage/enabled": "transparent_hugepage",
        "transparent_hugepage/defrag": "transparent_hugepage",
        "SELINUX": "selinux",
        "keepalive_timeout": "keepalive_timeout",
    }

    def _resolve_param_alias(key: str) -> str:
        """Normalize a parameter key using the alias map."""
        key_s = str(key).strip()
        if key_s.startswith("tc_qdisc_"):
            return "tc_rules"
        return _param_aliases.get(key_s, _param_aliases.get(key_s.lower(), key_s))

    _SHELL_UNSAFE = re.compile(r"[;&|`$(){}!\\\n\r]")

    def _sanitize_shell_value(raw: str) -> str:
        """Reject values containing shell metacharacters."""
        if _SHELL_UNSAFE.search(raw):
            raise ValueError(f"unsafe shell characters in value: {raw!r}")
        return raw.strip()

    def _emit_apply_param(
        scope: str,
        param: str,
        value: Any,
        status: str,
        detail: str = "",
    ) -> None:
        value_s = str(value).strip()
        msg = f"{scope}.{param}={value_s} status={status}"
        if detail:
            msg += f" detail={detail[:180]}"
        tool_result("apply_param", msg)

    def _extract_result_params(result_bucket: Any) -> set[str]:
        """Coerce keys to str — result payloads may contain int keys from JSON."""
        if isinstance(result_bucket, dict):
            return {str(k) for k in result_bucket.keys()}
        if isinstance(result_bucket, list):
            return {str(item) for item in result_bucket}
        return set()

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
            "service_worker_cores": sorted(set(worker_cores)),
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

    def inspect_service_impl(deps: AgentDeps) -> dict:
        svc_name = (deps.config.get("service") or {}).get("name", "service")
        tool_call("inspect", f"{svc_name} config — stage 1 analysis")

        # Delegate to adapter — each adapter knows how to inspect its own service
        adapter_result = deps.adapter.inspect(service_targets)
        needs_fixing = adapter_result.get("needs_fixing", {})
        current = adapter_result.get("current", {})
        ok_count = adapter_result.get("ok_count", 0)

        result = {
            "needs_fixing": needs_fixing,
            "already_ok": [p for p in service_targets if p not in needs_fixing],
            "current": current,
        }
        _langfuse_event(
            deps,
            "tool.inspect_service_config",
            input={"directive_count": len(service_targets)},
            output=result,
        )
        deps.token_counter.tool_calls += 1
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            "inspect_service",
            str(result),
            f"{svc_name}: {len(needs_fixing)} need fixing, {ok_count} ok",
        )
        tool_result(
            "inspect", f"{svc_name}: {len(needs_fixing)} need fixing, {ok_count} already ok"
        )
        state["service_inspection"] = result
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

    def apply_service_impl(
        deps: AgentDeps, changes: dict[str, str] | str | None, **kwargs: Any
    ) -> dict:
        normalized_changes, parse_error = _coerce_tool_changes(changes, kwargs, "apply_service")
        if parse_error:
            tool_call("apply_service", "invalid input payload")
            tool_result("apply_service", f"FAILED: {parse_error}")
            _langfuse_event(
                deps,
                "tool.apply_service_tuning",
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
                for param in unsupported:
                    _emit_apply_param("webserver", param, "<unsupported>", "failed", "unsupported")
                error = f"unsupported nginx directives: {', '.join(unsupported)}"
                tool_call("apply_service", "unsupported directives")
                tool_result("apply_service", f"FAILED: {error}")
                _langfuse_event(
                    deps,
                    "tool.apply_service_tuning",
                    input={"changes": changes_dict, "unsupported": unsupported},
                    output={"error": error},
                    level="ERROR",
                )
                deps.token_counter.tool_calls += 1
                return {"applied": [], "failed": unsupported, "reload": "FAILED", "error": error}

        tool_call("apply_service", f"{len(changes_dict)} changes: {', '.join(changes_dict.keys())}")

        config_path = deps.config["service"]["config_path"]
        batch_backup = f"/tmp/slay_nginx_batch_{deps.session_id}.conf"
        deps.ssh.execute(f"cp {config_path} {batch_backup}")

        applied = []
        failed = list(unsupported)
        for param, value in changes_dict.items():
            success = deps.adapter.apply_config(param, value)
            if success:
                applied.append(param)
                _emit_apply_param("webserver", param, value, "applied")
            else:
                failed.append(param)
                _emit_apply_param("webserver", param, value, "failed", "adapter.apply_config returned false")
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
                tool_result("apply_service", f"FAILED: {result['error']}")
                _langfuse_event(
                    deps,
                    "tool.apply_service_tuning",
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
            tool_result("apply_service", f"FAILED: {error_msg}")
            _langfuse_event(
                deps,
                "tool.apply_service_tuning",
                input={"changes": changes_dict},
                output=result,
                level="ERROR",
            )
            return result

        reload_ok = deps.adapter.reload()
        result = {"applied": applied, "failed": failed, "reload": "OK" if reload_ok else "FAILED"}
        if not reload_ok:
            for param in applied:
                _emit_apply_param(
                    "webserver", param, changes_dict.get(param, ""), "failed", "reload failed"
                )
        if unsupported:
            result["warning"] = f"ignored unsupported nginx directives: {', '.join(unsupported)}"

        deps.token_counter.tool_calls += 1
        state["service_applied"] = state["service_applied"] or bool(applied and reload_ok)
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
            "tool.apply_service_tuning",
            input={"changes": changes_dict},
            output=result,
        )
        tool_result("apply_service", summary)
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
            try:
                value = _sanitize_shell_value(str(value))
            except ValueError as exc:
                failed[param] = str(exc)
                _emit_apply_param("system", param, value, "failed", failed[param])
                continue
            if param == "transparent_hugepage":
                result = ssh.execute(
                    f"echo {value} > /sys/kernel/mm/transparent_hugepage/enabled 2>&1"
                )
                if result.ok:
                    applied[param] = value
                    _emit_apply_param("system", param, value, "applied")
                else:
                    failed[param] = result.stderr.strip()
                    _emit_apply_param("system", param, value, "failed", failed[param])
            elif param == "selinux":
                mode = "0" if value.lower() in ("permissive", "0") else "1"
                result = ssh.execute(f"setenforce {mode} 2>&1")
                # Persist across reboots
                if value.lower() in ("permissive", "0"):
                    persist = ssh.execute(
                        "sed -i 's/^SELINUX=enforcing/SELINUX=permissive/'"
                        " /etc/selinux/config 2>/dev/null"
                    )
                    if not persist.ok:
                        # Rollback the runtime change since we cannot persist
                        rollback_mode = "1" if mode == "0" else "0"
                        ssh.execute(f"setenforce {rollback_mode} 2>&1")
                        failed[param] = persist.stderr.strip() or "failed to persist SELinux mode"
                        _emit_apply_param("system", param, value, "failed", failed[param])
                        continue
                # Verify it actually changed
                verify = ssh.execute("getenforce 2>/dev/null")
                actual = verify.stdout.strip().lower()
                expected = "permissive" if mode == "0" else "enforcing"
                if actual == expected:
                    applied[param] = value
                    _emit_apply_param("system", param, value, "applied")
                else:
                    failed[param] = f"setenforce ran but getenforce={actual}"
                    _emit_apply_param("system", param, value, "failed", failed[param])
            elif param == "cpu_governor":
                result = ssh.execute(
                    f"echo {value} | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>&1"
                )
                if result.ok:
                    applied[param] = value
                    _emit_apply_param("system", param, value, "applied")
                else:
                    failed[param] = result.stderr.strip()
                    _emit_apply_param("system", param, value, "failed", failed[param])
            elif param == "net.ipv4.ip_local_port_range":
                result = ssh.execute(f'sysctl -w net.ipv4.ip_local_port_range="{value}" 2>&1')
                if result.ok:
                    applied[param] = value
                    _emit_apply_param("system", param, value, "applied")
                else:
                    failed[param] = result.stderr.strip()
                    _emit_apply_param("system", param, value, "failed", failed[param])
            elif param == "nofile":
                cmds = [
                    "sed -i '/nofile/d' /etc/security/limits.conf 2>/dev/null",
                    f"echo '* soft nofile {value}' >> /etc/security/limits.conf",
                    f"echo '* hard nofile {value}' >> /etc/security/limits.conf",
                    f"systemctl set-property nginx.service LimitNOFILE={value} 2>/dev/null",
                    "systemctl daemon-reload 2>&1",
                    "systemctl restart nginx 2>&1",
                ]
                cmd_errors = []
                for cmd in cmds:
                    cmd_result = ssh.execute(cmd)
                    if not cmd_result.ok:
                        err = cmd_result.stderr.strip() or cmd_result.stdout.strip() or "command failed"
                        cmd_errors.append(f"{cmd}: {err}")
                        break
                if cmd_errors:
                    failed[param] = "; ".join(cmd_errors)[:500]
                    _emit_apply_param("system", param, value, "failed", failed[param])
                    continue
                limits = ssh.execute(
                    "cat /proc/$(pgrep -o nginx)/limits 2>/dev/null | grep 'Max open files'"
                )
                if re.search(rf"Max open files\s+{re.escape(str(value))}\s", limits.stdout):
                    applied[param] = value
                    _emit_apply_param("system", param, value, "applied")
                else:
                    failed[param] = (
                        "nginx limit verification failed: "
                        + (limits.stdout.strip() or limits.stderr.strip() or "no limits output")
                    )[:500]
                    _emit_apply_param("system", param, value, "failed", failed[param])
            elif param == "irqbalance":
                result = ssh.execute("systemctl enable --now irqbalance 2>&1")
                if result.ok:
                    applied[param] = value
                    _emit_apply_param("system", param, value, "applied")
                else:
                    failed[param] = result.stderr.strip() or "irqbalance enable failed"
                    _emit_apply_param("system", param, value, "failed", failed[param])
            else:
                result = ssh.execute(f"sysctl -w {param}={value} 2>&1")
                if result.ok:
                    applied[param] = value
                    _emit_apply_param("system", param, value, "applied")
                else:
                    failed[param] = result.stderr.strip()
                    _emit_apply_param("system", param, value, "failed", failed[param])

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
            raw_scope = str(item.get("scope", "nginx")).strip().lower() or "nginx"
            scope = {
                "service": "nginx",
                "webserver": "nginx",
                "nginx": "nginx",
                "system": "system",
                "kernel": "system",
                "resource_limits": "system",
                "network": "system",
                "storage": "system",
            }.get(raw_scope, raw_scope)
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
            allowed_params = set(service_targets) if scope == "nginx" else set(system_allowed_params)
            filtered_changes = {
                _resolve_param_alias(key): value
                for key, value in (changes or {}).items()
                if _resolve_param_alias(key) in allowed_params
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
                # Record rejected recommendation for lessons-learned tracking
                from core.apply_failures import REASON_RECOMMEND_REJECTED, record_failure

                _iter = getattr(deps, "iteration", 0)
                for _rp, _rv in (changes or {}).items():
                    record_failure(
                        deps.memory,
                        session_id=deps.session_id,
                        iteration=_iter,
                        category=scope or "unknown",
                        parameter=_rp,
                        attempted_value=str(_rv),
                        failure_reason=REASON_RECOMMEND_REJECTED,
                        llm_param_name=_rp,
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

    def _verify_applied_changes(
        deps: AgentDeps,
        service_applied: list[str] | Any,
        service_changes: dict[str, str],
        system_applied: dict[str, str],
    ) -> dict[str, Any]:
        """Re-read DUT state and verify changes actually took effect."""
        ssh = deps.ssh
        mismatches: list[dict[str, str]] = []

        # ── Verify service directives ────────────────────────────────────
        if service_applied:
            raw = ssh.execute("nginx -T 2>/dev/null").stdout
            for param in service_applied:
                expected = service_changes.get(param, "")

                # Skip verify for params where "remove" means absence is correct
                if expected == "remove" and param in ("limit_req", "limit_conn"):
                    match = re.search(rf"^\s*{param}\s+", raw, re.MULTILINE)
                    if not match:
                        continue  # absent = success
                    actual = "still present"
                elif param == "error_log_level":
                    match = re.search(r"error_log\s+\S+\s+(\w+)\s*;", raw)
                    actual = match.group(1) if match else "warn"
                    if actual == expected:
                        continue
                elif param == "listen_backlog":
                    match = re.search(r"listen\s+.*backlog=(\d+)", raw)
                    actual = match.group(1) if match else "not set"
                else:
                    match = re.search(rf"^\s*{param}\s+(.+?);", raw, re.MULTILINE)
                    actual = match.group(1).strip() if match else "not set"
                if actual != expected and actual != "not set":
                    # Check if the value is functionally equivalent
                    if actual.replace(" ", "") == expected.replace(" ", ""):
                        continue
                if actual == "not set" or actual != expected:
                    mismatches.append(
                        {
                            "scope": "nginx",
                            "param": param,
                            "expected": expected,
                            "actual": actual,
                        }
                    )
                    # Verify phase is read-only: report mismatch but do not mutate config.

        # ── Verify system parameters ────────────────────────────────────
        sysctl_params = [p for p in system_applied if p.startswith("net.") or p.startswith("fs.")]
        for param in sysctl_params:
            expected = system_applied[param]
            result = ssh.execute(f"sysctl -n {param} 2>/dev/null")
            actual = result.stdout.strip()
            if actual != expected:
                mismatches.append(
                    {
                        "scope": "system",
                        "param": param,
                        "expected": expected,
                        "actual": actual,
                    }
                )
                # Verify phase is read-only: report mismatch but do not mutate config.

        if "transparent_hugepage" in system_applied:
            result = ssh.execute("cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null")
            match = re.search(r"\[(\w+)\]", result.stdout)
            actual = match.group(1) if match else "unknown"
            if actual != system_applied["transparent_hugepage"]:
                mismatches.append(
                    {
                        "scope": "system",
                        "param": "transparent_hugepage",
                        "expected": system_applied["transparent_hugepage"],
                        "actual": actual,
                    }
                )

        if "selinux" in system_applied:
            result = ssh.execute("getenforce 2>/dev/null")
            actual = result.stdout.strip().lower()
            if actual != system_applied["selinux"]:
                mismatches.append(
                    {
                        "scope": "system",
                        "param": "selinux",
                        "expected": system_applied["selinux"],
                        "actual": actual,
                    }
                )

        if "nofile" in system_applied:
            result = ssh.execute(
                "cat /proc/$(pgrep -o nginx)/limits 2>/dev/null | grep 'Max open files'"
            )
            actual_line = result.stdout.strip()
            nofile_match = re.search(r"(\d+)\s+(\d+)", actual_line)
            actual_soft = nofile_match.group(1) if nofile_match else "unknown"
            if actual_soft != system_applied["nofile"]:
                mismatches.append(
                    {
                        "scope": "system",
                        "param": "nofile",
                        "expected": system_applied["nofile"],
                        "actual": actual_soft,
                    }
                )
                # Verify phase is read-only: record mismatch and let apply phase decide remediation.

        # Log results
        if mismatches:
            summary = "; ".join(
                f"{m['param']}={m['actual']} (expected {m['expected']})" for m in mismatches
            )
            tool_result("verify", f"mismatches found: {summary}")
            deps.memory.save_context(
                deps.session_id,
                "command_output",
                "post_apply_verification",
                json.dumps({"mismatches": mismatches}),
                f"verification: {len(mismatches)} mismatches",
            )
            # Record verify failures for lessons-learned tracking
            from core.apply_failures import record_verify_mismatches

            _iter = getattr(deps, "iteration", 0)
            record_verify_mismatches(deps.memory, deps.session_id, _iter, mismatches)
        else:
            tool_result("verify", "all changes verified on DUT")
            deps.memory.save_context(
                deps.session_id,
                "command_output",
                "post_apply_verification",
                json.dumps({"status": "all_verified"}),
                "verification: all changes confirmed on DUT",
            )

        return {"mismatches": mismatches}

    def _translate_recommendations(
        raw_recommendations: list[dict[str, Any]],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Extract nginx and system changes from recommendations.

        Uses service_targets/system_targets keys as anchors — scans all text
        fields in each recommendation for known parameter names, extracts
        values. Falls back to target defaults if value is unclear.
        No format-specific parsing. Works with ANY LLM output shape.
        """
        nginx_changes: dict[str, str] = {}
        system_changes: dict[str, str] = {}

        # Build a blob of all recommendation text for scanning
        all_text = json.dumps(raw_recommendations, ensure_ascii=False)

        # For each known target, check if any recommendation mentions it
        for param, default_value in service_targets.items():
            if param not in all_text:
                continue
            # Find the recommendation that mentions this param
            value = _extract_value_for_param(param, raw_recommendations)
            nginx_changes[param] = value or default_value

        for param, default_value in system_targets.items():
            if param not in all_text:
                continue
            value = _extract_value_for_param(param, raw_recommendations)
            system_changes[param] = value or default_value

        return nginx_changes, system_changes

    def _extract_value_for_param(param: str, recommendations: list[dict[str, Any]]) -> str | None:
        """Extract the value for a parameter from recommendation fields.

        Checks structured fields first (setting/value, changes, directive),
        then falls back to regex extraction from text fields.
        """
        for rec in recommendations:
            # 1. Direct changes dict
            changes = rec.get("changes", {})
            if isinstance(changes, dict) and param in changes:
                val = str(changes[param]).strip().rstrip(";").strip()
                # Strip inline comments
                if "#" in val:
                    val = val[: val.index("#")].strip().rstrip(";").strip()
                if val:
                    return val

            # 2. setting/value pair
            if str(rec.get("setting", "")).strip() == param:
                val = str(rec.get("value", "")).strip().rstrip(";").strip()
                if "#" in val:
                    val = val[: val.index("#")].strip().rstrip(";").strip()
                if val:
                    return val

            # 3. directive field: "param value;" or "param value"
            directive = rec.get("directive")
            directives = (
                directive
                if isinstance(directive, list)
                else [directive]
                if isinstance(directive, str)
                else []
            )
            for d in directives:
                d_str = str(d).strip().rstrip(";").strip()
                parts = d_str.split(None, 1)
                if parts and parts[0] == param and len(parts) == 2:
                    val = parts[1].strip().rstrip(";").strip()
                    if "#" in val:
                        val = val[: val.index("#")].strip()
                    if val:
                        return val

            # 4. config_snippet: parse lines for "param value;"
            snippet = str(rec.get("config_snippet", "")).strip()
            if snippet and param in snippet:
                for line in snippet.replace("\\n", "\n").splitlines():
                    line = line.strip().rstrip(";").strip()
                    if line.startswith("#"):
                        continue
                    # Special: listen ... backlog=N
                    if param == "listen_backlog" and "backlog=" in line:
                        m = re.search(r"backlog=(\d+)", line)
                        if m:
                            return m.group(1)
                    parts = line.split(None, 1)
                    if parts and parts[0] == param and len(parts) == 2:
                        val = parts[1].strip().rstrip(";").strip()
                        if "#" in val:
                            val = val[: val.index("#")].strip()
                        if val:
                            return val

            # 5. commands field: extract value from shell commands
            commands = rec.get("commands") or (
                [rec["command"]] if isinstance(rec.get("command"), str) else []
            )
            for cmd in commands:
                cmd_str = str(cmd).strip()
                # sysctl -w param=value
                m = re.search(rf"{re.escape(param)}=('[^']*'|\"[^\"]*\"|[^\s]+)", cmd_str)
                if m:
                    return m.group(1).strip().strip("'\"")
                # nginx directive in commands: "param value;"
                parts = cmd_str.rstrip(";").strip().split(None, 1)
                if parts and parts[0] == param and len(parts) == 2:
                    return parts[1].strip().rstrip(";").strip()

        return None

    def _batch_apply_service(deps: AgentDeps, changes: dict[str, str]) -> dict:
        """Apply all nginx changes in a single operation."""
        if not changes:
            return {"applied": [], "failed": [], "reload": "SKIPPED"}

        tool_call(
            "apply_service",
            f"batch {len(changes)} directives: {', '.join(changes.keys())}",
        )

        ssh = deps.ssh
        config_path = deps.config["service"]["config_path"]

        # Backup
        ssh.execute(f"cp {config_path} {config_path}.pre_batch")

        # Apply via adapter (it handles upsert + conf.d cleanup)
        applied = []
        failed = []
        # Apply in priority order: kill debug logging first (prevents OOM),
        # then worker_processes (fundamental), then file descriptors
        priority_order = [
            "error_log_level",
            "access_log",
            "worker_processes",
            "worker_rlimit_nofile",
        ]
        ordered_params = sorted(
            changes.keys(),
            key=lambda p: priority_order.index(p) if p in priority_order else 99,
        )
        for param in ordered_params:
            value = changes[param]
            if deps.adapter.apply_config(param, value):
                applied.append(param)
                _emit_apply_param("webserver", param, value, "applied")
            else:
                failed.append(param)
                _emit_apply_param("webserver", param, value, "failed", "adapter.apply_config returned false")

        # Reload/restart once after all changes.
        # Use hard restart (not graceful reload) when error_log_level changed —
        # graceful reload keeps old debug-logging workers alive, causing OOM.
        if applied:
            is_valid = deps.adapter.validate_config()
            if is_valid:
                if "error_log_level" in applied or "access_log" in applied:
                    restart_ok = deps.adapter.restart()
                else:
                    restart_ok = deps.adapter.reload()
                reload_status = "OK" if restart_ok else "FAILED"
                if not restart_ok:
                    failed = list(changes.keys())
                    applied = []
                    for param, value in changes.items():
                        _emit_apply_param("webserver", param, value, "failed", "reload/restart failed")
            else:
                # Rollback everything
                ssh.execute(f"cp {config_path}.pre_batch {config_path}")
                deps.adapter.reload()
                reload_status = "FAILED"
                failed = list(changes.keys())
                applied = []
                for param, value in changes.items():
                    _emit_apply_param("webserver", param, value, "failed", "post-apply validate failed")
        else:
            reload_status = "SKIPPED"

        result = {"applied": applied, "failed": failed, "reload": reload_status}
        tool_result(
            "apply_service",
            f"applied={applied} failed={failed} reload={reload_status}",
        )

        deps.token_counter.tool_calls += 1
        state["service_applied"] = state["service_applied"] or bool(applied)
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            f"batch_apply_service:{','.join(changes.keys())}"[:250],
            json.dumps(result),
            f"nginx batch: {len(applied)} applied, {len(failed)} failed",
        )
        return result

    def _batch_apply_system(deps: AgentDeps, changes: dict[str, str]) -> dict:
        """Apply all system changes in a single SSH script."""
        if not changes:
            return {"applied": {}, "failed": {}}

        tool_call(
            "apply_system",
            f"batch {len(changes)} params: {', '.join(changes.keys())}",
        )

        ssh = deps.ssh
        # Sanitize all values before building the script
        sanitized: dict[str, str] = {}
        pre_failed: dict[str, str] = {}
        for param, value in changes.items():
            try:
                sanitized[param] = _sanitize_shell_value(str(value))
            except ValueError as exc:
                pre_failed[param] = str(exc)
                _emit_apply_param("system", param, value, "failed", pre_failed[param])
        if not sanitized:
            return {"applied": {}, "failed": pre_failed}

        # Build a single script
        script_lines = ["#!/bin/bash", "set +e", "RESULT=''"]

        for param, value in sanitized.items():
            if param == "transparent_hugepage":
                script_lines.append(
                    f"echo {value} > /sys/kernel/mm/transparent_hugepage/enabled >/dev/null 2>&1"
                    f' && RESULT="$RESULT {param}=OK"'
                    f' || RESULT="$RESULT {param}=FAIL"'
                )
            elif param == "selinux":
                mode = "0" if value.lower() in ("permissive", "0") else "1"
                expected = "permissive" if mode == "0" else "enforcing"
                if value.lower() in ("permissive", "0"):
                    script_lines.append(
                        f"setenforce {mode} >/dev/null 2>&1"
                        " && getenforce 2>/dev/null | tr '[:upper:]' '[:lower:]'"
                        f" | grep -qx '{expected}'"
                        " && sed -i 's/^SELINUX=enforcing/SELINUX=permissive/'"
                        " /etc/selinux/config >/dev/null 2>&1"
                        f' && RESULT="$RESULT {param}=OK"'
                        f' || RESULT="$RESULT {param}=FAIL"'
                    )
                else:
                    script_lines.append(
                        f"setenforce {mode} >/dev/null 2>&1"
                        " && getenforce 2>/dev/null | tr '[:upper:]' '[:lower:]'"
                        f" | grep -qx '{expected}'"
                        f' && RESULT="$RESULT {param}=OK"'
                        f' || RESULT="$RESULT {param}=FAIL"'
                    )
            elif param == "cpu_governor":
                script_lines.append(
                    f"echo {value} | tee /sys/devices/system/cpu/cpu*/cpufreq/"
                    f"scaling_governor >/dev/null 2>&1"
                    f' && RESULT="$RESULT {param}=OK"'
                    f' || RESULT="$RESULT {param}=FAIL"'
                )
            elif param == "nofile":
                script_lines.append(
                    "{ "
                    "sed -i '/nofile/d' /etc/security/limits.conf >/dev/null 2>&1"
                    f" && echo '* soft nofile {value}' >> /etc/security/limits.conf"
                    f" && echo '* hard nofile {value}' >> /etc/security/limits.conf"
                    f" && systemctl set-property nginx.service LimitNOFILE={value} >/dev/null 2>&1"
                    " && systemctl daemon-reload >/dev/null 2>&1"
                    " && systemctl restart nginx >/dev/null 2>&1"
                    " && cat /proc/$(pgrep -o nginx)/limits 2>/dev/null"
                    f" | grep -Eq 'Max open files[[:space:]]+{value}[[:space:]]+'"
                    "; }"
                    f' && RESULT="$RESULT {param}=OK"'
                    f' || RESULT="$RESULT {param}=FAIL"'
                )
            elif param == "irqbalance":
                script_lines.append(
                    "systemctl enable --now irqbalance >/dev/null 2>&1"
                    f' && RESULT="$RESULT {param}=OK"'
                    f' || RESULT="$RESULT {param}=FAIL"'
                )
            elif param == "net.ipv4.ip_local_port_range":
                script_lines.append(
                    f"sysctl -w 'net.ipv4.ip_local_port_range={value}' >/dev/null 2>&1"
                    f' && RESULT="$RESULT {param}=OK"'
                    f' || RESULT="$RESULT {param}=FAIL"'
                )
            elif param.startswith("net.") or param.startswith("fs."):
                script_lines.append(
                    f"sysctl -w {param}={value} >/dev/null 2>&1"
                    f' && RESULT="$RESULT {param}=OK"'
                    f' || RESULT="$RESULT {param}=FAIL"'
                )
            else:
                script_lines.append(f'RESULT="$RESULT {param}=SKIP"')

        script_lines.append('echo "__RESULT__ $RESULT"')
        script = "\n".join(script_lines)

        result = ssh.execute(f"bash -c '{script}'", timeout=30)
        output = result.stdout.strip()
        result_line = ""
        for line in reversed(output.splitlines()):
            if line.startswith("__RESULT__ "):
                result_line = line[len("__RESULT__ ") :]
                break

        # Parse results
        applied = {}
        failed = {}
        for token in result_line.split():
            if "=" not in token:
                continue
            key, status = token.rsplit("=", 1)
            if status == "OK":
                applied[key] = changes.get(key, "")
            elif status == "FAIL":
                failed[key] = "command failed"
            elif status == "SKIP":
                failed[key] = "unsupported parameter"

        # Any param missing from script output is treated as failure.
        for param, value in sanitized.items():
            if param not in applied and param not in failed:
                failed[param] = "missing result token"

        # Merge pre-sanitization failures
        failed.update(pre_failed)

        for param, value in changes.items():
            if param in applied:
                _emit_apply_param("system", param, value, "applied")
            else:
                _emit_apply_param("system", param, value, "failed", str(failed.get(param, "unknown")))

        result_dict: dict[str, Any] = {"applied": applied, "failed": failed}
        tool_result(
            "apply_system",
            f"applied={list(applied.keys())} failed={list(failed.keys())}",
        )

        deps.token_counter.tool_calls += 1
        state["system_applied"] = state["system_applied"] or bool(applied)
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            f"batch_apply_system:{','.join(changes.keys())}"[:250],
            json.dumps(result_dict),
            f"system batch: {len(applied)} applied, {len(failed)} failed",
        )
        return result_dict

    def apply_saved_recommendations_impl(deps: AgentDeps) -> dict[str, Any]:
        from agents.tools_apply import (
            apply_kernel,
            apply_network,
            apply_resource_limits,
            apply_storage,
        )

        apply_plan = state.get("apply_plan") or {}
        _tuning = deps.config.get("tuning") or {}

        # Extract changes per category, filtered by config allowlist
        category_configs = {
            "webserver": _tuning.get("webserver_targets") or {},
            "kernel": _tuning.get("kernel_targets") or {},
            "resource_limits": _tuning.get("resource_limits_targets") or {},
            "network": _tuning.get("network_targets") or {},
            "storage": _tuning.get("storage_targets") or {},
        }
        changes_by_cat: dict[str, dict[str, str]] = {}
        for cat, allowed in category_configs.items():
            raw = apply_plan.get(cat, {})
            _cfg = getattr(deps, "config", None) or {}
            if isinstance(raw, dict):
                filtered = {
                    _resolve_param_alias(str(k).strip()): str(v).strip().split(";")[0].strip()
                    for k, v in raw.items()
                    if _resolve_param_alias(str(k).strip()) in allowed
                    and not _is_blocked(
                        _resolve_param_alias(str(k).strip()),
                        str(v).strip().split(";")[0].strip(),
                        _cfg,
                    )
                }
                # Apply forced_values guardrail — override LLM values
                forced = _tuning.get("forced_values") or {}
                for param in filtered:
                    if param in forced:
                        original = filtered[param]
                        filtered[param] = str(forced[param])
                        if original != filtered[param]:
                            tool_result(
                                "guardrail",
                                f"forced {param}: '{original}' -> '{filtered[param]}'",
                            )
                            state.setdefault("guardrails_triggered", []).append(
                                f"{param}: '{original}' -> '{filtered[param]}'"
                            )

                # Apply max_values guardrail — clamp numeric values
                max_vals = _tuning.get("max_values") or {}
                for param in filtered:
                    if param in max_vals:
                        try:
                            current_num = int(filtered[param])
                            cap = int(max_vals[param])
                            if current_num > cap:
                                tool_result(
                                    "guardrail",
                                    f"capped {param}: {current_num} -> {cap}",
                                )
                                state.setdefault("guardrails_triggered", []).append(
                                    f"{param}: {current_num} -> {cap} (max)"
                                )
                                filtered[param] = str(cap)
                        except (ValueError, TypeError):
                            pass
                    # Cap tcp_rmem/tcp_wmem third value using rmem_max/wmem_max cap
                    if param in ("net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem"):
                        buf_cap_key = (
                            "net.core.rmem_max" if "rmem" in param else "net.core.wmem_max"
                        )
                        buf_cap = max_vals.get(buf_cap_key)
                        if buf_cap:
                            parts = filtered[param].strip().strip('"').split()
                            if len(parts) == 3:
                                try:
                                    if int(parts[2]) > int(buf_cap):
                                        old_val = filtered[param]
                                        parts[2] = str(int(buf_cap))
                                        filtered[param] = " ".join(parts)
                                        tool_result(
                                            "guardrail",
                                            f"capped {param} max: {old_val} -> {filtered[param]}",
                                        )
                                except (ValueError, TypeError):
                                    pass

                if filtered:
                    changes_by_cat[cat] = filtered

        # Also accept webserver params from adapter allowlist
        adapter_allowed: set[str] = set(getattr(deps.adapter, "ALLOWED_BATCH_DIRECTIVES", set()))
        raw_web = apply_plan.get("webserver", {})
        if isinstance(raw_web, dict):
            for k, v in raw_web.items():
                k_s = str(k).strip()
                if k_s in adapter_allowed and k_s not in changes_by_cat.get("webserver", {}):
                    changes_by_cat.setdefault("webserver", {})[k_s] = (
                        str(v).strip().split(";")[0].strip()
                    )

        # Auto-apply resource_limits/network/storage from inspection problems
        # even if the apply_planner didn't include them (safe: removes throttling)
        inspection = state.get("inspection") or {}
        for cat, targets_key in (
            ("resource_limits", "resource_limits_targets"),
            ("network", "network_targets"),
            ("storage", "storage_targets"),
        ):
            cat_inspection = inspection.get(cat, {})
            if cat_inspection.get("problems") and cat not in changes_by_cat:
                # Inspection found problems but LLM didn't recommend fixes
                # Auto-apply all defaults from config
                defaults = _tuning.get(targets_key) or {}
                if defaults:
                    changes_by_cat[cat] = dict(defaults)
                    n_problems = len(cat_inspection["problems"])
                    tool_result(
                        "auto_fix",
                        f"{cat}: applying defaults due to {n_problems} problems",
                    )

        tool_call(
            "apply",
            " | ".join(f"{c}={len(v)}" for c, v in changes_by_cat.items() if v) or "no changes",
        )

        results: dict[str, Any] = {}
        ssh = deps.ssh

        # ── Apply order matters: remove cgroup/resource caps BEFORE restarting
        # nginx with more workers, otherwise nginx OOM-kills instantly.

        from agents.tool_gate import ToolAction, gate_and_execute

        res_changes = changes_by_cat.get("resource_limits", {})
        kern_changes = changes_by_cat.get("kernel", {})
        net_changes = changes_by_cat.get("network", {})
        stor_changes = changes_by_cat.get("storage", {})
        web_changes = changes_by_cat.get("webserver", {})

        # Apply in order: resource_limits → kernel → network → storage → webserver
        _apply_steps = [
            ("resource_limits", res_changes, lambda c=res_changes: apply_resource_limits(ssh, c)),
            ("kernel", kern_changes, lambda c=kern_changes: apply_kernel(ssh, c)),
            ("network", net_changes, lambda c=net_changes: apply_network(ssh, c)),
            ("storage", stor_changes, lambda c=stor_changes: apply_storage(ssh, c)),
            ("webserver", web_changes, lambda c=web_changes: _batch_apply_service(deps, c)),
        ]
        for scope, changes, executor in _apply_steps:
            if not changes:
                continue
            action = ToolAction(
                scope=scope, changes=changes, executor=executor,
                description=f"Apply {len(changes)} {scope} changes",
            )
            gate_result = gate_and_execute(action, deps.config, deps.session_id)
            results[scope] = gate_result.result or {"applied": {}, "failed": changes, "actions": ["denied"]}
            if not gate_result.decision.approved:
                for param, value in changes.items():
                    _emit_apply_param(scope, str(param), value, "denied", gate_result.decision.reason)
            else:
                result_payload = results[scope]
                applied_params = _extract_result_params(result_payload.get("applied", {}))
                failed_payload = result_payload.get("failed", {})
                failed_params = _extract_result_params(failed_payload)
                failed_details = failed_payload if isinstance(failed_payload, dict) else {}
                for param, value in changes.items():
                    param_s = str(param)
                    if param_s in applied_params:
                        _emit_apply_param(scope, param_s, value, "applied")
                    elif param_s in failed_params:
                        _emit_apply_param(
                            scope,
                            param_s,
                            value,
                            "failed",
                            str(failed_details.get(param_s, "failed")),
                        )
                    else:
                        _emit_apply_param(scope, param_s, value, "unknown", "not present in apply result")
            if gate_result.decision.approved:
                if scope == "webserver":
                    state["service_applied"] = True
                else:
                    state["system_applied"] = True

        # Log results per category
        for cat, result in results.items():
            applied = result.get("applied", {})
            failed = result.get("failed", {})
            actions = result.get("actions", [])
            detail = (
                f"applied={list(applied.keys()) if isinstance(applied, dict) else applied}"
                f" failed={list(failed.keys()) if isinstance(failed, dict) else failed}"
            )
            if actions:
                detail += f" actions={actions}"
            tool_result(f"apply_{cat}", detail)

        # Store apply results for Slack notification
        state["apply_results"] = results

        # Verify changes on DUT
        tool_call("verify", "checking applied changes on DUT")
        web_applied = results.get("webserver", {}).get("applied", [])
        kern_applied = results.get("kernel", {}).get("applied", {})
        _verify_applied_changes(
            deps,
            service_applied=web_applied,
            service_changes=web_changes,
            system_applied=kern_applied,
        )

        # Build findings from all categories, enriched with RCA context
        inspection = state.get("inspection") or {}
        web_current = inspection.get("webserver", {}).get("current") or {}
        kern_current = inspection.get("kernel", {}).get("current") or {}

        # Build RCA lookup: map parameter names to their root cause + evidence
        rca_records = state.get("rca_records") or []
        rca_by_param: dict[str, str] = {}
        for rca in rca_records:
            symptom = rca.get("symptom", "")
            root_cause = rca.get("root_cause", "")
            evidence = rca.get("evidence", [])
            evidence_str = "; ".join(str(e) for e in evidence[:3]) if evidence else ""
            rca_text = f"{root_cause}"
            if evidence_str:
                rca_text += f" [evidence: {evidence_str[:200]}]"
            # Match RCA to params by checking if param name appears in symptom/cause
            for param in list(web_current.keys()) + list(kern_current.keys()):
                if param in symptom or param in root_cause:
                    rca_by_param[param] = rca_text

        # Normalize values to canonical forms for comparison and storage
        _VALUE_ALIASES = {
            "enabled": "active",
            "on": "active",
            "true": "active",
            "True": "active",
            "30s": "30",
        }

        def _normalize(v: str) -> str:
            return _VALUE_ALIASES.get(v, v)

        findings: list[dict[str, Any]] = []
        for param in web_applied if isinstance(web_applied, list) else web_applied.keys():
            raw_before = web_current.get(param, "")
            raw_after = web_changes.get(param, "")
            if _normalize(raw_before) == _normalize(raw_after):
                continue  # no change — don't save duplicate
            findings.append(
                {
                    "parameter": f"webserver.{param}",
                    "before_value": raw_before,
                    "after_value": raw_after,
                    "reasoning": rca_by_param.get(param, "config-driven tuning"),
                }
            )
        for param, value in kern_applied.items():
            raw_before = kern_current.get(param, "")
            raw_after = value
            if _normalize(raw_before) == _normalize(raw_after):
                continue  # no change — don't save duplicate
            findings.append(
                {
                    "parameter": f"kernel.{param}",
                    "before_value": raw_before,
                    "after_value": raw_after,
                    "reasoning": rca_by_param.get(param, "config-driven tuning"),
                }
            )
        for cat in ("resource_limits", "network", "storage"):
            cat_problems = inspection.get(cat, {}).get("problems", [])
            problem_text = "; ".join(str(p) for p in cat_problems[:3]) if cat_problems else ""
            cat_result = results.get(cat, {})
            applied = cat_result.get("applied", {})
            if isinstance(applied, dict):
                for param, value in applied.items():
                    findings.append(
                        {
                            "parameter": f"{cat}.{param}",
                            "before_value": "",
                            "after_value": str(value),
                            "reasoning": problem_text or "config-driven fix",
                        }
                    )

        if findings:
            save_findings_impl(deps, findings)

        return {"results": results, "findings": findings}

    async def inspect_service_config(ctx) -> dict:
        return inspect_service_impl(ctx.deps)

    async def inspect_system_tuning(ctx) -> dict:
        return inspect_system_impl(ctx.deps)

    async def inspect_irq_distribution(ctx) -> dict:
        return inspect_irq_impl(ctx.deps)

    async def apply_service_tuning(
        ctx, changes: dict[str, str] | str | None = None, **kwargs: Any
    ) -> dict:
        return apply_service_impl(ctx.deps, changes, **kwargs)

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
        def inspect_service_config() -> dict:
            """Inspect nginx config and return only what needs fixing vs proven targets."""
            return inspect_service_impl(deps)

        @tool
        def inspect_system_tuning() -> dict:
            """Stage 2: inspect RHEL/kernel/system tuning and return candidate fixes."""
            return inspect_system_impl(deps)

        @tool
        def inspect_irq_distribution() -> dict:
            """Stage 3: inspect IRQ distribution, worker CPU spread, and IRQ lock signals."""
            return inspect_irq_impl(deps)

        @tool
        def apply_service_tuning(changes: dict[str, Any] | str | None = None) -> dict:
            """Apply multiple nginx config changes from a structured changes object."""
            return apply_service_impl(deps, changes)

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
            inspect_service_config,
            inspect_system_tuning,
            inspect_irq_distribution,
            save_rca,
            save_recommendations,
            query_memory,
        ]

    test_tools = {
        "inspect_service_config": inspect_service_config,
        "inspect_system_tuning": inspect_system_tuning,
        "inspect_irq_distribution": inspect_irq_distribution,
        "save_rca": save_rca,
        "save_recommendations": save_recommendations,
        "apply_service_tuning": apply_service_tuning,
        "apply_system_tuning": apply_system_tuning,
        "run_benchmark": run_benchmark,
        "query_memory": query_memory,
        "save_findings": save_findings,
    }
    workflow = DiagnosisWorkflow(model, state, test_tools, tool_factory)
    workflow._apply_from_recommendations = apply_saved_recommendations_impl  # type: ignore[attr-defined]
    return workflow


def _save_preflight_hypothesis(
    deps: AgentDeps,
    diag: dict[str, Any],
    url_results: dict[str, dict[str, Any]],
    problems: list[str],
) -> None:
    """Save preflight diagnostics to the hypothesis folder."""
    cfg = getattr(deps, "config", {}) or {}
    agent_cfg = cfg.get("agent") or {}
    enabled = bool(agent_cfg.get("persist_hypotheses")) or bool(
        agent_cfg.get("debug_planner_payloads", False)
    )
    if not enabled:
        return

    path = Path(__file__).resolve().parent.parent / "hypothesis" / str(deps.session_id)
    path.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Pre-flight Validation",
        "",
        "## HTTP Response Checks",
        "",
        "| Workload | Status | Size | Time | Result |",
        "|----------|--------|------|------|--------|",
    ]
    for name, result in url_results.items():
        status = result.get("status", "?")
        size = result.get("size", "?")
        time = result.get("time", "?")
        ok = "OK" if status == "200" else "FAIL"
        lines.append(f"| {name} | {status} | {size} | {time}s | {ok} |")

    lines.extend(["", "## System Diagnostics", ""])

    diag_items = [
        ("SELinux Mode", "selinux_mode"),
        ("SELinux Denials", "selinux_denials"),
        ("File Labels", "file_labels"),
        ("Stress Data Labels", "stress_data_labels"),
        ("Nginx Config Test", "nginx_test"),
        ("Nginx Error Log", "nginx_error_log"),
        ("Iptables Rules", "iptables_rules"),
        ("Nftables Rules", "nftables_rules"),
        ("Traffic Control", "tc_rules"),
        ("Nginx Nice/Priority", "nginx_nice"),
        ("Nginx Cgroup CPU", "nginx_cgroup_cpu"),
        ("Nginx Service Limits", "nginx_service_limits"),
        ("Top CPU Processes", "top_cpu_procs"),
        ("Swap Usage", "swap_usage"),
        ("MTU", "mtu"),
        ("NIC Offload", "nic_offload"),
        ("CPU Governor", "cpu_governor"),
        ("Document Root", "document_root"),
    ]
    for label, key in diag_items:
        val = diag.get(key, "")
        if val:
            lines.append(f"### {label}")
            lines.append(f"```\n{val[:500]}\n```")
            lines.append("")

    if problems:
        lines.extend(["## Problems Detected", ""])
        for p in problems:
            lines.append(f"- {p}")
        lines.append("")
    else:
        lines.extend(["## Result", "", "All workloads return HTTP 200 — no issues found.", ""])

    target = path / "00_preflight.md"
    target.write_text("\n".join(lines), encoding="utf-8")


async def run_preflight(model, deps: AgentDeps) -> dict[str, Any]:
    """Pre-flight validation agent: verify DUT serves all workloads correctly.

    Runs before baseline benchmarks. Detects misconfigurations that would
    produce invalid baselines (403s, connection refused, wrong content).
    Uses LLM to diagnose and fix issues found.
    """
    ssh = deps.ssh
    cfg = deps.config
    host = cfg["target"].get("host", "localhost")
    tool_call("preflight", "verifying DUT serves all workloads correctly")

    # ── Collect diagnostics from DUT ────────────────────────────────
    diag: dict[str, Any] = {}

    # 1. HTTP response check — curl each workload type
    test_urls = {
        "homepage": f"http://{host}/",
        "small": f"http://{host}/stress_test_data/small/000/000/file_000000000.html",
        "medium": f"http://{host}/stress_test_data/medium/000/000/file_000000000.html",
        "large": f"http://{host}/stress_test_data/large/000/000/file_000000000.html",
    }
    # Discover actual document root and test files
    docroot_r = ssh.execute(
        "nginx -T 2>/dev/null | grep -E '^\\s*root ' | head -1 | awk '{print $2}' | tr -d ';'"
    )
    docroot = docroot_r.stdout.strip() or "/var/www/nginx"
    diag["document_root"] = docroot

    url_results: dict[str, dict[str, Any]] = {}
    for name, url in test_urls.items():
        r = ssh.execute(
            f"curl -s -o /dev/null -w '%{{http_code}} %{{size_download}} %{{time_total}}'"
            f" {url} 2>&1"
        )
        parts = r.stdout.strip().split()
        status = parts[0] if parts else "000"
        size = parts[1] if len(parts) > 1 else "0"
        time = parts[2] if len(parts) > 2 else "0"
        url_results[name] = {"status": status, "size": size, "time": time}
    diag["url_checks"] = url_results

    # Helper: run SSH with 5s timeout — all commands finish in <1s normally
    def _q(cmd: str) -> str:
        return ssh.execute(cmd, timeout=5).stdout.strip()

    # 2. SELinux state and AVC denials
    diag["selinux_mode"] = _q("getenforce 2>/dev/null")
    diag["selinux_denials"] = _q("timeout 5 ausearch -m avc -ts recent 2>/dev/null | tail -20")[
        :2000
    ]

    # 3. File permissions and SELinux labels
    diag["file_labels"] = _q(f"ls -laZ {docroot}/ 2>/dev/null | head -10")
    if "stress_test_data" in _q(f"ls {docroot}/ 2>/dev/null"):
        diag["stress_data_labels"] = _q(
            f"ls -laZ {docroot}/stress_test_data/small/000/000/ 2>/dev/null | head -5"
        )

    # 4. Nginx config validation
    diag["nginx_test"] = _q("nginx -t 2>&1")
    diag["nginx_error_log"] = _q("tail -20 /var/log/nginx/error.log 2>/dev/null")[:1000]

    # 5. Firewall / traffic control rules
    diag["iptables_rules"] = _q(
        "timeout 5 iptables -L -n 2>/dev/null | grep -v '^Chain\\|^target\\|^$' | head -20"
    )
    diag["nftables_rules"] = _q(
        "timeout 5 nft list ruleset 2>/dev/null | grep -E 'drop|reject|limit' | head -10"
    )
    diag["tc_rules"] = _q("tc qdisc show 2>/dev/null | grep -v 'noqueue\\|pfifo_fast' | head -10")

    # 6. Process-level throttling
    diag["nginx_nice"] = _q("ps -o pid,ni,cls,rtprio,comm -C nginx | head -5")
    diag["nginx_cgroup_cpu"] = _q(
        "cat /sys/fs/cgroup/system.slice/nginx.service/cpu.max 2>/dev/null"
        " || cat /sys/fs/cgroup/cpu/system.slice/nginx.service/"
        "cpu.cfs_quota_us 2>/dev/null || echo 'no cgroup throttle'"
    )
    diag["nginx_service_limits"] = _q(
        "systemctl show nginx.service 2>/dev/null"
        " | grep -E 'LimitNOFILE|LimitMEMLOCK|CPUQuota|MemoryLimit'"
    )

    # 7. Background resource hogs
    diag["top_cpu_procs"] = _q("ps aux --sort=-%cpu | head -10")
    diag["swap_usage"] = _q("free -m | grep Swap")

    # 8. Network interface checks
    diag["mtu"] = _q(
        "ip link show $(ip route get 1 | awk '{print $5; exit}') 2>/dev/null | grep mtu"
    )
    diag["nic_offload"] = _q(
        "ethtool -k $(ip route get 1 | awk '{print $5; exit}')"
        " 2>/dev/null"
        " | grep -E 'tcp-segmentation|generic-segmentation"
        "|generic-receive'"
    )

    # 9. CPU governor
    diag["cpu_governor"] = _q(
        "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null"
    )

    # ── Check if there are problems ─────────────────────────────────
    problems = []
    for name, result in url_results.items():
        if result["status"] != "200":
            problems.append(f"{name}: HTTP {result['status']} (expected 200)")

    # Save preflight diagnostics to hypothesis folder
    _save_preflight_hypothesis(deps, diag, url_results, problems)

    if not problems:
        tool_result("preflight", "all workloads return HTTP 200 — OK")
        deps.memory.save_context(
            deps.session_id,
            "command_output",
            "preflight_validation",
            json.dumps({"status": "ok", "checks": diag}),
            "preflight: all workloads serve correctly",
        )
        return {"status": "ok", "problems": [], "fixes": [], "diagnostics": diag}

    # ── Problems found — ask LLM to diagnose and fix ────────────────
    tool_result(
        "preflight",
        f"PROBLEMS: {'; '.join(problems)}",
    )

    preflight_prompt = (
        "You are a RHEL system administrator. The DUT (device under test) "
        "is failing to serve some workload URLs correctly. "
        "Diagnose the root cause and provide exact shell commands to fix it.\n\n"
        "IMPORTANT RULES:\n"
        "- Do NOT insert directives that already exist in the config file. "
        "Use 'sed -i s/old/new/' to REPLACE existing values, never append duplicates.\n"
        "- For nginx, always use 'sed -i' to modify existing directives in-place.\n"
        "- Always run 'nginx -t' before 'systemctl restart nginx' to validate.\n\n"
        "Return strict JSON with keys:\n"
        '- "diagnosis": one-sentence root cause\n'
        '- "fixes": list of shell commands to run on the DUT to fix the issue\n\n'
        "Problems detected:\n"
        + "\n".join(f"  - {p}" for p in problems)
        + "\n\nDiagnostics collected from DUT:\n"
        + json.dumps(diag, indent=2, ensure_ascii=False)[:6000]
    )

    fix_plan, fix_usage = _invoke_json_planner(
        model, "planner.preflight_fix", preflight_prompt, deps
    )
    tool_result(
        "preflight",
        f"diagnosis: {fix_plan.get('diagnosis', 'unknown')}",
    )

    deps.token_counter.input_tokens += fix_usage["input_tokens"]
    deps.token_counter.output_tokens += fix_usage["output_tokens"]

    # Apply fixes — gated through tool approval
    fixes_applied = []
    fix_commands = fix_plan.get("fixes", [])
    _PREFLIGHT_ALLOWED_CMDS = frozenset({
        "systemctl", "sysctl", "sed", "nginx", "redis-cli", "psql",
        "ip", "ss", "sar", "grep", "echo", "cat", "rm", "cp",
        "chmod", "chown", "mkdir", "touch", "tuned-adm",
    })
    valid_commands = []
    for _raw_cmd in (fix_commands if isinstance(fix_commands, list) else []):
        _cmd = str(_raw_cmd).strip()
        if not _cmd or _cmd.startswith("#"):
            continue
        if _cmd.split()[0] not in _PREFLIGHT_ALLOWED_CMDS:
            tool_result("preflight_fix", f"BLOCKED (not allowlisted): {_cmd[:80]}")
            continue
        valid_commands.append(_cmd)

    if valid_commands:
        from agents.tool_gate import ToolAction, gate_and_execute

        preflight_changes = {f"cmd_{i}": cmd for i, cmd in enumerate(valid_commands)}

        def _execute_preflight():
            results = []
            for cmd_str in valid_commands:
                tool_call("preflight_fix", cmd_str[:100])
                # Guard: validate service config before any restart/reload
                _svc_proc = deps.adapter.get_service_info().get("process_name", "")
                if any(kw in cmd_str for kw in (
                    f"restart {_svc_proc}", f"reload {_svc_proc}",
                    "systemctl restart", "systemctl reload",
                )):
                    if not deps.adapter.validate_config():
                        tool_result("preflight_fix", "SKIPPED restart — config validation failed")
                        results.append({"command": cmd_str, "ok": False, "output": "skipped: config invalid"})
                        continue
                cmd_result = ssh.execute(cmd_str, timeout=120)
                results.append({"command": cmd_str, "ok": cmd_result.ok, "output": cmd_result.stdout.strip()[:200]})
            return {"applied": results, "failed": {}}

        action = ToolAction(
            scope="preflight",
            changes=preflight_changes,
            executor=_execute_preflight,
            description=f"Execute {len(valid_commands)} preflight fix commands",
        )
        gate_result = gate_and_execute(action, deps.config, deps.session_id)
        if gate_result.result:
            fixes_applied = gate_result.result.get("applied", [])

    # Re-verify after fixes
    recheck: dict[str, str] = {}
    remaining_problems = []
    for name, url in test_urls.items():
        r = ssh.execute(f"curl -s -o /dev/null -w '%{{http_code}}' {url} 2>&1")
        status = r.stdout.strip()
        recheck[name] = status
        if status != "200":
            remaining_problems.append(f"{name}: still HTTP {status}")

    if remaining_problems:
        tool_result(
            "preflight",
            f"STILL FAILING after fixes: {'; '.join(remaining_problems)}",
        )
    else:
        tool_result("preflight", "all workloads now return HTTP 200 — fixed")

    # Persist results
    _iter = getattr(deps, "iteration", 0)
    _save_planner_artifact(deps, "preflight_fix", fix_plan, iteration=_iter)
    deps.memory.save_context(
        deps.session_id,
        "command_output",
        "preflight_validation",
        json.dumps(
            {
                "status": "fixed" if not remaining_problems else "partial",
                "problems": problems,
                "diagnosis": fix_plan.get("diagnosis", ""),
                "fixes_applied": fixes_applied,
                "recheck": recheck,
                "remaining": remaining_problems,
            }
        ),
        f"preflight: {len(problems)} problems, "
        f"{len(fixes_applied)} fixes, "
        f"{len(remaining_problems)} remaining",
    )

    return {
        "status": "fixed" if not remaining_problems else "partial",
        "problems": problems,
        "fixes": fixes_applied,
        "diagnostics": diag,
    }


async def run(
    model, deps: AgentDeps, context_prompt: str, iteration_phase: str = "full",
) -> DiagnosisOutput:
    """Run the diagnosis workflow with phase-based execution.

    iteration_phase:
      "diagnose"   — iter1: inspect + experts only, no apply
      "plan_apply" — iter2: synthesizer + apply_planner + apply
      "review"     — iter3: experts with feedback + targeted apply
      "full"       — legacy: all in one (backward compat)
    """
    llm_call("agent", f"Starting diagnosis — phase={iteration_phase!r}")
    agent = build(model, config=getattr(deps, "config", None))
    state = getattr(agent, "_slaymetrics_state", {})
    config = getattr(deps, "config", {}) or {}
    planner_mode = str((config.get("agent") or {}).get("planner_mode", "debate")).strip().lower()
    if planner_mode == "single":
        planner_mode = "deterministic"
    llm_call("agent", f"Planner mode: {planner_mode!r}")

    if iteration_phase == "diagnose":
        # Iter1: inspect + experts only — no apply
        result = await _run_debate_experts_only(agent, model, deps, context_prompt, state)
    elif iteration_phase == "plan_apply":
        # Iter2: synthesize + plan + apply using iter1 expert outputs
        result = await _run_synthesis_and_apply(agent, model, deps, context_prompt, state)
    elif iteration_phase == "review":
        # Iter3: re-run experts with feedback + targeted apply
        result = await _run_review_iteration(agent, model, deps, context_prompt, state)
    elif planner_mode in ("deterministic", "hybrid"):
        result = await _run_rules_engine(
            agent, model, deps, context_prompt, state,
            hybrid=(planner_mode == "hybrid"),
        )
    elif planner_mode == "debate":
        result = await _run_debate_planner(agent, model, deps, context_prompt, state)
    else:
        result = await agent.run(context_prompt, deps=deps)

    # Apply phase — only for "full", "plan_apply", "review" modes
    if iteration_phase in ("full", "plan_apply", "review"):
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
        service_applied=bool(state.get("service_applied", False)),
        system_applied=bool(state.get("system_applied", False)),
        after_rps=after_rps,
        improvement_pct=improvement_pct,
        notes=notes,
        rca_records=list(state.get("rca_records") or []),
        recommendations=list(state.get("recommendations") or []),
        apply_results=state.get("apply_results"),
        guardrails_triggered=state.get("guardrails_triggered"),
        eval_results=state.get("eval_results"),
        _service_analysis=state.get("service_analysis"),
        _rhel_analysis=state.get("rhel_analysis"),
        _inspection=state.get("inspection"),
    )


async def _run_rules_engine(
    agent,
    model,
    deps: AgentDeps,
    context_prompt: str,
    agent_state: dict | None = None,
    *,
    hybrid: bool = False,
):
    """Deterministic/hybrid planner — builds apply plan without (or with minimal) LLM tokens."""
    from agents.rules_engine import (
        apply_validation_result,
        build_apply_plan,
        build_rca_records,
        build_recommendations,
        build_summary,
        build_validation_prompt,
    )
    from agents.tools_inspect import inspect_all

    ctx = SimpleNamespace(deps=deps)
    save_rca = agent._function_toolset.tools["save_rca"].function
    save_recommendations = agent._function_toolset.tools["save_recommendations"].function

    # ── 1. Compound inspection (same as debate planner) ──────────────
    tool_call("inspect", "compound inspect — all 5 categories")
    all_inspection = inspect_all(deps.ssh, deps.config)
    tool_result(
        "inspect",
        f"issues: {all_inspection.get('summary', {}).get('total_issues', 0)} "
        f"({json.dumps(all_inspection.get('summary', {}).get('by_category', {}))}) ",
    )
    deps.token_counter.tool_calls += 1
    deps.memory.save_context(
        deps.session_id,
        "command_output",
        "compound_inspection",
        json.dumps(all_inspection, ensure_ascii=True)[:8000],
        f"compound inspect: {all_inspection.get('summary', {}).get('total_issues', 0)} issues",
    )
    if agent_state is not None:
        agent_state["inspection"] = all_inspection

    # ── 2. Deterministic plan + RCA + recommendations ────────────────
    tool_call("rules_engine", "building deterministic apply plan")
    apply_plan = build_apply_plan(all_inspection, deps.config)
    rca_records = build_rca_records(all_inspection)
    recommendations = build_recommendations(all_inspection, deps.config)
    plan_summary_parts = {
        cat: list(changes.keys()) for cat, changes in apply_plan.items() if changes
    }
    tool_result(
        "rules_engine",
        " | ".join(f"{c}={len(v)}" for c, v in plan_summary_parts.items()),
    )

    # ── 3. Optional hybrid LLM validation ────────────────────────────
    total_in = 0
    total_out = 0
    validation_reasoning = ""

    if hybrid:
        _sys_fp = getattr(deps, "system_fingerprint", "") or ""
        parts = _sys_fp.split(",") if _sys_fp else []
        cpu_cores = "unknown"
        ram_gb = "unknown"
        for part in parts:
            part = part.strip()
            if "core" in part.lower():
                cpu_cores = part.split(":")[1].strip() if ":" in part else part
            elif "ram" in part.lower() or "gb" in part.lower():
                ram_gb = part.split(":")[1].strip() if ":" in part else part

        validate_prompt = build_validation_prompt(
            apply_plan,
            cpu_cores=cpu_cores,
            ram_gb=ram_gb,
            baseline_summary=context_prompt[:300] if context_prompt else "",
        )
        tool_call("validator", "LLM validation of deterministic plan")
        validation, val_usage = _invoke_json_planner(
            model,
            "planner.validator",
            validate_prompt,
            deps,
        )
        total_in = val_usage.get("input_tokens", 0)
        total_out = val_usage.get("output_tokens", 0)
        validation_reasoning = str(validation.get("reasoning", "")).strip()
        if validation_reasoning:
            tool_result("validator", f"reasoning: {validation_reasoning[:200]}")

        # Apply validator feedback
        apply_plan = apply_validation_result(apply_plan, validation, deps.config)

    # ── 4. Persist RCA + recommendations ─────────────────────────────
    _iter = getattr(deps, "iteration", 0)
    _coerced_rca = _coerce_records(rca_records, deps=deps)
    _coerced_recs = _coerce_recommendations(recommendations, deps=deps)
    if _coerced_rca:
        await save_rca(ctx, _coerced_rca)
    if _coerced_recs:
        await save_recommendations(ctx, _coerced_recs)

    _save_planner_artifact(deps, "rules_engine", apply_plan, iteration=_iter)

    # ── 5. Apply safety-net defaults (same as debate planner) ────────
    _tuning = (getattr(deps, "config", None) or {}).get("tuning") or {}
    _defaults_by_cat = {
        "webserver": _tuning.get("webserver_targets") or {},
        "kernel": _tuning.get("kernel_targets") or {},
        "resource_limits": _tuning.get("resource_limits_targets") or {},
        "network": _tuning.get("network_targets") or {},
        "storage": _tuning.get("storage_targets") or {},
    }
    _cfg = getattr(deps, "config", None) or {}
    for _cat, _defaults in _defaults_by_cat.items():
        plan_cat = apply_plan.get(_cat)
        if not isinstance(plan_cat, dict):
            plan_cat = {}
            apply_plan[_cat] = plan_cat
        for _param, _default_val in _defaults.items():
            if _param not in plan_cat and not _is_blocked(_param, str(_default_val), _cfg):
                plan_cat[_param] = str(_default_val)

    # ── 5b. Nginx FD consistency — deterministic correction ──────────
    _enforce_nginx_fd_consistency(apply_plan, getattr(deps, "config", None) or {})

    # ── 6. Store apply plan for apply_saved_recommendations_impl ─────
    if agent_state is not None:
        agent_state["apply_plan"] = apply_plan

    summary = build_summary(all_inspection, apply_plan)
    if validation_reasoning:
        summary += f" Validator: {validation_reasoning}"

    return SimpleNamespace(
        output=summary,
        usage=lambda: SimpleNamespace(input_tokens=total_in, output_tokens=total_out),
        all_messages=lambda: [],
    )


# High-impact params that deserve LLM reasoning; the rest are applied
# deterministically from config targets without wasting tokens.
_HIGH_IMPACT_PARAMS: set[str] = {
    # Webserver — these have outsized throughput effects
    "worker_processes",
    "worker_connections",
    "worker_rlimit_nofile",
    "listen_backlog",
    "limit_rate",
    "limit_rate_after",
    "limit_req",
    "limit_conn",
    "access_log",
    "tcp_nodelay",
    "keepalive_requests",
    "keepalive_timeout",
    "accept_mutex",
    # Kernel — these cause connection drops and swapping
    "net.core.somaxconn",
    "net.ipv4.tcp_max_syn_backlog",
    "net.core.netdev_max_backlog",
    "vm.swappiness",
    "vm.vfs_cache_pressure",
    "vm.dirty_ratio",
    "vm.dirty_background_ratio",
    "net.ipv4.tcp_tw_reuse",
    "net.ipv4.ip_local_port_range",
    "transparent_hugepage",
    "selinux",
    "irqbalance",
}


def _filter_inspection_for_llm(
    category_data: dict[str, Any],
    *,
    max_items: int = 20,
) -> dict[str, Any]:
    """Strip ``current`` dict and limit ``needs_fixing`` to high-impact params.

    Low-impact params (sendfile, tcp_nopush, gzip_comp_level, etc.) are still
    applied by the safety-net defaults in the apply step — they just don't need
    LLM reasoning.
    """
    filtered: dict[str, Any] = {}
    trimmed_count = 0
    for key, value in category_data.items():
        if key == "current":
            continue  # never send to LLM
        if key == "needs_fixing" and isinstance(value, dict):
            # Partition into high/low impact
            high = {k: v for k, v in value.items() if k in _HIGH_IMPACT_PARAMS}
            low = {k: v for k, v in value.items() if k not in _HIGH_IMPACT_PARAMS}
            # Always include high-impact; fill remaining slots with low-impact
            remaining = max(0, max_items - len(high))
            merged = {**high, **dict(list(low.items())[:remaining])}
            filtered[key] = merged
            trimmed_count = len(low) - min(len(low), remaining)
        else:
            filtered[key] = value
    # Adjust ok_count to reflect trimmed low-impact params
    if trimmed_count > 0:
        filtered["ok_count"] = filtered.get("ok_count", 0) + trimmed_count
    return filtered


async def _run_debate_planner(
    agent, model, deps: AgentDeps, context_prompt: str, agent_state: dict | None = None
):
    ctx = SimpleNamespace(deps=deps)
    save_rca = agent._function_toolset.tools["save_rca"].function
    save_recommendations = agent._function_toolset.tools["save_recommendations"].function

    # Compound inspect: all 5 categories in one call
    from agents.tools_inspect import inspect_all

    tool_call("inspect", "compound inspect — all 5 categories")
    all_inspection = inspect_all(deps.ssh, deps.config)
    tool_result(
        "inspect",
        f"issues: {all_inspection.get('summary', {}).get('total_issues', 0)} "
        f"({json.dumps(all_inspection.get('summary', {}).get('by_category', {}))}) ",
    )
    deps.token_counter.tool_calls += 1
    deps.memory.save_context(
        deps.session_id,
        "command_output",
        "compound_inspection",
        json.dumps(all_inspection, ensure_ascii=True)[:8000],
        f"compound inspect: {all_inspection.get('summary', {}).get('total_issues', 0)} issues",
    )

    # Save to state for later use
    if agent_state is not None:
        agent_state["inspection"] = all_inspection

    # Split inspection data for the two experts
    webserver_data = all_inspection.get("webserver", {})
    kernel_data = all_inspection.get("kernel", {})
    resource_data = all_inspection.get("resource_limits", {})
    network_data = all_inspection.get("network", {})
    storage_data = all_inspection.get("storage", {})

    # Strip 'current' dicts and pre-filter to high-impact params (saves tokens)
    _web_llm = _filter_inspection_for_llm(webserver_data)
    _kern_llm = _filter_inspection_for_llm(kernel_data)

    _sys_line = getattr(deps, "system_fingerprint", "") or ""
    from core.prompts import service_expert as service_expert_prompt
    from core.prompts import rhel_expert as rhel_expert_prompt

    _profile = getattr(deps, "service_profile", None)
    service_prompt = service_expert_prompt.build(
        system_line=_sys_line,
        service_inspection=_web_llm,
        profile=_profile,
    )
    rhel_prompt = rhel_expert_prompt.build(
        system_line=_sys_line,
        kernel_inspection=_kern_llm,
        resource_data=resource_data,
        network_data=network_data,
        storage_data=storage_data,
    )

    _svc_name = _profile.name if _profile else "service"
    (service_analysis, svc_usage), (rhel_analysis, rhel_usage) = await asyncio.gather(
        asyncio.to_thread(_invoke_json_planner, model, f"planner.{_svc_name}_expert", service_prompt, deps),
        asyncio.to_thread(_invoke_json_planner, model, "planner.rhel_expert", rhel_prompt, deps),
    )
    if _planner_debug_enabled(deps):
        tool_result(
            "debug",
            f"{_svc_name}_expert raw:\n{json.dumps(service_analysis, indent=2, ensure_ascii=False)}",
        )
        tool_result(
            "debug",
            f"rhel_expert raw:\n{json.dumps(rhel_analysis, indent=2, ensure_ascii=False)}",
        )

    from core.prompts import synthesizer as synthesizer_prompt

    synth_prompt = synthesizer_prompt.build(
        system_fingerprint=getattr(deps, "system_fingerprint", "") or "unknown",
        service_analysis=service_analysis,
        rhel_analysis=rhel_analysis,
        service_name=_svc_name.upper(),
    )
    synthesis, synth_usage = _invoke_json_planner(model, "planner.synthesizer", synth_prompt, deps)
    if _planner_debug_enabled(deps):
        tool_result("debug", f"synthesizer raw:\n{json.dumps(synthesis, indent=2, ensure_ascii=False)}")

    _iter = getattr(deps, "iteration", 0)
    _save_planner_artifact(deps, f"{_svc_name}_expert", service_analysis, iteration=_iter)
    _save_planner_artifact(deps, "rhel_expert", rhel_analysis, iteration=_iter)
    _save_planner_artifact(deps, "synthesizer", synthesis, iteration=_iter)
    _obs_result = _run_observational_debate_eval(
        deps,
        model,
        iteration=_iter,
        inspection=all_inspection,
        service_analysis=service_analysis,
        rhel_analysis=rhel_analysis,
        synthesis=synthesis,
    )
    if _obs_result and agent_state is not None:
        agent_state["eval_results"] = _obs_result

    rca_records = _coerce_records(synthesis.get("rca_records"), deps=deps)
    recommendations = _coerce_recommendations(synthesis.get("recommendations"), deps=deps)
    if rca_records:
        await save_rca(ctx, rca_records)
    if recommendations:
        await save_recommendations(ctx, recommendations)

    # ── 4th agent: apply_planner ─────────────────────────────────────
    # Group all recommendations into 5 categories matching our tools.
    _tuning = (getattr(deps, "config", None) or {}).get("tuning") or {}
    _web_tgt = _tuning.get("service_targets") or _tuning.get("webserver_targets") or {}
    _kern_tgt = _tuning.get("kernel_targets") or {}
    _res_tgt = _tuning.get("resource_limits_targets") or {}
    _net_tgt = _tuning.get("network_targets") or {}
    _stor_tgt = _tuning.get("storage_targets") or {}
    from core.prompts import apply_planner as apply_planner_prompt

    apply_prompt = apply_planner_prompt.build(
        service_targets=_web_tgt,
        kernel_targets=_kern_tgt,
        resource_limits_targets=_res_tgt,
        network_targets=_net_tgt,
        storage_targets=_stor_tgt,
        recommendations=_clean_recs_for_planner(synthesis.get("recommendations", []), deps.config),
        resource_problems=resource_data.get("problems", []),
        network_problems=network_data.get("problems", []),
        storage_problems=storage_data.get("problems", []),
    )
    apply_plan, apply_usage = _invoke_json_planner(
        model, "planner.apply_planner", apply_prompt, deps
    )
    # Always log apply_planner output — this is the critical translation step
    categories = ["webserver", "kernel", "resource_limits", "network", "storage"]
    plan_summary = {cat: list((apply_plan.get(cat) or {}).keys()) for cat in categories}
    tool_call(
        "apply_planner",
        " | ".join(f"{c}={len(v)}" for c, v in plan_summary.items() if v),
    )
    if _planner_debug_enabled(deps):
        tool_result(
            "debug",
            f"apply_planner raw:\n{json.dumps(apply_plan, indent=2, ensure_ascii=False)}",
        )
    _save_planner_artifact(deps, "apply_planner", apply_plan, iteration=_iter)

    # Safety net: inject config defaults for any allowlist params the planner
    # omitted.  The planner sometimes drops params when the synthesizer
    # feeds it dirty values — ensure critical settings like worker_processes
    # are never silently lost.
    _defaults_by_cat = {
        "webserver": _web_tgt,
        "kernel": _kern_tgt,
        "resource_limits": _res_tgt,
        "network": _net_tgt,
        "storage": _stor_tgt,
    }
    for _cat, _defaults in _defaults_by_cat.items():
        plan_cat = apply_plan.get(_cat)
        if not isinstance(plan_cat, dict):
            plan_cat = {}
            apply_plan[_cat] = plan_cat
        _cfg = getattr(deps, "config", None) or {}
        for _param, _default_val in _defaults.items():
            if _param not in plan_cat and not _is_blocked(_param, _default_val, _cfg):
                plan_cat[_param] = _default_val

    # Nginx FD consistency — deterministic correction
    _enforce_nginx_fd_consistency(apply_plan, getattr(deps, "config", None) or {})

    # Store the grouped changes for apply_saved_recommendations_impl
    if agent_state is not None:
        agent_state["apply_plan"] = apply_plan

    total_in = (
        svc_usage["input_tokens"]
        + rhel_usage["input_tokens"]
        + synth_usage["input_tokens"]
        + apply_usage["input_tokens"]
    )
    total_out = (
        svc_usage["output_tokens"]
        + rhel_usage["output_tokens"]
        + synth_usage["output_tokens"]
        + apply_usage["output_tokens"]
    )
    return SimpleNamespace(
        output=str(synthesis.get("summary") or "Debate planning completed.").strip(),
        usage=lambda: SimpleNamespace(input_tokens=total_in, output_tokens=total_out),
        all_messages=lambda: [],
    )


async def _run_debate_experts_only(
    agent, model, deps: AgentDeps, context_prompt: str, agent_state: dict | None = None,
):
    """Iteration 1: Inspect + run service_expert + rhel_expert in parallel. No synthesis or apply."""
    ctx = SimpleNamespace(deps=deps)
    save_rca = agent._function_toolset.tools["save_rca"].function
    save_recommendations = agent._function_toolset.tools["save_recommendations"].function

    from agents.tools_inspect import inspect_all

    tool_call("inspect", "compound inspect — all 5 categories")
    all_inspection = inspect_all(deps.ssh, deps.config, adapter=deps.adapter)
    tool_result(
        "inspect",
        f"issues: {all_inspection.get('summary', {}).get('total_issues', 0)} "
        f"({json.dumps(all_inspection.get('summary', {}).get('by_category', {}))}) ",
    )
    deps.token_counter.tool_calls += 1
    deps.memory.save_context(
        deps.session_id, "command_output", "compound_inspection",
        json.dumps(all_inspection, ensure_ascii=True)[:8000],
        f"compound inspect: {all_inspection.get('summary', {}).get('total_issues', 0)} issues",
    )
    if agent_state is not None:
        agent_state["inspection"] = all_inspection

    # Split inspection data for experts
    webserver_data = all_inspection.get("webserver", {})
    kernel_data = all_inspection.get("kernel", {})
    resource_data = all_inspection.get("resource_limits", {})
    network_data = all_inspection.get("network", {})
    storage_data = all_inspection.get("storage", {})

    _web_llm = _filter_inspection_for_llm(webserver_data)
    _kern_llm = _filter_inspection_for_llm(kernel_data)
    _sys_line = getattr(deps, "system_fingerprint", "") or ""

    from core.prompts import service_expert as service_expert_prompt
    from core.prompts import rhel_expert as rhel_expert_prompt

    _profile = getattr(deps, "service_profile", None)
    service_prompt = service_expert_prompt.build(
        system_line=_sys_line, service_inspection=_web_llm, profile=_profile,
    )
    rhel_prompt = rhel_expert_prompt.build(
        system_line=_sys_line, kernel_inspection=_kern_llm,
        resource_data=resource_data, network_data=network_data, storage_data=storage_data,
    )

    _svc_name = _profile.name if _profile else "service"
    (service_analysis, svc_usage), (rhel_analysis, rhel_usage) = await asyncio.gather(
        asyncio.to_thread(_invoke_json_planner, model, f"planner.{_svc_name}_expert", service_prompt, deps),
        asyncio.to_thread(_invoke_json_planner, model, "planner.rhel_expert", rhel_prompt, deps),
    )
    if _planner_debug_enabled(deps):
        tool_result("debug", f"{_svc_name}_expert raw:\n{json.dumps(service_analysis, indent=2, ensure_ascii=False)}")
        tool_result("debug", f"rhel_expert raw:\n{json.dumps(rhel_analysis, indent=2, ensure_ascii=False)}")

    _iter = getattr(deps, "iteration", 0)
    _save_planner_artifact(deps, f"{_svc_name}_expert", service_analysis, iteration=_iter)
    _save_planner_artifact(deps, "rhel_expert", rhel_analysis, iteration=_iter)

    # Save expert outputs to state for iter2
    if agent_state is not None:
        agent_state["service_analysis"] = service_analysis
        agent_state["rhel_analysis"] = rhel_analysis

    # Save RCA + recommendations from expert outputs (without synthesis)
    rca_records = _coerce_records(service_analysis.get("rca_records"), deps=deps)
    rca_records += _coerce_records(rhel_analysis.get("rca_records"), deps=deps)
    recommendations = _coerce_recommendations(service_analysis.get("recommendations"), deps=deps)
    recommendations += _coerce_recommendations(rhel_analysis.get("recommendations"), deps=deps)
    if rca_records:
        await save_rca(ctx, rca_records)
    if recommendations:
        await save_recommendations(ctx, recommendations)

    total_in = svc_usage["input_tokens"] + rhel_usage["input_tokens"]
    total_out = svc_usage["output_tokens"] + rhel_usage["output_tokens"]

    summary = service_analysis.get("summary", "") or rhel_analysis.get("summary", "")
    return SimpleNamespace(
        output=f"Diagnosis complete: {summary}".strip(),
        usage=lambda: SimpleNamespace(input_tokens=total_in, output_tokens=total_out),
        all_messages=lambda: [],
    )


async def _run_synthesis_and_apply(
    agent, model, deps: AgentDeps, context_prompt: str, agent_state: dict | None = None,
):
    """Iteration 2: Synthesize iter1 expert outputs + apply_planner + apply."""
    ctx = SimpleNamespace(deps=deps)
    save_rca = agent._function_toolset.tools["save_rca"].function
    save_recommendations = agent._function_toolset.tools["save_recommendations"].function

    # Get expert outputs from iter1 (stored on deps by orchestrator)
    expert_state = getattr(deps, "expert_state", {}) or {}
    service_analysis = expert_state.get("service_analysis", {})
    rhel_analysis = expert_state.get("rhel_analysis", {})
    all_inspection = expert_state.get("inspection", {})

    if agent_state is not None:
        agent_state["inspection"] = all_inspection

    resource_data = all_inspection.get("resource_limits", {})
    network_data = all_inspection.get("network", {})
    storage_data = all_inspection.get("storage", {})

    _profile = getattr(deps, "service_profile", None)
    _svc_name = _profile.name if _profile else "service"

    # Run synthesizer
    from core.prompts import synthesizer as synthesizer_prompt

    synth_prompt = synthesizer_prompt.build(
        system_fingerprint=getattr(deps, "system_fingerprint", "") or "unknown",
        service_analysis=service_analysis, rhel_analysis=rhel_analysis,
        service_name=_svc_name.upper(),
    )
    synthesis, synth_usage = _invoke_json_planner(model, "planner.synthesizer", synth_prompt, deps)
    if _planner_debug_enabled(deps):
        tool_result("debug", f"synthesizer raw:\n{json.dumps(synthesis, indent=2, ensure_ascii=False)}")

    _iter = getattr(deps, "iteration", 0)
    _save_planner_artifact(deps, "synthesizer", synthesis, iteration=_iter)

    rca_records = _coerce_records(synthesis.get("rca_records"), deps=deps)
    recommendations = _coerce_recommendations(synthesis.get("recommendations"), deps=deps)
    if rca_records:
        await save_rca(ctx, rca_records)
    if recommendations:
        await save_recommendations(ctx, recommendations)

    # Run apply_planner
    _tuning = (getattr(deps, "config", None) or {}).get("tuning") or {}
    _web_tgt = _tuning.get("service_targets") or _tuning.get("webserver_targets") or {}
    _kern_tgt = _tuning.get("kernel_targets") or {}
    _res_tgt = _tuning.get("resource_limits_targets") or {}
    _net_tgt = _tuning.get("network_targets") or {}
    _stor_tgt = _tuning.get("storage_targets") or {}

    from core.prompts import apply_planner as apply_planner_prompt

    apply_prompt = apply_planner_prompt.build(
        service_targets=_web_tgt, kernel_targets=_kern_tgt,
        resource_limits_targets=_res_tgt, network_targets=_net_tgt,
        storage_targets=_stor_tgt,
        recommendations=_clean_recs_for_planner(synthesis.get("recommendations", []), deps.config),
        resource_problems=resource_data.get("problems", []),
        network_problems=network_data.get("problems", []),
        storage_problems=storage_data.get("problems", []),
    )
    apply_plan, apply_usage = _invoke_json_planner(model, "planner.apply_planner", apply_prompt, deps)

    categories = ["webserver", "kernel", "resource_limits", "network", "storage"]
    plan_summary = {cat: list((apply_plan.get(cat) or {}).keys()) for cat in categories}
    tool_call("apply_planner", " | ".join(f"{c}={len(v)}" for c, v in plan_summary.items() if v))
    if _planner_debug_enabled(deps):
        tool_result("debug", f"apply_planner raw:\n{json.dumps(apply_plan, indent=2, ensure_ascii=False)}")
    _save_planner_artifact(deps, "apply_planner", apply_plan, iteration=_iter)

    # Safety net: inject config defaults
    _defaults_by_cat = {
        "webserver": _web_tgt, "kernel": _kern_tgt, "resource_limits": _res_tgt,
        "network": _net_tgt, "storage": _stor_tgt,
    }
    for _cat, _defaults in _defaults_by_cat.items():
        plan_cat = apply_plan.get(_cat)
        if not isinstance(plan_cat, dict):
            plan_cat = {}
            apply_plan[_cat] = plan_cat
        _cfg = getattr(deps, "config", None) or {}
        for _param, _default_val in _defaults.items():
            if _param not in plan_cat and not _is_blocked(_param, _default_val, _cfg):
                plan_cat[_param] = _default_val

    _enforce_nginx_fd_consistency(apply_plan, getattr(deps, "config", None) or {})

    if agent_state is not None:
        agent_state["apply_plan"] = apply_plan

    total_in = synth_usage["input_tokens"] + apply_usage["input_tokens"]
    total_out = synth_usage["output_tokens"] + apply_usage["output_tokens"]
    return SimpleNamespace(
        output=str(synthesis.get("summary") or "Plan and apply completed.").strip(),
        usage=lambda: SimpleNamespace(input_tokens=total_in, output_tokens=total_out),
        all_messages=lambda: [],
    )


async def _run_review_iteration(
    agent, model, deps: AgentDeps, context_prompt: str, agent_state: dict | None = None,
):
    """Iteration 3: Re-run experts with benchmark feedback, synthesize delta, apply."""
    ctx = SimpleNamespace(deps=deps)
    save_rca = agent._function_toolset.tools["save_rca"].function
    save_recommendations = agent._function_toolset.tools["save_recommendations"].function

    # Re-inspect to see current state after iter2 apply
    from agents.tools_inspect import inspect_all

    tool_call("inspect", "re-inspect — all 5 categories (post-apply)")
    all_inspection = inspect_all(deps.ssh, deps.config, adapter=deps.adapter)
    tool_result(
        "inspect",
        f"remaining issues: {all_inspection.get('summary', {}).get('total_issues', 0)} "
        f"({json.dumps(all_inspection.get('summary', {}).get('by_category', {}))}) ",
    )
    if agent_state is not None:
        agent_state["inspection"] = all_inspection

    # Build review context with feedback from iter2
    feedback = getattr(deps, "iteration_feedback", "") or ""

    webserver_data = all_inspection.get("webserver", {})
    kernel_data = all_inspection.get("kernel", {})
    resource_data = all_inspection.get("resource_limits", {})
    network_data = all_inspection.get("network", {})
    storage_data = all_inspection.get("storage", {})

    _web_llm = _filter_inspection_for_llm(webserver_data)
    _kern_llm = _filter_inspection_for_llm(kernel_data)
    _sys_line = getattr(deps, "system_fingerprint", "") or ""

    from core.prompts import service_expert as service_expert_prompt
    from core.prompts import rhel_expert as rhel_expert_prompt

    _profile = getattr(deps, "service_profile", None)
    _svc_name = _profile.name if _profile else "service"

    # Augment expert prompts with benchmark feedback
    review_context = (
        f"\n\nPREVIOUS ITERATION RESULTS:\n{feedback}\n\n"
        "Focus on parameters that may have been missed or need adjustment "
        "based on the benchmark results above. Only recommend CHANGES from "
        "the current applied state — do not repeat already-applied params."
    )

    service_prompt = service_expert_prompt.build(
        system_line=_sys_line + review_context,
        service_inspection=_web_llm, profile=_profile,
    )
    rhel_prompt = rhel_expert_prompt.build(
        system_line=_sys_line + review_context,
        kernel_inspection=_kern_llm, resource_data=resource_data,
        network_data=network_data, storage_data=storage_data,
    )

    (service_analysis, svc_usage), (rhel_analysis, rhel_usage) = await asyncio.gather(
        asyncio.to_thread(_invoke_json_planner, model, f"planner.{_svc_name}_expert_review", service_prompt, deps),
        asyncio.to_thread(_invoke_json_planner, model, "planner.rhel_expert_review", rhel_prompt, deps),
    )
    if _planner_debug_enabled(deps):
        tool_result("debug", f"{_svc_name}_expert_review raw:\n{json.dumps(service_analysis, indent=2, ensure_ascii=False)}")
        tool_result("debug", f"rhel_expert_review raw:\n{json.dumps(rhel_analysis, indent=2, ensure_ascii=False)}")

    _iter = getattr(deps, "iteration", 0)
    _save_planner_artifact(deps, f"{_svc_name}_expert_review", service_analysis, iteration=_iter)
    _save_planner_artifact(deps, "rhel_expert_review", rhel_analysis, iteration=_iter)

    if agent_state is not None:
        agent_state["service_analysis"] = service_analysis
        agent_state["rhel_analysis"] = rhel_analysis

    # Synthesize + plan (same as iter2)
    from core.prompts import synthesizer as synthesizer_prompt

    synth_prompt = synthesizer_prompt.build(
        system_fingerprint=_sys_line, service_analysis=service_analysis,
        rhel_analysis=rhel_analysis, service_name=_svc_name.upper(),
    )
    synthesis, synth_usage = _invoke_json_planner(model, "planner.synthesizer_review", synth_prompt, deps)

    rca_records = _coerce_records(synthesis.get("rca_records"), deps=deps)
    recommendations = _coerce_recommendations(synthesis.get("recommendations"), deps=deps)
    if rca_records:
        await save_rca(ctx, rca_records)
    if recommendations:
        await save_recommendations(ctx, recommendations)

    # Apply planner for delta
    _tuning = (getattr(deps, "config", None) or {}).get("tuning") or {}
    _web_tgt = _tuning.get("service_targets") or _tuning.get("webserver_targets") or {}
    _kern_tgt = _tuning.get("kernel_targets") or {}
    _res_tgt = _tuning.get("resource_limits_targets") or {}
    _net_tgt = _tuning.get("network_targets") or {}
    _stor_tgt = _tuning.get("storage_targets") or {}

    from core.prompts import apply_planner as apply_planner_prompt

    apply_prompt = apply_planner_prompt.build(
        service_targets=_web_tgt, kernel_targets=_kern_tgt,
        resource_limits_targets=_res_tgt, network_targets=_net_tgt,
        storage_targets=_stor_tgt,
        recommendations=_clean_recs_for_planner(synthesis.get("recommendations", []), deps.config),
        resource_problems=resource_data.get("problems", []),
        network_problems=network_data.get("problems", []),
        storage_problems=storage_data.get("problems", []),
    )
    apply_plan, apply_usage = _invoke_json_planner(model, "planner.apply_planner_review", apply_prompt, deps)

    categories = ["webserver", "kernel", "resource_limits", "network", "storage"]
    plan_summary = {cat: list((apply_plan.get(cat) or {}).keys()) for cat in categories}
    tool_call("apply_planner", " | ".join(f"{c}={len(v)}" for c, v in plan_summary.items() if v))
    _save_planner_artifact(deps, "apply_planner_review", apply_plan, iteration=_iter)

    _enforce_nginx_fd_consistency(apply_plan, getattr(deps, "config", None) or {})

    if agent_state is not None:
        agent_state["apply_plan"] = apply_plan

    total_in = svc_usage["input_tokens"] + rhel_usage["input_tokens"] + synth_usage["input_tokens"] + apply_usage["input_tokens"]
    total_out = svc_usage["output_tokens"] + rhel_usage["output_tokens"] + synth_usage["output_tokens"] + apply_usage["output_tokens"]
    return SimpleNamespace(
        output=str(synthesis.get("summary") or "Review and targeted fix completed.").strip(),
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
        # Fallback: LLM may use {component, setting, current, target, impact}
        setting = str(item.get("setting", "")).strip()
        current = str(item.get("current", "")).strip()
        target = str(item.get("target", "")).strip()
        # Build symptom from setting+current if standard fields are missing
        setting_symptom = ""
        if setting and current:
            setting_symptom = f"{setting}={current}" + (f" (target {target})" if target else "")
        elif setting:
            setting_symptom = setting
        symptom = (
            str(item.get("symptom", "")).strip() or issue or setting_symptom or cause or description
        )
        root_cause = str(item.get("root_cause", "")).strip()
        if not root_cause:
            if impact:
                root_cause = impact
            elif cause:
                root_cause = cause
            elif description:
                root_cause = description
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
                "scope": {
                    "service": "nginx",
                    "webserver": "nginx",
                    "nginx": "nginx",
                    "system": "system",
                    "kernel": "system",
                    "resource_limits": "system",
                    "network": "system",
                    "storage": "system",
                }.get(
                    str(normalized_item.get("scope", "nginx")).strip().lower() or "nginx",
                    str(normalized_item.get("scope", "nginx")).strip().lower() or "nginx",
                ),
                "changes": changes,
            }
        )
    return recommendations


def _strip_inline_comment(value: str) -> str:
    """Strip trailing '# comment' and ';' from a directive value."""
    # Remove inline comments: "auto; # resolves to 112" -> "auto"
    idx = value.find("#")
    if idx > 0:
        value = value[:idx]
    return value.strip().split(";")[0].strip()


def _enforce_nginx_fd_consistency(apply_plan: dict[str, Any], config: dict[str, Any]) -> None:
    """Ensure worker_rlimit_nofile can support worker_connections.

    worker_rlimit_nofile is a PER-PROCESS limit — each nginx worker
    independently gets this many FDs. Each connection uses ~2 FDs
    (client socket + file/upstream), so:
        worker_connections <= worker_rlimit_nofile / 2

    If worker_connections exceeds the FD budget, cap it.
    If worker_rlimit_nofile is too low for worker_connections, raise it.
    """

    web = apply_plan.get("webserver", {})
    if not isinstance(web, dict):
        return

    try:
        conns = int(web.get("worker_connections", "65536"))
        nofile = int(web.get("worker_rlimit_nofile", "200000"))
    except (ValueError, TypeError):
        return

    res_targets = config.get("tuning", {}).get("resource_limits_targets", {})
    max_safe_nofile = int(res_targets.get("systemd_nofile", "524288"))

    # Ensure nofile is aligned with systemd limit
    if nofile > max_safe_nofile:
        web["worker_rlimit_nofile"] = str(max_safe_nofile)
        nofile = max_safe_nofile

    # Each connection uses ~2 FDs (per worker, not shared across workers)
    max_conns = max(1024, (nofile // 2) - 512)
    if conns > max_conns:
        web["worker_connections"] = str(max_conns)

    # Align systemd LimitNOFILE with worker_rlimit_nofile
    res = apply_plan.get("resource_limits", {})
    if isinstance(res, dict):
        res["systemd_nofile"] = str(max_safe_nofile)


def _is_blocked(param: str, value: str, config: dict[str, Any] | None = None) -> bool:
    """Return True if param=value is blocked by config.yaml guardrails."""
    blocked = (config or {}).get("tuning", {}).get("blocked_values", {})
    blocked_vals = blocked.get(param)
    if not blocked_vals:
        return False
    return value.strip().lower() in {str(v).lower() for v in blocked_vals}


def _clean_recs_for_planner(
    recs: list[dict[str, Any]], config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Clean recommendation values before feeding to apply_planner.

    The synthesizer LLM sometimes appends '; reload nginx' or similar
    command fragments to values in the changes dict. Strip them so the
    planner sees clean param=value pairs. Also removes blocked values.
    """
    cleaned: list[dict[str, Any]] = []
    for rec in recs if isinstance(recs, list) else []:
        if not isinstance(rec, dict):
            continue
        rec = dict(rec)
        changes = rec.get("changes")
        if isinstance(changes, dict):
            rec["changes"] = {
                str(k).strip(): str(v).strip().split(";")[0].strip()
                for k, v in changes.items()
                if not _is_blocked(str(k).strip(), str(v).strip().split(";")[0].strip(), config)
            }
        cleaned.append(rec)
    return cleaned


def _normalize_synthesized_recommendation(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    raw_scope = str(item.get("scope", "")).strip().lower()
    if raw_scope in {"service", "webserver", "nginx"}:
        normalized["scope"] = "nginx"
    elif raw_scope in {"system", "kernel", "resource_limits", "network", "storage"}:
        normalized["scope"] = "system"

    changes = item.get("changes")
    if isinstance(changes, dict) and changes:
        return normalized

    # Fallback: map 'category' to 'scope' (LLM may use {category, description, command})
    category = str(item.get("category", "")).strip().lower()
    rec_type = str(item.get("type", "")).strip().lower() or category
    if rec_type in {"nginx", "webserver"}:
        normalized["scope"] = "nginx"
    elif rec_type in {"system", "irq", "kernel", "cgroup", "network"}:
        normalized["scope"] = "system"

    # Fallback: map {setting, recommended_value} to changes dict
    _setting = str(item.get("setting", "")).strip()
    _rec_val = str(item.get("recommended_value", "")).strip()
    if _setting and _rec_val and not changes:
        # recommended_value may be a dict (batched sysctls) or a scalar
        raw_rv = item.get("recommended_value")
        if isinstance(raw_rv, dict):
            normalized["changes"] = {
                str(k): _strip_inline_comment(str(v)) for k, v in raw_rv.items()
            }
        else:
            normalized["changes"] = {_setting: _strip_inline_comment(_rec_val)}
        normalized["title"] = (
            normalized.get("title") or str(item.get("description", "")).strip() or f"Set {_setting}"
        )
        normalized["recommendation"] = (
            normalized.get("recommendation")
            or str(item.get("description", "")).strip()
            or str(item.get("justification", "")).strip()
            or f"Set {_setting} to {_rec_val}"
        )
        normalized["rationale"] = (
            normalized.get("rationale") or str(item.get("justification", "")).strip() or ""
        )
        return normalized

    setting = item.get("setting")
    value = item.get("value")
    if isinstance(setting, list) and isinstance(value, list) and len(setting) == len(value):
        normalized["changes"] = {
            str(k): _strip_inline_comment(str(v)) for k, v in zip(setting, value)
        }
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
        normalized["changes"] = {str(setting): _strip_inline_comment(str(value))}
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
            directive_changes[d_str] = _strip_inline_comment(str(value))
        else:
            # Parse "directive_name value" from the string itself
            parts = d_str.split(None, 1)
            if len(parts) == 2:
                directive_changes[parts[0]] = _strip_inline_comment(parts[1].rstrip(";").strip())
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
            # Extract backlog=N from listen lines
            if line.startswith("listen "):
                backlog_m = re.search(r"backlog=(\d+)", line)
                if backlog_m:
                    snippet_changes["listen_backlog"] = backlog_m.group(1)
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                snippet_changes[parts[0]] = _strip_inline_comment(parts[1].rstrip(";").strip())
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

        # sed -i 's/^worker_processes .*/worker_processes auto;/' ...
        sed_match = re.search(
            r"sed\s.*'s[|/].*[|/]"
            r"(\w+)\s+([^;/|]+?)\s*;?\s*[|/]'",
            command,
        )
        if sed_match:
            extracted[sed_match.group(1)] = sed_match.group(2).strip()
            continue

        # systemctl set-property nginx.service IOWeight=100
        prop_match = re.search(r"set-property\s+\S+\s+(\w+)=(\S+)", command)
        if prop_match:
            key = prop_match.group(1)
            val = prop_match.group(2)
            prop_map = {"IOWeight": "cgroup_io_weight", "CPUWeight": "cgroup_cpu_weight"}
            extracted[prop_map.get(key, key)] = val
            continue

    return extracted


def _save_planner_artifact(
    deps: AgentDeps,
    source: str,
    payload: dict[str, Any],
    iteration: int = 0,
) -> None:
    iter_label = f"iter{iteration}_{source}" if iteration else source
    deps.memory.save_context(
        deps.session_id,
        "command_output",
        iter_label,
        json.dumps(payload, ensure_ascii=True),
        f"{iter_label} planner output",
    )
    file_map = {
        "rhel_expert": "02_rhel_expert",
        "synthesizer": "03_synthesizer",
        "apply_planner": "04_apply_planner",
    }
    base = file_map.get(source, f"01_{source}" if source.endswith("_expert") else source)
    if iteration:
        filename = f"iter{iteration}_{base}.md"
    else:
        filename = f"{base}.md"
    _persist_hypothesis_markdown(
        deps,
        filename=filename,
        title=f"{'Iter ' + str(iteration) + ' — ' if iteration else ''}"
        f"{source.replace('_', ' ').title()}",
        sections=[
            ("Summary", str(payload.get("summary", "")).strip() or "No summary returned."),
            ("Payload", _markdown_json(payload)),
        ],
    )


def _run_observational_debate_eval(
    deps: AgentDeps,
    model: Any,
    *,
    iteration: int,
    inspection: dict[str, Any],
    service_analysis: dict[str, Any],
    rhel_analysis: dict[str, Any],
    synthesis: dict[str, Any],
) -> dict[str, Any] | None:
    agent_cfg = (getattr(deps, "config", None) or {}).get("agent") or {}
    eval_cfg = agent_cfg.get("eval") or {}
    if not bool(eval_cfg.get("enabled", False)) or not bool(eval_cfg.get("observational", False)):
        return None

    from core.eval_harness import evaluate_case_bundle, llm_synth_judge

    system = (inspection.get("system") or {}).copy()
    if not system.get("ram_gb"):
        profile = deps.memory.get_profile(deps.session_id) or {}
        system["ram_gb"] = profile.get("ram_gb")
        system["os_cpu_count"] = system.get("os_cpu_count") or profile.get("cpu_cores")
        system["host"] = system.get("host") or profile.get("host")
        system["service"] = system.get("service") or profile.get("service")

    bundle = {
        "session_id": deps.session_id,
        "iteration": iteration,
        "system": system,
        "inspection": inspection,
        "service_expert": service_analysis,
        "rhel_expert": rhel_analysis,
        "synthesizer": synthesis,
        "requested_format": "json",
    }
    timeout_sec = float(eval_cfg.get("synth_timeout_sec", 300.0) or 300.0)
    result = evaluate_case_bundle(
        bundle,
        synth_judge=lambda payload: llm_synth_judge(model, payload, timeout_sec=timeout_sec),
    )
    findings = list(result.get("findings") or [])
    by_agent: dict[str, list[dict[str, Any]]] = {"nginx": [], "rhel": [], "synthesizer": []}
    for item in findings:
        if not isinstance(item, dict):
            continue
        agent_name = str(item.get("agent") or "")
        if agent_name in by_agent:
            by_agent[agent_name].append(item)

    def _agent_verdict(agent_findings: list[dict[str, Any]]) -> str:
        if any(str(item.get("severity")) == "fail" for item in agent_findings):
            return "fail"
        if any(str(item.get("severity")) == "warn" for item in agent_findings):
            return "warning"
        return "pass"

    def _agent_severity_counts(agent_findings: list[dict[str, Any]]) -> str:
        counts = Counter(
            str(item.get("severity"))
            for item in agent_findings
            if isinstance(item, dict) and item.get("severity")
        )
        parts = []
        for level in ("fail", "warn", "info"):
            count = counts.get(level, 0)
            if count:
                parts.append(f"{count} {level}")
        return ", ".join(parts) or "clean"

    def _agent_finding_summary(agent_findings: list[dict[str, Any]]) -> str:
        counts = Counter(
            str(item.get("rule_id"))
            for item in agent_findings
            if isinstance(item, dict) and item.get("rule_id")
        )
        return (
            ", ".join(
                f"{rule_id} x{count}" if count > 1 else rule_id
                for rule_id, count in counts.most_common(3)
            )
            or "clean"
        )

    def _agent_correction_summary(agent_name: str, agent_findings: list[dict[str, Any]]) -> str:
        if agent_name != "nginx":
            return ""
        corrections = []
        for item in agent_findings:
            if not isinstance(item, dict):
                continue
            correction = str(item.get("correction") or "").strip()
            if correction:
                corrections.append(correction)
        return "; ".join(corrections[:2])

    def _agent_detail_summary(agent_name: str, agent_findings: list[dict[str, Any]]) -> str:
        if agent_name != "synthesizer":
            return ""
        details = []
        for item in agent_findings:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "").strip()
            if message:
                details.append(message)
        return "; ".join(details[:2])

    agent_scores = {
        "nginx": float(result.get("nginx_score", 0.0)),
        "rhel": float(result.get("rhel_score", 0.0)),
        "synthesizer": float(result.get("synthesizer_score", 0.0)),
    }
    result["by_agent"] = {
        agent_name: {
            "score": agent_scores.get(agent_name, 0.0),
            "verdict": _agent_verdict(agent_findings),
            "severity_counts": _agent_severity_counts(agent_findings),
            "findings": agent_findings,
            "summary": _agent_finding_summary(agent_findings),
            "corrections": _agent_correction_summary(agent_name, agent_findings),
            "details": _agent_detail_summary(agent_name, agent_findings),
        }
        for agent_name, agent_findings in by_agent.items()
    }

    tool_result(
        "eval",
        (
            "debate observational: "
            f"score={float(result.get('total_score', 0.0)):.2f} "
            f"action={result.get('action', 'unknown')}"
        ),
    )
    for agent_name in ("nginx", "rhel", "synthesizer"):
        agent_view = result["by_agent"][agent_name]
        tool_result(
            "eval",
            (
                f"{agent_name} score={float(agent_view.get('score', 0.0)):.2f} "
                f"verdict={agent_view.get('verdict', 'unknown')} "
                f"counts={agent_view.get('severity_counts', 'clean')} "
                f"findings={agent_view.get('summary', 'clean')}"
            ),
        )
        if agent_view.get("corrections"):
            tool_result("eval", f"{agent_name} corrections={agent_view['corrections']}")
        if agent_view.get("details"):
            tool_result("eval", f"{agent_name} details={agent_view['details']}")
    source = f"iter{iteration}_debate_eval" if iteration else "debate_eval"
    deps.memory.save_context(
        deps.session_id,
        "command_output",
        source,
        json.dumps(result, ensure_ascii=True),
        (
            "debate eval: "
            f"score={float(result.get('total_score', 0.0)):.2f} "
            f"action={result.get('action', 'unknown')}"
        ),
    )
    return result


def save_iteration_summary(
    deps: AgentDeps,
    *,
    iteration: int,
    baselines: dict[str, Any],
    results: dict[str, Any],
    regressions: list[str],
    decision: str,
    diagnosis: Any = None,
) -> None:
    """Save a complete iteration summary to the hypothesis folder."""
    cfg = getattr(deps, "config", {}) or {}
    agent_cfg = cfg.get("agent") or {}
    enabled = bool(agent_cfg.get("persist_hypotheses")) or bool(
        agent_cfg.get("debug_planner_payloads", False)
    )
    if not enabled:
        return

    path = Path(__file__).resolve().parent.parent / "hypothesis" / str(deps.session_id)
    path.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Iteration {iteration} Summary",
        "",
        "## Benchmark Results",
        "",
        "| Workload | Baseline RPS | Current RPS | Change | p99 (ms) | Status |",
        "|----------|-------------|-------------|--------|----------|--------|",
    ]
    for workload in ("homepage", "small", "medium", "large", "mixed"):
        b_rps = float(baselines.get(workload, {}).get("rps", 0) or 0)
        c_rps = float(results.get(workload, {}).get("rps", 0) or 0)
        c_p99 = float(results.get(workload, {}).get("p99", 0) or 0)
        if not b_rps and not c_rps:
            continue
        pct = ((c_rps - b_rps) / b_rps * 100) if b_rps else 0
        status = "OK" if c_rps >= b_rps * 0.99 else "REGRESSED"
        lines.append(
            f"| {workload} | {b_rps:.0f} | {c_rps:.0f} | {pct:+.1f}% | {c_p99:.1f} | {status} |"
        )

    # Applied changes
    lines.extend(["", "## Applied Changes", ""])
    if diagnosis:
        svc_applied = getattr(diagnosis, "service_applied", False)
        system_applied = getattr(diagnosis, "system_applied", False)
        lines.append(f"- Service applied: {svc_applied}")
        lines.append(f"- System applied: {system_applied}")
        recs = getattr(diagnosis, "recommendations", []) or []
        if recs:
            lines.append(f"- Recommendations count: {len(recs)}")
            for r in recs[:20]:
                title = r.get("title", r.get("action", "?"))
                scope = r.get("scope", "?")
                changes = r.get("changes", {})
                lines.append(f"  - [{scope}] {title}: {changes}")

    # Regressions
    if regressions:
        lines.extend(["", "## Regressions Detected", ""])
        for r in regressions:
            lines.append(f"- {r}")

    # Decision
    lines.extend(["", "## Decision", "", decision, ""])

    target = path / f"iter{iteration}_00_summary.md"
    target.write_text("\n".join(lines), encoding="utf-8")

    # Also save to database
    deps.memory.save_context(
        deps.session_id,
        "command_output",
        f"iter{iteration}_summary",
        json.dumps(
            {
                "iteration": iteration,
                "baselines": {
                    w: baselines.get(w, {}).get("rps", 0) for w in ("small", "medium", "large")
                },
                "results": {
                    w: results.get(w, {}).get("rps", 0) for w in ("small", "medium", "large")
                },
                "regressions": regressions,
                "decision": decision,
            }
        ),
        f"iteration {iteration}: {decision}",
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
