from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class BenchmarkResult:
    requests_per_sec: float
    latency_p50_ms: float
    latency_p99_ms: float
    error_rate: float
    duration_sec: int
    url: str = ""
    payload_size: str = ""
    cpu_pct: float = 0.0
    mem_mb: float = 0.0

    def improvement_pct(self, baseline: "BenchmarkResult") -> float:
        if baseline.requests_per_sec == 0:
            return 0.0
        return (self.requests_per_sec - baseline.requests_per_sec) / baseline.requests_per_sec * 100


class ServiceAdapter(ABC):
    @abstractmethod
    def get_config(self) -> dict:
        """Read and return current service configuration as a dict."""

    @abstractmethod
    def apply_config(self, parameter: str, value: str) -> bool:
        """Apply a single config parameter change. Returns True on success."""

    @abstractmethod
    def benchmark(self, duration: int = 30, url: str = "") -> BenchmarkResult:
        """Run benchmark and return structured result."""

    @abstractmethod
    def get_metrics(self) -> dict:
        """Collect live service metrics."""

    @abstractmethod
    def get_logs(self, tail: int = 100) -> str:
        """Fetch recent service logs."""

    @abstractmethod
    def reload(self) -> bool:
        """Reload service to apply config changes. Returns True on success."""

    @abstractmethod
    def validate_config(self) -> bool:
        """Validate service configuration syntax without applying. Returns True if valid."""

    @abstractmethod
    def restart(self) -> bool:
        """Hard restart the service (not graceful reload). Returns True on success."""

    @abstractmethod
    def get_hypothesis_queue(self) -> list[dict]:
        """Return ordered list of {name, priority} hypotheses to test."""

    @abstractmethod
    def inspect(self, targets: dict[str, str]) -> dict[str, Any]:
        """Inspect current service configuration against targets.

        Returns {category, needs_fixing, ok_count, current}.
        """

    @abstractmethod
    def get_service_info(self) -> dict[str, str]:
        """Return service metadata: process_name, binary_path, systemd_unit, config_path."""
