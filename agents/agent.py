from __future__ import annotations

import re

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from agents import AgentDeps
from adapters.base import BenchmarkResult
from core.log import log, tokens, tool_call, tool_result, llm_call


class DiagnosisOutput(BaseModel):
    """Final output after the agent has diagnosed and fixed issues."""
    fixes_applied: list[dict]
    summary: str
    total_improvement_pct: float


SYSTEM_PROMPT = """\
You are SlayMetricsAgent — an autonomous performance diagnostics and remediation agent for RHEL systems.

You have been given:
1. RHEL system check results
2. Baseline benchmark numbers for small, medium, and large payloads
3. The current nginx configuration
4. Relevant knowledge from Red Hat performance tuning documentation
5. Previously applied fixes (if any)

Your job:
1. Use inspect_nginx_config and inspect_system_tuning to see the current state
2. Identify the TOP 3 most impactful performance fixes
3. Run a benchmark BEFORE applying fixes
4. Apply nginx fixes in one batch with apply_nginx_tuning
5. Apply system/kernel fixes in one batch with apply_system_tuning
6. Run a benchmark AFTER to measure impact
7. Save findings and return DiagnosisOutput

Rules:
- Do NOT repeat previously applied fixes
- Do NOT install packages
- Do NOT reboot
- Use the batch tools — do not run individual commands for config changes
- Keep it to 3 fixes maximum
- If inspect shows something is already configured correctly, skip it
- query_memory is capped at 3 calls; use it only for high-value lookups
"""


def build(model) -> Agent:
    agent: Agent[AgentDeps, DiagnosisOutput] = Agent(
        model,
        deps_type=AgentDeps,
        output_type=DiagnosisOutput,
        system_prompt=SYSTEM_PROMPT,
    )

    @agent.tool
    async def inspect_nginx_config(ctx: RunContext[AgentDeps]) -> dict:
        """Inspect all performance-relevant nginx configuration directives in one call."""
        tool_call("inspect", "nginx config — all tunable directives")
        ssh = ctx.deps.ssh

        # Get full config via nginx -T
        raw = ssh.execute("nginx -T 2>/dev/null").stdout

        # Parse key directives
        directives = {}
        patterns = [
            "worker_processes", "worker_connections", "sendfile",
            "tcp_nopush", "tcp_nodelay", "keepalive_timeout",
            "keepalive_requests", "open_file_cache", "access_log",
            "worker_rlimit_nofile", "gzip", "gzip_comp_level",
            "gzip_types", "reset_timedout_connection", "lingering_close",
        ]
        for d in patterns:
            m = re.search(rf"^\s*{d}\s+(.+?);", raw, re.MULTILINE)
            directives[d] = m.group(1).strip() if m else "not set"

        # Listen backlog
        m = re.search(r"listen\s+.*backlog=(\d+)", raw)
        directives["listen_backlog"] = m.group(1) if m else "not set (default 511)"

        # Worker count vs CPU
        nproc = ssh.execute("nproc").stdout.strip().splitlines()[0]
        workers = ssh.execute("pgrep -c 'nginx: worker' 2>/dev/null || echo 0").stdout.strip().splitlines()[0]
        try:
            directives["cpu_cores"] = int(nproc)
            directives["active_workers"] = int(workers)
        except ValueError:
            directives["cpu_cores"] = 0
            directives["active_workers"] = 0

        ctx.deps.token_counter.tool_calls += 1
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output", "inspect_nginx",
            str(directives), "nginx config inspection",
        )
        tool_result("inspect", f"nginx: {len(directives)} directives inspected")
        return directives

    @agent.tool
    async def inspect_system_tuning(ctx: RunContext[AgentDeps]) -> dict:
        """Inspect all performance-relevant OS/kernel parameters in one call."""
        tool_call("inspect", "system tuning — sysctl, THP, SELinux, limits")
        ssh = ctx.deps.ssh

        state = {}

        # Sysctl params
        sysctl_keys = [
            "net.core.somaxconn", "net.ipv4.tcp_max_syn_backlog",
            "net.core.netdev_max_backlog", "net.ipv4.tcp_tw_reuse",
            "net.ipv4.tcp_autocorking", "net.core.rmem_max",
            "net.core.wmem_max", "net.ipv4.ip_local_port_range",
            "net.ipv4.tcp_fin_timeout",
        ]
        r = ssh.execute(f"sysctl {' '.join(sysctl_keys)} 2>/dev/null")
        for line in r.stdout.strip().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                state[k.strip()] = v.strip()

        # THP
        r = ssh.execute("cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null")
        m = re.search(r"\[(\w+)\]", r.stdout)
        state["transparent_hugepages"] = m.group(1) if m else "unknown"

        # SELinux
        state["selinux"] = ssh.execute("getenforce 2>/dev/null || echo Disabled").stdout.strip()

        # CPU governor
        r = ssh.execute("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null")
        state["cpu_governor"] = r.stdout.strip() or "not available (VM)"

        # File limits
        state["ulimit_nofile"] = ssh.execute("ulimit -n").stdout.strip()
        state["fs_file_max"] = ssh.execute("cat /proc/sys/fs/file-max").stdout.strip()

        # Tuned profile
        state["tuned_profile"] = ssh.execute(
            "tuned-adm active 2>/dev/null | awk -F': ' '{print $2}' || echo 'not installed'"
        ).stdout.strip()

        ctx.deps.token_counter.tool_calls += 1
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output", "inspect_system",
            str(state), "system tuning inspection",
        )
        tool_result("inspect", f"system: {len(state)} parameters inspected")
        return state

    @agent.tool
    async def apply_nginx_tuning(ctx: RunContext[AgentDeps],
                                 changes: dict[str, str], reason: str) -> dict:
        """Apply multiple nginx config changes in one batch, then reload.
        Example: {"sendfile": "on", "tcp_nopush": "on", "open_file_cache": "max=10000 inactive=60s"}
        """
        tool_call("apply_nginx", f"{len(changes)} changes: {', '.join(changes.keys())}")
        log("agent", f"Reason: {reason[:150]}", "info")

        applied = []
        failed = []
        for param, value in changes.items():
            success = ctx.deps.adapter.apply_config(param, value)
            if success:
                applied.append(param)
            else:
                failed.append(param)

        # Test config before reload
        test = ctx.deps.ssh.execute("nginx -t 2>&1")
        if "syntax is ok" not in test.stdout and "test is successful" not in test.stdout:
            # Config is broken — revert to backup and report error
            error_msg = test.stdout.strip()[:200]
            result = {
                "applied": applied,
                "failed": failed,
                "reload": "FAILED",
                "error": f"nginx -t failed: {error_msg}",
            }
            ctx.deps.token_counter.tool_calls += 1
            ctx.deps.memory.save_context(
                ctx.deps.session_id, "command_output",
                f"apply_nginx:{','.join(changes.keys())}"[:250],
                str(result), reason,
            )
            tool_result("apply_nginx", f"FAILED: {error_msg[:100]}")
            return result

        # Config valid — reload
        reload_ok = ctx.deps.adapter.reload()

        result = {
            "applied": applied,
            "failed": failed,
            "reload": "OK" if reload_ok else "FAILED",
        }

        ctx.deps.token_counter.tool_calls += 1
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output",
            f"apply_nginx:{','.join(changes.keys())}"[:250],
            str(result), reason,
        )
        tool_result("apply_nginx", f"applied={applied} failed={failed} reload={'OK' if reload_ok else 'FAILED'}")
        return result

    @agent.tool
    async def apply_system_tuning(ctx: RunContext[AgentDeps],
                                  changes: dict[str, str], reason: str) -> dict:
        """Apply multiple sysctl/kernel changes in one batch.
        Example: {"net.core.somaxconn": "65535", "transparent_hugepage": "never", "selinux": "permissive"}
        """
        tool_call("apply_system", f"{len(changes)} changes: {', '.join(changes.keys())}")
        log("agent", f"Reason: {reason[:150]}", "info")

        ssh = ctx.deps.ssh
        applied = {}
        failed = {}

        for param, value in changes.items():
            if param == "transparent_hugepage":
                r = ssh.execute(f"echo {value} > /sys/kernel/mm/transparent_hugepage/enabled 2>&1")
                if r.ok:
                    applied[param] = value
                else:
                    failed[param] = r.stderr.strip()[:100]
            elif param == "selinux":
                mode = "0" if value.lower() in ("permissive", "0") else "1"
                r = ssh.execute(f"setenforce {mode} 2>&1")
                applied[param] = value
            else:
                r = ssh.execute(f"sysctl -w {param}={value} 2>&1")
                if r.ok:
                    applied[param] = value
                else:
                    failed[param] = r.stderr.strip()[:100]

        result = {"applied": applied, "failed": failed}

        ctx.deps.token_counter.tool_calls += 1
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output",
            f"apply_system:{','.join(changes.keys())}"[:250],
            str(result), reason,
        )
        tool_result("apply_system", f"applied={list(applied.keys())} failed={list(failed.keys())}")
        return result

    @agent.tool
    async def run_benchmark(ctx: RunContext[AgentDeps],
                            duration: int = 30) -> dict:
        """Run wrk2 benchmark against the small file URL. Returns RPS and latency."""
        bench_cfg = ctx.deps.config["service"]["benchmark"]
        url = bench_cfg.get("small_file_url", "http://localhost/")
        tool_call("benchmark", f"wrk2 {duration}s -> {url}")
        result: BenchmarkResult = ctx.deps.adapter.benchmark(duration, url)
        tool_result("benchmark", f"{result.requests_per_sec:.1f} RPS, "
                    f"p99={result.latency_p99_ms:.1f}ms, CPU={result.cpu_pct:.1f}%")
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "benchmark", url,
            str(result.__dict__),
            f"RPS={result.requests_per_sec:.1f} p99={result.latency_p99_ms:.1f}ms",
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
        tool_call("memory", f"query {_memory_query_count}/{MAX_MEMORY_QUERIES}: {symptom[:80]}")
        results = ctx.deps.memory.semantic_search(symptom, ctx.deps.session_id, top_k=5)
        tool_result("memory", f"{len(results)} results found")
        ctx.deps.token_counter.tool_calls += 1
        return results

    @agent.tool
    async def save_finding(ctx: RunContext[AgentDeps],
                           parameter: str, reasoning: str,
                           before_value: str = "", after_value: str = "",
                           before_rps: float = 0, after_rps: float = 0,
                           impact_pct: float = 0) -> bool:
        """Save a fix result to long-term memory."""
        tool_call("save", f"{parameter} ({impact_pct:+.1f}%)")
        ctx.deps.memory.save_fact(
            session_id=ctx.deps.session_id,
            type="fix",
            parameter=parameter,
            reasoning=reasoning,
            before_value=before_value,
            after_value=after_value,
            before_rps=before_rps,
            after_rps=after_rps,
            impact_pct=impact_pct,
        )
        ctx.deps.token_counter.tool_calls += 1
        return True

    return agent


async def run(model, deps: AgentDeps, context_prompt: str) -> DiagnosisOutput:
    """Run the single agent with full context."""
    llm_call("agent", "Starting diagnosis — sending context to LLM...")
    agent = build(model)
    result = await agent.run(context_prompt, deps=deps)
    inp, out = deps.token_counter.add(result.usage())
    llm_call("agent", f"LLM finished — {inp:,} in / {out:,} out tokens")
    tokens("agent", inp, out, deps.token_counter.summary())
    return result.output
