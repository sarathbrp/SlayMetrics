"""
MLflow observability for SlayMetrics agent runs.

Each agent run becomes an MLflow run under the configured experiment.
All calls are wrapped with graceful fallback — if MLflow is unavailable,
the agent continues without interruption.
"""

import logging
from pathlib import Path

from .config import Config

logger = logging.getLogger("slayMetrics.tracking")


class RunTracker:
    """Wraps MLflow tracking for one agent session. No-op if disabled/unavailable."""

    def __init__(self, config: Config):
        self.config  = config
        self._active = False
        self._mlflow = None
        if config.mlflow_enabled:
            try:
                import mlflow
                self._mlflow = mlflow
                mlflow.set_tracking_uri(config.mlflow_tracking_uri)
                mlflow.set_experiment(config.mlflow_experiment)
                # Enable automatic DSPy tracing — captures prompt, response,
                # token usage and latency for every DSPy Predict/ChainOfThought call
                try:
                    mlflow.dspy.autolog()
                    logger.info("MLflow DSPy autolog enabled (Traces will appear in UI)")
                except Exception as ae:
                    logger.debug("MLflow DSPy autolog not available: %s", ae)
                logger.info("MLflow tracking enabled → %s / %s",
                            config.mlflow_tracking_uri, config.mlflow_experiment)
            except Exception as e:
                logger.warning("MLflow init failed — tracking disabled: %s", e)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, session_id: str) -> None:
        if not self._mlflow:
            return
        try:
            self._mlflow.start_run(run_name=session_id)
            self._active = True
            self._mlflow.set_tag("session_id", session_id)
            self._mlflow.set_tag("dut_host", self.config.dut_host)
            self._mlflow.set_tag("llm_model", self.config.llm_model)
            self._mlflow.log_params({
                "improvement_threshold_pct": self.config.remediation_threshold,
                "degradation_tolerance_pct": self.config.remediation_degradation_tolerance,
                "max_fixes":                 self.config.remediation_max_fixes,
            })
            logger.info("MLflow run started (session: %s)", session_id[:8])
        except Exception as e:
            logger.warning("MLflow start_run failed: %s", e)

    def end(self) -> None:
        if not self._active or not self._mlflow:
            return
        try:
            self._mlflow.end_run()
            self._active = False
        except Exception as e:
            logger.warning("MLflow end_run failed: %s", e)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def log_baseline(self, baseline_rps: dict[str, float]) -> None:
        if not self._active:
            return
        try:
            for workload, rps in baseline_rps.items():
                self._mlflow.log_metric(f"baseline_rps.{workload}", rps)
        except Exception as e:
            logger.warning("MLflow log_baseline failed: %s", e)

    def log_llm_call(self, domain: str, elapsed: float,
                     in_tok: int, out_tok: int, num_fixes: int) -> None:
        if not self._active:
            return
        try:
            self._mlflow.log_metrics({
                f"{domain}.elapsed_s":  elapsed,
                f"{domain}.in_tokens":  in_tok,
                f"{domain}.out_tokens": out_tok,
                f"{domain}.num_fixes":  num_fixes,
            })
        except Exception as e:
            logger.warning("MLflow log_llm_call failed: %s", e)

    def log_fix(self, description: str, tool: str, keep: bool, pct: float) -> None:
        if not self._active:
            return
        try:
            import re
            import unicodedata
            # Normalize unicode (e.g. non-breaking hyphen → ASCII hyphen), strip non-ASCII
            normalized = unicodedata.normalize("NFKC", description)
            # Replace any char not in MLflow's allowed set with underscore
            safe_name = re.sub(r"[^a-zA-Z0-9_\-. :/]", "_", normalized)
            safe_name = safe_name[:50].strip("_")
            self._mlflow.log_metrics({
                f"fix.{safe_name}.improvement_pct": pct,
                f"fix.{safe_name}.accepted":        int(keep),
            })
        except Exception as e:
            logger.warning("MLflow log_fix failed: %s", e)

    def log_final(self, applied: list, rejected: list,
                  in_tok: int, out_tok: int,
                  session_dir: Path | None = None) -> None:
        if not self._active:
            return
        try:
            max_pct = max((p for _, p in applied), default=0.0)
            self._mlflow.log_metrics({
                "total.input_tokens":  in_tok,
                "total.output_tokens": out_tok,
                "total.tokens":        in_tok + out_tok,
                "applied_count":       len(applied),
                "rejected_count":      len(rejected),
                "max_improvement_pct": max_pct,
            })
            # Upload session artifacts
            if session_dir and session_dir.exists():
                for artifact in session_dir.iterdir():
                    if artifact.suffix in (".md", ".csv", ".txt", ".json"):
                        self._mlflow.log_artifact(str(artifact))
            logger.info("MLflow run logged (applied=%d, rejected=%d, max_gain=%.1f%%)",
                        len(applied), len(rejected), max_pct)
        except Exception as e:
            logger.warning("MLflow log_final failed: %s", e)
