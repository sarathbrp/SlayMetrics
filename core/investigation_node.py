"""Investigation graph node: autonomous SRE diagnostic loop on the DUT.

Runs between run_benchmark and analyze_network. Uses the SREInvestigator
to drive a multi-turn SSH investigation, accumulating findings that feed
into all three domain analyzers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .command_validator import CommandValidator
from .constants import REPORTS_DIR

if TYPE_CHECKING:
    from .rca_agent import RCAAgent, RCAState

logger = logging.getLogger("slayMetrics.investigation")


def _save_iteration(save_dir: Path, iteration: int,
                    hypothesis: str, evidence: str, plan: str,
                    findings: str, commands_run: list[dict]) -> None:
    """Save one investigation iteration to session folder for debugging."""
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"investigation_iter_{iteration}.json"
        path.write_text(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "iteration": iteration,
            "hypothesis": hypothesis,
            "evidence": evidence,
            "plan": plan,
            "findings": findings,
            "commands": commands_run,
        }, indent=2, default=str))
    except Exception as e:
        logger.warning("Failed to save investigation iteration %d: %s", iteration, e)


def _run_commands(executor, commands: list[str], findings: list[str],
                  cmd_timeout: int, max_output: int, max_cmds: int) -> list[dict]:
    """Execute validated commands via SSH, append results to findings."""
    iter_log: list[dict] = []
    for cmd in commands[:max_cmds]:
        check = CommandValidator.validate(cmd)
        if check.blocked:
            logger.warning("BLOCKED: %s — %s", cmd, check.reason)
            findings.append(f"$ {cmd}\nBLOCKED: {check.reason}")
            iter_log.append({"cmd": cmd, "blocked": True, "reason": check.reason})
            continue
        logger.info("  Running: %s", cmd)
        try:
            stdout, stderr = executor.run(cmd, timeout=cmd_timeout)
        except Exception as e:
            logger.warning("Command failed: %s — %s", cmd, e)
            findings.append(f"$ {cmd}\nERROR: {e}")
            iter_log.append({"cmd": cmd, "blocked": False, "error": str(e)})
            continue
        output = stdout[:max_output]
        if len(stdout) > max_output:
            output += f"\n[TRUNCATED — {len(stdout)} bytes total]"
        findings.append(f"$ {cmd}\n{output}")
        iter_log.append({"cmd": cmd, "blocked": False, "output": output[:1024],
                         "output_len": len(stdout)})
    return iter_log


def investigate(state: RCAState, agent: RCAAgent) -> RCAState:
    """Plan-driven SRE investigation: plan hypotheses upfront, then execute."""
    if state.get("error"):
        return state
    if not agent.config.investigation_enabled:
        logger.info("Investigation phase disabled — skipping.")
        return {**state, "investigation_notes": ""}

    cmd_timeout = agent.config.investigation_command_timeout
    max_output = agent.config.investigation_max_output_bytes
    max_cmds = agent.config.investigation_max_commands_per_iteration
    save_dir = REPORTS_DIR / state.get("session_id", "unknown")
    findings: list[str] = []
    confirmed: list[str] = []
    conclusion = ""
    total_in = total_out = 0
    total_elapsed = 0.0

    try:
        # === PHASE 1: PLANNING — LLM produces hypothesis table (no SSH) ===
        logger.info("=== Investigation: Planning Phase ===")
        inv_plan, in_tok, out_tok, elapsed = agent.investigator.plan(
            audit_baseline=state["audit_output"],
            benchmark_results=state.get("benchmark_results", ""),
            live_audit_output=state.get("live_audit_output", ""),
            performance_rules=agent.perf_rules,
        )
        total_in += in_tok
        total_out += out_tok
        total_elapsed += elapsed

        # Log the plan
        if inv_plan.system_blueprint:
            bp = inv_plan.system_blueprint
            logger.info("  System: %s cores, %sGB RAM, %s NIC, %s disk",
                        bp.get("cpu_cores", "?"), bp.get("memory_gb", "?"),
                        bp.get("nic_speed", "?"), bp.get("disk_type", "?"))
            logger.info("  Capacity: %s actual vs %s theoretical = %s utilization",
                        bp.get("actual_rps_small", "?"),
                        bp.get("theoretical_max_rps_small", "?"),
                        bp.get("capacity_utilization", "?"))

        if inv_plan.hypotheses:
            logger.info("  Investigation plan (%d hypotheses):", len(inv_plan.hypotheses))
            for h in inv_plan.hypotheses:
                logger.info("    [P%d] %s (est. %s)", h.priority, h.hypothesis, h.estimated_impact)
        else:
            logger.warning("  No hypotheses produced — falling back to adaptive investigation.")

        # Save the plan
        _save_iteration(save_dir, 0,
                        "Investigation Plan", "",
                        json.dumps([{"priority": h.priority, "hypothesis": h.hypothesis,
                                     "impact": h.estimated_impact,
                                     "commands": h.commands_to_verify}
                                    for h in inv_plan.hypotheses], default=str),
                        json.dumps(inv_plan.system_blueprint, default=str), [])

        # === PHASE 2: EXECUTION — one hypothesis per iteration ===
        with agent._executor() as executor:
            hypotheses = inv_plan.hypotheses or []
            max_iter = min(len(hypotheses), agent.config.investigation_max_iterations)
            if max_iter == 0:
                max_iter = agent.config.investigation_max_iterations

            for iteration in range(1, max_iter + 1):
                # Build context for this iteration
                confirmed_text = ""
                if confirmed:
                    confirmed_text = (
                        "=== CONFIRMED SO FAR (do NOT re-check) ===\n"
                        + "\n".join(confirmed) + "\n=== END ===\n\n"
                    )
                recent = findings[-10:] if len(findings) > 10 else findings
                context = confirmed_text + "\n---\n".join(recent)

                # If we have a planned hypothesis, inject it as guidance
                if iteration <= len(hypotheses):
                    h = hypotheses[iteration - 1]
                    logger.info("=== Investigation iteration %d/%d: [P%d] %s ===",
                                iteration, max_iter, h.priority, h.hypothesis)
                    context = (
                        f"EXECUTE HYPOTHESIS {h.priority}: {h.hypothesis}\n"
                        f"Estimated impact: {h.estimated_impact}\n"
                        f"Evidence: {h.evidence_so_far}\n"
                        f"Suggested commands: {', '.join(h.commands_to_verify)}\n\n"
                        + context
                    )
                else:
                    logger.info("=== Investigation iteration %d/%d (adaptive) ===",
                                iteration, max_iter)

                result, in_tok, out_tok, elapsed = agent.investigator.step(
                    audit_baseline=state["audit_output"],
                    benchmark_results=state.get("benchmark_results", ""),
                    live_audit_output=state.get("live_audit_output", ""),
                    previous_findings=context,
                    performance_rules=agent.perf_rules,
                )
                total_in += in_tok
                total_out += out_tok
                total_elapsed += elapsed

                if result.hypothesis:
                    logger.info("  Hypothesis: %s", result.hypothesis)
                if result.evidence:
                    logger.info("  Evidence: %s", result.evidence[:200])
                if result.plan:
                    logger.info("  Plan: %s", result.plan)

                if result.done:
                    logger.info("Investigation complete at iteration %d (%.1fs total)",
                                iteration, total_elapsed)
                    if result.findings:
                        conclusion = result.findings
                    _save_iteration(save_dir, iteration,
                                    result.hypothesis, result.evidence, result.plan,
                                    result.findings, [])
                    break

                iter_log = _run_commands(executor, result.commands, findings,
                                        cmd_timeout, max_output, max_cmds)
                _save_iteration(save_dir, iteration,
                                result.hypothesis, result.evidence, result.plan,
                                result.findings, iter_log)

                summary_line = f"Iter {iteration}"
                if result.hypothesis:
                    summary_line += f": {result.hypothesis[:100]}"
                if result.findings:
                    summary_line += f" → {result.findings[:100]}"
                confirmed.append(summary_line)

    except Exception as e:
        logger.error("Investigation failed: %s", e)
        conclusion = f"[ERROR] Investigation aborted: {e}"

    # Force final summary if needed
    if not conclusion and findings:
        logger.info("Forcing final summary...")
        try:
            recent = findings[-5:] if len(findings) > 5 else findings
            summary_prompt = (
                "=== CONFIRMED ===\n" + "\n".join(confirmed) + "\n===\n\n"
                + "\n---\n".join(recent)
                + "\n\n=== FINAL: Set done=true, return findings with bottleneck_ranking, "
                "attack_plan, cross_layer_violations, systemd_sabotage, "
                "effective_nginx_values, severity. No commands. ==="
            )
            result, in_tok, out_tok, elapsed = agent.investigator.step(
                audit_baseline=state["audit_output"],
                benchmark_results=state.get("benchmark_results", ""),
                live_audit_output=state.get("live_audit_output", ""),
                previous_findings=summary_prompt,
                performance_rules=agent.perf_rules,
            )
            total_in += in_tok
            total_out += out_tok
            total_elapsed += elapsed
            if result.findings and "parse error" not in result.findings:
                conclusion = result.findings
                logger.info("Forced summary produced (%d bytes)", len(conclusion))
            else:
                logger.warning("Forced summary unusable — using raw findings.")
        except Exception as e:
            logger.warning("Forced summary failed: %s", e)

    notes = conclusion if conclusion else "\n---\n".join(findings)
    logger.info(
        "Investigation produced %d bytes of notes (%d iterations, %.1fs, %d commands run)",
        len(notes), min(max_iter, len(findings)), total_elapsed, len(findings),
    )

    calls = list(state.get("llm_calls", []))
    calls.append(("investigation", round(total_elapsed, 1), total_in, total_out, len(findings)))
    agent.tracker.log_llm_call("investigation", total_elapsed, total_in, total_out, len(findings))

    return {
        **state,
        "investigation_notes": notes,
        "llm_calls": calls,
        "total_input_tokens": state.get("total_input_tokens", 0) + total_in,
        "total_output_tokens": state.get("total_output_tokens", 0) + total_out,
    }
