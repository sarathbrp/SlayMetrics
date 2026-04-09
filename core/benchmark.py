import logging
import subprocess
import time
from pathlib import Path



from .config import Config

logger = logging.getLogger("slayMetrics.benchmark")


class BenchmarkRunner:
    """Runs benchmark-fast.sh locally on system2 against the DUT.

    The script takes contestant_name as $1 and TARGET_HOST as env var.
    It runs all configured workloads internally.
    """

    def __init__(self, config: Config):
        self.config = config

    def _env(self) -> dict:
        """Build subprocess env with TARGET_HOST and RESULTS_DIR injected."""
        import os
        env = os.environ.copy()
        env["TARGET_HOST"] = self.config.dut_host
        env["RESULTS_DIR"] = self.config.benchmark_results_dir
        return env

    def run(self) -> str:
        """Run the full benchmark suite. Returns formatted plain-text results."""
        script = self.config.benchmark_script
        contestant = self.config.benchmark_contestant

        if not Path(script).exists():
            raise FileNotFoundError(f"Benchmark script not found: {script}")

        logger.info("Running benchmark: %s %s (TARGET_HOST=%s)",
                    script, contestant, self.config.dut_host)

        result = subprocess.run(
            [script, contestant],
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=600,
        )

        output = result.stdout.strip()
        if result.returncode != 0:
            logger.warning("Benchmark exited %d. stderr: %s",
                           result.returncode, result.stderr.strip())

        if not output:
            raise RuntimeError(
                f"Benchmark produced no output. stderr: {result.stderr.strip()}"
            )

        logger.info("Benchmark complete (%d bytes)", len(output))

        cooling = self.config.benchmark_cooling_period
        if cooling > 0:
            logger.info("Cooling period: %ds — waiting for DUT to drain connections", cooling)
            time.sleep(cooling)

        return output

    def run_final(self, duration_minutes: int) -> str:
        """Run extended final benchmark with BENCHMARK_DURATION_MINUTES env var."""
        script     = self.config.benchmark_script
        contestant = self.config.benchmark_contestant

        if not Path(script).exists():
            raise FileNotFoundError(f"Benchmark script not found: {script}")

        logger.info("Running FINAL benchmark (%d min): %s %s (TARGET_HOST=%s)",
                    duration_minutes, script, contestant, self.config.dut_host)

        env = self._env()
        env["BENCHMARK_DURATION_MINUTES"] = str(duration_minutes)

        timeout = max(600, duration_minutes * 60 * 6)  # generous: 6× workloads
        result = subprocess.run(
            [script, contestant],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = result.stdout.strip()
        if result.returncode != 0:
            logger.warning("Final benchmark exited %d. stderr: %s",
                           result.returncode, result.stderr.strip())
        if not output:
            raise RuntimeError(
                f"Final benchmark produced no output. stderr: {result.stderr.strip()}"
            )

        logger.info("Final benchmark complete (%d bytes)", len(output))
        return output

    def format_for_llm(self, benchmark_output: str) -> str:
        """Format benchmark output as plain text for the LLM."""
        lines = [
            f"=== Benchmark Results (contestant: {self.config.benchmark_contestant}) ===",
            f"Target Host: {self.config.dut_host}",
            "",
            benchmark_output,
        ]
        return "\n".join(lines)
