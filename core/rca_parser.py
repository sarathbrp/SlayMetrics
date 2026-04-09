import json
import logging
from datetime import datetime

import dspy

from .config import Config
from .remediation_tools import TOOL_REGISTRY, NETWORK_TOOL_NAMES

logger = logging.getLogger("slayMetrics.rca_parser")

_TOOL_DOCS = "\n".join(
    f'  "{name}": params={cls.params_schema}'
    for name, cls in sorted(TOOL_REGISTRY.items())
)


class FixExtractSignature(dspy.Signature):
    """Extract actionable fixes from an RCA report as a JSON array.
    Output ONLY valid JSON — no markdown, no explanation.
    Each fix must use one of the allowed tools exactly as listed.
    Order fixes by tier (Tier 1 first).
    """

    rca_report: str = dspy.InputField(
        desc="Structured RCA report with Tier 1/2/3 action plan"
    )
    similar_cases: str = dspy.InputField(
        desc=(
            "Similar past cases from semantic memory showing what fixes worked and failed "
            "in comparable system states. Use this to avoid re-suggesting fixes that "
            "previously failed and to prioritize fixes that showed improvement. "
            "Empty string if no history available."
        )
    )
    fixes_json: str = dspy.OutputField(
        desc=(
            "JSON array ordered by priority. Each element: "
            '{"tier": 1, "description": "short label", '
            '"tool": "<tool_name>", "params": {<typed params>}, '
            '"rollback_params": {<params to restore original — optional, '
            "tool handles rollback automatically>}}. "
            f"Allowed tools:\n{_TOOL_DOCS}"
        )
    )


class RCAParser:
    """Uses DSPy to extract structured, tool-based fixes from an RCA report."""

    def __init__(self, config: Config):
        self.config = config
        self._module = dspy.Predict(FixExtractSignature)

    def extract_fixes(self, rca_report: str,
                      similar_cases: str = "") -> tuple[list[dict], int, int]:
        logger.info("Sending RCA report to LLM for fix extraction (%d chars) — waiting for response...", len(rca_report))
        _t0 = datetime.now()
        prediction = self._module(rca_report=rca_report, similar_cases=similar_cases)
        elapsed = (datetime.now() - _t0).total_seconds()

        input_tokens = output_tokens = 0
        history = dspy.settings.lm.history
        if history:
            usage = history[-1].get("usage", {})
            input_tokens  = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

        raw = prediction.fixes_json.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else ""

        fixes: list[dict] = json.loads(raw)
        # Within each tier, network tools come first — they remove the hardest caps
        fixes.sort(key=lambda f: (
            f.get("tier", 99),
            0 if f.get("tool") in NETWORK_TOOL_NAMES else 1,
        ))

        unknown = [f["tool"] for f in fixes if f.get("tool") not in TOOL_REGISTRY]
        if unknown:
            logger.warning("Dropping fixes with unknown tools: %s", unknown)
            fixes = [f for f in fixes if f.get("tool") in TOOL_REGISTRY]

        logger.info("Extracted %d valid fixes in %.1fs (tiers: %s, %d input / %d output tokens)",
                    len(fixes), elapsed, [f.get("tier") for f in fixes], input_tokens, output_tokens)
        return fixes, input_tokens, output_tokens
