from __future__ import annotations

import re

from adapters.base import BenchmarkResult, ServiceAdapter
from tools.ssh import SSHClient


class PostgresAdapter(ServiceAdapter):

    def __init__(self, cfg: dict, ssh: SSHClient):
        self._cfg = cfg["service"]
        self._bench_cfg = self._cfg["benchmark"]
        self._ssh = ssh

    def get_config(self) -> dict:
        result = self._ssh.execute(
            "psql -U postgres -c 'SHOW ALL;' 2>/dev/null || "
            f"cat {self._cfg['config_path']}"
        )
        return {"raw": result.stdout, "path": self._cfg["config_path"]}

    def apply_config(self, parameter: str, value: str) -> bool:
        config_path = self._cfg["config_path"]
        sed_cmd = (
            f"sed -i 's/^#\\?{parameter}\\s*=.*/{parameter} = {value}/' {config_path}"
        )
        result = self._ssh.execute(sed_cmd)
        return result.ok

    def benchmark(self, duration: int = 60, url: str = "") -> BenchmarkResult:
        args = self._bench_cfg.get("args", "-c10 -j2 -T60")
        cmd = f"pgbench {args} postgres 2>&1"
        result = self._ssh.execute(cmd, timeout=duration + 30)
        return _parse_pgbench(result.stdout, duration)

    def get_metrics(self) -> dict:
        r = self._ssh.execute(
            "psql -U postgres -c \"SELECT count(*) FROM pg_stat_activity;\" 2>/dev/null"
        )
        return {"pg_stat_activity": r.stdout.strip()}

    def get_logs(self, tail: int = 100) -> str:
        result = self._ssh.execute(
            f"tail -{tail} {self._cfg.get('log_path', '/var/log/postgresql/postgresql.log')}"
        )
        return result.stdout

    def reload(self) -> bool:
        result = self._ssh.execute(f"systemctl reload {self._cfg['systemd_unit']}")
        return result.ok

    def get_hypothesis_queue(self) -> list[dict]:
        return [
            {"name": "shared_buffers_tuned",       "priority": 1},
            {"name": "cpu_governor_performance",   "priority": 1},
            {"name": "max_connections_tuned",      "priority": 2},
            {"name": "work_mem_tuned",             "priority": 2},
            {"name": "effective_cache_size_tuned", "priority": 2},
            {"name": "checkpoint_tuned",           "priority": 3},
            {"name": "wal_buffers_tuned",          "priority": 3},
        ]


def _parse_pgbench(output: str, duration: int) -> BenchmarkResult:
    tps_m = re.search(r"tps\s*=\s*([\d.]+)", output)
    lat_m = re.search(r"latency average\s*=\s*([\d.]+)\s*ms", output)
    rps = float(tps_m.group(1)) if tps_m else 0.0
    p50 = float(lat_m.group(1)) if lat_m else 0.0
    return BenchmarkResult(
        requests_per_sec=rps,
        latency_p50_ms=p50,
        latency_p99_ms=0.0,
        error_rate=0.0,
        duration_sec=duration,
    )
