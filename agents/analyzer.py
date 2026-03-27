from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from agents import AgentDeps
from core.log import log


@dataclass
class AnalysisOutput:
    symptom: str
    root_cause: str
    confidence: float
    hypothesis: str
    recommended_action: str
    reasoning: str
    skip: bool = False


def build(model):
    del model

    async def query_memory(ctx, symptom: str) -> list[dict]:
        log("analyzer", f"Querying memory: {symptom[:80]}", "info")
        results = ctx.deps.memory.semantic_search(symptom, ctx.deps.session_id, top_k=3)
        log("analyzer", f"Memory returned {len(results)} results", "info")
        ctx.deps.token_counter.tool_calls += 1
        return results

    async def get_past_facts(ctx) -> list[dict]:
        facts = ctx.deps.memory.get_facts(ctx.deps.session_id)
        log("analyzer", f"Retrieved {len(facts)} past facts", "info")
        ctx.deps.token_counter.tool_calls += 1
        return [{key: value for key, value in fact.items() if key != "embedding"} for fact in facts]

    async def run_diagnostic_command(ctx, command: str, reason: str) -> str:
        log("analyzer", f"SSH: {command[:80]} ({reason[:50]})", "action")
        result = ctx.deps.ssh.execute(command)
        ctx.deps.memory.save_context(
            ctx.deps.session_id,
            "command_output",
            command,
            str(result),
            reason,
        )
        ctx.deps.token_counter.tool_calls += 1
        log("analyzer", f"-> {str(result)[:120]}", "info")
        return str(result)

    return SimpleNamespace(
        _function_toolset=SimpleNamespace(
            tools={
                "query_memory": SimpleNamespace(function=query_memory),
                "get_past_facts": SimpleNamespace(function=get_past_facts),
                "run_diagnostic_command": SimpleNamespace(function=run_diagnostic_command),
            }
        )
    )


async def run(model, deps: AgentDeps, hypothesis: str, context_summary: str) -> AnalysisOutput:
    del model, deps, context_summary
    log("analyzer", f"Analyzing hypothesis: {hypothesis}", "action")
    return AnalysisOutput(
        symptom=hypothesis,
        root_cause="LangGraph runtime does not use analyzer.py",
        confidence=0.0,
        hypothesis=hypothesis,
        recommended_action="N/A",
        reasoning="Legacy analyzer path is not active in the LangGraph runtime.",
        skip=True,
    )
