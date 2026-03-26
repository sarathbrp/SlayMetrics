from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from agents import AgentDeps
from adapters.base import BenchmarkResult
from core.log import log, tokens


class RemediationOutput(BaseModel):
    parameter: str
    old_value: str
    new_value: str
    reasoning: str
    success: bool
    before_rps: float
    after_rps: float
    impact_pct: float


def build(model) -> Agent:
    agent: Agent[AgentDeps, RemediationOutput] = Agent(
        model,
        deps_type=AgentDeps,
        output_type=RemediationOutput,
        system_prompt=(
            "You are a remediation agent for RHEL performance issues. "
            "Apply ONE config change at a time. "
            "Always run a benchmark before the change (to capture before_rps) "
            "and after the change (to capture after_rps). "
            "Reload the service after applying the change. "
            "Record the impact and reasoning clearly. "
            "IMPORTANT: Never install packages. Never run yum/dnf. "
            "Only modify config files, sysctl values, or kernel parameters. "
            "Use at most 3 commands to apply the fix, plus 2 benchmarks (before + after)."
        ),
    )

    @agent.tool
    async def run_benchmark(ctx: RunContext[AgentDeps],
                            duration: int = 30) -> dict:
        """Run the benchmark and return req/sec and latency metrics."""
        bench_cfg = ctx.deps.config["service"]["benchmark"]
        url = bench_cfg.get("small_file_url", "http://localhost/")
        log("remediation", f"Benchmarking {url} for {duration}s...", "action")
        result: BenchmarkResult = ctx.deps.adapter.benchmark(duration, url)
        log("remediation", f"-> {result.requests_per_sec:.1f} RPS, "
            f"p99={result.latency_p99_ms:.1f}ms", "result")
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "benchmark", url,
            str(result.__dict__),
            f"RPS={result.requests_per_sec:.1f} p99={result.latency_p99_ms:.1f}ms",
        )
        ctx.deps.token_counter.tool_calls += 1
        return result.__dict__

    @agent.tool
    async def apply_config_change(ctx: RunContext[AgentDeps],
                                  parameter: str, value: str,
                                  reason: str) -> bool:
        """Apply a single config parameter change. Returns True on success."""
        log("remediation", f"Applying: {parameter} = {value}", "action")
        log("remediation", f"Reason: {reason[:100]}", "info")
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output",
            f"apply_config:{parameter}",
            f"{parameter}={value}", reason,
        )
        ctx.deps.token_counter.tool_calls += 1
        success = ctx.deps.adapter.apply_config(parameter, value)
        log("remediation", f"-> {'OK' if success else 'FAILED'}", "result")
        return success

    @agent.tool
    async def reload_service(ctx: RunContext[AgentDeps], reason: str) -> bool:
        """Reload the service to apply the config change."""
        log("remediation", f"Reloading service: {reason[:80]}", "action")
        ctx.deps.token_counter.tool_calls += 1
        success = ctx.deps.adapter.reload()
        log("remediation", f"-> {'OK' if success else 'FAILED'}", "result")
        return success

    @agent.tool
    async def run_command(ctx: RunContext[AgentDeps],
                          command: str, reason: str) -> str:
        """Run a shell command — for sysctl/kernel-level changes."""
        log("remediation", f"SSH: {command[:80]}", "action")
        result = ctx.deps.ssh.execute(command)
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output", command,
            str(result), reason,
        )
        ctx.deps.token_counter.tool_calls += 1
        log("remediation", f"-> {str(result)[:120]}", "info")
        return str(result)

    return agent


async def run(model, deps: AgentDeps, analysis_summary: str,
              recommended_action: str) -> RemediationOutput:
    log("remediation", f"Starting: {recommended_action[:100]}", "action")
    agent = build(model)
    result = await agent.run(
        f"Analysis: {analysis_summary}\n\n"
        f"Recommended action: {recommended_action}\n\n"
        f"Apply this fix. Benchmark before and after. Return RemediationOutput.",
        deps=deps,
    )
    inp, out = deps.token_counter.add(result.usage())

    output = result.output
    log("remediation", f"Done: {output.parameter} = {output.new_value} "
        f"({output.impact_pct:+.1f}%)", "result")
    tokens("remediation", inp, out, deps.token_counter.summary())

    # Persist the fix to Facts table
    if output.success:
        deps.memory.save_fact(
            session_id=deps.session_id,
            type="fix",
            parameter=output.parameter,
            reasoning=output.reasoning,
            before_value=output.old_value,
            after_value=output.new_value,
            before_rps=output.before_rps,
            after_rps=output.after_rps,
            impact_pct=output.impact_pct,
        )
    return output
