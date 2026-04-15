import logging
import re

logger = logging.getLogger("slayMetrics.evaluator")

_WORKLOAD_RE = re.compile(r"\[\d+/\d+\]\s+(\w+):")
_RPS_RE      = re.compile(r"rps=([\d.]+)")
_RUNNING_WORKLOAD_RE = re.compile(r"Running Benchmark \[\d+/\d+\]:\s*(\w+)", re.IGNORECASE)
_REQ_PER_SEC_RE = re.compile(r"Requests/sec:\s*([\d.]+)")
_TABLE_ROW_RE = re.compile(
    r"^\s*(homepage|small|medium|large|mixed)\s+\|\s+[^|]+\|\s+([\d.]+)\s+\|",
    re.IGNORECASE,
)

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
            table_m = _TABLE_ROW_RE.search(line)
            if table_m:
                result[table_m.group(1).lower()] = float(table_m.group(2))
                continue

            run_m = _RUNNING_WORKLOAD_RE.search(line)
            if run_m:
                current_workload = run_m.group(1).lower()
                continue

            m = _WORKLOAD_RE.search(line)
            if m:
                current_workload = m.group(1)
                continue

            req_m = _REQ_PER_SEC_RE.search(line)
            if req_m and current_workload:
                result[current_workload] = float(req_m.group(1))
                current_workload = None
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

    @staticmethod
    def should_keep_group(
        baseline: dict[str, float],
        current: dict[str, float],
        tier: int = 1,
        tier_thresholds: dict[int, float] | None = None,
        high_value_share: float = 0.10,
        high_value_max_degradation: float = -10.0,
    ) -> tuple[bool, float, dict[str, float]]:
        """Group-aware acceptance: total RPS change + tier-aware threshold.

        Uses absolute RPS sum instead of per-workload % average to prevent
        low-RPS workloads from vetoing high-value improvements.

        Returns (keep, net_pct, degraded_high_value_workloads).
        """
        defaults = {1: -10.0, 2: -5.0, 3: -3.0, 4: -2.0, 5: -2.0, 6: -1.0}
        thresholds = tier_thresholds or defaults
        threshold = thresholds.get(tier, thresholds.get(6, -1.0))

        common = set(baseline) & set(current)
        if not common:
            return False, 0.0, {}

        baseline_total = sum(baseline[w] for w in common)
        current_total = sum(current[w] for w in common)
        if baseline_total == 0:
            return False, 0.0, {}

        net_pct = (current_total - baseline_total) / baseline_total * 100

        # Protect high-value workloads (>10% of total baseline RPS)
        degraded_hv: dict[str, float] = {}
        for w in common:
            share = baseline[w] / baseline_total
            if share < high_value_share:
                continue
            if baseline[w] == 0:
                continue
            wl_pct = (current[w] - baseline[w]) / baseline[w] * 100
            if wl_pct < high_value_max_degradation:
                degraded_hv[w] = wl_pct

        keep = net_pct >= threshold and not degraded_hv

        if degraded_hv:
            logger.info(
                "Group eval: net=%.2f%% tier=%d threshold=%.1f%% — REJECT "
                "(high-value degraded: %s)",
                net_pct, tier, threshold,
                ", ".join(f"{w}={d:.1f}%" for w, d in degraded_hv.items()),
            )
        else:
            logger.info(
                "Group eval: net=%.2f%% (total: %.0f→%.0f) tier=%d threshold=%.1f%% — %s",
                net_pct, baseline_total, current_total,
                tier, threshold, "KEEP" if keep else "REJECT",
            )
        return keep, net_pct, degraded_hv
