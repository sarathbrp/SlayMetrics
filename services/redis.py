"""Redis service profile — cache/datastore-specific tuning knowledge."""

from __future__ import annotations

import json
from typing import Any

from services import ServiceProfile


def build_expert_prompt(*, system_line: str, service_inspection: dict[str, Any]) -> str:
    return (
        "You are a High-Performance Redis and RHEL 9 Kernel Optimization Expert. "
        "Your goal is to maximize operations per second on bare-metal hardware. "
        "Analyze the provided inspection evidence and identify bottlenecks in the "
        "interaction between Redis and the RHEL network/memory subsystem.\n\n"
        "CRITICAL MISSION PARAMETERS:\n"
        "1. MEMORY POLICY: Ensure maxmemory-policy is appropriate for the workload "
        "(allkeys-lru for caches, noeviction for persistent stores).\n"
        "2. IO THREADS: Enable io-threads for multi-threaded I/O on high-core systems.\n"
        "3. TCP BACKLOG: Align tcp-backlog with net.core.somaxconn.\n"
        "4. THP DISABLED: Transparent huge pages must be disabled for Redis.\n"
        "5. PERSISTENCE TUNING: If AOF is enabled, tune appendfsync and "
        "no-appendfsync-on-rewrite.\n\n"
        "Return strict JSON with keys: summary, rca_records, recommendations.\n\n"
        f"System: {system_line}\n"
        f"Service Inspection:\n{json.dumps(service_inspection, ensure_ascii=True)}"
    )


DEFAULT_CONFIG = """\
# Redis default configuration
bind 127.0.0.1
port 6379
tcp-backlog 511
maxmemory-policy noeviction
appendonly no
"""

OPTIMIZATION_GROUPS: dict[str, dict[str, Any]] = {
    "memory_policy": {
        "description": "Memory management and eviction",
        "risk": "low",
        "params": (
            "service.maxmemory",
            "service.maxmemory-policy",
            "service.maxmemory-samples",
        ),
    },
    "io_threading": {
        "description": "Multi-threaded I/O for high-core systems",
        "risk": "medium",
        "params": (
            "service.io-threads",
            "service.io-threads-do-reads",
        ),
    },
    "network_tuning": {
        "description": "Network stack alignment",
        "risk": "low",
        "params": (
            "service.tcp-backlog",
            "service.tcp-keepalive",
            "kernel.net.core.somaxconn",
        ),
    },
}

SERVICE_TARGETS = {
    "tcp-backlog": "65535",
    "maxmemory-policy": "allkeys-lru",
    "io-threads": "4",
    "io-threads-do-reads": "yes",
    "tcp-keepalive": "300",
    "hz": "100",
}

DEGRADE_SCENARIOS = [
    {
        "name": "low_tcp_backlog",
        "hypothesis": "tcp_backlog_tuned",
        "degrade": "redis-cli CONFIG SET tcp-backlog 128",
        "restore": "redis-cli CONFIG SET tcp-backlog 65535",
        "verify": "redis-cli CONFIG GET tcp-backlog",
    },
]


def get_profile() -> ServiceProfile:
    return ServiceProfile(
        name="redis",
        type="cache",
        process_name="redis-server",
        binary_path="/usr/bin/redis-server",
        default_config=DEFAULT_CONFIG,
        service_targets=SERVICE_TARGETS,
        optimization_groups=OPTIMIZATION_GROUPS,
        degrade_scenarios=DEGRADE_SCENARIOS,
        eval_weights={"service": 0.4, "system": 0.4, "synthesizer": 0.2},
        expert_prompt_builder=build_expert_prompt,
    )
