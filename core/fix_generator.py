"""Fix Generator — converts investigation findings into actionable fix list.

Replaces the 3 domain analyzers (network/kernel/nginx) with a single LLM call
that sees the full investigation report and all tool docs.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import dspy

from .config import Config
from .analyzer_utils import extract_tokens, save_prompt

logger = logging.getLogger("slayMetrics.fix_generator")


def _parse_fixes(raw: str) -> tuple[list[dict], str]:
    """Parse LLM JSON response into (fixes, rca_summary)."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
    try:
        data = json.loads(raw)
        fixes = data.get("fixes", []) if isinstance(data, dict) else data
        summary = data.get("rca_summary", "") if isinstance(data, dict) else ""
        return fixes, summary
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Failed to parse fix generator response")
        return [], ""


class FixGenerator:
    """Single-call fix generator from investigation findings."""

    def __init__(self, config: Config, prompts_dir: Path):
        self.config = config
        self.prompts_dir = prompts_dir
        self._module: dspy.Module | None = None

    def _build(self) -> dspy.Module:
        instructions = (self.prompts_dir / "fix_generator.md").read_text()

        class Sig(dspy.Signature):
            investigation_notes: str = dspy.InputField(
                desc="Structured investigation report with bottleneck ranking and attack plan"
            )
            benchmark_results: str = dspy.InputField(
                desc="Benchmark RPS results per workload"
            )
            live_audit_output: str = dspy.InputField(
                desc="Live sampler findings from during the benchmark"
            )
            performance_rules: str = dspy.InputField(
                desc="Mandatory performance rules — constraint chains, fix ordering"
            )
            result_json: str = dspy.OutputField(
                desc='JSON: {"fixes": [{"tier": N, "description": "...", '
                     '"tool": "...", "params": {...}}, ...], "rca_summary": "..."}'
            )

        Sig.__doc__ = instructions
        return dspy.Predict(Sig)

    def generate(
        self,
        investigation_notes: str,
        benchmark_results: str,
        live_audit_output: str,
        performance_rules: str,
        save_dir: Path | None = None,
    ) -> tuple[list[dict], str, int, int, float]:
        """Generate fixes from investigation findings.

        Returns (fixes, rca_summary, input_tokens, output_tokens, elapsed).
        """
        if self._module is None:
            self._module = self._build()
        logger.info("Fix generation — running inference...")
        t0 = datetime.now()
        pred = self._module(
            investigation_notes=investigation_notes,
            benchmark_results=benchmark_results,
            live_audit_output=live_audit_output,
            performance_rules=performance_rules,
        )
        elapsed = (datetime.now() - t0).total_seconds()
        fixes, rca_summary = _parse_fixes(pred.result_json)
        in_tok, out_tok = extract_tokens()
        logger.info("Fix generation done in %.1fs — %d fixes", elapsed, len(fixes))
        for f in fixes:
            logger.info("  [Fix] %s → tool=%s params=%s",
                        f.get("description", ""), f.get("tool", ""), f.get("params", {}))
        if save_dir:
            save_prompt(save_dir, "fix_generator",
                        {"investigation_notes": investigation_notes,
                         "benchmark_results": benchmark_results,
                         "live_audit_output": live_audit_output,
                         "performance_rules": performance_rules},
                        fixes, rca_summary, in_tok, out_tok)
        return fixes, rca_summary, in_tok, out_tok, elapsed
