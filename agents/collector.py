from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from agents import AgentDeps


class CollectionOutput(BaseModel):
    checks_run: list[str]
    findings: list[str]
    raw_summary: str


def build(model) -> Agent:
    agent: Agent[AgentDeps, CollectionOutput] = Agent(
        model,
        deps_type=AgentDeps,
        result_type=CollectionOutput,
        system_prompt=(
            "You are a data collection agent for RHEL system diagnostics. "
            "Run the requested commands via SSH, record results to memory, "
            "and return a structured summary of what you observed. "
            "Always pass a reason explaining why you ran each command."
        ),
    )

    @agent.tool
    async def run_command(ctx: RunContext[AgentDeps],
                          command: str, reason: str) -> str:
        """Run a shell command on the target RHEL system via SSH."""
        result = ctx.deps.ssh.execute(command)
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output", command,
            str(result), reason,
        )
        ctx.deps.token_counter.tool_calls += 1
        return str(result)

    @agent.tool
    async def get_service_logs(ctx: RunContext[AgentDeps], tail: int = 50) -> str:
        """Fetch the most recent service log lines."""
        logs = ctx.deps.adapter.get_logs(tail)
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "log", "service_logs", logs,
            f"Last {tail} lines of service log",
        )
        ctx.deps.token_counter.tool_calls += 1
        return logs

    @agent.tool
    async def get_service_metrics(ctx: RunContext[AgentDeps]) -> dict:
        """Collect live service metrics (connections, throughput, etc.)."""
        metrics = ctx.deps.adapter.get_metrics()
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "metric", "live_metrics",
            str(metrics), "live service metrics snapshot",
        )
        ctx.deps.token_counter.tool_calls += 1
        return metrics

    @agent.tool
    async def get_service_config(ctx: RunContext[AgentDeps]) -> dict:
        """Read the current service configuration file."""
        config = ctx.deps.adapter.get_config()
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output", "service_config",
            config.get("raw", ""), "current service config",
        )
        ctx.deps.token_counter.tool_calls += 1
        return {"path": config.get("path"), "preview": config.get("raw", "")[:1000]}

    return agent


async def run(model, deps: AgentDeps, task: str) -> CollectionOutput:
    agent = build(model)
    result = await agent.run(task, deps=deps)
    deps.token_counter.add(result.usage())
    return result.data
