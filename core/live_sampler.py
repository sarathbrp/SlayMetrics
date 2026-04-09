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
        """Load CSV, downsample, compute deltas/peaks/trends → compact hypothesis."""
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

        # Cumulative deltas
        for col in _CUMULATIVE:
            if col not in df.columns:
                continue
            delta = float(df[col].iloc[-1] - df[col].iloc[0])
            if delta <= 0:
                continue
            label, unit, crit, high = _THRESHOLDS.get(col, (col, "", 1, 1))
            display = delta / 1_000_000 if unit == "s" else delta
            sev     = _severity(delta, crit, high)
            lines.append(f"[{sev:<8}] {label}: {display:>12,.1f}{unit}")

        # Instant peaks
        for col in _INSTANT:
            if col not in df.columns:
                continue
            peak = float(df[col].max())
            if peak <= 0:
                continue
            label, unit, crit, high = _THRESHOLDS.get(col, (col, "", 1e9, 1e9))
            sev  = _severity(peak, crit, high)
            lines.append(f"[{sev:<8}] {label}: {peak:>12,.0f}{unit}")

        # CPU busy = us + sy combined
        if "cpu_us" in df.columns and "cpu_sy" in df.columns:
            cpu_busy_peak = float((df["cpu_us"] + df["cpu_sy"]).max())
            sev = _severity(cpu_busy_peak, 90, 70)
            lines.append(f"[{sev:<8}] CPU_busy_peak (us+sy): {cpu_busy_peak:>8.0f}%")

        # Trend detection
        for col in ["rx_discards", "softnet_squeezed", "tcp_time_wait",
                    "cgroup_throttled_usec"]:
            if col not in df.columns:
                continue
            trend = _detect_trend(df[col])
            if trend != "stable":
                label, *_ = _THRESHOLDS.get(col, (col,))
                lines.append(f"[TREND   ] {label}: {trend}")

        logger.info("Live analysis: %d findings over %ds", len(lines) - 1, duration)
        return "\n".join(lines)
