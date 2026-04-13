import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


_REQUIRED_SECTIONS = ("target", "benchmark")


class Config:
    def __init__(self, config_path: Path, env_path: Path | None = None):
        load_dotenv(env_path)
        with open(config_path) as f:
            self._cfg = yaml.safe_load(f)
        if not isinstance(self._cfg, dict):
            raise ValueError(f"Config file {config_path} must be a YAML mapping, got {type(self._cfg).__name__}")
        missing = [s for s in _REQUIRED_SECTIONS if s not in self._cfg]
        if missing:
            raise ValueError(f"Config file {config_path} missing required sections: {missing}")

    # --- target (DUT) — env vars override config.yaml ---
    @property
    def dut_host(self) -> str:
        return os.environ.get("SLAY_DUT_HOST", self._cfg["target"]["host"])

    @property
    def dut_user(self) -> str:
        return os.environ.get("SLAY_DUT_USER", self._cfg["target"]["user"])

    @property
    def dut_key(self) -> str:
        return os.environ.get("SLAY_DUT_KEY", self._cfg["target"]["private_key_path"])

    @property
    def dut_port(self) -> int:
        return int(os.environ.get("SLAY_DUT_PORT", self._cfg["target"].get("port", 22)))

    @property
    def dut_timeout(self) -> int:
        return int(os.environ.get("SLAY_DUT_TIMEOUT", self._cfg["target"].get("connect_timeout_seconds", 10)))

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

    # --- MLflow — env vars override config.yaml ---
    @property
    def mlflow_enabled(self) -> bool:
        env = os.environ.get("SLAY_MLFLOW_ENABLED")
        if env is not None:
            return env.lower() in ("true", "1", "yes")
        return self._cfg.get("mlflow", {}).get("enabled", False)

    @property
    def mlflow_tracking_uri(self) -> str:
        return os.environ.get("SLAY_MLFLOW_URI", self._cfg.get("mlflow", {}).get("tracking_uri", "http://localhost:5000"))

    @property
    def mlflow_experiment(self) -> str:
        return os.environ.get("SLAY_MLFLOW_EXPERIMENT", self._cfg.get("mlflow", {}).get("experiment", "SlayMetrics"))

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

    # --- investigation ---
    def _inv(self, key: str, default: int | bool) -> int | bool:
        return self._cfg.get("investigation", {}).get(key, default)

    @property
    def investigation_enabled(self) -> bool:
        return bool(self._inv("enabled", True))

    @property
    def investigation_max_iterations(self) -> int:
        return int(self._inv("max_iterations", 8))

    @property
    def investigation_command_timeout(self) -> int:
        return int(self._inv("command_timeout_seconds", 30))

    @property
    def investigation_max_output_bytes(self) -> int:
        return int(self._inv("max_output_bytes", 8192))

    @property
    def investigation_max_commands_per_iteration(self) -> int:
        return int(self._inv("max_commands_per_iteration", 5))

    # --- misc ---
    @property
    def log_level(self) -> str:
        return self._cfg.get("log_level", "INFO").upper()

    # --- fleet/orchestration ---
    @property
    def orchestration_max_parallel_audits(self) -> int:
        return int(self._cfg.get("orchestration", {}).get("max_parallel_audits", 10))

    @property
    def orchestration_target_password(self) -> str:
        return str(
            os.environ.get(
                "SLAY_TARGET_PASSWORD",
                self._cfg.get("orchestration", {}).get("target_password", ""),
            )
        )

    def _installer(self, key: str, default: Any) -> Any:
        return self._cfg.get("orchestration", {}).get("installer", {}).get(key, default)

    @property
    def orchestration_installer_user(self) -> str:
        return str(self._installer("user", "root"))

    @property
    def orchestration_installer_key(self) -> str:
        return str(self._installer("private_key_path", self.dut_key))

    @property
    def orchestration_installer_port(self) -> int:
        return int(self._installer("port", 22))

    @property
    def orchestration_installer_timeout(self) -> int:
        return int(self._installer("connect_timeout_seconds", 30))

    @property
    def orchestration_installer_remote_tmp(self) -> str:
        return str(self._installer("remote_tmp", "/tmp/slaymetrics_orchestrate"))

    @property
    def orchestration_installer_auto_install_wrk(self) -> bool:
        return bool(self._installer("auto_install_wrk", True))

    @property
    def target_specs(self) -> list[dict[str, Any]]:
        """Return normalized target specs for single or fleet mode.

        Uses top-level `targets:` list when present; otherwise falls back to
        the single `target:` section (including env-var overrides).
        """
        defaults = {
            "host": self.dut_host,
            "user": self.dut_user,
            "private_key_path": self.dut_key,
            "port": self.dut_port,
            "connect_timeout_seconds": self.dut_timeout,
        }

        raw_targets = self._cfg.get("targets")
        if raw_targets is None:
            raw_targets = [{
                "name": "default",
                "host": defaults["host"],
                "user": defaults["user"],
                "private_key_path": defaults["private_key_path"],
                "port": defaults["port"],
                "connect_timeout_seconds": defaults["connect_timeout_seconds"],
            }]

        if not isinstance(raw_targets, list):
            raise ValueError("config.yaml field 'targets' must be a list when provided")

        specs: list[dict[str, Any]] = []
        for idx, raw in enumerate(raw_targets, 1):
            if not isinstance(raw, dict):
                raise ValueError(f"targets[{idx - 1}] must be a mapping, got {type(raw).__name__}")

            host = str(raw.get("host", defaults["host"])).strip()
            if not host:
                raise ValueError(f"targets[{idx - 1}] is missing required 'host'")

            user = str(raw.get("user", defaults["user"])).strip()
            key_path = str(raw.get("private_key_path", defaults["private_key_path"])).strip()
            port = int(raw.get("port", defaults["port"]))
            timeout = int(raw.get("connect_timeout_seconds", defaults["connect_timeout_seconds"]))
            name = str(raw.get("name", "")).strip() or host
            inferred_group = name.rsplit("-", 1)[0] if "-" in name else "default"
            group = str(raw.get("group", inferred_group)).strip() or inferred_group

            specs.append({
                "name": f"{name}-{idx}" if any(t["name"] == name for t in specs) else name,
                "group": group,
                "host": host,
                "user": user,
                "private_key_path": key_path,
                "password": str(raw.get("password", self.orchestration_target_password)),
                "port": port,
                "connect_timeout_seconds": timeout,
            })
        return specs
