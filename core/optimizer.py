"""
Automated feedback pipeline for RCA Slay Metrics.

After enough new examples accumulate in dspy_data/examples.jsonl, run
DSPy BootstrapFewShot to compile an optimized RCA module. Compiled programs
are saved to dspy_data/rca_program/ and loaded on the next run automatically.
"""

import json
import logging
from pathlib import Path

import dspy
from dspy.teleprompt import BootstrapFewShot

from .remediation_tools import TOOL_REGISTRY

logger = logging.getLogger("slayMetrics.optimizer")

_TIER_KEYWORDS = {"Tier 1", "Tier 2", "Tier 3"}
_TOOL_NAMES    = set(TOOL_REGISTRY.keys())
_MARKER_FILE   = ".last_optimized"


class FeedbackOptimizer:
    """Loads examples.jsonl, runs BootstrapFewShot, saves compiled programs."""

    def __init__(self, min_new_examples: int = 5, max_bootstrap_demos: int = 3):
        self.min_new_examples    = min_new_examples
        self.max_bootstrap_demos = max_bootstrap_demos

    # ------------------------------------------------------------------
    # Example loading
    # ------------------------------------------------------------------

    def load_examples(self, jsonl_path: Path) -> tuple[list[dspy.Example], list[dspy.Example]]:
        """Load JSONL → dspy.Example list, split 70/30 train/val."""
        examples: list[dspy.Example] = []
        with jsonl_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                ex = dspy.Example(
                    audit_output=data.get("audit_output", ""),
                    benchmark_results=data.get("benchmark_results", ""),
                    rca_report=data.get("rca_report", ""),
                    applied_fixes=data.get("applied_fixes", []),
                ).with_inputs("audit_output", "benchmark_results")
                examples.append(ex)

        split = max(1, int(len(examples) * 0.7))
        return examples[:split], examples[split:]

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    @staticmethod
    def rca_metric(example: dspy.Example, prediction: dspy.Prediction,
                   trace: object = None) -> bool:
        """Quality metric for the RCA report.

        Checks:
          - Length between 500 and 10 000 chars
          - Contains at least two Tier sections (Tier 1, Tier 2)
          - Mentions at least one known tool name
        """
        report: str = getattr(prediction, "rca_report", "") or ""
        if not (500 <= len(report) <= 10_000):
            return False
        tier_hits = sum(1 for kw in _TIER_KEYWORDS if kw in report)
        if tier_hits < 2:
            return False
        if not any(t in report for t in _TOOL_NAMES):
            return False
        return True

    # ------------------------------------------------------------------
    # Trigger guard
    # ------------------------------------------------------------------

    def should_optimize(self, jsonl_path: Path) -> bool:
        """Return True if there are >= min_new_examples new entries since last run."""
        if not jsonl_path.exists():
            return False
        current_count = sum(1 for line in jsonl_path.open() if line.strip())
        if current_count < self.min_new_examples:
            return False
        marker = jsonl_path.parent / _MARKER_FILE
        last_count = 0
        if marker.exists():
            try:
                last_count = int(marker.read_text().strip())
            except ValueError:
                pass
        return (current_count - last_count) >= self.min_new_examples

    # ------------------------------------------------------------------
    # Optimization
    # ------------------------------------------------------------------

    def optimize_rca(self, analyzer: object, dspy_dir: Path) -> None:
        """Run BootstrapFewShot on the RCA module and persist compiled program."""
        jsonl_path = dspy_dir / "examples.jsonl"
        if not jsonl_path.exists():
            logger.warning("No examples file found — skipping optimization")
            return

        trainset, valset = self.load_examples(jsonl_path)
        if not trainset:
            logger.warning("Empty trainset — skipping optimization")
            return

        module = getattr(analyzer, "_module", None)
        if module is None:
            logger.warning("RCA module not initialized — skipping optimization")
            return

        logger.info("BootstrapFewShot: %d train / %d val examples, max_demos=%d",
                    len(trainset), len(valset), self.max_bootstrap_demos)

        teleprompter = BootstrapFewShot(
            metric=self.rca_metric,
            max_bootstrapped_demos=self.max_bootstrap_demos,
        )
        compiled = teleprompter.compile(module, trainset=trainset)
        analyzer._module = compiled
        analyzer.save_program()

        # Update marker so we don't re-optimize until more examples arrive
        current_count = sum(1 for line in jsonl_path.open() if line.strip())
        marker = jsonl_path.parent / _MARKER_FILE
        marker.write_text(str(current_count))
        logger.info("Optimization complete — marker set to %d examples", current_count)
