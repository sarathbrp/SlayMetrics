from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict

from adapters.base import BenchmarkResult
from agents import AgentDeps, TokenCounter
from core.log import llm_call, log, tokens, tool_call, tool_result
from langgraph.graph.message import add_messages


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

    def __post_init__(self) -> None:
        self.after_rps = _coerce_float(self.after_rps)
        self.improvement_pct = _coerce_float(self.improvement_pct)
        self.notes = _coerce_notes(self.notes)


SYSTEM_PROMPT = """\
You are SlayMetricsAgent. Steps:
1. Inspect nginx and system tuning
2. Apply missing nginx fixes via apply_nginx_tuning
3. Apply missing system fixes via apply_system_tuning
4. Benchmark AFTER (baselines are provided — do NOT benchmark before)
5. Call save_findings with both results in one call
6. Return one short plain-text summary sentence

For apply_nginx_tuning and apply_system_tuning:
- Pass a single string argument named payload
- payload must be a JSON object string, for example: {"access_log":"off","listen_backlog":"65535"}

For save_findings:
- Pass a single string argument named findings_json
- findings_json must be a JSON array string

Proven nginx fixes (bare metal, 112 cores, 2 NUMA nodes, 25Gbps NIC):
- worker_connections=65536
- worker_rlimit_nofile=200000
- worker_cpu_affinity=auto
- open_file_cache=max=200000 inactive=60s
- open_file_cache_valid=30s
- open_file_cache_min_uses=2
- access_log=off
- tcp_nodelay=on
- keepalive_requests=10000
- keepalive_timeout=30
- reset_timedout_connection=on
- listen_backlog=65535
- aio=threads

Proven system fixes:
- net.core.somaxconn=65535
- net.ipv4.tcp_max_syn_backlog=65535
- net.core.netdev_max_backlog=65535
- net.core.rmem_max=16777216
- net.core.wmem_max=16777216
- net.ipv4.tcp_tw_reuse=1
- net.ipv4.tcp_max_tw_buckets=2000000
- net.ipv4.ip_local_port_range=1024 65535
- transparent_hugepage=never
- selinux=permissive
- cpu_governor=performance

Do NOT apply gzip — test files are random binary data, compression wastes CPU.

Rules: skip already-applied fixes, no packages, no reboot, no pre-fix benchmark.
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
            response = llm.invoke(messages)
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


def build(model) -> DiagnosisWorkflow:
    state: dict[str, Any] = {
        "nginx_applied": False,
        "system_applied": False,
        "after_rps": 0.0,
        "findings": [],
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

    def _parse_payload_arg(payload: str | None, tool_name: str) -> tuple[dict[str, str] | None, str | None]:
        return _normalize_changes(payload, tool_name)

    nginx_targets = {
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
    }

    system_targets = {
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
    }

    def inspect_nginx_impl(deps: AgentDeps) -> dict:
        tool_call("inspect", "nginx config — comparing against proven fixes")
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

        result = {"needs_fixing": needs_fixing, "already_ok": already_ok}
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
        return result

    def inspect_system_impl(deps: AgentDeps) -> dict:
        tool_call("inspect", "system tuning — comparing against proven fixes")
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
            result = ssh.execute(f"sysctl -n {key} 2>/dev/null")
            current[key] = result.stdout.strip()

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

        needs_fixing = {}
        already_ok = []
        for parameter, target in system_targets.items():
            cur = current.get(parameter, "unknown")
            if cur != target:
                needs_fixing[parameter] = {"current": cur, "target": target}
            else:
                already_ok.append(parameter)

        result = {"needs_fixing": needs_fixing, "already_ok": already_ok}
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
        return result

    def apply_nginx_impl(deps: AgentDeps, changes: dict[str, str] | str | None, **kwargs: Any) -> dict:
        normalized_changes, parse_error = _coerce_tool_changes(changes, kwargs, "apply_nginx")
        if parse_error:
            tool_call("apply_nginx", "invalid input payload")
            tool_result("apply_nginx", f"FAILED: {parse_error}")
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
        tool_result("apply_nginx", summary)
        return result

    def apply_system_impl(deps: AgentDeps, changes: dict[str, str] | str | None, **kwargs: Any) -> dict:
        normalized_changes, parse_error = _coerce_tool_changes(changes, kwargs, "apply_system")
        if parse_error:
            tool_call("apply_system", "invalid input payload")
            tool_result("apply_system", f"FAILED: {parse_error}")
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
                ssh.execute(f"setenforce {mode} 2>&1")
                applied[param] = value
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
            f"RPS={result.requests_per_sec:.1f} p99={result.latency_p99_ms:.1f}ms CPU={result.cpu_pct:.1f}%",
            "post-fix benchmark",
        )
        deps.token_counter.tool_calls += 1
        state["after_rps"] = float(result.requests_per_sec)
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

        for finding in findings:
            param = finding.get("parameter", "unknown")
            before_rps = _coerce_optional_float(finding.get("before_rps", 0))
            after_rps = _coerce_optional_float(finding.get("after_rps", 0))
            impact_pct = _coerce_optional_float(finding.get("impact_pct", 0))
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
        return True

    async def inspect_nginx_config(ctx) -> dict:
        return inspect_nginx_impl(ctx.deps)

    async def inspect_system_tuning(ctx) -> dict:
        return inspect_system_impl(ctx.deps)

    async def apply_nginx_tuning(ctx, changes: dict[str, str] | str | None = None, **kwargs: Any) -> dict:
        return apply_nginx_impl(ctx.deps, changes, **kwargs)

    async def apply_system_tuning(ctx, changes: dict[str, str] | str | None = None, **kwargs: Any) -> dict:
        return apply_system_impl(ctx.deps, changes, **kwargs)

    async def run_benchmark(ctx, duration: int = 30) -> dict:
        return run_benchmark_impl(ctx.deps, duration)

    async def query_memory(ctx, symptom: str) -> list[dict]:
        return query_memory_impl(ctx.deps, symptom)

    async def save_findings(ctx, findings: list[dict[str, Any]]) -> bool:
        return save_findings_impl(ctx.deps, findings)

    def tool_factory(deps: AgentDeps):
        from langchain_core.tools import tool

        @tool
        def inspect_nginx_config() -> dict:
            """Inspect nginx config and return only what needs fixing vs proven targets."""
            return inspect_nginx_impl(deps)

        @tool
        def inspect_system_tuning() -> dict:
            """Inspect system tuning and return only what needs fixing vs proven targets."""
            return inspect_system_impl(deps)

        @tool
        def apply_nginx_tuning(payload: str = "{}") -> dict:
            """Apply multiple nginx config changes from a JSON object string payload."""
            changes, parse_error = _parse_payload_arg(payload, "apply_nginx")
            if parse_error:
                tool_call("apply_nginx", "invalid input payload")
                tool_result("apply_nginx", f"FAILED: {parse_error}")
                deps.token_counter.tool_calls += 1
                return {"applied": [], "failed": [], "reload": "FAILED", "error": parse_error}
            return apply_nginx_impl(deps, changes)

        @tool
        def apply_system_tuning(payload: str = "{}") -> dict:
            """Apply multiple system changes from a JSON object string payload."""
            changes, parse_error = _parse_payload_arg(payload, "apply_system")
            if parse_error:
                tool_call("apply_system", "invalid input payload")
                tool_result("apply_system", f"FAILED: {parse_error}")
                deps.token_counter.tool_calls += 1
                return {"applied": {}, "failed": {"_input": parse_error}}
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
        def save_findings(findings_json: str = "[]") -> bool:
            """Save all findings from a JSON array string."""
            try:
                findings = json.loads(findings_json)
            except json.JSONDecodeError as e:
                tool_call("save", "invalid findings_json payload")
                tool_result("save", f"FAILED: invalid findings_json ({e.msg})")
                deps.token_counter.tool_calls += 1
                return False
            if not isinstance(findings, list):
                tool_call("save", "invalid findings_json payload")
                tool_result("save", "FAILED: findings_json must decode to a list")
                deps.token_counter.tool_calls += 1
                return False
            return save_findings_impl(deps, findings)

        return [
            inspect_nginx_config,
            inspect_system_tuning,
            apply_nginx_tuning,
            apply_system_tuning,
            run_benchmark,
            query_memory,
            save_findings,
        ]

    test_tools = {
        "inspect_nginx_config": inspect_nginx_config,
        "inspect_system_tuning": inspect_system_tuning,
        "apply_nginx_tuning": apply_nginx_tuning,
        "apply_system_tuning": apply_system_tuning,
        "run_benchmark": run_benchmark,
        "query_memory": query_memory,
        "save_findings": save_findings,
    }
    return DiagnosisWorkflow(model, state, test_tools, tool_factory)


async def run(model, deps: AgentDeps, context_prompt: str) -> DiagnosisOutput:
    """Run the diagnosis workflow and derive the final structured result in Python."""
    llm_call("agent", "Starting diagnosis — sending context to LLM...")
    agent = build(model)
    state = getattr(agent, "_slaymetrics_state", {})
    result = await agent.run(context_prompt, deps=deps)
    _attribute_tool_tokens(result, deps.token_counter)
    inp, out = deps.token_counter.add(result.usage())
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

    return DiagnosisOutput(
        nginx_applied=bool(state.get("nginx_applied", False)),
        system_applied=bool(state.get("system_applied", False)),
        after_rps=after_rps,
        improvement_pct=improvement_pct,
        notes=notes,
    )


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


def _attribute_tool_tokens(run_result: Any, token_counter: TokenCounter) -> None:
    try:
        messages = run_result.all_messages()
    except Exception:
        return

    for message in messages:
        usage = getattr(message, "usage_metadata", None) or {}
        if not usage:
            continue
        input_tokens = (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("input_token_count")
            or 0
        )
        output_tokens = (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("output_token_count")
            or 0
        )
        if input_tokens or output_tokens:
            token_counter.input_tokens += int(input_tokens)
            token_counter.output_tokens += int(output_tokens)


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
