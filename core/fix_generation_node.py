"""Fix generation graph node — converts investigation findings into fix list."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .constants import REPORTS_DIR

if TYPE_CHECKING:
    from .rca_agent import RCAAgent, RCAState

logger = logging.getLogger("slayMetrics.fix_generation")


def generate_fixes(state: RCAState, agent: RCAAgent) -> RCAState:
    """Generate fix list from investigation findings in one LLM call."""
    if state.get("error"):
        return state

    save_dir = REPORTS_DIR / state.get("session_id", "unknown")
    try:
        fixes, rca_summary, in_tok, out_tok, elapsed = agent.fix_generator.generate(
            investigation_notes=state.get("investigation_notes", ""),
            benchmark_results=state.get("benchmark_results", ""),
            live_audit_output=state.get("live_audit_output", ""),
            performance_rules=agent.perf_rules,
            save_dir=save_dir,
        )
        calls = list(state.get("llm_calls", []))
        calls.append(("fix_generator", round(elapsed, 1), in_tok, out_tok, len(fixes)))
        agent.tracker.log_llm_call("fix_generator", elapsed, in_tok, out_tok, len(fixes))

        return {
            **state,
            "fixes": fixes,
            "rca_report": rca_summary,
            "llm_calls": calls,
            "total_input_tokens": state.get("total_input_tokens", 0) + in_tok,
            "total_output_tokens": state.get("total_output_tokens", 0) + out_tok,
        }
    except Exception as e:
        logger.error("generate_fixes failed: %s", e)
        return {**state, "error": str(e)}
