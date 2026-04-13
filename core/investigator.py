"""Autonomous SRE investigation agent.

Uses a DSPy-driven multi-turn loop to investigate the DUT's performance
stack via SSH. Each step() call is one LLM turn that can request diagnostic
commands and accumulate findings across 5 layers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import dspy

from .config import Config
from .analyzer_utils import extract_tokens

logger = logging.getLogger("slayMetrics.investigator")


@dataclass
class InvestigationResult:
    """Parsed output from one investigation turn."""
    done: bool
    commands: list[str] = field(default_factory=list)
    reasoning: str = ""
    findings: str = ""
    layer: str = ""


def _parse_response(raw: str) -> InvestigationResult:
    """Parse LLM JSON response into InvestigationResult."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
    try:
        data = json.loads(raw)
        return InvestigationResult(
            done=bool(data.get("done", False)),
            commands=data.get("commands", []),
            reasoning=data.get("reasoning", ""),
            findings=data.get("findings", ""),
            layer=data.get("layer", ""),
        )
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Failed to parse investigation response, treating as done")
        return InvestigationResult(done=True, findings="(parse error — investigation ended)")


class SREInvestigator:
    """DSPy-driven interactive investigator for the DUT performance stack."""

    def __init__(self, config: Config, prompts_dir: Path):
        self.config = config
        self.prompts_dir = prompts_dir
        self._module: dspy.Module | None = None

    def _build(self) -> dspy.Module:
        instructions = (self.prompts_dir / "investigation.md").read_text()

        class Sig(dspy.Signature):
            audit_baseline: str = dspy.InputField(
                desc="Static audit output from omega_master_audit.sh (5-group baseline)"
            )
            benchmark_results: str = dspy.InputField(
                desc="Benchmark RPS results per workload"
            )
            live_audit_output: str = dspy.InputField(
                desc="Dynamic runtime metrics from live sampler during benchmark"
            )
            previous_findings: str = dspy.InputField(
                desc=(
                    "Commands already run and their outputs from prior iterations. "
                    "Empty on first iteration. Build on these — do not repeat."
                )
            )
            response_json: str = dspy.OutputField(
                desc=(
                    'JSON: {"layer": "1-5 or cross-layer", "commands": ["cmd1", ...], '
                    '"reasoning": "why", "findings": "summary so far", "done": bool}'
                )
            )

        Sig.__doc__ = instructions
        return dspy.Predict(Sig)

    def step(
        self,
        audit_baseline: str,
        benchmark_results: str,
        live_audit_output: str,
        previous_findings: str,
    ) -> tuple[InvestigationResult, int, int, float]:
        """Run one investigation turn.

        Returns (result, input_tokens, output_tokens, elapsed_seconds).
        """
        if self._module is None:
            self._module = self._build()
        logger.info("Investigation step — running inference...")
        t0 = datetime.now()
        pred = self._module(
            audit_baseline=audit_baseline,
            benchmark_results=benchmark_results,
            live_audit_output=live_audit_output,
            previous_findings=previous_findings or "First iteration — no prior findings.",
        )
        elapsed = (datetime.now() - t0).total_seconds()
        result = _parse_response(pred.response_json)
        in_tok, out_tok = extract_tokens()
        logger.info(
            "Investigation step done in %.1fs — layer=%s, done=%s, commands=%d",
            elapsed, result.layer, result.done, len(result.commands),
        )
        if result.reasoning:
            logger.info("  Reasoning: %s", result.reasoning[:200])
        return result, in_tok, out_tok, elapsed
