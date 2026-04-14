"""Fix generation graph node — converts investigation findings into fix list."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .constants import REPORTS_DIR

if TYPE_CHECKING:
    from .rca_agent import RCAAgent, RCAState

logger = logging.getLogger("slayMetrics.fix_generation")


def generate_fixes(state: RCAState, agent: RCAAgent) -> RCAState:
    """Generate fix groups from investigation findings in one LLM call."""
    if state.get("error"):
        return state

    save_dir = REPORTS_DIR / state.get("session_id", "unknown")
    try:
        groups, rca_summary, in_tok, out_tok, elapsed = agent.fix_generator.generate(
            investigation_notes=state.get("investigation_notes", ""),
            benchmark_results=state.get("benchmark_results", ""),
            live_audit_output=state.get("live_audit_output", ""),
            performance_rules=agent.perf_rules,
            save_dir=save_dir,
        )
        total_fixes = sum(len(g.get("fixes", [])) for g in groups)
        calls = list(state.get("llm_calls", []))
        calls.append(("fix_generator", round(elapsed, 1), in_tok, out_tok, total_fixes))
        agent.tracker.log_llm_call("fix_generator", elapsed, in_tok, out_tok, total_fixes)

        # Flatten groups into fixes list with group metadata for merge_fixes
        all_fixes = []
        for g in groups:
            group_id = g.get("group", 0)
            label = g.get("label", "")
            rationale = g.get("rationale", "")
            for fix in g.get("fixes", []):
                fix["_group"] = group_id
                fix["_group_label"] = label
                fix["_group_rationale"] = rationale
                if "tier" not in fix:
                    fix["tier"] = group_id
                all_fixes.append(fix)

        return {
            **state,
            "fix_groups": groups,
            "fixes": all_fixes,
            "rca_report": rca_summary,
            "llm_calls": calls,
            "total_input_tokens": state.get("total_input_tokens", 0) + in_tok,
            "total_output_tokens": state.get("total_output_tokens", 0) + out_tok,
        }
    except Exception as e:
        logger.error("generate_fixes failed: %s", e)
        return {**state, "error": str(e)}
