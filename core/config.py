import os
from pathlib import Path

import yaml
from dotenv import load_dotenv


class Config:
    def __init__(self, config_path: Path, env_path: Path | None = None):
        load_dotenv(env_path)
        with open(config_path) as f:
            self._cfg = yaml.safe_load(f)

    # --- target (DUT) ---
    @property
    def dut_host(self) -> str:
        return self._cfg["target"]["host"]

    @property
    def dut_user(self) -> str:
        return self._cfg["target"]["user"]

    @property
    def dut_key(self) -> str:
        return self._cfg["target"]["private_key_path"]

    @property
    def dut_port(self) -> int:
        return self._cfg["target"].get("port", 22)

    @property
    def dut_timeout(self) -> int:
        return self._cfg["target"].get("connect_timeout_seconds", 10)

    # --- LLM ---
    @property
    def llm_base_url(self) -> str:
        return os.environ["GPT_OSS_BASE_URL"]

    @property
    def llm_api_key(self) -> str:
        return os.environ["GPT_OSS_API_KEY"]

    @property
    def llm_model(self) -> str:
        return os.environ["GPT_OSS_MODEL"]

    @property
    def llm_embed_model(self) -> str:
        return os.environ.get("GPT_OSS_EMBED_MODEL", os.environ["GPT_OSS_MODEL"])

    # --- benchmark ---
    @property
    def benchmark_script(self) -> str:
        return self._cfg["benchmark"]["script_path"]

    @property
    def benchmark_compare_script(self) -> str:
        return self._cfg["benchmark"]["compare_script_path"]

    @property
    def benchmark_contestant(self) -> str:
        return self._cfg["benchmark"]["contestant_name"]

    @property
    def benchmark_results_dir(self) -> str:
        return self._cfg["benchmark"]["results_directory"]

    @property
    def benchmark_cooling_period(self) -> int:
        return self._cfg["benchmark"].get("cooling_period_seconds", 30)

    @property
    def benchmark_final_duration_minutes(self) -> int:
        return self._cfg.get("benchmark", {}).get("final_benchmark_duration_minutes", 5)

    @property
    def benchmark_collect_live_audit(self) -> bool:
        return self._cfg.get("benchmark", {}).get("collect_live_audit", True)

    @property
    def live_sampling_enabled(self) -> bool:
        return (self.benchmark_collect_live_audit and
                self._cfg.get("benchmark", {}).get("live_sampling", {}).get("enabled", True))

    @property
    def live_sampling_interval(self) -> int:
        return self._cfg.get("benchmark", {}).get("live_sampling", {}).get("interval_seconds", 2)

    @property
    def live_sampling_max_samples(self) -> int:
        return self._cfg.get("benchmark", {}).get("live_sampling", {}).get("max_samples", 25)

    @property
    def benchmark_workloads(self) -> list[str]:
        return self._cfg["benchmark"].get("workloads", [])

    # --- remediation ---
    @property
    def remediation_threshold(self) -> float:
        return self._cfg.get("remediation", {}).get("improvement_threshold_pct", 5.0)

    @property
    def remediation_max_fixes(self) -> int:
        return self._cfg.get("remediation", {}).get("max_fixes", 10)

    @property
    def remediation_degradation_tolerance(self) -> float:
        return self._cfg.get("remediation", {}).get("degradation_tolerance_pct", -3.0)

    @property
    def remediation_llm_review_rejected(self) -> bool:
        return self._cfg.get("remediation", {}).get("llm_review_rejected", False)

    # --- MLflow ---
    @property
    def mlflow_enabled(self) -> bool:
        return self._cfg.get("mlflow", {}).get("enabled", False)

    @property
    def mlflow_tracking_uri(self) -> str:
        return self._cfg.get("mlflow", {}).get("tracking_uri", "http://localhost:5000")

    @property
    def mlflow_experiment(self) -> str:
        return self._cfg.get("mlflow", {}).get("experiment", "SlayMetrics")

    @property
    def memory_inject_into_rca(self) -> bool:
        return self._cfg.get("memory", {}).get("inject_into_rca_analysis", True)

    @property
    def memory_inject_into_fix_extraction(self) -> bool:
        return self._cfg.get("memory", {}).get("inject_into_fix_extraction", True)

    def remediation_network_tool_scope(self, tool: str) -> str:
        """Returns 'none', 'read', or 'write' for a given network tool."""
        return self._cfg.get("remediation", {}).get("network_tools", {}).get(tool, "none")

    # --- optimization ---
    @property
    def optimization_min_new_examples(self) -> int:
        return self._cfg.get("optimization", {}).get("min_new_examples", 5)

    @property
    def optimization_max_bootstrap_demos(self) -> int:
        return self._cfg.get("optimization", {}).get("max_bootstrap_demos", 3)

    # --- misc ---
    @property
    def log_level(self) -> str:
        return self._cfg.get("log_level", "INFO").upper()
