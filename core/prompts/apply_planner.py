"""Prompt for the apply planner agent.

Converts synthesizer recommendations into a structured 5-category
apply plan with only allowed parameter names.
"""

import json
from typing import Any


def build(
    *,
    service_targets: dict[str, str],
    kernel_targets: dict[str, str],
    resource_limits_targets: dict[str, str],
    network_targets: dict[str, str],
    storage_targets: dict[str, str],
    recommendations: list[dict[str, Any]],
    resource_problems: list[Any],
    network_problems: list[Any],
    storage_problems: list[Any],
) -> str:
    return (
        "You are the Lead Performance Tuning Executor for a 112-core RHEL 9.7 "
        "environment. "
        "Your goal is to transform the synthesizer's recommendations into a final, "
        "high-performance "
        "configuration payload. You must be aggressive in clearing bottlenecks.\n\n"
        "THE 5 CATEGORY SCHEMA (use EXACTLY these param names and values):\n"
        '1. "webserver" — Service-specific directives. '
        f"Allowed: {', '.join(sorted(service_targets))}\n"
        "   ACTION PARAMS with EXACT values:\n"
        '     limit_req: "remove"  (removes all limit_req zones and directives)\n'
        '     limit_conn: "remove"  (removes all limit_conn zones and directives)\n'
        '     limit_rate: "0"  (disables rate limiting)\n'
        '     limit_rate_after: "0"  (disables rate limiting threshold)\n'
        '     access_log: "off"  (disables access logging)\n'
        '     error_log_level: "warn"  (reduces log verbosity)\n\n'
        '2. "kernel" — sysctl, THP, SELinux, CPU governor, IRQ. '
        f"Allowed: {', '.join(sorted(kernel_targets))}\n"
        "   ACTION PARAMS with EXACT values:\n"
        '     selinux: "permissive"  (not SELINUX, not Permissive)\n'
        '     transparent_hugepage: "never"\n'
        '     irqbalance: "active"\n'
        '     cpu_governor: "performance"\n\n'
        '3. "resource_limits" — cgroup weights, systemd limits, NUMA pinning. '
        f"Allowed: {', '.join(sorted(resource_limits_targets))}\n"
        "   ACTION PARAMS with EXACT values:\n"
        '     cgroup_cpu: "max"  (removes CPU quota)\n'
        '     cgroup_memory: "max"  (removes memory cap)\n'
        '     numa_policy: "remove"  (removes NUMA pinning)\n'
        '     kill_background_hogs: "true"\n\n'
        '4. "network" — iptables, conntrack, tc/pacing rules. '
        f"Allowed: {', '.join(sorted(network_targets))}\n"
        "   ACTION PARAMS with EXACT values:\n"
        "     iptables_drop_rules: \"flush\"  (not 'clear', not 'remove')\n"
        "     tc_rules: \"remove\"  (not 'delete', not 'fq_codel')\n\n"
        '5. "storage" — I/O scheduler, readahead. '
        f"Allowed: {', '.join(sorted(storage_targets))}\n"
        "   ACTION PARAMS with EXACT values:\n"
        '     kill_io_hogs: "true"\n'
        '     io_scheduler: "none"  (passthrough)\n\n'
        "CRITICAL EXECUTION RULES:\n"
        "1. CROSS-LAYER SYNC: If 'worker_rlimit_nofile' is increased, you MUST "
        "ensure 'systemd_nofile' in "
        "resource_limits is set to at least 1,048,576. One cannot succeed without "
        "the other.\n"
        "2. MANDATORY NEUTRALIZATION: If 'limit_rate' or 'limit_req' are detected "
        'in inspection, you MUST include limit_rate: "0" and limit_req: "remove" '
        "to clear residual degradation.\n"
        "3. KERNEL SCALING: For a 112-core system, ensure 'somaxconn' and "
        "'tcp_max_syn_backlog' are "
        "set to 65535. Ensure 'tcp_max_tw_buckets' is set to 2,000,000 to handle "
        "the socket churn of 1.5M RPS.\n"
        "4. NO DEFAULTS FOR PERFORMANCE: If the synthesizer mentions a parameter "
        "without a value, "
        "use the highest known performance value for RHEL 9 (e.g., swappiness=10, "
        "vfs_cache_pressure=50).\n"
        "5. CLEANLINESS: Return ONLY the JSON object. No comments, no semicolons, "
        "no shell commands.\n\n"
        "Synthesizer recommendations (Priority):\n"
        + json.dumps(recommendations, ensure_ascii=True)
        + "\n\n"
        "Inspection problems detected (Mandatory fixes):\n"
        f"resource_limits: {json.dumps(resource_problems)}\n"
        f"network: {json.dumps(network_problems)}\n"
        f"storage: {json.dumps(storage_problems)}"
    )
