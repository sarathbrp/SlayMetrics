"""Nginx service profile — all nginx-specific knowledge in one place."""

from __future__ import annotations

import json
from typing import Any

from services import ServiceProfile

# ── Expert prompt builder ────────────────────────────────────────────────


def build_expert_prompt(*, system_line: str, service_inspection: dict[str, Any]) -> str:
    return (
        "You are a High-Performance NGINX and RHEL 9 Kernel Optimization Expert. "
        "Your goal is to achieve 1.5M+ RPS on 112-core bare-metal hardware. "
        "Analyze the provided inspection evidence with a 'Full-Stack' perspective. "
        "You MUST identify and resolve bottlenecks in the interaction between "
        "NGINX and the RHEL network stack.\n\n"
        "CRITICAL MISSION PARAMETERS:\n"
        "1. NEUTRALIZE THROTTLES: If 'limit_rate', 'limit_req', or any "
        "bandwidth/request caps are detected or suspected, "
        "explicitly recommend setting them to '0' or removing them to clear "
        "the path for maximum throughput.\n"
        "2. KERNEL ALIGNMENT: Ensure 'net.core.somaxconn', 'tcp_max_syn_backlog', "
        "and 'tcp_max_tw_buckets' are set to "
        "at least 65535 (or higher for buckets) to prevent socket drops at 1M+ RPS.\n"
        "3. INVISIBLE OVERHEAD: Always check for SELinux status. If RPS is below 1M, "
        "recommend 'permissive' mode "
        "to eliminate syscall overhead. Ensure 'irqbalance' is aligned or NIC interrupts "
        "are pinned to the worker NUMA node.\n"
        "4. WORKER SCALING: Mandate 'worker_processes auto' and 'worker_cpu_affinity auto' "
        "to pin 112 workers "
        "to 112 cores, preventing L3 cache trashing and cross-socket UPI latency.\n"
        "5. SOCKET PACING: Recommend 'fq_codel' on the active NIC to stabilize "
        "RPS variance.\n\n"
        "Return strict JSON with keys: summary, rca_records, recommendations.\n\n"
        f"System: {system_line}\n"
        f"Service Inspection:\n{json.dumps(service_inspection, ensure_ascii=True)}"
    )


# ── Default config (for reset tool) ─────────────────────────────────────

DEFAULT_CONFIG = """\
user nginx;
worker_processes auto;
worker_rlimit_nofile 1024;
error_log /var/log/nginx/error.log;
pid /run/nginx.pid;

# Load dynamic modules. See /usr/share/doc/nginx/README.dynamic.
include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 1024;
}

http {
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';

        access_log /var/log/nginx/access.log main;

    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    keepalive_requests 100;
    types_hash_max_size 4096;
    client_body_buffer_size 8k;
    client_max_body_size 1m;

    aio off;

        gzip off;

        open_file_cache off;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    # Load modular configuration files from the /etc/nginx/conf.d directory.
    # See http://nginx.org/en/docs/ngx_core_module.html#include
    # for more information.
    include /etc/nginx/conf.d/*.conf;
}
"""

# ── Optimization groups (moved from core/lessons.py) ─────────────────────

OPTIMIZATION_GROUPS: dict[str, dict[str, Any]] = {
    "accept_path": {
        "description": "Connection admission and queue depth",
        "risk": "low",
        "params": (
            "webserver.worker_connections",
            "webserver.listen_backlog",
            "kernel.net.core.somaxconn",
            "kernel.net.ipv4.tcp_max_syn_backlog",
            "kernel.net.core.netdev_max_backlog",
        ),
    },
    "nginx_worker_model": {
        "description": "Worker parallelism and accept loop behavior",
        "risk": "low",
        "params": (
            "webserver.worker_processes",
            "webserver.worker_cpu_affinity",
            "webserver.multi_accept",
            "webserver.accept_mutex",
        ),
    },
    "http_connection_reuse": {
        "description": "Keepalive and request lifecycle tuning",
        "risk": "low",
        "params": (
            "webserver.keepalive_requests",
            "webserver.keepalive_timeout",
            "webserver.tcp_nodelay",
            "webserver.tcp_nopush",
            "webserver.reset_timedout_connection",
        ),
    },
    "fd_and_file_cache": {
        "description": "Descriptor limits and static file cache",
        "risk": "low",
        "params": (
            "webserver.worker_rlimit_nofile",
            "webserver.open_file_cache",
            "webserver.open_file_cache_valid",
            "webserver.open_file_cache_min_uses",
        ),
    },
    "logging_and_rate_limits": {
        "description": "Request-path overhead and throttling controls",
        "risk": "medium",
        "params": (
            "webserver.access_log",
            "webserver.error_log_level",
            "webserver.limit_req",
            "webserver.limit_conn",
            "webserver.limit_rate",
            "webserver.limit_rate_after",
        ),
    },
    "socket_buffers": {
        "description": "Socket buffer sizing for high-throughput connections",
        "risk": "medium",
        "params": (
            "kernel.net.core.rmem_max",
            "kernel.net.core.wmem_max",
            "kernel.net.core.rmem_default",
            "kernel.net.core.wmem_default",
            "kernel.net.ipv4.tcp_rmem",
            "kernel.net.ipv4.tcp_wmem",
        ),
    },
    "tcp_lifecycle": {
        "description": "TCP connection turnover and idle behavior",
        "risk": "medium",
        "params": (
            "kernel.net.ipv4.tcp_tw_reuse",
            "kernel.net.ipv4.tcp_fin_timeout",
            "kernel.net.ipv4.tcp_max_tw_buckets",
            "kernel.net.ipv4.tcp_slow_start_after_idle",
            "kernel.net.ipv4.tcp_max_orphans",
        ),
    },
    "memory_writeback": {
        "description": "Memory pressure and writeback tuning",
        "risk": "medium",
        "params": (
            "kernel.vm.swappiness",
            "kernel.vm.vfs_cache_pressure",
            "kernel.vm.dirty_ratio",
            "kernel.vm.dirty_background_ratio",
            "kernel.vm.dirty_expire_centisecs",
            "kernel.vm.dirty_writeback_centisecs",
        ),
    },
    "platform_latency": {
        "description": "Platform-level latency knobs",
        "risk": "high",
        "params": (
            "kernel.irqbalance",
            "kernel.transparent_hugepage",
            "kernel.cpu_governor",
            "kernel.selinux",
        ),
    },
}

# ── Degradation scenarios (for testing) ──────────────────────────────────

DEGRADE_SCENARIOS = [
    {
        "name": "cpu_governor_powersave",
        "hypothesis": "cpu_governor_performance",
        "degrade": "echo powersave | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
        "restore": "echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
        "verify": "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
    },
    {
        "name": "transparent_hugepages_always",
        "hypothesis": "transparent_hugepages_disabled",
        "degrade": "echo always > /sys/kernel/mm/transparent_hugepage/enabled",
        "restore": "echo never > /sys/kernel/mm/transparent_hugepage/enabled",
        "verify": "cat /sys/kernel/mm/transparent_hugepage/enabled",
    },
    {
        "name": "selinux_enforcing",
        "hypothesis": "selinux_tuned",
        "degrade": "setenforce 1 2>/dev/null || true",
        "restore": "setenforce 0 2>/dev/null || true",
        "verify": "getenforce 2>/dev/null || echo Disabled",
    },
    {
        "name": "low_somaxconn",
        "hypothesis": "net_somaxconn_backlog",
        "degrade": "sysctl -w net.core.somaxconn=128",
        "restore": "sysctl -w net.core.somaxconn=65535",
        "verify": "sysctl net.core.somaxconn",
    },
    {
        "name": "low_tcp_backlog",
        "hypothesis": "net_somaxconn_backlog",
        "degrade": (
            "sysctl -w net.ipv4.tcp_max_syn_backlog=128 && "
            "sysctl -w net.core.netdev_max_backlog=300"
        ),
        "restore": (
            "sysctl -w net.ipv4.tcp_max_syn_backlog=65535 && "
            "sysctl -w net.core.netdev_max_backlog=65535"
        ),
        "verify": "sysctl net.ipv4.tcp_max_syn_backlog net.core.netdev_max_backlog",
    },
    {
        "name": "nginx_worker_processes_1",
        "hypothesis": "worker_processes_match_cores",
        "degrade": (
            "sed -i 's/^worker_processes.*/worker_processes 1;/' "
            "/etc/nginx/nginx.conf && systemctl reload nginx"
        ),
        "restore": (
            "sed -i 's/^worker_processes.*/worker_processes auto;/' "
            "/etc/nginx/nginx.conf && systemctl reload nginx"
        ),
        "verify": "grep worker_processes /etc/nginx/nginx.conf",
    },
    {
        "name": "nginx_sendfile_off",
        "hypothesis": "sendfile_enabled",
        "degrade": (
            "sed -i 's/sendfile\\s\\+on;/sendfile off;/' "
            "/etc/nginx/nginx.conf && systemctl reload nginx"
        ),
        "restore": (
            "sed -i 's/sendfile\\s\\+off;/sendfile on;/' "
            "/etc/nginx/nginx.conf && systemctl reload nginx"
        ),
        "verify": "grep sendfile /etc/nginx/nginx.conf",
    },
    {
        "name": "nginx_tcp_nopush_off",
        "hypothesis": "tcp_nopush_nodelay",
        "degrade": (
            "sed -i 's/tcp_nopush\\s\\+on;/tcp_nopush off;/' /etc/nginx/nginx.conf && "
            "sed -i 's/tcp_nodelay\\s\\+on;/tcp_nodelay off;/' /etc/nginx/nginx.conf && "
            "systemctl reload nginx"
        ),
        "restore": (
            "sed -i 's/tcp_nopush\\s\\+off;/tcp_nopush on;/' /etc/nginx/nginx.conf && "
            "sed -i 's/tcp_nodelay\\s\\+off;/tcp_nodelay on;/' /etc/nginx/nginx.conf && "
            "systemctl reload nginx"
        ),
        "verify": "grep -E 'tcp_nopush|tcp_nodelay' /etc/nginx/nginx.conf",
    },
]

# ── Default tuning targets ───────────────────────────────────────────────

SERVICE_TARGETS = {
    "worker_processes": "auto",
    "worker_connections": "65536",
    "worker_rlimit_nofile": "524288",
    "worker_cpu_affinity": "auto",
    "sendfile": "on",
    "tcp_nopush": "on",
    "tcp_nodelay": "on",
    "multi_accept": "on",
    "accept_mutex": "off",
    "access_log": "off",
    "error_log_level": "warn",
    "open_file_cache": "max=200000 inactive=60s",
    "open_file_cache_valid": "30s",
    "open_file_cache_min_uses": "2",
    "keepalive_requests": "10000",
    "keepalive_timeout": "30",
    "reset_timedout_connection": "on",
    "listen_backlog": "65535",
    "gzip": "off",
    "gzip_comp_level": "1",
    "limit_rate": "0",
    "limit_rate_after": "0",
    "limit_req": "remove",
    "limit_conn": "remove",
    "directio": "off",
    "aio": "off",
    "output_buffers": "2 32k",
    "postpone_output": "0",
    "client_body_timeout": "60",
    "client_header_timeout": "60",
    "send_timeout": "60",
}


# ── Profile factory ─────────────────────────────────────────────────────


def get_profile() -> ServiceProfile:
    return ServiceProfile(
        name="nginx",
        type="webserver",
        process_name="nginx",
        binary_path="/usr/sbin/nginx",
        default_config=DEFAULT_CONFIG,
        service_targets=SERVICE_TARGETS,
        optimization_groups=OPTIMIZATION_GROUPS,
        degrade_scenarios=DEGRADE_SCENARIOS,
        eval_weights={"service": 0.4, "system": 0.4, "synthesizer": 0.2},
        expert_prompt_builder=build_expert_prompt,
    )
