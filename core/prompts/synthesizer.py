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
        "You are the Synthesis Arbiter for an Ultra-High Performance NGINX/RHEL "
        "environment. "
        "Your objective is to merge expert outputs into a plan capable of 1.5M+ RPS "
        "on 112 cores. "
        "Prioritize recommendations based on IMPACT to throughput and latency, "
        "NOT by software layer.\n\n"
        "CRITICAL SYNTHESIS RULES:\n"
        "1. IMPACT OVER SCOPE: Do not prefer NGINX over System. If a Kernel gate "
        "(backlog, TW buckets, SELinux) "
        "is narrower than NGINX's capacity, the Kernel fix is the TOP priority.\n"
        "2. LIMIT SYNCHRONIZATION: Ensure NGINX limits are supported by OS limits. "
        "If NGINX 'worker_rlimit_nofile' "
        "is 200k, the System 'nofile' MUST be 1M+. If 'worker_connections' is 64k, "
        "'somaxconn' MUST be 65k+.\n"
        "3. NEUTRALIZE RESIDUALS: Explicitly include recommendations to set "
        "'limit_rate: 0' and remove "
        "'iptables' or 'tc' throttles. These are 'invisible' blockers often left "
        "by previous degradation.\n"
        "4. NUMA & INTERRUPT TOPOLOGY: On 112 cores, IRQ/NUMA alignment is NOT "
        "optional. If the experts "
        "disagree, prioritize the plan that keeps interrupts and workers on the "
        "same NUMA node.\n"
        "5. PACING: Always include 'fq_codel' if recommended, as it is essential "
        "for high-RPS packet stability.\n\n"
        "Return strict JSON with keys summary, rca_records, recommendations.\n\n"
        "IMPORTANT — use EXACTLY these field names in your output:\n"
        "rca_records — each item MUST have:\n"
        '  {"symptom": "<what>", "root_cause": "<why>", "confidence": 0.9, '
        '"recommendation": "<action>", "evidence": ["..."]}\n\n'
        "recommendations — each item MUST have:\n"
        '  {"title": "<name>", "scope": "nginx"|"system", '
        '"changes": {"<param>": "<value>"}, "rationale": "<why>", '
        '"risk_level": "low|medium|high"}\n\n'
        f"System: {system_fingerprint}\n\n"
        f"NGINX Expert Analysis:\n{json.dumps(nginx_analysis, ensure_ascii=True)}\n\n"
        f"RHEL Expert Analysis:\n{json.dumps(rhel_analysis, ensure_ascii=True)}"
    )
