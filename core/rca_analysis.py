"""LLM analysis graph nodes: network, kernel, nginx."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .analyzer_utils import extract_audit_groups
from .constants import REPORTS_DIR

if TYPE_CHECKING:
    from .rca_agent import RCAAgent, RCAState

logger = logging.getLogger("slayMetrics.analysis")


def analyze_network(state: RCAState, agent: RCAAgent) -> RCAState:
    if state.get("error"):
        return state
    audit_output = state["audit_output"]
    benchmark_results = state.get("benchmark_results", "")
    similar_cases = (
        agent.memory.retrieve(audit_output, benchmark_results)
        if agent.config.memory_inject_into_rca else ""
    )
    network_section = extract_audit_groups(audit_output, [5])
    save_dir = REPORTS_DIR / state.get("session_id", "unknown")
    try:
        fixes, summary, in_tok, out_tok, elapsed = agent.net_analyzer.analyze(
            network_section, state.get("live_audit_output", ""), similar_cases,
            investigation_notes=state.get("investigation_notes", ""),
            performance_rules=agent.perf_rules,
            save_dir=save_dir,
        )
        calls = list(state.get("llm_calls", []))
        calls.append(("network", round(elapsed, 1), in_tok, out_tok, len(fixes)))
        agent.tracker.log_llm_call("network", elapsed, in_tok, out_tok, len(fixes))
        agent._partial_state.update({
            "session_id": state.get("session_id", ""),
            "audit_output": audit_output,
            "benchmark_results": benchmark_results,
            "rca_report": summary, "applied_fixes": [], "rejected_fixes": [],
        })
        return {**state, "similar_cases": similar_cases,
                "network_fixes": fixes, "network_summary": summary,
                "llm_calls": calls,
                "total_input_tokens": state.get("total_input_tokens", 0) + in_tok,
                "total_output_tokens": state.get("total_output_tokens", 0) + out_tok}
    except Exception as e:
        logger.error("analyze_network failed: %s", e)
        return {**state, "similar_cases": similar_cases, "network_fixes": [], "network_summary": ""}


def analyze_kernel(state: RCAState, agent: RCAAgent) -> RCAState:
    if state.get("error"):
        return state
    kernel_section = extract_audit_groups(state["audit_output"], [1, 2, 3])
    sc = state.get("similar_cases", "") if agent.config.memory_inject_into_fix_extraction else ""
    save_dir = REPORTS_DIR / state.get("session_id", "unknown")
    try:
        fixes, summary, in_tok, out_tok, elapsed = agent.kernel_analyzer.analyze(
            kernel_section, state.get("benchmark_results", ""),
            state.get("network_summary", ""), sc,
            investigation_notes=state.get("investigation_notes", ""),
            performance_rules=agent.perf_rules,
            save_dir=save_dir,
        )
        calls = list(state.get("llm_calls", []))
        calls.append(("kernel", round(elapsed, 1), in_tok, out_tok, len(fixes)))
        agent.tracker.log_llm_call("kernel", elapsed, in_tok, out_tok, len(fixes))
        return {**state, "kernel_fixes": fixes, "kernel_summary": summary,
                "llm_calls": calls,
                "total_input_tokens": state.get("total_input_tokens", 0) + in_tok,
                "total_output_tokens": state.get("total_output_tokens", 0) + out_tok}
    except Exception as e:
        logger.error("analyze_kernel failed: %s", e)
        return {**state, "kernel_fixes": [], "kernel_summary": ""}


def analyze_nginx(state: RCAState, agent: RCAAgent) -> RCAState:
    if state.get("error"):
        return state
    nginx_section = extract_audit_groups(state["audit_output"], [4])
    sc = state.get("similar_cases", "") if agent.config.memory_inject_into_fix_extraction else ""
    save_dir = REPORTS_DIR / state.get("session_id", "unknown")
    try:
        fixes, in_tok, out_tok, elapsed = agent.nginx_analyzer.analyze(
            nginx_section, state.get("benchmark_results", ""),
            state.get("network_summary", ""), state.get("kernel_summary", ""), sc,
            investigation_notes=state.get("investigation_notes", ""),
            performance_rules=agent.perf_rules,
            save_dir=save_dir,
        )
        calls = list(state.get("llm_calls", []))
        calls.append(("nginx", round(elapsed, 1), in_tok, out_tok, len(fixes)))
        agent.tracker.log_llm_call("nginx", elapsed, in_tok, out_tok, len(fixes))
        rca_report = "\n\n".join(filter(None, [
            state.get("network_summary", ""),
            state.get("kernel_summary", ""),
            f"Nginx fixes: {len(fixes)} identified.",
        ]))
        return {**state, "nginx_fixes": fixes, "rca_report": rca_report,
                "llm_calls": calls,
                "total_input_tokens": state.get("total_input_tokens", 0) + in_tok,
                "total_output_tokens": state.get("total_output_tokens", 0) + out_tok}
    except Exception as e:
        logger.error("analyze_nginx failed: %s", e)
        return {**state, "nginx_fixes": [], "rca_report": state.get("rca_report", "")}
