from __future__ import annotations

from dataclasses import dataclass

from agents import AgentDeps
from core.log import log


@dataclass
class CollectionOutput:
    checks_run: list[str]
    findings: list[str]
    raw_summary: str


async def run(model, deps: AgentDeps, task: str) -> CollectionOutput:
    del model, task
    checks_run = []
    findings = []

    log("collector", "Reading service config...", "action")
    config = deps.adapter.get_config()
    config_preview = config.get("raw", "")[:2000]
    deps.memory.save_context(
        deps.session_id,
        "command_output",
        "service_config",
        config.get("raw", ""),
        "current service config",
    )
    checks_run.append("service_config")
    findings.append(f"Config path: {config.get('path', 'unknown')}")
    deps.token_counter.tool_calls += 1
    log("collector", f"Config loaded ({len(config.get('raw', ''))} chars)", "info")

    log("collector", "Fetching service logs...", "action")
    logs = deps.adapter.get_logs(tail=50)
    deps.memory.save_context(
        deps.session_id,
        "log",
        "service_logs",
        logs,
        "last 50 lines of service log",
    )
    checks_run.append("service_logs")
    log_lines = len(logs.strip().splitlines()) if logs.strip() else 0
    findings.append(f"Log lines retrieved: {log_lines}")
    deps.token_counter.tool_calls += 1
    log("collector", f"{log_lines} log lines retrieved", "info")

    log("collector", "Collecting live metrics...", "action")
    metrics = deps.adapter.get_metrics()
    deps.memory.save_context(
        deps.session_id,
        "metric",
        "live_metrics",
        str(metrics),
        "live service metrics snapshot",
    )
    checks_run.append("live_metrics")
    for key, value in metrics.items():
        findings.append(f"{key}: {str(value)[:100]}")
    deps.token_counter.tool_calls += 1
    log("collector", f"{len(metrics)} metric groups collected", "info")

    return CollectionOutput(
        checks_run=checks_run,
        findings=findings,
        raw_summary=f"Collected config ({len(config_preview)} chars), "
        f"{log_lines} log lines, {len(metrics)} metric groups",
    )
