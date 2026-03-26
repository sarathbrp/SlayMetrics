from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from adapters.base import BenchmarkResult
from agents import AgentDeps, TokenCounter
from core.log import llm_call, log, tokens, tool_call, tool_result


class DiagnosisOutput(BaseModel):
    """Minimal output — orchestrator builds the full report from tool returns."""

    nginx_applied: bool
    system_applied: bool
    after_rps: float
    improvement_pct: float
    notes: str = ""


SYSTEM_PROMPT = """\
You are SlayMetricsAgent. Steps:
1. Inspect nginx and system tuning
2. Apply missing nginx fixes via apply_nginx_tuning
3. Apply missing system fixes via apply_system_tuning
4. Benchmark AFTER (baselines are provided — do NOT benchmark before)
5. Call save_findings with both results in one call
6. Return DiagnosisOutput

Proven nginx fixes: worker_connections=8192, open_file_cache=max=10000 inactive=60s, open_file_cache_valid=30s, open_file_cache_min_uses=2, worker_rlimit_nofile=65536, access_log=off, tcp_nodelay=on, keepalive_requests=1000, gzip=on, gzip_comp_level=1, listen_backlog=65535

Proven system fixes: net.core.somaxconn=65535, net.ipv4.tcp_max_syn_backlog=65535, net.core.netdev_max_backlog=65535, transparent_hugepage=never, selinux=permissive, net.ipv4.tcp_tw_reuse=1, net.core.rmem_max=16777216, net.core.wmem_max=16777216

Rules: skip already-applied fixes, no packages, no reboot, no pre-fix benchmark.
"""


def build(model) -> Agent:
    agent: Agent[AgentDeps, DiagnosisOutput] = Agent(
        model,
        deps_type=AgentDeps,
        output_type=DiagnosisOutput,
        system_prompt=SYSTEM_PROMPT,
    )

    def _normalize_changes(
        raw_changes: dict[str, str] | str, tool_name: str
    ) -> tuple[dict[str, str] | None, str | None]:
        changes_obj = raw_changes
        if isinstance(changes_obj, str):
            try:
                changes_obj = json.loads(changes_obj)
            except json.JSONDecodeError as e:
                return None, f"{tool_name}: invalid JSON for 'changes' ({e.msg})"

        if not isinstance(changes_obj, dict):
            return None, f"{tool_name}: 'changes' must be a dictionary"

        normalized: dict[str, str] = {}
        for key, value in changes_obj.items():
            if not isinstance(key, str):
                key = str(key)
            if isinstance(value, (dict, list)):
                normalized[key] = json.dumps(value, separators=(",", ":"))
            else:
                normalized[key] = str(value)
        return normalized, None

    # Proven optimal values — inspect compares against these
    NGINX_TARGETS = {
        "worker_connections": "8192",
        "open_file_cache": "max=10000 inactive=60s",
        "open_file_cache_valid": "30s",
        "open_file_cache_min_uses": "2",
        "worker_rlimit_nofile": "65536",
        "access_log": "off",
        "tcp_nodelay": "on",
        "keepalive_requests": "1000",
        "gzip": "on",
        "gzip_comp_level": "1",
        "listen_backlog": "65535",
    }

    SYSTEM_TARGETS = {
        "net.core.somaxconn": "65535",
        "net.ipv4.tcp_max_syn_backlog": "65535",
        "net.core.netdev_max_backlog": "65535",
        "transparent_hugepage": "never",
        "selinux": "permissive",
        "net.ipv4.tcp_tw_reuse": "1",
        "net.core.rmem_max": "16777216",
        "net.core.wmem_max": "16777216",
    }

    @agent.tool
    async def inspect_nginx_config(ctx: RunContext[AgentDeps]) -> dict:
        """Inspect nginx config and return only what needs fixing vs proven targets."""
        tool_call("inspect", "nginx config — comparing against proven fixes")
        ssh = ctx.deps.ssh

        raw = ssh.execute("nginx -T 2>/dev/null").stdout

        # Parse current values
        current = {}
        for d in NGINX_TARGETS:
            if d == "listen_backlog":
                m = re.search(r"listen\s+.*backlog=(\d+)", raw)
                current[d] = m.group(1) if m else "not set"
            else:
                m = re.search(rf"^\s*{d}\s+(.+?);", raw, re.MULTILINE)
                current[d] = m.group(1).strip() if m else "not set"

        # Compare against targets
        needs_fixing = {}
        already_ok = []
        for param, target in NGINX_TARGETS.items():
            cur = current.get(param, "not set")
            if cur == "not set" or cur != target:
                needs_fixing[param] = {"current": cur, "target": target}
            else:
                already_ok.append(param)

        result = {
            "needs_fixing": needs_fixing,
            "already_ok": already_ok,
        }

        ctx.deps.token_counter.tool_calls += 1
        ctx.deps.memory.save_context(
            ctx.deps.session_id,
            "command_output",
            "inspect_nginx",
            str(result),
            f"nginx: {len(needs_fixing)} need fixing, {len(already_ok)} ok",
        )
        tool_result(
            "inspect", f"nginx: {len(needs_fixing)} need fixing, {len(already_ok)} already ok"
        )
        return result

    @agent.tool
    async def inspect_system_tuning(ctx: RunContext[AgentDeps]) -> dict:
        """Inspect system tuning and return only what needs fixing vs proven targets."""
        tool_call("inspect", "system tuning — comparing against proven fixes")
        ssh = ctx.deps.ssh

        current = {}

        # Sysctl params
        for key in [
            "net.core.somaxconn",
            "net.ipv4.tcp_max_syn_backlog",
            "net.core.netdev_max_backlog",
            "net.ipv4.tcp_tw_reuse",
            "net.core.rmem_max",
            "net.core.wmem_max",
        ]:
            r = ssh.execute(f"sysctl -n {key} 2>/dev/null")
            current[key] = r.stdout.strip()

        # THP
        r = ssh.execute("cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null")
        m = re.search(r"\[(\w+)\]", r.stdout)
        current["transparent_hugepage"] = m.group(1) if m else "unknown"

        # SELinux
        current["selinux"] = (
            ssh.execute("getenforce 2>/dev/null || echo Disabled").stdout.strip().lower()
        )

        # Compare against targets
        needs_fixing = {}
        already_ok = []
        for param, target in SYSTEM_TARGETS.items():
            cur = current.get(param, "unknown")
            if cur != target:
                needs_fixing[param] = {"current": cur, "target": target}
            else:
                already_ok.append(param)

        result = {
            "needs_fixing": needs_fixing,
            "already_ok": already_ok,
        }

        ctx.deps.token_counter.tool_calls += 1
        ctx.deps.memory.save_context(
            ctx.deps.session_id,
            "command_output",
            "inspect_system",
            str(result),
            f"system: {len(needs_fixing)} need fixing, {len(already_ok)} ok",
        )
        tool_result(
            "inspect", f"system: {len(needs_fixing)} need fixing, {len(already_ok)} already ok"
        )
        return result

    @agent.tool
    async def apply_nginx_tuning(
        ctx: RunContext[AgentDeps], changes: dict[str, str] | str
    ) -> dict:
        """Apply multiple nginx config changes in one batch, then reload.
        Example: {"sendfile": "on", "tcp_nopush": "on",
        "open_file_cache": "max=10000 inactive=60s"}
        """
        normalized_changes, parse_error = _normalize_changes(changes, "apply_nginx")
        if parse_error:
            tool_call("apply_nginx", "invalid input payload")
            tool_result("apply_nginx", f"FAILED: {parse_error}")
            ctx.deps.token_counter.tool_calls += 1
            return {"applied": [], "failed": [], "reload": "FAILED", "error": parse_error}

        if normalized_changes is None:
            return {"applied": [], "failed": [], "reload": "FAILED", "error": "invalid payload"}
        changes_dict = normalized_changes
        allowed = getattr(ctx.deps.adapter, "ALLOWED_BATCH_DIRECTIVES", None)
        unsupported: list[str] = []
        if allowed is not None:
            unsupported = [k for k in changes_dict if k not in allowed]
            changes_dict = {k: v for k, v in changes_dict.items() if k in allowed}
            if unsupported and not changes_dict:
                error = f"unsupported nginx directives: {', '.join(unsupported)}"
                tool_call("apply_nginx", "unsupported directives")
                tool_result("apply_nginx", f"FAILED: {error}")
                ctx.deps.token_counter.tool_calls += 1
                return {"applied": [], "failed": unsupported, "reload": "FAILED", "error": error}

        tool_call("apply_nginx", f"{len(changes_dict)} changes: {', '.join(changes_dict.keys())}")

        config_path = ctx.deps.config["service"]["config_path"]
        batch_backup = f"/tmp/slay_nginx_batch_{ctx.deps.session_id}.conf"
        ctx.deps.ssh.execute(f"cp {config_path} {batch_backup}")

        applied = []
        failed = list(unsupported)
        for param, value in changes_dict.items():
            success = ctx.deps.adapter.apply_config(param, value)
            if success:
                applied.append(param)
            else:
                failed.append(param)
                ctx.deps.ssh.execute(f"cp {batch_backup} {config_path}")
                result = {
                    "applied": applied,
                    "failed": failed,
                    "reload": "FAILED",
                    "error": f"failed to apply nginx directive: {param}",
                }
                ctx.deps.token_counter.tool_calls += 1
                ctx.deps.memory.save_context(
                    ctx.deps.session_id,
                    "command_output",
                    f"apply_nginx:{','.join(changes_dict.keys())}"[:250],
                    f"applied={applied} failed={failed}",
                    "nginx batch apply",
                )
                tool_result("apply_nginx", f"FAILED: {result['error']}")
                return result

        # Test config before reload
        test = ctx.deps.ssh.execute("nginx -t 2>&1")
        if "syntax is ok" not in test.stdout and "test is successful" not in test.stdout:
            # Config is broken — revert to backup and report error
            ctx.deps.ssh.execute(f"cp {batch_backup} {config_path}")
            error_msg = test.stdout.strip()[:200]
            result = {
                "applied": applied,
                "failed": failed,
                "reload": "FAILED",
                "error": f"nginx -t failed: {error_msg}",
            }
            ctx.deps.token_counter.tool_calls += 1
            ctx.deps.memory.save_context(
                ctx.deps.session_id,
                "command_output",
                f"apply_nginx:{','.join(changes_dict.keys())}"[:250],
                f"applied={applied} failed={failed}",
                "nginx batch apply failed",
            )
            tool_result("apply_nginx", f"FAILED: {error_msg}")
            return result

        # Config valid — reload
        reload_ok = ctx.deps.adapter.reload()

        result = {
            "applied": applied,
            "failed": failed,
            "reload": "OK" if reload_ok else "FAILED",
        }
        if unsupported:
            result["warning"] = f"ignored unsupported nginx directives: {', '.join(unsupported)}"

        ctx.deps.token_counter.tool_calls += 1
        summary = f"applied={applied} failed={failed} reload={'OK' if reload_ok else 'FAILED'}"
        ctx.deps.memory.save_context(
            ctx.deps.session_id,
            "command_output",
            f"apply_nginx:{','.join(changes_dict.keys())}"[:250],
            summary,
            "nginx batch apply",
        )
        tool_result("apply_nginx", summary)
        return result

    @agent.tool
    async def apply_system_tuning(
        ctx: RunContext[AgentDeps], changes: dict[str, str] | str
    ) -> dict:
        """Apply multiple sysctl/kernel changes in one batch.
        Example: {"net.core.somaxconn": "65535", "transparent_hugepage": "never",
        "selinux": "permissive"}
        """
        normalized_changes, parse_error = _normalize_changes(changes, "apply_system")
        if parse_error:
            tool_call("apply_system", "invalid input payload")
            tool_result("apply_system", f"FAILED: {parse_error}")
            ctx.deps.token_counter.tool_calls += 1
            return {"applied": {}, "failed": {"_input": parse_error}}

        if normalized_changes is None:
            return {"applied": {}, "failed": {"_input": "invalid payload"}}
        changes_dict = normalized_changes
        tool_call(
            "apply_system",
            f"{len(changes_dict)} changes: {', '.join(changes_dict.keys())}",
        )

        ssh = ctx.deps.ssh
        applied = {}
        failed = {}

        for param, value in changes_dict.items():
            if param == "transparent_hugepage":
                r = ssh.execute(f"echo {value} > /sys/kernel/mm/transparent_hugepage/enabled 2>&1")
                if r.ok:
                    applied[param] = value
                else:
                    failed[param] = r.stderr.strip()
            elif param == "selinux":
                mode = "0" if value.lower() in ("permissive", "0") else "1"
                r = ssh.execute(f"setenforce {mode} 2>&1")
                applied[param] = value
            else:
                r = ssh.execute(f"sysctl -w {param}={value} 2>&1")
                if r.ok:
                    applied[param] = value
                else:
                    failed[param] = r.stderr.strip()

        result = {"applied": applied, "failed": failed}

        ctx.deps.token_counter.tool_calls += 1
        summary = f"applied={list(applied.keys())} failed={list(failed.keys())}"
        ctx.deps.memory.save_context(
            ctx.deps.session_id,
            "command_output",
            f"apply_system:{','.join(changes_dict.keys())}"[:250],
            summary,
            "system batch apply",
        )
        tool_result("apply_system", summary)
        return result

    @agent.tool
    async def run_benchmark(ctx: RunContext[AgentDeps], duration: int = 30) -> dict:
        """Run wrk2 benchmark against the small file URL. Returns RPS and latency."""
        bench_cfg = ctx.deps.config["service"]["benchmark"]
        url = bench_cfg.get("small_file_url", "http://localhost/")
        tool_call("benchmark", f"wrk2 {duration}s -> {url}")
        result: BenchmarkResult = ctx.deps.adapter.benchmark(duration, url)
        tool_result(
            "benchmark",
            f"{result.requests_per_sec:.1f} RPS, "
            f"p99={result.latency_p99_ms:.1f}ms, CPU={result.cpu_pct:.1f}%",
        )
        ctx.deps.memory.save_context(
            ctx.deps.session_id,
            "benchmark",
            url,
            f"RPS={result.requests_per_sec:.1f} p99={result.latency_p99_ms:.1f}ms CPU={result.cpu_pct:.1f}%",
            "post-fix benchmark",
        )
        ctx.deps.token_counter.tool_calls += 1
        return result.__dict__

    _memory_query_count = 0
    MAX_MEMORY_QUERIES = 3

    @agent.tool
    async def query_memory(ctx: RunContext[AgentDeps], symptom: str) -> list[dict]:
        """Search the knowledge base and past findings. Limited to 3 queries — use wisely."""
        nonlocal _memory_query_count
        _memory_query_count += 1
        if _memory_query_count > MAX_MEMORY_QUERIES:
            tool_call("memory", f"BLOCKED — limit reached ({MAX_MEMORY_QUERIES})")
            tool_result("memory", "limit reached; returning no additional memory results")
            ctx.deps.token_counter.tool_calls += 1
            return []
        tool_call("memory", f"query {_memory_query_count}/{MAX_MEMORY_QUERIES}: {symptom}")
        results = ctx.deps.memory.semantic_search(symptom, ctx.deps.session_id, top_k=5)
        tool_result("memory", f"{len(results)} results found")
        ctx.deps.token_counter.tool_calls += 1
        return results

    @agent.tool
    async def save_findings(
        ctx: RunContext[AgentDeps],
        findings: list[dict[str, Any]],
    ) -> bool:
        """Save all fix results to memory in one call.
        Each finding: {parameter, before_value, after_value, before_rps, after_rps, impact_pct}
        """
        tool_call("save", f"{len(findings)} findings")
        for f in findings:
            param = f.get("parameter", "unknown")
            ctx.deps.memory.save_fact(
                session_id=ctx.deps.session_id,
                type="fix",
                parameter=param,
                reasoning=f.get("reasoning", "proven fix applied"),
                before_value=f.get("before_value", ""),
                after_value=f.get("after_value", ""),
                before_rps=f.get("before_rps", 0),
                after_rps=f.get("after_rps", 0),
                impact_pct=f.get("impact_pct", 0),
            )
            tool_result("save", f"{param} ({f.get('impact_pct', 0):+.1f}%)")
        ctx.deps.token_counter.tool_calls += 1
        return True

    return agent


async def run(model, deps: AgentDeps, context_prompt: str) -> DiagnosisOutput:
    """Run the single agent with full context."""
    llm_call("agent", "Starting diagnosis — sending context to LLM...")
    agent = build(model)
    result = await agent.run(context_prompt, deps=deps)
    _attribute_tool_tokens(result, deps.token_counter)
    inp, out = deps.token_counter.add(result.usage())
    llm_call("agent", f"LLM finished — {inp:,} in / {out:,} out tokens")
    tokens("agent", inp, out, deps.token_counter.summary())
    rows = deps.token_counter.tool_token_rows()
    if rows:
        top = ", ".join(
            f"{r['tool']}={r['total_tokens']:,}t/{r['calls']}c"
            for r in sorted(rows, key=lambda x: x["total_tokens"], reverse=True)[:5]
        )
        log("agent", f"Tool token attribution: {top}", "result")
    return result.output


def _attribute_tool_tokens(run_result: Any, token_counter: TokenCounter) -> None:
    """Approximate per-tool token attribution from model message usage deltas."""
    try:
        messages = run_result.all_messages()
    except Exception:
        return

    pending_post_tools: list[str] = []
    for msg in messages:
        usage = getattr(msg, "usage", None)
        parts = getattr(msg, "parts", None) or []

        tool_names = [p.tool_name for p in parts if getattr(p, "part_kind", "") == "tool-call"]

        if usage and pending_post_tools and not tool_names:
            _distribute_usage(
                token_counter,
                pending_post_tools,
                int(usage.input_tokens or 0),
                int(usage.output_tokens or 0),
                phase="post",
            )
            pending_post_tools = []

        if usage and tool_names:
            _distribute_usage(
                token_counter,
                tool_names,
                int(usage.input_tokens or 0),
                int(usage.output_tokens or 0),
                phase="call",
            )
            pending_post_tools = list(tool_names)


def _distribute_usage(
    token_counter: TokenCounter,
    tool_names: list[str],
    input_tokens: int,
    output_tokens: int,
    phase: str,
) -> None:
    n = len(tool_names)
    if n == 0:
        return
    in_base, in_rem = divmod(max(0, input_tokens), n)
    out_base, out_rem = divmod(max(0, output_tokens), n)
    for i, name in enumerate(tool_names):
        in_part = in_base + (1 if i < in_rem else 0)
        out_part = out_base + (1 if i < out_rem else 0)
        if phase == "call":
            token_counter.add_tool_tokens(name, calls=1, call_input=in_part, call_output=out_part)
        else:
            token_counter.add_tool_tokens(name, post_input=in_part, post_output=out_part)
