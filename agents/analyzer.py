from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from agents import AgentDeps
from core.log import log, tokens


class AnalysisOutput(BaseModel):
    symptom: str
    root_cause: str
    confidence: float       # 0.0 - 1.0
    hypothesis: str
    recommended_action: str
    reasoning: str
    skip: bool = False      # True if memory says this was already tried


def build(model) -> Agent:
    agent: Agent[AgentDeps, AnalysisOutput] = Agent(
        model,
        deps_type=AgentDeps,
        output_type=AnalysisOutput,
        system_prompt=(
            "You are a performance analysis agent for RHEL systems. "
            "Given a hypothesis name and collected system data, determine: "
            "1) what symptom is present, 2) what the root cause likely is, "
            "3) what action should be taken. "
            "Always check memory first — do not re-diagnose something already confirmed. "
            "Set skip=True if this hypothesis was already tried and had no effect. "
            "Confidence should reflect how certain you are (0.0=guess, 1.0=confirmed). "
            "IMPORTANT: Use at most 5 diagnostic commands. Be targeted, not exhaustive. "
            "If a system feature is not available (e.g. cpufreq on a VM), set skip=True immediately. "
            "Never install packages or modify the system — only diagnose."
        ),
    )

    @agent.tool
    async def query_memory(ctx: RunContext[AgentDeps], symptom: str) -> list[dict]:
        """Search long-term memory for similar past symptoms and their fixes."""
        log("analyzer", f"Querying memory: {symptom[:80]}", "info")
        results = ctx.deps.memory.semantic_search(symptom, ctx.deps.session_id, top_k=3)
        log("analyzer", f"Memory returned {len(results)} results", "info")
        ctx.deps.token_counter.tool_calls += 1
        return results

    @agent.tool
    async def get_past_facts(ctx: RunContext[AgentDeps]) -> list[dict]:
        """Return all facts (findings + fixes) recorded so far in this session."""
        facts = ctx.deps.memory.get_facts(ctx.deps.session_id)
        log("analyzer", f"Retrieved {len(facts)} past facts", "info")
        ctx.deps.token_counter.tool_calls += 1
        return [{k: v for k, v in f.items() if k != "embedding"} for f in facts]

    @agent.tool
    async def run_diagnostic_command(ctx: RunContext[AgentDeps],
                                     command: str, reason: str) -> str:
        """Run a targeted diagnostic command to gather evidence for the hypothesis."""
        log("analyzer", f"SSH: {command[:80]} ({reason[:50]})", "action")
        result = ctx.deps.ssh.execute(command)
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "command_output", command,
            str(result), reason,
        )
        ctx.deps.token_counter.tool_calls += 1
        log("analyzer", f"-> {str(result)[:120]}", "info")
        return str(result)

    return agent


async def run(model, deps: AgentDeps, hypothesis: str,
              context_summary: str) -> AnalysisOutput:
    log("analyzer", f"Analyzing hypothesis: {hypothesis}", "action")
    agent = build(model)
    result = await agent.run(
        f"Hypothesis: {hypothesis}\n\nContext: {context_summary}\n\n"
        f"Analyze this hypothesis. Check memory for prior knowledge first. "
        f"Run diagnostic commands as needed (max 5). Return AnalysisOutput.",
        deps=deps,
    )
    inp, out = deps.token_counter.add(result.usage())
    output = result.output
    log("analyzer", f"Verdict: confidence={output.confidence:.1f} skip={output.skip}", "result")
    tokens("analyzer", inp, out, deps.token_counter.summary())
    return output
