from __future__ import annotations

import re

from adapters.base import BenchmarkResult, ServiceAdapter
from tools.ssh import LocalClient, SSHClient

HTTP_DIRECTIVES = {
    "sendfile",
    "tcp_nopush",
    "tcp_nodelay",
    "keepalive_timeout",
    "keepalive_requests",
    "open_file_cache",
    "open_file_cache_valid",
    "open_file_cache_min_uses",
    "access_log",
    "gzip",
    "gzip_comp_level",
    "gzip_types",
    "reset_timedout_connection",
    "lingering_close",
    "lingering_timeout",
}


class NginxAdapter(ServiceAdapter):
    def __init__(self, cfg: dict, ssh: LocalClient | SSHClient):
        self._cfg = cfg["service"]
        self._bench_cfg = self._cfg["benchmark"]
        self._ssh = ssh

    def get_config(self) -> dict:
        result = self._ssh.execute(f"cat {self._cfg['config_path']}")
        return {"raw": result.stdout, "path": self._cfg["config_path"]}

    # Which block each directive belongs in
    MAIN_DIRECTIVES = {
        "worker_processes",
        "worker_rlimit_nofile",
        "worker_priority",
        "worker_cpu_affinity",
        "pid",
        "error_log",
    }
    EVENTS_DIRECTIVES = {
        "worker_connections",
        "use",
        "multi_accept",
    }
    # Directives that belong in server {} block
    SERVER_DIRECTIVES = {
        "listen",
        "server_name",
        "root",
    }
    # One source of truth for validation and placement.
    DIRECTIVE_SPECS = {
        **{name: {"context": "main", "indent": ""} for name in MAIN_DIRECTIVES},
        **{name: {"context": "events", "indent": "    "} for name in EVENTS_DIRECTIVES},
        **{name: {"context": "server", "indent": "    "} for name in SERVER_DIRECTIVES},
        **{name: {"context": "http", "indent": "    "} for name in HTTP_DIRECTIVES},
        "listen_backlog": {"context": "special", "indent": "    "},
    }
    ALLOWED_BATCH_DIRECTIVES = set(DIRECTIVE_SPECS)

    def apply_config(self, parameter: str, value: str) -> bool:
        # Handle listen_backlog specially — modifies existing listen directives
        if parameter == "listen_backlog":
            return self._set_listen_backlog(value)
        spec = self.DIRECTIVE_SPECS.get(parameter)
        if spec is None or spec["context"] == "special":
            return False
        block = spec["context"]
        if block == "server":
            return False  # server block directives need special handling

        config_path = self._cfg["config_path"]
        current = self._ssh.execute(f"cat {config_path}").stdout
        lines = current.splitlines()

        # Backup before modifying
        self._ssh.execute(f"cp {config_path} {config_path}.bak")

        new_directive = f"{spec['indent']}{parameter} {value};"
        updated = _upsert_directive_in_context(lines, parameter, new_directive, block)
        if updated is None:
            return False

        # Write to temp, test, then apply
        new_config = "\n".join(updated) + "\n"
        self._ssh.execute(
            f"cat > /tmp/nginx_new.conf << 'NGINX_CONF_EOF'\n{new_config}NGINX_CONF_EOF"
        )
        self._ssh.execute(f"cp /tmp/nginx_new.conf {config_path}")

        # Validate — rollback if broken
        test = self._ssh.execute("nginx -t 2>&1")
        if "syntax is ok" not in test.stdout and "test is successful" not in test.stdout:
            self._ssh.execute(f"cp {config_path}.bak {config_path}")
            return False

        return True

    def _set_listen_backlog(self, backlog: str) -> bool:
        """Add backlog= to listen directives inside server {} blocks only."""
        config_path = self._cfg["config_path"]
        current = self._ssh.execute(f"cat {config_path}").stdout
        self._ssh.execute(f"cp {config_path} {config_path}.bak")

        lines = current.splitlines()
        new_lines = []
        in_server = False
        brace_depth = 0

        for line in lines:
            stripped = line.strip()

            # Track server block depth
            if re.match(r"server\s*\{", stripped):
                in_server = True
                brace_depth = 0
            if in_server:
                brace_depth += stripped.count("{") - stripped.count("}")
                if brace_depth <= 0 and "}" in stripped:
                    in_server = False

            # Only modify listen inside server blocks.
            if in_server and re.match(r"listen\s+", stripped):
                line = _rewrite_listen_backlog_line(line, backlog)

            new_lines.append(line)

        new_config = "\n".join(new_lines) + "\n"
        self._ssh.execute(
            f"cat > /tmp/nginx_new.conf << 'NGINX_CONF_EOF'\n{new_config}NGINX_CONF_EOF"
        )
        self._ssh.execute(f"cp /tmp/nginx_new.conf {config_path}")

        # Validate — rollback if broken
        test = self._ssh.execute("nginx -t 2>&1")
        if "syntax is ok" not in test.stdout and "test is successful" not in test.stdout:
            self._ssh.execute(f"cp {config_path}.bak {config_path}")
            return False
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
            {"name": "sendfile_enabled", "priority": 1},
            {"name": "cpu_governor_performance", "priority": 1},
            {"name": "tcp_nopush_nodelay", "priority": 1},
            {"name": "worker_processes_match_cores", "priority": 1},
            {"name": "open_file_cache_enabled", "priority": 2},
            {"name": "transparent_hugepages_disabled", "priority": 2},
            {"name": "selinux_tuned", "priority": 2},
            {"name": "net_somaxconn_backlog", "priority": 2},
            {"name": "irq_affinity_tuned", "priority": 3},
            {"name": "numa_binding", "priority": 3},
            {"name": "filesystem_noatime", "priority": 3},
            {"name": "nic_offload_enabled", "priority": 3},
            {"name": "gzip_compression_tuned", "priority": 3},
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


def _rewrite_listen_backlog_line(line: str, backlog: str) -> str:
    """Rewrite one listen directive line to set backlog=<value>, preserving comments."""
    comment = ""
    hash_idx = line.find("#")
    if hash_idx != -1:
        comment = line[hash_idx:]
        line = line[:hash_idx]

    m = re.match(r"^(\s*listen\s+)([^;]*)(;\s*)?$", line.rstrip())
    if not m:
        return line + (f" {comment}" if comment else "")

    prefix = m.group(1)
    args = m.group(2).strip()

    parts = [p for p in args.split() if not p.startswith("backlog=")]
    parts.append(f"backlog={backlog}")
    rewritten = f"{prefix}{' '.join(parts)};"

    if comment:
        rewritten = f"{rewritten} {comment}"
    return rewritten


def _upsert_directive_in_context(
    lines: list[str], parameter: str, new_directive: str, context: str
) -> list[str] | None:
    pattern = re.compile(rf"^\s*{re.escape(parameter)}\s+[^;]*;")
    new_lines: list[str] = []
    stack: list[str] = []
    block_open_idx: int | None = None
    seen = False

    for line in lines:
        stripped = line.strip()
        current_scope = _scope_name(stack)

        if current_scope == context and pattern.match(line):
            if not seen:
                new_lines.append(new_directive)
                seen = True
            continue

        new_lines.append(line)

        if context in {"http", "events"} and block_open_idx is None:
            block_name = _block_open_name(stripped)
            if block_name == context and current_scope == "main":
                block_open_idx = len(new_lines) - 1

        _update_block_stack(stack, stripped)

    if seen:
        return new_lines

    if context == "main":
        insert_idx = 0
        for idx, line in enumerate(new_lines):
            if re.match(r"^(worker_processes|pid|error_log)\s", line):
                insert_idx = idx + 1
        new_lines.insert(insert_idx, new_directive)
        return new_lines

    if block_open_idx is None:
        return None

    new_lines.insert(block_open_idx + 1, new_directive)
    return new_lines


def _scope_name(stack: list[str]) -> str:
    if not stack:
        return "main"
    if stack == ["http"]:
        return "http"
    if stack == ["events"]:
        return "events"
    return stack[-1]


def _block_open_name(stripped: str) -> str | None:
    match = re.match(r"^(events|http|server|location)\b[^{;]*\{", stripped)
    if not match:
        return None
    return match.group(1)


def _update_block_stack(stack: list[str], stripped: str) -> None:
    block_name = _block_open_name(stripped)
    if block_name:
        stack.append(block_name)

    for _ in range(stripped.count("}")):
        if stack:
            stack.pop()


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
