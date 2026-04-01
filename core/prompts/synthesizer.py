"""Prompt for the synthesis arbiter agent.

Merges nginx_expert and rhel_expert outputs into one final plan.
"""

import json
from typing import Any


def build(
    *,
    system_fingerprint: str,
    nginx_analysis: dict[str, Any],
    rhel_analysis: dict[str, Any],
) -> str:
    return (
        "You are the synthesis arbiter between an NGINX expert "
        "and a RHEL Linux performance expert. "
        "Merge their outputs into one final plan. "
        "Keep only grounded recommendations supported by evidence. "
        "Prefer nginx fixes first, system fixes second, IRQ fixes only if clearly justified. "
        "Return strict JSON with keys summary, rca_records, recommendations.\n\n"
        "IMPORTANT — use EXACTLY these field names in your output:\n\n"
        "rca_records — each item MUST have:\n"
        '  {"symptom": "<what is wrong>", "root_cause": "<why it is wrong>", '
        '"confidence": 0.9, "recommendation": "<what to do>", "evidence": ["..."]}\n'
        "Example:\n"
        '  {"symptom": "worker_processes fixed at 8 on 112-core system", '
        '"root_cause": "Most CPU cores idle, limiting parallel request handling", '
        '"confidence": 0.95, "recommendation": "Set worker_processes auto", '
        '"evidence": ["nginx -T shows worker_processes 8"]}\n\n'
        "recommendations — each item MUST have:\n"
        '  {"title": "<short name>", "scope": "nginx" or "system", '
        '"changes": {"<param_name>": "<target_value>"}, '
        '"rationale": "<why>", "risk_level": "low|medium|high"}\n'
        "Example:\n"
        '  {"title": "Increase worker connections", "scope": "nginx", '
        '"changes": {"worker_connections": "65536"}, '
        '"rationale": "Current 2048 caps concurrency", "risk_level": "low"}\n'
        '  {"title": "Tune TCP backlog", "scope": "system", '
        '"changes": {"net.core.somaxconn": "65535"}, '
        '"rationale": "Current 1024 causes connection refusals", '
        '"risk_level": "low"}\n\n'
        "CRITICAL RULES:\n"
        "- changes must be a dict of {param_name: value}, NOT a command string\n"
        "- values must be clean (no semicolons, no 'reload nginx')\n"
        "- one param per recommendation for nginx, may batch sysctls\n\n"
        f"System: {system_fingerprint}\n\n"
        f"NGINX Expert:\n{json.dumps(nginx_analysis, ensure_ascii=True)}\n\n"
        f"RHEL Expert:\n{json.dumps(rhel_analysis, ensure_ascii=True)}"
    )
