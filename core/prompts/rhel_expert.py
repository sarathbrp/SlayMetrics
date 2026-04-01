"""Prompt for the RHEL Linux performance expert agent."""

import json
from typing import Any


def build(
    *,
    system_line: str,
    kernel_inspection: dict[str, Any],
    resource_data: dict[str, Any],
    network_data: dict[str, Any],
    storage_data: dict[str, Any],
) -> str:
    return (
        "You are a RHEL Linux performance expert. "
        "Review kernel, resource limits, network, and storage evidence. "
        "Return strict JSON with keys summary, rca_records, recommendations.\n\n"
        f"System: {system_line}\n"
        f"Kernel Inspection:\n{json.dumps(kernel_inspection, ensure_ascii=True)}\n\n"
        f"Resource Limits:\n{json.dumps(resource_data, ensure_ascii=True)}\n\n"
        f"Network:\n{json.dumps(network_data, ensure_ascii=True)}\n\n"
        f"Storage:\n{json.dumps(storage_data, ensure_ascii=True)}"
    )
