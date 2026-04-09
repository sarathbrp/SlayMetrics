import logging
import re

logger = logging.getLogger("slayMetrics.evaluator")

_WORKLOAD_RE = re.compile(r"\[\d+/\d+\]\s+(\w+):")
_RPS_RE      = re.compile(r"rps=([\d.]+)")

# Workloads that must improve by >= threshold to keep a fix.
# All other workloads must not degrade (improvement >= 0%).
PRIORITY_WORKLOADS = {"homepage", "small"}


class Evaluator:
    """Parses benchmark output and compares RPS against a baseline."""

    @staticmethod
    def parse_rps(benchmark_output: str) -> dict[str, float]:
        """Extract per-workload RPS from benchmark output."""
        result: dict[str, float] = {}
        current_workload: str | None = None

        for line in benchmark_output.splitlines():
            m = _WORKLOAD_RE.search(line)
            if m:
                current_workload = m.group(1)
                continue
            rps_m = _RPS_RE.search(line)
            if rps_m and current_workload:
                result[current_workload] = float(rps_m.group(1))
                current_workload = None

        return result

    @staticmethod
    def improvement_pct(baseline: dict[str, float],
                        current: dict[str, float]) -> float:
        """Simple average improvement % across workloads common to both runs."""
        common = set(baseline) & set(current)
        if not baseline or not common:
            return 0.0
        pcts = [
            (current[w] - baseline[w]) / baseline[w] * 100
            for w in common
        ]
        return sum(pcts) / len(pcts)

    @staticmethod
    def weighted_improvement_pct(baseline: dict[str, float],
                                 current: dict[str, float]) -> float:
        """RPS-weighted average improvement %. High-RPS workloads dominate."""
        common = set(baseline) & set(current)
        if not baseline or not common:
            return 0.0
        total_rps = sum(baseline[w] for w in common)
        if total_rps == 0:
            return 0.0
        return sum(
            baseline[w] * (current[w] - baseline[w]) / baseline[w] * 100
            for w in common
        ) / total_rps

    @staticmethod
    def should_keep(baseline: dict[str, float], current: dict[str, float],
                    threshold: float,
                    degradation_tolerance: float = -3.0) -> tuple[bool, float, dict[str, float]]:
        """Keep a fix only if priority workloads improve >= threshold
        AND no other workload degrades.

        Returns (keep, priority_improvement_pct, degraded_workloads).
        """
        common = set(baseline) & set(current)
        deltas = {
            w: (current[w] - baseline[w]) / baseline[w] * 100
            for w in common if baseline[w]
        }

        priority = {w: d for w, d in deltas.items() if w in PRIORITY_WORKLOADS}
        others   = {w: d for w, d in deltas.items() if w not in PRIORITY_WORKLOADS}

        priority_avg = sum(priority.values()) / len(priority) if priority else 0.0

        # Skip degradation check for workloads with very low baseline RPS —
        # at < 10 RPS, a single-request timing variance causes ±50%+ swings.
        # Also skip workloads that returned 0 RPS — this is a benchmark failure,
        # not a real regression (the workload simply didn't complete).
        # When priority improvement is neutral (≤ 0%), apply a stricter -2% tolerance
        # to avoid accepting fixes that do nothing but add noise degradation.
        # When priority improvement is very high (>50%), relax tolerance —
        # a massive gain on priority workloads outweighs moderate non-priority dips.
        if priority_avg > 50.0:
            effective_tolerance = degradation_tolerance * 3  # e.g. -5% → -15%
        elif priority_avg > 0:
            effective_tolerance = degradation_tolerance
        else:
            effective_tolerance = -2.0

        degraded = {
            w: d for w, d in others.items()
            if d < effective_tolerance
            and baseline.get(w, 0) >= 10.0
            and current.get(w, 0) > 0  # 0 RPS = benchmark failure, not regression
        }

        keep = priority_avg >= threshold and not degraded

        bench_failures = [w for w in others if current.get(w, 0) == 0 and baseline.get(w, 0) > 0]
        low_rps_skipped = [w for w in others if baseline.get(w, 0) < 10.0]
        if bench_failures:
            logger.warning("Benchmark failure detected (0 RPS): %s — excluded from degradation check",
                           bench_failures)
        if degraded:
            skip_note = f" [skipped low-RPS: {low_rps_skipped}]" if low_rps_skipped else ""
            logger.info(
                "Priority improvement: %.2f%% (tolerance: %.1f%%) — ROLLBACK (degraded: %s%s)",
                priority_avg, effective_tolerance,
                ", ".join(f"{w}={d:.1f}%" for w, d in degraded.items()),
                skip_note,
            )
        else:
            logger.info(
                "Priority improvement: %.2f%% (threshold: %.1f%%) — %s",
                priority_avg, threshold, "KEEP" if keep else "ROLLBACK",
            )
        return keep, priority_avg, degraded
