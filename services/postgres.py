"""PostgreSQL service profile — database-specific tuning knowledge."""

from __future__ import annotations

import json
from typing import Any

from services import ServiceProfile


def build_expert_prompt(*, system_line: str, service_inspection: dict[str, Any]) -> str:
    return (
        "You are a High-Performance PostgreSQL and RHEL 9 Kernel Optimization Expert. "
        "Your goal is to maximize transaction throughput (TPS) on bare-metal hardware. "
        "Analyze the provided inspection evidence and identify bottlenecks in the "
        "interaction between PostgreSQL and the RHEL storage/memory subsystem.\n\n"
        "CRITICAL MISSION PARAMETERS:\n"
        "1. MEMORY ALLOCATION: Ensure shared_buffers is 25% of RAM, "
        "effective_cache_size is 75% of RAM.\n"
        "2. WAL OPTIMIZATION: Tune wal_buffers, checkpoint_completion_target, "
        "and max_wal_size for write-heavy workloads.\n"
        "3. CONNECTION HANDLING: Optimize max_connections and work_mem based on "
        "available memory and expected concurrency.\n"
        "4. I/O TUNING: Align random_page_cost with storage type (SSD vs HDD), "
        "tune effective_io_concurrency.\n"
        "5. KERNEL ALIGNMENT: Ensure vm.dirty_ratio, vm.dirty_background_ratio, "
        "and huge pages are tuned for database workloads.\n\n"
        "Return strict JSON with keys: summary, rca_records, recommendations.\n\n"
        f"System: {system_line}\n"
        f"Service Inspection:\n{json.dumps(service_inspection, ensure_ascii=True)}"
    )


DEFAULT_CONFIG = """\
# PostgreSQL default configuration
shared_buffers = 128MB
work_mem = 4MB
maintenance_work_mem = 64MB
effective_cache_size = 4GB
max_connections = 100
wal_buffers = -1
checkpoint_completion_target = 0.9
"""

OPTIMIZATION_GROUPS: dict[str, dict[str, Any]] = {
    "memory_allocation": {
        "description": "Shared memory and cache sizing",
        "risk": "low",
        "params": (
            "service.shared_buffers",
            "service.effective_cache_size",
            "service.work_mem",
            "service.maintenance_work_mem",
        ),
    },
    "wal_tuning": {
        "description": "Write-ahead log performance",
        "risk": "medium",
        "params": (
            "service.wal_buffers",
            "service.max_wal_size",
            "service.min_wal_size",
            "service.checkpoint_completion_target",
        ),
    },
    "connection_handling": {
        "description": "Connection pool and concurrency",
        "risk": "low",
        "params": (
            "service.max_connections",
            "service.superuser_reserved_connections",
        ),
    },
    "io_tuning": {
        "description": "Storage I/O alignment",
        "risk": "medium",
        "params": (
            "service.random_page_cost",
            "service.effective_io_concurrency",
            "service.seq_page_cost",
        ),
    },
}

SERVICE_TARGETS = {
    "shared_buffers": "4GB",
    "effective_cache_size": "12GB",
    "work_mem": "64MB",
    "maintenance_work_mem": "512MB",
    "max_connections": "200",
    "wal_buffers": "64MB",
    "checkpoint_completion_target": "0.9",
    "max_wal_size": "2GB",
    "random_page_cost": "1.1",
    "effective_io_concurrency": "200",
}

DEGRADE_SCENARIOS = [
    {
        "name": "low_shared_buffers",
        "hypothesis": "shared_buffers_tuned",
        "degrade": "sed -i \"s/shared_buffers.*/shared_buffers = '32MB'/\" /var/lib/pgsql/data/postgresql.conf && systemctl restart postgresql",
        "restore": "sed -i \"s/shared_buffers.*/shared_buffers = '4GB'/\" /var/lib/pgsql/data/postgresql.conf && systemctl restart postgresql",
        "verify": "psql -U postgres -c 'SHOW shared_buffers;'",
    },
]


def get_profile() -> ServiceProfile:
    return ServiceProfile(
        name="postgres",
        type="database",
        process_name="postgres",
        binary_path="/usr/bin/postgres",
        default_config=DEFAULT_CONFIG,
        service_targets=SERVICE_TARGETS,
        optimization_groups=OPTIMIZATION_GROUPS,
        degrade_scenarios=DEGRADE_SCENARIOS,
        eval_weights={"service": 0.4, "system": 0.4, "synthesizer": 0.2},
        expert_prompt_builder=build_expert_prompt,
    )
