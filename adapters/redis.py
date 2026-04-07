from __future__ import annotations

import re
from typing import Any

from adapters.base import BenchmarkResult, ServiceAdapter
from tools.ssh import SSHClient


class RedisAdapter(ServiceAdapter):
    def __init__(self, cfg: dict, ssh: SSHClient):
        self._cfg = cfg["service"]
        self._ssh = ssh

    def get_config(self) -> dict:
        result = self._ssh.execute(
            f"redis-cli CONFIG GETALL 2>/dev/null || cat {self._cfg['config_path']}"
        )
        return {"raw": result.stdout, "path": self._cfg["config_path"]}

    def apply_config(self, parameter: str, value: str) -> bool:
        # Try live CONFIG SET first, then fall back to file edit
        live = self._ssh.execute(f"redis-cli CONFIG SET {parameter} {value}")
        if live.ok:
            return True
        config_path = self._cfg["config_path"]
        sed_cmd = f"sed -i 's/^{parameter}.*/{parameter} {value}/' {config_path}"
        return self._ssh.execute(sed_cmd).ok

    def benchmark(self, duration: int = 30, url: str = "") -> BenchmarkResult:
        cmd = "redis-benchmark -t get,set -n 100000 -q 2>&1"
        result = self._ssh.execute(cmd, timeout=duration + 30)
        return _parse_redis_bench(result.stdout, duration)

    def get_metrics(self) -> dict:
        r = self._ssh.execute("redis-cli INFO stats 2>/dev/null")
        return {"redis_info": r.stdout.strip()}

    def get_logs(self, tail: int = 100) -> str:
        result = self._ssh.execute(
            f"tail -{tail} {self._cfg.get('log_path', '/var/log/redis/redis.log')}"
        )
        return result.stdout

    def reload(self) -> bool:
        result = self._ssh.execute(f"systemctl reload {self._cfg['systemd_unit']}")
        return result.ok

    def inspect(self, targets: dict[str, str]) -> dict[str, Any]:
        """Inspect Redis configuration against targets."""
        raw = self._ssh.execute("redis-cli CONFIG GET '*' 2>/dev/null", timeout=10).stdout

        current: dict[str, str] = {}
        lines = raw.splitlines()
        for i in range(0, len(lines) - 1, 2):
            current[lines[i].strip()] = lines[i + 1].strip()

        needs_fixing: dict[str, dict[str, str]] = {}
        already_ok: list[str] = []
        for param, target in targets.items():
            cur = current.get(param, "not set")
            if cur != target:
                needs_fixing[param] = {"current": cur, "target": target}
            else:
                already_ok.append(param)

        return {
            "category": "cache",
            "needs_fixing": needs_fixing,
            "ok_count": len(already_ok),
            "current": current,
        }

    def get_service_info(self) -> dict[str, str]:
        return {
            "process_name": "redis-server",
            "binary_path": "/usr/bin/redis-server",
            "systemd_unit": self._cfg.get("systemd_unit", "redis.service"),
            "config_path": self._cfg.get("config_path", "/etc/redis/redis.conf"),
        }

    def get_hypothesis_queue(self) -> list[dict]:
        return [
            {"name": "cpu_governor_performance", "priority": 1},
            {"name": "tcp_backlog_tuned", "priority": 1},
            {"name": "maxmemory_policy_tuned", "priority": 2},
            {"name": "io_threads_tuned", "priority": 2},
            {"name": "transparent_hugepages_disabled", "priority": 2},
            {"name": "tcp_nodelay_enabled", "priority": 3},
        ]


def _parse_redis_bench(output: str, duration: int) -> BenchmarkResult:
    # redis-benchmark -q outputs: "SET: 123456.00 requests per second"
    ops = re.findall(r"[\d.]+(?= requests per second)", output)
    avg = sum(float(o) for o in ops) / len(ops) if ops else 0.0
    return BenchmarkResult(
        requests_per_sec=avg,
        latency_p50_ms=0.0,
        latency_p99_ms=0.0,
        error_rate=0.0,
        duration_sec=duration,
    )
