"""
LLM-in-the-loop fix evaluation.

When the mechanical gate (Evaluator.should_keep) rejects a fix that has
positive overall improvement, this module asks the LLM to review the
decision with full context about what the fix does and why.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import dspy

from .config import Config

logger = logging.getLogger("slayMetrics.fixEvaluator")

# MLflow trace decorator — falls back to no-op if mlflow unavailable
try:
    import mlflow
    _trace = mlflow.trace
except (ImportError, AttributeError):
    def _trace(fn=None, **kwargs):  # type: ignore
        return fn if fn else (lambda f: f)


def _extract_tokens() -> tuple[int, int]:
    history = dspy.settings.lm.history
    if not history:
        return 0, 0
    usage = history[-1].get("usage", {})
    return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def _format_workload_deltas(baseline: dict[str, float],
                            current: dict[str, float]) -> str:
    """Build a human-readable table of per-workload RPS changes."""
    lines = ["Workload     | Baseline RPS | Current RPS |  Delta %"]
    lines.append("-" * 55)
    for w in sorted(set(baseline) & set(current)):
        b = baseline[w]
        c = current[w]
        pct = (c - b) / b * 100 if b else 0.0
        lines.append(f"{w:<12} | {b:>12.1f} | {c:>11.1f} | {pct:+7.1f}%")
    return "\n".join(lines)


class FixEvaluatorLLM:
    """DSPy module that reviews rejected-but-positive fixes."""

    def __init__(self, config: Config, prompts_dir: Path):
        self.config = config
        self.prompts_dir = prompts_dir
        self._module: dspy.Module | None = None

    def _build(self) -> dspy.Module:
        instructions = (self.prompts_dir / "fix_evaluation.md").read_text()

        class Sig(dspy.Signature):
            fix_description: str = dspy.InputField(
                desc="What the fix does (tool name, parameters, description)"
            )
            workload_results: str = dspy.InputField(
                desc="Per-workload baseline vs current RPS with delta percentages"
            )
            rca_context: str = dspy.InputField(
                desc="RCA summary explaining the bottleneck this fix addresses"
            )
            rejection_reason: str = dspy.InputField(
                desc="Why the mechanical gate rejected: which workloads degraded and by how much"
            )
            verdict_json: str = dspy.OutputField(
                desc='JSON: {"verdict": "accept"|"reject", "reasoning": "...", '
                     '"workload_analysis": {"workload": "note"}}'
            )

        Sig.__doc__ = instructions
        return dspy.Predict(Sig)

    @_trace
    def review(self, fix: dict,
               baseline: dict[str, float],
               current: dict[str, float],
               rca_context: str,
               degraded_workloads: dict[str, float],
               save_dir: Path | None = None) -> tuple[bool, str, int, int]:
        """Review a rejected fix. Returns (should_accept, reasoning, in_tok, out_tok)."""
        if self._module is None:
            self._module = self._build()

        fix_desc = (
            f"Tool: {fix.get('tool', 'unknown')}\n"
            f"Description: {fix.get('description', 'N/A')}\n"
            f"Parameters: {json.dumps(fix.get('params', {}))}"
        )
        workload_table = _format_workload_deltas(baseline, current)
        rejection_reason = "Degraded workloads: " + ", ".join(
            f"{w}={d:+.1f}%" for w, d in degraded_workloads.items()
        )

        logger.info("LLM fix review — calling LLM for: %s", fix.get("description", ""))
        t0 = datetime.now()
        pred = self._module(
            fix_description=fix_desc,
            workload_results=workload_table,
            rca_context=rca_context,
            rejection_reason=rejection_reason,
        )
        elapsed = (datetime.now() - t0).total_seconds()
        in_tok, out_tok = _extract_tokens()

        # Parse verdict
        raw = pred.verdict_json.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else ""

        try:
            result = json.loads(raw)
            verdict = result.get("verdict", "reject").lower() == "accept"
            reasoning = result.get("reasoning", "No reasoning provided")
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Failed to parse LLM verdict, defaulting to reject: %s", raw[:200])
            verdict = False
            reasoning = f"Parse error — raw response: {raw[:200]}"

        logger.info(
            "LLM fix review done in %.1fs — verdict: %s — %s",
            elapsed, "ACCEPT (override)" if verdict else "REJECT (confirmed)", reasoning,
        )

        if save_dir:
            self._save_review(save_dir, fix, workload_table, rca_context,
                              rejection_reason, verdict, reasoning, in_tok, out_tok)

        return verdict, reasoning, in_tok, out_tok

    @staticmethod
    def _save_review(save_dir: Path, fix: dict, workload_table: str,
                     rca_context: str, rejection_reason: str,
                     verdict: bool, reasoning: str,
                     in_tok: int, out_tok: int) -> None:
        """Save LLM review to session folder for debugging."""
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            fix_name = fix.get("tool", "unknown")
            path = save_dir / f"llm_review_{fix_name}.json"
            payload = {
                "timestamp": datetime.now().isoformat(),
                "fix": fix,
                "workload_table": workload_table,
                "rca_context": rca_context[:2000],
                "rejection_reason": rejection_reason,
                "verdict": "accept" if verdict else "reject",
                "reasoning": reasoning,
                "tokens": {"input": in_tok, "output": out_tok},
            }
            path.write_text(json.dumps(payload, indent=2, default=str))
        except Exception as e:
            logger.warning("Failed to save LLM review: %s", e)
