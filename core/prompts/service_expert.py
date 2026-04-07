"""Generic service expert prompt — delegates to the service profile's builder."""

from __future__ import annotations

import json
from typing import Any


def build(
    *,
    system_line: str,
    service_inspection: dict[str, Any],
    profile: Any | None = None,
) -> str:
    """Build the service expert prompt.

    If a service profile with an expert_prompt_builder is provided, delegates to it.
    Otherwise falls back to a generic prompt.
    """
    if profile and profile.expert_prompt_builder:
        return profile.expert_prompt_builder(
            system_line=system_line,
            service_inspection=service_inspection,
        )

    # Generic fallback for services without a custom expert prompt
    svc_name = profile.name.upper() if profile else "SERVICE"
    return (
        f"You are a High-Performance {svc_name} and RHEL 9 Kernel Optimization Expert. "
        f"Your goal is to maximize throughput on bare-metal hardware. "
        f"Analyze the provided inspection evidence and identify bottlenecks.\n\n"
        "Return strict JSON with keys: summary, rca_records, recommendations.\n\n"
        f"System: {system_line}\n"
        f"Service Inspection:\n{json.dumps(service_inspection, ensure_ascii=True)}"
    )
