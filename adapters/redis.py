from __future__ import annotations

import re

from adapters.base import BenchmarkResult, ServiceAdapter
from tools.ssh import SSHClient


class RedisAdapter(ServiceAdapter):

    def __init__(self, cfg: dict, ssh: SSHClient):
        self._cfg = cfg["service"]
        self._ssh = ssh

    def get_config(self) -> dict:
        result = self._ssh.execute(
            "redis-cli CONFIG GETALL 2>/dev/null || "
            f"cat {self._cfg['config_path']}"
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

    def get_hypothesis_queue(self) -> list[dict]:
        return [
            {"name": "cpu_governor_performance",  "priority": 1},
            {"name": "tcp_backlog_tuned",         "priority": 1},
            {"name": "maxmemory_policy_tuned",    "priority": 2},
            {"name": "io_threads_tuned",          "priority": 2},
            {"name": "transparent_hugepages_disabled", "priority": 2},
            {"name": "tcp_nodelay_enabled",       "priority": 3},
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
