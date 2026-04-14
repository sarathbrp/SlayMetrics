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
                    reasoning: str, commands_run: list[dict]) -> None:
    """Save one investigation iteration to session folder for debugging."""
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"investigation_iter_{iteration}.json"
        path.write_text(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "iteration": iteration,
            "reasoning": reasoning,
            "commands": commands_run,
        }, indent=2, default=str))
    except Exception as e:
        logger.warning("Failed to save investigation iteration %d: %s", iteration, e)


def investigate(state: RCAState, agent: RCAAgent) -> RCAState:
    """Autonomous SRE investigation node — multi-turn SSH diagnostic loop."""
    if state.get("error"):
        return state
    if not agent.config.investigation_enabled:
        logger.info("Investigation phase disabled — skipping.")
        return {**state, "investigation_notes": ""}

    max_iter = agent.config.investigation_max_iterations
    cmd_timeout = agent.config.investigation_command_timeout
    max_output = agent.config.investigation_max_output_bytes
    max_cmds = agent.config.investigation_max_commands_per_iteration
    save_dir = REPORTS_DIR / state.get("session_id", "unknown")
    findings: list[str] = []
    conclusion = ""
    total_in = total_out = 0
    total_elapsed = 0.0

    try:
        with agent._executor() as executor:
            for iteration in range(1, max_iter + 1):
                logger.info("=== Investigation iteration %d/%d ===", iteration, max_iter)

                result, in_tok, out_tok, elapsed = agent.investigator.step(
                    audit_baseline=state["audit_output"],
                    benchmark_results=state.get("benchmark_results", ""),
                    live_audit_output=state.get("live_audit_output", ""),
                    previous_findings="\n---\n".join(findings),
                )
                total_in += in_tok
                total_out += out_tok
                total_elapsed += elapsed

                if result.done:
                    logger.info(
                        "Investigation complete at iteration %d (%.1fs total)",
                        iteration, total_elapsed,
                    )
                    # Use the structured conclusion as the final notes
                    # (replaces raw command dumps with clean summary)
                    if result.findings:
                        conclusion = result.findings
                    _save_iteration(save_dir, iteration, result.reasoning, [])
                    break

                iter_log: list[dict] = []
                for cmd in result.commands[:max_cmds]:
                    check = CommandValidator.validate(cmd)
                    if check.blocked:
                        logger.warning("BLOCKED: %s — %s", cmd, check.reason)
                        findings.append(f"$ {cmd}\nBLOCKED: {check.reason}")
                        iter_log.append({
                            "cmd": cmd, "blocked": True, "reason": check.reason,
                        })
                        continue

                    logger.info("  Running: %s", cmd)
                    try:
                        stdout, stderr = executor.run(cmd, timeout=cmd_timeout)
                    except Exception as e:
                        logger.warning("Command failed: %s — %s", cmd, e)
                        findings.append(f"$ {cmd}\nERROR: {e}")
                        iter_log.append({
                            "cmd": cmd, "blocked": False, "error": str(e),
                        })
                        continue

                    output = stdout[:max_output]
                    if len(stdout) > max_output:
                        output += f"\n[TRUNCATED — {len(stdout)} bytes total]"
                    findings.append(f"$ {cmd}\n{output}")
                    iter_log.append({
                        "cmd": cmd, "blocked": False,
                        "output_len": len(stdout),
                    })

                _save_iteration(save_dir, iteration, result.reasoning, iter_log)

    except Exception as e:
        logger.error("Investigation failed: %s", e)
        conclusion = f"[ERROR] Investigation aborted: {e}"

    # If the LLM never signaled done, force a final summary call
    if not conclusion and findings:
        logger.info("Investigation hit max iterations without structured conclusion — forcing summary.")
        try:
            # Send only the last 5 findings to avoid token overflow
            recent = findings[-5:] if len(findings) > 5 else findings
            summary_prompt = (
                "\n---\n".join(recent) +
                "\n\n=== FINAL ITERATION: You MUST now set done=true and return findings as a JSON object "
                "with keys: bottleneck_ranking (list of {issue, impact, severity}), "
                "attack_plan (list of {phase, label, fixes}), "
                "cross_layer_violations (list of strings), "
                "systemd_sabotage (list of strings), "
                "effective_nginx_values (dict), severity (string). "
                "Do NOT request more commands. ==="
            )
            result, in_tok, out_tok, elapsed = agent.investigator.step(
                audit_baseline=state["audit_output"],
                benchmark_results=state.get("benchmark_results", ""),
                live_audit_output=state.get("live_audit_output", ""),
                previous_findings=summary_prompt,
            )
            total_in += in_tok
            total_out += out_tok
            total_elapsed += elapsed
            # Only use if it's a real summary, not a parse-error fallback
            if result.findings and "parse error" not in result.findings:
                conclusion = result.findings
                logger.info("Forced summary produced (%d bytes)", len(conclusion))
            else:
                logger.warning("Forced summary was unusable — falling back to raw findings.")
        except Exception as e:
            logger.warning("Forced summary call failed: %s", e)

    # Use structured conclusion if available, otherwise raw findings
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
