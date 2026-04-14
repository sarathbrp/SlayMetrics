"""
LiveSampler — background thread that collects runtime metrics from the DUT
while a benchmark is running, saves to CSV, and analyzes into a compact
hypothesis summary for the LLM.
"""

import logging
import threading
import time
from pathlib import Path

import pandas as pd

from .config import Config
from .ssh import RemoteExecutor

logger = logging.getLogger("slayMetrics.live_sampler")

_LIVE_SCRIPT   = "live_audit.sh"
_CUMULATIVE    = ["softnet_dropped", "softnet_squeezed", "rx_discards",
                  "rx_errors", "cgroup_throttled_usec", "cgroup_nr_throttled"]
_INSTANT       = ["tcp_time_wait", "tcp_established", "cpu_us", "cpu_sy", "cpu_wa"]

# (label, unit, critical_threshold, high_threshold)
_THRESHOLDS: dict[str, tuple[str, str, float, float]] = {
    "softnet_dropped":     ("Softnet_Dropped_delta",       "",    1,       1),
    "softnet_squeezed":    ("Softnet_Squeezed_delta",       "", 1_000_000, 100_000),
    "rx_discards":         ("NIC_rx_discards_delta",        "",    1_000,   100),
    "rx_errors":           ("NIC_rx_errors_delta",          "",    100,     10),
    "cgroup_throttled_usec": ("Cgroup_throttle_sec",     "s",   1_000_000, 100_000),
    "cgroup_nr_throttled": ("Cgroup_nr_throttled_delta",    "",    10,      1),
    "tcp_time_wait":       ("TCP_TIME_WAIT_peak",           "",    50_000,  20_000),
    "tcp_established":     ("TCP_ESTABLISHED_peak",         "",    10_000,  5_000),
    "cpu_us":              ("CPU_user_peak",               "%",    80,      60),
    "cpu_sy":              ("CPU_sys_peak",                "%",    40,      20),
    "cpu_wa":              ("CPU_iowait_peak",             "%",    5,       2),
}


def _severity(value: float, critical: float, high: float) -> str:
    if value >= critical:
        return "CRITICAL"
    if value >= high:
        return "HIGH"
    if value > 0:
        return "ELEVATED"
    return "OK"


def _detect_trend(series: "pd.Series") -> str:
    """Return 'rising', 'falling', or 'stable' based on linear slope."""
    if len(series) < 4:
        return "stable"
    x = range(len(series))
    try:
        import numpy as np
        slope = np.polyfit(x, series.values, 1)[0]
        std   = series.std()
        if std == 0:
            return "stable"
        norm = slope / (std + 1e-9)
        if norm > 0.1:
            return "monotonic_rise"
        if norm < -0.1:
            return "monotonic_fall"
        return "stable"
    except Exception:
        return "stable"


class LiveSampler:
    """Collects per-second DUT metrics in a background thread during benchmarking."""

    def __init__(self, config: Config, scripts_dir: Path, remote_tmp: str,
                 executor_factory):
        self.config           = config
        self.scripts_dir      = scripts_dir
        self.remote_tmp       = remote_tmp
        self.executor_factory = executor_factory
        self._stop            = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, csv_path: Path) -> None:
        if not self.config.live_sampling_enabled:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(csv_path,), daemon=True,
        )
        self._thread.start()
        logger.info("Live sampler started (interval=%ds)", self.config.live_sampling_interval)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=15)
        self._thread = None
        logger.info("Live sampler stopped")

    def _loop(self, csv_path: Path) -> None:
        try:
            remote_script = f"{self.remote_tmp}/{_LIVE_SCRIPT}"
            with self.executor_factory() as executor:
                # Deploy script once
                local_script = self.scripts_dir / _LIVE_SCRIPT
                if local_script.exists():
                    executor.upload(local_script, remote_script)

                # Write CSV header
                header, _ = executor.run(f"bash {remote_script} --header", timeout=10)
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                with csv_path.open("w") as f:
                    f.write(header.strip() + "\n")

                # Sample loop
                interval = self.config.live_sampling_interval
                while not self._stop.is_set():
                    t0 = time.monotonic()
                    row, _ = executor.run(f"bash {remote_script}", timeout=10)
                    if row.strip():
                        with csv_path.open("a") as f:
                            f.write(row.strip() + "\n")
                    sleep = max(0.0, interval - (time.monotonic() - t0))
                    self._stop.wait(timeout=sleep)
        except Exception as e:
            logger.warning("Live sampler thread error: %s", e)

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def analyze(self, csv_path: Path) -> str:
        """Load CSV, downsample, compute deltas/peaks/trends → actionable hypothesis."""
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            return ""
        try:
            df = pd.read_csv(csv_path)
        except Exception as e:
            logger.warning("Live analysis — CSV read failed: %s", e)
            return ""

        if df.empty or len(df) < 2:
            return ""

        # Downsample to max_samples
        max_s = self.config.live_sampling_max_samples
        if len(df) > max_s:
            step = max(1, len(df) // max_s)
            df   = df.iloc[::step].reset_index(drop=True)

        duration = int(df["ts"].iloc[-1] - df["ts"].iloc[0]) if "ts" in df.columns else 0
        lines = [f"=== Live Benchmark Analysis ({len(df)} samples over {duration}s) ==="]
        lines.append("Findings below are from DURING the benchmark — they show behavior under load.\n")

        # --- Cgroup throttle (most critical — check first) ---
        cg_throttle_delta = self._delta(df, "cgroup_throttled_usec")
        cg_nr_delta = self._delta(df, "cgroup_nr_throttled")
        if cg_throttle_delta > 0:
            secs = cg_throttle_delta / 1_000_000
            sev = _severity(cg_throttle_delta, 1_000_000, 100_000)
            lines.append(
                f"[{sev:<8}] Cgroup CPU throttle: {secs:.1f}s total ({cg_nr_delta:,.0f} events)"
                f" → CPUQuota is actively limiting nginx. Remove CPUQuota to unlock CPU."
            )

        # --- CPU utilization ---
        if "cpu_us" in df.columns and "cpu_sy" in df.columns:
            cpu_busy_peak = float((df["cpu_us"] + df["cpu_sy"]).max())
            cpu_busy_avg = float((df["cpu_us"] + df["cpu_sy"]).mean())
            if cpu_busy_peak < 10:
                lines.append(
                    f"[WARNING ] CPU busy peak only {cpu_busy_peak:.0f}% (avg {cpu_busy_avg:.0f}%)"
                    f" during benchmark → nginx is severely underutilizing CPU."
                    f" Check worker_processes count and CPUQuota."
                )
            elif cpu_busy_peak > 90:
                lines.append(
                    f"[CRITICAL] CPU saturated: peak {cpu_busy_peak:.0f}% (avg {cpu_busy_avg:.0f}%)"
                    f" → CPU is the bottleneck. Consider more cores or optimizing per-request CPU."
                )
            elif cpu_busy_peak > 70:
                lines.append(
                    f"[HIGH    ] CPU busy peak {cpu_busy_peak:.0f}% (avg {cpu_busy_avg:.0f}%)"
                    f" → approaching CPU saturation."
                )

        # --- Softnet squeeze ---
        sqz_delta = self._delta(df, "softnet_squeezed")
        if sqz_delta > 100_000:
            lines.append(
                f"[HIGH    ] Softnet squeeze: {sqz_delta:,.0f} events"
                f" → kernel softirq budget exhausted. Check irqbalance and NIC IRQ affinity."
            )
        elif sqz_delta > 0 and sqz_delta < 100:
            lines.append(
                f"[INFO    ] Softnet squeeze: {sqz_delta:,.0f} (low)"
                f" → NIC not under pressure, likely because nginx RPS is too low to stress it."
            )

        # --- Softnet drops ---
        drop_delta = self._delta(df, "softnet_dropped")
        if drop_delta > 0:
            lines.append(
                f"[CRITICAL] Softnet drops: {drop_delta:,.0f} packets lost"
                f" → kernel dropping packets before they reach nginx. Check netdev_max_backlog."
            )

        # --- NIC discards ---
        disc_delta = self._delta(df, "rx_discards")
        if disc_delta > 100:
            lines.append(
                f"[HIGH    ] NIC rx_discards: {disc_delta:,.0f}"
                f" → packets dropped at NIC level. Increase NIC ring buffers (ethtool -G)."
            )

        # --- NIC errors ---
        err_delta = self._delta(df, "rx_errors")
        if err_delta > 10:
            lines.append(
                f"[HIGH    ] NIC rx_errors: {err_delta:,.0f}"
                f" → NIC receiving malformed frames. Check cable/NIC health."
            )

        # --- TCP state ---
        if "tcp_time_wait" in df.columns:
            tw_peak = float(df["tcp_time_wait"].max())
            if tw_peak > 50_000:
                lines.append(
                    f"[CRITICAL] TCP TIME_WAIT peak: {tw_peak:,.0f}"
                    f" → port exhaustion risk. Enable tcp_tw_reuse=2, widen ip_local_port_range."
                )
            elif tw_peak > 20_000:
                lines.append(
                    f"[HIGH    ] TCP TIME_WAIT peak: {tw_peak:,.0f}"
                    f" → elevated connection churn. Check keepalive settings."
                )
            elif tw_peak > 1_000:
                lines.append(
                    f"[INFO    ] TCP TIME_WAIT peak: {tw_peak:,.0f} → within normal range."
                )

        if "tcp_established" in df.columns:
            est_peak = float(df["tcp_established"].max())
            if est_peak > 10_000:
                lines.append(
                    f"[HIGH    ] TCP ESTABLISHED peak: {est_peak:,.0f}"
                    f" → high concurrency. Verify worker_connections and LimitNOFILE."
                )

        # --- Trends ---
        for col, hypothesis in [
            ("cgroup_throttled_usec", "cgroup throttle accumulating → CPUQuota actively limiting"),
            ("rx_discards", "NIC discards increasing → ring buffers filling up under load"),
            ("softnet_squeezed", "softirq budget exhaustion worsening over time"),
        ]:
            if col not in df.columns:
                continue
            trend = _detect_trend(df[col])
            if trend == "monotonic_rise":
                label, *_ = _THRESHOLDS.get(col, (col,))
                lines.append(f"[TREND   ] {label}: {trend} → {hypothesis}")

        if len(lines) <= 2:
            lines.append("[OK      ] No significant issues detected during benchmark.")

        logger.info("Live analysis: %d findings over %ds", len(lines) - 2, duration)
        return "\n".join(lines)

    @staticmethod
    def _delta(df: "pd.DataFrame", col: str) -> float:
        """Compute cumulative delta for a column (last - first)."""
        if col not in df.columns:
            return 0.0
        return float(df[col].iloc[-1] - df[col].iloc[0])
