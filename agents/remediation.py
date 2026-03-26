from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from agents import AgentDeps
from adapters.base import BenchmarkResult


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
        result_type=RemediationOutput,
        system_prompt=(
            "You are a remediation agent for RHEL performance issues. "
            "Apply ONE config change at a time. "
            "Always run a benchmark before the change (to capture before_rps) "
            "and after the change (to capture after_rps). "
            "Reload the service after applying the change. "
            "Record the impact and reasoning clearly."
        ),
    )

    @agent.tool
    async def run_benchmark(ctx: RunContext[AgentDeps],
                            duration: int = 30) -> dict:
        """Run the benchmark and return req/sec and latency metrics."""
        bench_cfg = ctx.deps.config["service"]["benchmark"]
        url = bench_cfg.get("small_file_url", "http://localhost/")
        result: BenchmarkResult = ctx.deps.adapter.benchmark(duration, url)
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
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output",
            f"apply_config:{parameter}",
            f"{parameter}={value}", reason,
        )
        ctx.deps.token_counter.tool_calls += 1
        return ctx.deps.adapter.apply_config(parameter, value)

    @agent.tool
    async def reload_service(ctx: RunContext[AgentDeps], reason: str) -> bool:
        """Reload the service to apply the config change."""
        ctx.deps.token_counter.tool_calls += 1
        return ctx.deps.adapter.reload()

    @agent.tool
    async def run_command(ctx: RunContext[AgentDeps],
                          command: str, reason: str) -> str:
        """Run a shell command — for sysctl/kernel-level changes."""
        result = ctx.deps.ssh.execute(command)
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output", command,
            str(result), reason,
        )
        ctx.deps.token_counter.tool_calls += 1
        return str(result)

    return agent


async def run(model, deps: AgentDeps, analysis_summary: str,
              recommended_action: str) -> RemediationOutput:
    agent = build(model)
    result = await agent.run(
        f"Analysis: {analysis_summary}\n\n"
        f"Recommended action: {recommended_action}\n\n"
        f"Apply this fix. Benchmark before and after. Return RemediationOutput.",
        deps=deps,
    )
    deps.token_counter.add(result.usage())

    output = result.data
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
