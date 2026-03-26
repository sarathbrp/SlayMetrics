from __future__ import annotations

import re

from adapters.base import BenchmarkResult, ServiceAdapter
from tools.ssh import SSHClient


class NginxAdapter(ServiceAdapter):

    def __init__(self, cfg: dict, ssh: SSHClient):
        self._cfg = cfg["service"]
        self._bench_cfg = self._cfg["benchmark"]
        self._ssh = ssh

    def get_config(self) -> dict:
        result = self._ssh.execute(f"cat {self._cfg['config_path']}")
        return {"raw": result.stdout, "path": self._cfg["config_path"]}

    # Which block each directive belongs in
    MAIN_DIRECTIVES = {
        "worker_processes", "worker_rlimit_nofile", "worker_priority",
        "worker_cpu_affinity", "pid", "error_log",
    }
    EVENTS_DIRECTIVES = {
        "worker_connections", "use", "multi_accept",
    }
    # Not a real directive — needs special handling
    SKIP_DIRECTIVES = {"listen_backlog"}

    def apply_config(self, parameter: str, value: str) -> bool:
        if parameter in self.SKIP_DIRECTIVES:
            return False

        config_path = self._cfg["config_path"]
        current = self._ssh.execute(f"cat {config_path}").stdout
        lines = current.splitlines()

        # Determine block and indent
        if parameter in self.MAIN_DIRECTIVES:
            block = "main"
            indent = ""
        elif parameter in self.EVENTS_DIRECTIVES:
            block = "events"
            indent = "    "
        else:
            block = "http"
            indent = "    "

        new_directive = f"{indent}{parameter} {value};"

        # First: remove ALL existing occurrences of this directive (prevent duplicates)
        pattern = re.compile(rf"^\s*{re.escape(parameter)}\s+[^;]*;", re.MULTILINE)
        cleaned = [l for l in lines if not pattern.match(l)]

        # Find insertion point based on block
        insert_idx = None
        if block == "main":
            # Insert after the last main-level directive before events/http
            for i, line in enumerate(cleaned):
                if re.match(r"^(worker_processes|pid|error_log)\s", line):
                    insert_idx = i + 1
            if insert_idx is None:
                insert_idx = 0
        elif block == "events":
            for i, line in enumerate(cleaned):
                if re.match(r"^events\s*\{", line):
                    insert_idx = i + 1
                    break
        else:  # http
            for i, line in enumerate(cleaned):
                if re.match(r"^http\s*\{", line):
                    insert_idx = i + 1
                    break

        if insert_idx is not None:
            cleaned.insert(insert_idx, new_directive)

        # Write back
        new_config = "\n".join(cleaned) + "\n"
        # Use a temp file to avoid partial writes
        self._ssh.execute(f"cat > /tmp/nginx_new.conf << 'NGINX_CONF_EOF'\n{new_config}NGINX_CONF_EOF")
        self._ssh.execute(f"cp /tmp/nginx_new.conf {config_path}")

        return True

    def benchmark(self, duration: int = 30, url: str = "") -> BenchmarkResult:
        target_url = url or self._bench_cfg.get("small_file_url", "http://localhost/")
        threads = self._bench_cfg.get("threads", 4)
        connections = self._bench_cfg.get("connections", 100)
        rate = self._bench_cfg.get("rate", 2000)

        # Run wrk2 + resource monitoring in a single compound command
        cmd = (
            f"sar -u {duration} 1 > /tmp/slay_sar.log 2>&1 & "
            f"wrk2 -t{threads} -c{connections} -d{duration}s -R{rate} "
            f"--latency {target_url}; "
            f"wait; "
            f"echo '---SAR---'; cat /tmp/slay_sar.log 2>/dev/null; "
            f"echo '---MEM---'; free -m 2>/dev/null | grep Mem"
        )
        result = self._ssh.execute(cmd, timeout=duration + 60)
        stdout = result.stdout

        # Split wrk2 output from resource data
        wrk_output = stdout.split("---SAR---")[0] if "---SAR---" in stdout else stdout
        bench = _parse_wrk2(wrk_output, duration, target_url)

        # Parse resource usage (graceful fallback if sar not installed)
        if "---SAR---" in stdout:
            sar_section = stdout.split("---SAR---")[1].split("---MEM---")[0]
            bench.cpu_pct = _parse_sar_avg(sar_section)
        if "---MEM---" in stdout:
            mem_section = stdout.split("---MEM---")[1].strip()
            bench.mem_mb = _parse_free_used(mem_section)

        return bench

    def get_metrics(self) -> dict:
        metrics = {}
        r = self._ssh.execute(
            "curl -s http://localhost/nginx_status 2>/dev/null || echo 'stub_status not enabled'"
        )
        metrics["nginx_status"] = r.stdout.strip()
        r2 = self._ssh.execute("ss -s")
        metrics["socket_summary"] = r2.stdout.strip()
        return metrics

    def get_logs(self, tail: int = 100) -> str:
        result = self._ssh.execute(f"tail -{tail} {self._cfg['log_path']}")
        return result.stdout

    def reload(self) -> bool:
        test = self._ssh.execute("nginx -t")
        if not test.ok:
            return False
        result = self._ssh.execute(f"systemctl reload {self._cfg['systemd_unit']}")
        return result.ok

    def get_hypothesis_queue(self) -> list[dict]:
        return [
            {"name": "sendfile_enabled",            "priority": 1},
            {"name": "cpu_governor_performance",    "priority": 1},
            {"name": "tcp_nopush_nodelay",          "priority": 1},
            {"name": "worker_processes_match_cores","priority": 1},
            {"name": "open_file_cache_enabled",     "priority": 2},
            {"name": "transparent_hugepages_disabled","priority": 2},
            {"name": "selinux_tuned",               "priority": 2},
            {"name": "net_somaxconn_backlog",        "priority": 2},
            {"name": "irq_affinity_tuned",          "priority": 3},
            {"name": "numa_binding",                "priority": 3},
            {"name": "filesystem_noatime",          "priority": 3},
            {"name": "nic_offload_enabled",         "priority": 3},
            {"name": "gzip_compression_tuned",      "priority": 3},
        ]


def _parse_wrk2(output: str, duration: int, url: str) -> BenchmarkResult:
    rps = _extract(output, r"Requests/sec:\s+([\d.]+)")
    p50 = _extract_latency(output, "50%")
    p99 = _extract_latency(output, "99%")
    errors = _extract(output, r"Non-2xx.*?(\d+)")
    total = _extract(output, r"(\d+) requests in")
    error_rate = (errors / total * 100) if total else 0.0
    return BenchmarkResult(
        requests_per_sec=rps,
        latency_p50_ms=p50,
        latency_p99_ms=p99,
        error_rate=error_rate,
        duration_sec=duration,
        url=url,
    )


def _extract(text: str, pattern: str) -> float:
    m = re.search(pattern, text)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return 0.0


def _extract_latency(text: str, percentile: str) -> float:
    # wrk2 outputs latency distribution like: "  50.000%    1.23ms"
    pattern = rf"{re.escape(percentile)}\s+([\d.]+)(ms|us|s)"
    m = re.search(pattern, text)
    if not m:
        return 0.0
    value, unit = float(m.group(1)), m.group(2)
    if unit == "us":
        return value / 1000
    if unit == "s":
        return value * 1000
    return value


def _parse_sar_avg(sar_output: str) -> float:
    """Extract average CPU usage from sar output (last 'Average:' line)."""
    for line in reversed(sar_output.splitlines()):
        if "Average:" in line and "%idle" not in line:
            parts = line.split()
            try:
                idle = float(parts[-1])  # last column is %idle
                return round(100.0 - idle, 1)
            except (ValueError, IndexError):
                pass
    return 0.0


def _parse_free_used(mem_line: str) -> float:
    """Extract used MB from 'free -m | grep Mem' output."""
    parts = mem_line.split()
    try:
        return float(parts[2])  # 'used' column
    except (ValueError, IndexError):
        return 0.0
