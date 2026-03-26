from __future__ import annotations

import json
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
You are SlayMetricsAgent — an autonomous performance diagnostics and remediation agent.

Steps:
1. Inspect current nginx config and system tuning
2. Apply all missing nginx fixes in ONE batch using apply_nginx_tuning
3. Apply all missing system fixes in ONE batch using apply_system_tuning
4. Benchmark AFTER fixes (baseline numbers are already provided — do NOT benchmark before)
5. Save findings and return DiagnosisOutput

Proven fixes (apply ALL that are not already configured):

NGINX (apply via apply_nginx_tuning):
- worker_connections: 8192
- open_file_cache: max=10000 inactive=60s
- open_file_cache_valid: 30s
- open_file_cache_min_uses: 2
- worker_rlimit_nofile: 65536
- access_log: off
- tcp_nodelay: on
- keepalive_requests: 1000
- gzip: on
- gzip_comp_level: 1
- listen_backlog: 65535

SYSTEM (apply via apply_system_tuning):
- net.core.somaxconn: 65535
- net.ipv4.tcp_max_syn_backlog: 65535
- net.core.netdev_max_backlog: 65535
- transparent_hugepage: never
- selinux: permissive
- net.ipv4.tcp_tw_reuse: 1
- net.core.rmem_max: 16777216
- net.core.wmem_max: 16777216

Rules:
- Do NOT repeat previously applied fixes
- Do NOT install packages or reboot
- If inspect shows a fix is already applied, skip it
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
                                 changes: dict[str, str] | str, reason: str) -> dict:
        """Apply multiple nginx config changes in one batch, then reload.
        Example: {"sendfile": "on", "tcp_nopush": "on", "open_file_cache": "max=10000 inactive=60s"}
        """
        normalized_changes, parse_error = _normalize_changes(changes, "apply_nginx")
        if parse_error:
            tool_call("apply_nginx", "invalid input payload")
            tool_result("apply_nginx", f"FAILED: {parse_error}")
            ctx.deps.token_counter.tool_calls += 1
            return {"applied": [], "failed": [], "reload": "FAILED", "error": parse_error}

        changes = normalized_changes
        allowed = getattr(ctx.deps.adapter, "ALLOWED_BATCH_DIRECTIVES", None)
        if allowed is not None:
            unsupported = [k for k in changes if k not in allowed]
            if unsupported:
                error = f"unsupported nginx directives: {', '.join(unsupported)}"
                tool_call("apply_nginx", "unsupported directives")
                tool_result("apply_nginx", f"FAILED: {error}")
                ctx.deps.token_counter.tool_calls += 1
                return {"applied": [], "failed": unsupported, "reload": "FAILED", "error": error}

        tool_call("apply_nginx", f"{len(changes)} changes: {', '.join(changes.keys())}")
        log("agent", f"Reason: {reason[:150]}", "info")

        config_path = ctx.deps.config["service"]["config_path"]
        batch_backup = f"/tmp/slay_nginx_batch_{ctx.deps.session_id}.conf"
        ctx.deps.ssh.execute(f"cp {config_path} {batch_backup}")

        applied = []
        failed = []
        for param, value in changes.items():
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
                    ctx.deps.session_id, "command_output",
                    f"apply_nginx:{','.join(changes.keys())}"[:250],
                    str(result), reason,
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
                                  changes: dict[str, str] | str, reason: str) -> dict:
        """Apply multiple sysctl/kernel changes in one batch.
        Example: {"net.core.somaxconn": "65535", "transparent_hugepage": "never", "selinux": "permissive"}
        """
        normalized_changes, parse_error = _normalize_changes(changes, "apply_system")
        if parse_error:
            tool_call("apply_system", "invalid input payload")
            tool_result("apply_system", f"FAILED: {parse_error}")
            ctx.deps.token_counter.tool_calls += 1
            return {"applied": {}, "failed": {"_input": parse_error}}

        changes = normalized_changes
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
