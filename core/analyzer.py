import json
import logging
from pathlib import Path
from datetime import datetime

import dspy

from .config import Config

logger = logging.getLogger("slayMetrics.analyzer")


class RCAAnalyzer:
    """DSPy-powered RCA analyzer. Self-improves via saved examples."""

    def __init__(self, config: Config, prompts_dir: Path, dspy_dir: Path):
        self.config = config
        self.prompts_dir = prompts_dir
        self.dspy_dir = dspy_dir
        self.examples_file = dspy_dir / "examples.jsonl"
        self.program_dir = dspy_dir / "rca_program"
        self._module: dspy.Module | None = None

    def configure(self) -> None:
        """Configure DSPy LM from config/.env."""
        # Keep the leading slash — LiteLLM strips "openai/" leaving "/models/..."
        # which is what this OpenAI-compatible endpoint expects.
        model = f"openai/{self.config.llm_model}"
        lm = dspy.LM(
            model=model,
            api_base=self.config.llm_base_url,
            api_key=self.config.llm_api_key,
            temperature=0.2,
        )
        dspy.configure(lm=lm)
        logger.info("DSPy configured with model: %s", model)

    def _build_module(self) -> dspy.Module:
        instructions = (self.prompts_dir / "rca.md").read_text()

        class RCASignature(dspy.Signature):
            audit_output: str = dspy.InputField(
                desc="Plain-text 5-group stack audit from omega_master_audit.sh"
            )
            benchmark_results: str = dspy.InputField(
                desc="Plain-text benchmark results showing RPS per workload"
            )
            live_audit_output: str = dspy.InputField(
                desc=(
                    "Dynamic runtime metrics collected immediately after the benchmark: "
                    "TCP socket state (ss -s), softnet drop/squeeze counters, NIC driver "
                    "rx_discards/rx_errors, socket memory pressure (/proc/net/sockstat), "
                    "CPU activity (vmstat), and cgroup throttle ratio. "
                    "Empty string if not collected."
                )
            )
            similar_cases: str = dspy.InputField(
                desc=(
                    "Similar past cases retrieved from semantic memory — "
                    "shows what fixes worked and failed in comparable system states. "
                    "Empty string if no past cases available."
                )
            )
            rca_report: str = dspy.OutputField(
                desc=(
                    "Structured RCA with: RCA Summary, Mismatch Table, "
                    "Action Plan (Tier 1/2/3), Impact Prediction, Verification Checklist"
                )
            )

        RCASignature.__doc__ = instructions

        class RCAModule(dspy.Module):
            def __init__(self):
                self.analyze = dspy.ChainOfThought(RCASignature)

            def forward(self, audit_output: str, benchmark_results: str = "",
                        live_audit_output: str = "",
                        similar_cases: str = "") -> dspy.Prediction:
                return self.analyze(
                    audit_output=audit_output,
                    benchmark_results=benchmark_results,
                    live_audit_output=live_audit_output,
                    similar_cases=similar_cases,
                )

        module = RCAModule()

        if self.program_dir.exists():
            try:
                module.load(str(self.program_dir))
                logger.info("Loaded compiled DSPy program from %s", self.program_dir)
            except Exception as e:
                logger.warning("Could not load compiled program (%s) — using fresh module", e)

        return module

    def analyze(self, audit_output: str, benchmark_results: str = "",
                live_audit_output: str = "",
                similar_cases: str = "") -> tuple[str, int, int]:
        """Run RCA. Returns (rca_report, input_tokens, output_tokens)."""
        if self._module is None:
            self._module = self._build_module()

        logger.info("Sending audit + benchmark data to LLM for RCA analysis — waiting for response...")
        _t0 = datetime.now()
        prediction = self._module(
            audit_output=audit_output,
            benchmark_results=benchmark_results or "No benchmark results available.",
            live_audit_output=live_audit_output,
            similar_cases=similar_cases,
        )
        elapsed = (datetime.now() - _t0).total_seconds()
        rca_report = prediction.rca_report

        input_tokens = output_tokens = 0
        history = dspy.settings.lm.history
        if history:
            usage = history[-1].get("usage", {})
            input_tokens  = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

        logger.info("RCA report received in %.1fs (%d chars, %d input / %d output tokens)",
                    elapsed, len(rca_report), input_tokens, output_tokens)
        return rca_report, input_tokens, output_tokens

    def save_example(self, audit_output: str, rca_report: str,
                     benchmark_results: str = "",
                     applied_fixes: list | None = None,
                     rejected_fixes: list | None = None) -> None:
        """Persist a training example (with remediation outcomes) for DSPy optimization."""
        self.dspy_dir.mkdir(exist_ok=True)
        example = {
            "timestamp":       datetime.now().isoformat(),
            "audit_output":    audit_output,
            "benchmark_results": benchmark_results,
            "rca_report":      rca_report,
            "applied_fixes":   applied_fixes or [],
            "rejected_fixes":  rejected_fixes or [],
        }
        with self.examples_file.open("a") as f:
            f.write(json.dumps(example) + "\n")

        total = sum(1 for _ in self.examples_file.open())
        logger.info("Example saved (%d total, %d applied / %d rejected) -> %s",
                    total, len(applied_fixes or []), len(rejected_fixes or []),
                    self.examples_file)

    def save_program(self) -> None:
        """Persist the current compiled DSPy program to disk."""
        if self._module is None:
            return
        self.program_dir.mkdir(parents=True, exist_ok=True)
        self._module.save(str(self.program_dir))
        logger.info("DSPy program saved to %s", self.program_dir)
