from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

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
    "gzip_min_length",
    "reset_timedout_connection",
    "lingering_close",
    "lingering_timeout",
    "aio",
    "directio",
    "limit_rate",
    "limit_rate_after",
    "output_buffers",
    "postpone_output",
    "client_body_buffer_size",
    "client_max_body_size",
    "client_body_timeout",
    "client_header_timeout",
    "send_timeout",
}


class NginxAdapter(ServiceAdapter):
    def __init__(
        self, cfg: dict, ssh: LocalClient | SSHClient, bench: LocalClient | SSHClient | None = None
    ):
        self._cfg = cfg["service"]
        self._bench_cfg = self._cfg["benchmark"]
        self._ssh = ssh  # DUT — config changes, sar monitoring
        self._bench = bench or ssh  # Bench node — wrk2 runs here

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
        "accept_mutex",
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

    def _allow_confd_mutation(self) -> bool:
        """Allow broad conf.d rewrites only when explicitly enabled."""
        return bool(self._cfg.get("allow_confd_mutation", False))

    def apply_config(self, parameter: str, value: str) -> bool:
        # Handle listen_backlog specially — modifies existing listen directives
        if parameter == "listen_backlog":
            return self._set_listen_backlog(value)
        # Handle error_log_level — changes the level arg of existing error_log
        if parameter == "error_log_level":
            return self._set_error_log_level(value)
        # Handle limit_req/limit_conn removal — remove from conf.d + main config
        if parameter in ("limit_req", "limit_conn") and value == "remove":
            return self._remove_rate_limiting(parameter)
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
            import sys

            print(
                f"[nginx_adapter] FAILED upsert {parameter}={value} in {block} "
                f"(config {len(lines)} lines)",
                file=sys.stderr,
            )
            return False

        # Write to temp, test, then apply
        new_config = "\n".join(updated) + "\n"
        self._ssh.execute(
            f"cat > /tmp/nginx_new.conf << 'NGINX_CONF_EOF'\n{new_config}NGINX_CONF_EOF"
        )
        self._ssh.execute(f"cp /tmp/nginx_new.conf {config_path}")

        # Optional legacy behavior: strip matching directives from conf.d.
        # Disabled by default because broad sed-based cleanup can remove user-owned config.
        if block == "http" and self._allow_confd_mutation():
            self._remove_from_confd(parameter)

        # Validate — rollback if broken
        test = self._ssh.execute("nginx -t 2>&1")
        if "syntax is ok" not in test.stdout and "test is successful" not in test.stdout:
            import sys

            print(
                f"[nginx_adapter] FAILED nginx -t for {parameter}={value}: {test.stdout[:200]}",
                file=sys.stderr,
            )
            self._ssh.execute(f"cp {config_path}.bak {config_path}")
            return False

        return True

    def _remove_from_confd(self, parameter: str) -> None:
        """Remove a directive from all conf.d/*.conf files (server block overrides)."""
        self._ssh.execute(
            f"sed -i '/^[[:space:]]*{parameter}[[:space:]]/d'"
            " /etc/nginx/conf.d/*.conf 2>/dev/null || true"
        )

    def _set_error_log_level(self, level: str) -> bool:
        """Change the error_log level without changing the file path."""
        config_path = self._cfg["config_path"]
        self._ssh.execute(f"cp {config_path} {config_path}.bak")
        # Replace error_log line: keep path, change level
        self._ssh.execute(
            f"sed -i -E 's|^(error_log\\s+\\S+)\\s+\\S+;|\\1 {level};|' {config_path}"
        )
        # Also fix in conf.d when explicitly allowed.
        if self._allow_confd_mutation():
            self._ssh.execute(
                f"sed -i -E 's|^(\\s*error_log\\s+\\S+)\\s+\\S+;|\\1 {level};|'"
                " /etc/nginx/conf.d/*.conf 2>/dev/null || true"
            )
        test = self._ssh.execute("nginx -t 2>&1")
        if "syntax is ok" not in test.stdout and "test is successful" not in test.stdout:
            self._ssh.execute(f"cp {config_path}.bak {config_path}")
            return False
        return True

    def _remove_rate_limiting(self, directive: str) -> bool:
        """Remove limit_req/limit_conn zones and directives from all configs."""
        config_path = self._cfg["config_path"]
        self._ssh.execute(f"cp {config_path} {config_path}.bak")
        # Remove zone definitions and directive usage from main config
        for pattern in (
            f"{directive}_zone",
            f"{directive} ",
        ):
            self._ssh.execute(f"sed -i '/{pattern}/d' {config_path} 2>/dev/null || true")
        # Also remove from conf.d files only when explicitly allowed.
        if self._allow_confd_mutation():
            for pattern in (
                f"{directive}_zone",
                f"{directive} ",
            ):
                self._ssh.execute(
                    f"sed -i '/{pattern}/d' /etc/nginx/conf.d/*.conf 2>/dev/null || true"
                )
        test = self._ssh.execute("nginx -t 2>&1")
        if "syntax is ok" not in test.stdout and "test is successful" not in test.stdout:
            self._ssh.execute(f"cp {config_path}.bak {config_path}")
            return False
        return True

    def _set_listen_backlog(self, backlog: str) -> bool:
        """Add backlog= to listen directives in main config and conf.d files."""
        config_path = self._cfg["config_path"]
        # Process main config and all conf.d files
        conf_files = [config_path]
        confd_list = self._ssh.execute("ls /etc/nginx/conf.d/*.conf 2>/dev/null")
        if confd_list.ok and confd_list.stdout.strip():
            for f in confd_list.stdout.strip().splitlines():
                f = f.strip()
                if f.endswith(".conf") and f not in conf_files:
                    conf_files.append(f)

        for conf_file in conf_files:
            current = self._ssh.execute(f"cat {conf_file}").stdout
            self._ssh.execute(f"cp {conf_file} {conf_file}.bak")

            lines = current.splitlines()
            new_lines = []
            modified = False
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

                if in_server and re.match(r"listen\s+", stripped):
                    rewritten = _rewrite_listen_backlog_line(line, backlog)
                    if rewritten != line:
                        modified = True
                    new_lines.append(rewritten)
                else:
                    new_lines.append(line)

            if modified:
                new_config = "\n".join(new_lines) + "\n"
                self._ssh.execute(
                    f"cat > /tmp/nginx_backlog.conf << 'NGINX_CONF_EOF'\n{new_config}NGINX_CONF_EOF"
                )
                self._ssh.execute(f"cp /tmp/nginx_backlog.conf {conf_file}")

        # Validate — rollback all if broken
        test = self._ssh.execute("nginx -t 2>&1")
        if "syntax is ok" not in test.stdout and "test is successful" not in test.stdout:
            for conf_file in conf_files:
                self._ssh.execute(f"cp {conf_file}.bak {conf_file} 2>/dev/null || true")
            return False
        return True

    def benchmark(self, duration: int = 30, url: str = "") -> BenchmarkResult:
        target_url = url or self._bench_cfg.get("small_file_url", "http://localhost/")
        if self._bench_cfg.get("tool") == "hackathon":
            return self._benchmark_hackathon(target_url)

        threads = self._bench_cfg.get("threads", 4)
        connections = self._bench_cfg.get("connections", 400)
        rate = self._bench_cfg.get("rate", 150000)

        # Start sar on DUT for resource monitoring
        self._ssh.execute(f"sar -u {duration} 1 > /tmp/slay_sar.log 2>&1 &")

        # Run wrk2 on bench node (may be same machine or separate)
        wrk_cmd = f"wrk2 -t{threads} -c{connections} -d{duration}s -R{rate} --latency {target_url}"
        result = self._bench.execute(wrk_cmd, timeout=duration + 60)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"wrk2 benchmark failed: {detail}")
        bench = _parse_wrk2(result.stdout, duration, target_url)
        if "Requests/sec:" not in result.stdout:
            detail = result.stderr.strip() or result.stdout.strip() or "missing Requests/sec output"
            raise RuntimeError(f"wrk2 benchmark output was not parseable: {detail}")

        # Collect resource data from DUT
        sar_result = self._ssh.execute("sleep 2; cat /tmp/slay_sar.log 2>/dev/null")
        bench.cpu_pct = _parse_sar_avg(sar_result.stdout)

        mem_result = self._ssh.execute("free -m 2>/dev/null | grep Mem")
        bench.mem_mb = _parse_free_used(mem_result.stdout)

        return bench

    def _benchmark_hackathon(self, target_url: str) -> BenchmarkResult:
        script = self._bench_cfg.get("script", "/root/hackathon-tools/benchmark.sh")
        name = self._bench_cfg.get("contestant_name", "slaymetrics")
        target_env = self._bench_cfg.get("target_host_env", "DUT_HOST")
        target_host = os.environ.get(target_env, target_url)
        workload = _workload_from_url(target_url, self._bench_cfg)
        contestant = f"{name}-agent-{workload}-{uuid.uuid4().hex[:8]}"

        cmd = f"TARGET_HOST={target_host} {script} {contestant}"
        result = self._bench.execute(cmd, timeout=600)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"hackathon benchmark failed: {detail}")

        json_path = f"/root/hackathon-results/{contestant}_{workload}.json"
        payload = self._bench.execute(f"cat {json_path} 2>/dev/null")
        if not payload.ok or not payload.stdout.strip():
            raise RuntimeError(f"hackathon benchmark result missing for workload {workload}")

        return _parse_hackathon_result(payload.stdout, target_url)

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

    def validate_config(self) -> bool:
        test = self._ssh.execute("nginx -t 2>&1")
        return "syntax is ok" in test.stdout or "test is successful" in test.stdout

    def restart(self) -> bool:
        result = self._ssh.execute(f"systemctl restart {self._cfg['systemd_unit']} 2>&1")
        return result.ok

    def inspect(self, targets: dict[str, str]) -> dict[str, Any]:
        """Inspect nginx configuration against targets."""
        raw = self._ssh.execute("nginx -T 2>/dev/null", timeout=10).stdout

        current: dict[str, str] = {}
        needs_fixing: dict[str, dict[str, str]] = {}
        already_ok: list[str] = []

        for param, target in targets.items():
            if param == "listen_backlog":
                match = re.search(r"listen\s+.*backlog=(\d+)", raw)
                current[param] = match.group(1) if match else "not set"
            elif param == "error_log_level":
                match = re.search(r"error_log\s+\S+\s+(\w+)\s*;", raw)
                current[param] = match.group(1) if match else "warn"
            elif param == "worker_processes":
                match = re.search(r"worker_processes\s+(\S+)\s*;", raw)
                current[param] = match.group(1).rstrip(";") if match else "auto"
            elif param == "limit_rate":
                match = re.search(r"limit_rate\s+(\S+)\s*;", raw)
                current[param] = match.group(1).rstrip(";") if match else "0"
            elif param == "directio":
                match = re.search(r"directio\s+(\S+)\s*;", raw)
                current[param] = match.group(1).rstrip(";") if match else "off"
            elif param == "gzip_comp_level":
                match = re.search(r"gzip_comp_level\s+(\S+)\s*;", raw)
                current[param] = match.group(1).rstrip(";") if match else "1"
            else:
                match = re.search(rf"^\s*{re.escape(param)}\s+(.+?);", raw, re.MULTILINE)
                current[param] = match.group(1).strip() if match else "not set"

            cur = current.get(param, "not set")
            if cur != target and cur != "not set":
                needs_fixing[param] = {"current": cur, "target": target}
            elif cur == "not set":
                needs_fixing[param] = {"current": "not set", "target": target}
            else:
                already_ok.append(param)

        return {
            "category": "webserver",
            "needs_fixing": needs_fixing,
            "ok_count": len(already_ok),
            "current": current,
        }

    def get_service_info(self) -> dict[str, str]:
        return {
            "process_name": "nginx",
            "binary_path": "/usr/sbin/nginx",
            "systemd_unit": self._cfg.get("systemd_unit", "nginx.service"),
            "config_path": self._cfg.get("config_path", "/etc/nginx/nginx.conf"),
        }

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


def _parse_hackathon_result(output: str, url: str) -> BenchmarkResult:
    data = json.loads(output)
    results = data.get("results", {})
    requests = results.get("requests", {})
    latency = results.get("latency", {})
    percentiles = latency.get("percentiles", {})
    return BenchmarkResult(
        requests_per_sec=float(requests.get("per_sec", 0) or 0),
        latency_p50_ms=_latency_to_ms(percentiles.get("p50", "0ms")),
        latency_p99_ms=_latency_to_ms(percentiles.get("p99", "0ms")),
        error_rate=0.0,
        duration_sec=int(results.get("duration", 0) or 0),
        url=url,
    )


def _latency_to_ms(value: str) -> float:
    text = str(value).strip()
    if text.endswith("us"):
        return float(text[:-2]) / 1000
    if text.endswith("ms"):
        return float(text[:-2])
    if text.endswith("s"):
        return float(text[:-1]) * 1000
    return float(text or 0)


def _workload_from_url(url: str, bench_cfg: dict) -> str:
    for workload in ("small", "medium", "large"):
        if url and url == bench_cfg.get(f"{workload}_file_url"):
            return workload
    return "homepage"


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

    # For http-level directives, also remove duplicates from server blocks
    # so the http-level value isn't overridden by a more specific scope.
    remove_from_children = context == "http"

    for line in lines:
        stripped = line.strip()
        current_scope = _scope_name(stack)

        if current_scope == context and pattern.match(line):
            if not seen:
                new_lines.append(new_directive)
                seen = True
            continue

        # Remove same directive from child scopes (e.g. server block)
        if remove_from_children and current_scope not in ("main", context) and pattern.match(line):
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
