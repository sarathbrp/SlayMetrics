"""Prompt for the apply planner agent.

Converts synthesizer recommendations into a structured 5-category
apply plan with only allowed parameter names.
"""

import json
from typing import Any


def build(
    *,
    webserver_targets: dict[str, str],
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
        "You are a performance tuning executor. "
        "Given the synthesizer's recommendations, produce a JSON object "
        "with exactly 5 keys — one per category. "
        "Only use allowed parameter names listed below.\n\n"
        '"1. "webserver" — nginx config directives.\n'
        f"   Allowed: {', '.join(sorted(webserver_targets))}\n\n"
        '"2. "kernel" — sysctl, THP, SELinux, CPU governor, IRQ.\n'
        f"   Allowed: {', '.join(sorted(kernel_targets))}\n\n"
        '"3. "resource_limits" — cgroup, systemd limits, background processes.\n'
        f"   Allowed: {', '.join(sorted(resource_limits_targets))}\n\n"
        '"4. "network" — iptables, conntrack, tc rules.\n'
        f"   Allowed: {', '.join(sorted(network_targets))}\n\n"
        '"5. "storage" — I/O scheduler, readahead, I/O hogs.\n'
        f"   Allowed: {', '.join(sorted(storage_targets))}\n\n"
        "Rules:\n"
        "- Include parameters the synthesizer recommends AND any "
        "problems detected in the inspection below\n"
        "- Values must be clean (no comments, no semicolons)\n"
        "- Use defaults if a param is mentioned without a value\n"
        "- Empty categories should be empty dicts {}\n"
        "- CRITICAL: if inspection shows cgroup CPU/memory caps or "
        "systemd LimitNOFILE too low, include resource_limits fixes\n\n"
        "Synthesizer recommendations:\n" + json.dumps(recommendations, ensure_ascii=True) + "\n\n"
        "Inspection problems detected (also fix these):\n"
        f"resource_limits: {json.dumps(resource_problems)}\n"
        f"network: {json.dumps(network_problems)}\n"
        f"storage: {json.dumps(storage_problems)}"
    )
