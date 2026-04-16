"""Post-run benchmark comparison against baseline and vanilla."""

import logging
import os
import subprocess

from .config import Config

logger = logging.getLogger("slayMetrics.comparison")


def run_comparisons(config: Config) -> None:
    """Run compare-results.sh to show recovery vs baseline and vanilla."""
    compare_script = config.benchmark_compare_script
    contestant = config.benchmark_contestant
    # Copy our latest results to /root/hackathon-results where baseline/vanilla live
    hackathon_dir = "/root/hackathon-results"
    our_dir = config.benchmark_results_dir
    try:
        subprocess.run(
            f"cp -f {our_dir}/{contestant}_*.json {hackathon_dir}/ 2>/dev/null",
            shell=True, timeout=10,
        )
    except Exception as e:
        logger.warning("Failed to copy results to %s: %s", hackathon_dir, e)
    env = {**os.environ, "TARGET_HOST": config.dut_host,
           "RESULTS_DIR": hackathon_dir}
    comparisons = [
        (contestant, "baseline", "vs DETUNED BASELINE"),
        (contestant, "vanilla", "vs VANILLA (healthy)"),
    ]
    for name, ref, label in comparisons:
        try:
            result = subprocess.run(
                [compare_script, name, ref],
                env=env, capture_output=True, text=True, timeout=30,
            )
            output = result.stdout.strip()
            if output:
                logger.info("Comparison %s:\n%s", label, output)
            elif result.returncode != 0:
                logger.warning("Comparison %s exited %d: %s",
                               label, result.returncode, result.stderr.strip())
        except FileNotFoundError:
            logger.warning("Compare script not found: %s — skipping comparisons", compare_script)
            return
        except Exception as e:
            logger.warning("Comparison %s failed: %s", label, e)
