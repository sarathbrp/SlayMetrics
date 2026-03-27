from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from adapters.base import BenchmarkResult
from agents import AgentDeps
from core.log import log


@dataclass
class RemediationOutput:
    parameter: str
    old_value: str
    new_value: str
    reasoning: str
    success: bool
    before_rps: float
    after_rps: float
    impact_pct: float


def build(model):
    del model

    async def run_benchmark(ctx, duration: int = 30) -> dict:
        bench_cfg = ctx.deps.config["service"]["benchmark"]
        url = bench_cfg.get("small_file_url", "http://localhost/")
        log("remediation", f"Benchmarking {url} for {duration}s...", "action")
        result: BenchmarkResult = ctx.deps.adapter.benchmark(duration, url)
        log(
            "remediation",
            f"-> {result.requests_per_sec:.1f} RPS, p99={result.latency_p99_ms:.1f}ms",
            "result",
        )
        ctx.deps.memory.save_context(
            ctx.deps.session_id,
            "benchmark",
            url,
            str(result.__dict__),
            f"RPS={result.requests_per_sec:.1f} p99={result.latency_p99_ms:.1f}ms",
        )
        ctx.deps.token_counter.tool_calls += 1
        return result.__dict__

    async def apply_config_change(ctx, parameter: str, value: str, reason: str) -> bool:
        log("remediation", f"Applying: {parameter} = {value}", "action")
        log("remediation", f"Reason: {reason[:100]}", "info")
        ctx.deps.memory.save_context(
            ctx.deps.session_id,
            "command_output",
            f"apply_config:{parameter}",
            f"{parameter}={value}",
            reason,
        )
        ctx.deps.token_counter.tool_calls += 1
        success = ctx.deps.adapter.apply_config(parameter, value)
        log("remediation", f"-> {'OK' if success else 'FAILED'}", "result")
        return success

    async def reload_service(ctx, reason: str) -> bool:
        log("remediation", f"Reloading service: {reason[:80]}", "action")
        ctx.deps.token_counter.tool_calls += 1
        success = ctx.deps.adapter.reload()
        log("remediation", f"-> {'OK' if success else 'FAILED'}", "result")
        return success

    async def run_command(ctx, command: str, reason: str) -> str:
        log("remediation", f"SSH: {command[:80]}", "action")
        result = ctx.deps.ssh.execute(command)
        ctx.deps.memory.save_context(
            ctx.deps.session_id,
            "command_output",
            command,
            str(result),
            reason,
        )
        ctx.deps.token_counter.tool_calls += 1
        log("remediation", f"-> {str(result)[:120]}", "info")
        return str(result)

    return SimpleNamespace(
        _function_toolset=SimpleNamespace(
            tools={
                "run_benchmark": SimpleNamespace(function=run_benchmark),
                "apply_config_change": SimpleNamespace(function=apply_config_change),
                "reload_service": SimpleNamespace(function=reload_service),
                "run_command": SimpleNamespace(function=run_command),
            }
        )
    )


async def run(model, deps: AgentDeps, analysis_summary: str, recommended_action: str) -> RemediationOutput:
    agent = build(model)
    if hasattr(agent, "run"):
        result = await agent.run(
            f"Analysis: {analysis_summary}\n\nRecommended action: {recommended_action}",
            deps=deps,
        )
        output = result.output
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

    return RemediationOutput(
        parameter="",
        old_value="",
        new_value="",
        reasoning="Legacy remediation path is not active in the LangGraph runtime.",
        success=False,
        before_rps=0.0,
        after_rps=0.0,
        impact_pct=0.0,
    )
