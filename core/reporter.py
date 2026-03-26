from __future__ import annotations

import json
import os
from datetime import datetime

from agents import TokenCounter
from memory.tidb_store import TiDBStore


def generate(
    session_id: str,
    memory: TiDBStore,
    token_counter: TokenCounter,
    output_dir: str = "report",
    baselines: dict | None = None,
    finals: dict | None = None,
    stability: dict | None = None,
    throughput: dict | None = None,
    token_history: list | None = None,
) -> str:
    os.makedirs(output_dir, exist_ok=True)

    profile = memory.get_profile(session_id) or {}
    facts = memory.get_facts(session_id)
    queue = memory.get_queue(session_id)

    fixes = [f for f in facts if f.get("type") == "fix"]
    findings = [f for f in facts if f.get("type") == "finding"]
    negatives = [f for f in facts if f.get("type") == "negative"]

    baseline_rps = profile.get("baseline_rps", 0.0) or 0.0
    best_rps = profile.get("best_rps", 0.0) or 0.0
    total_improvement = ((best_rps - baseline_rps) / baseline_rps * 100) if baseline_rps else 0.0

    # ── Markdown report ──────────────────────────────────────────────────────
    md = _md_report(
        profile,
        fixes,
        findings,
        negatives,
        queue,
        baseline_rps,
        best_rps,
        total_improvement,
        token_counter,
        baselines=baselines,
        finals=finals,
        stability=stability,
        throughput=throughput,
        token_history=token_history,
    )

    # ── JSON report ──────────────────────────────────────────────────────────
    report_data = {
        "session_id": session_id,
        "generated_at": datetime.utcnow().isoformat(),
        "profile": {k: v for k, v in profile.items() if k != "id"},
        "baseline_rps": baseline_rps,
        "best_rps": best_rps,
        "total_improvement_pct": round(total_improvement, 2),
        "baselines_by_size": baselines or {},
        "finals_by_size": finals or {},
        "stability": stability or {},
        "throughput": throughput or {},
        "fixes_applied": [_clean(f) for f in fixes],
        "findings": [_clean(f) for f in findings],
        "negatives": [_clean(f) for f in negatives],
        "hypothesis_queue": [_clean(q) for q in queue],
        "tokens": {
            "input": token_counter.input_tokens,
            "output": token_counter.output_tokens,
            "total": token_counter.total,
            "tool_calls": token_counter.tool_calls,
            "by_tool": token_counter.tool_token_rows(),
        },
    }

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    md_path = os.path.join(output_dir, f"report_{ts}_{session_id}.md")
    json_path = os.path.join(output_dir, f"report_{ts}_{session_id}.json")

    with open(md_path, "w") as f:
        f.write(md)
    with open(json_path, "w") as f:
        json.dump(report_data, f, indent=2, default=str)

    # Also write a latest symlink for convenience
    latest_md = os.path.join(output_dir, "report.md")
    latest_json = os.path.join(output_dir, "report.json")
    with open(latest_md, "w") as f:
        f.write(md)
    with open(latest_json, "w") as f:
        json.dump(report_data, f, indent=2, default=str)

    return md_path


def _md_report(
    profile,
    fixes,
    findings,
    negatives,
    queue,
    baseline_rps,
    best_rps,
    total_improvement,
    token_counter,
    baselines=None,
    finals=None,
    stability=None,
    throughput=None,
    token_history=None,
) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    service = profile.get("service", "unknown")
    host = profile.get("host", "unknown")
    rhel = profile.get("rhel_version", "unknown")
    kernel = profile.get("kernel_version", "unknown")
    llm = profile.get("llm_profile", "unknown")

    lines = [
        "# SlayMetricsAgent — Diagnostic Report",
        f"Generated: {now}",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
        "| | Value |",
        "|--|--|",
        f"| Service | {service} on {host} |",
        f"| Baseline RPS (small) | {baseline_rps:.1f} |",
        f"| Best RPS (small) | {best_rps:.1f} |",
        f"| Total improvement | **{total_improvement:+.1f}%** |",
        f"| Fixes applied | {len(fixes)} |",
        f"| LLM profile | {llm} |",
        "",
        "---",
        "",
        "## System Profile",
        "",
        f"- **Host:** {host}",
        f"- **RHEL version:** {rhel}",
        f"- **Kernel:** {kernel}",
        f"- **CPU cores:** {profile.get('cpu_cores', 'unknown')}",
        f"- **RAM:** {profile.get('ram_gb', 'unknown')} GB",
        "",
        "---",
        "",
    ]

    # ── Benchmark Results by Payload Size ─────────────────────────────────────
    if baselines or finals:
        lines += [
            "## Benchmark Results by Payload Size",
            "",
            "| Payload | Baseline RPS | Baseline p99 | Final RPS | Final p99 | Improvement |",
            "|---------|-------------|-------------|-----------|-----------|-------------|",
        ]
        for size in ["small", "medium", "large"]:
            b = (baselines or {}).get(size, {})
            f = (finals or {}).get(size, {})
            b_rps = b.get("rps", 0)
            f_rps = f.get("rps", 0)
            imp = ((f_rps - b_rps) / b_rps * 100) if b_rps else 0
            lines.append(
                f"| {size} | {b_rps:.1f} | {b.get('p99', 0):.1f}ms "
                f"| {f_rps:.1f} | {f.get('p99', 0):.1f}ms | **{imp:+.1f}%** |"
            )
        lines += ["", "---", ""]

    # ── Resource Usage During Benchmarks ──────────────────────────────────────
    if baselines or finals:
        lines += [
            "## Resource Usage During Benchmarks",
            "",
            (
                "| Payload | Baseline CPU% | Baseline Mem MB | Final CPU% "
                "| Final Mem MB | CPU Change |"
            ),
            "|---------|--------------|----------------|-----------|-------------|------------|",
        ]
        for size in ["small", "medium", "large"]:
            b = (baselines or {}).get(size, {})
            f = (finals or {}).get(size, {})
            cpu_change = f.get("cpu_pct", 0) - b.get("cpu_pct", 0)
            lines.append(
                f"| {size} "
                f"| {b.get('cpu_pct', 0):.1f} | {b.get('mem_mb', 0):.0f} "
                f"| {f.get('cpu_pct', 0):.1f} | {f.get('mem_mb', 0):.0f} "
                f"| **{cpu_change:+.1f}%** |"
            )
        lines += ["", "---", ""]

    # ── Bottleneck Analysis ─────────────────────────────────────────────────
    if throughput or (baselines and finals):
        lines += [
            "## Bottleneck Analysis",
            "",
        ]
        if throughput:
            nic = throughput.get("nic_speed", "unknown")
            disk = throughput.get("disk_write", "unknown")
            lines += [
                f"- **NIC Speed:** {nic}",
                f"- **Disk Write Throughput:** {disk}",
                "",
                "| Payload | Final RPS | Data Throughput | Bottleneck |",
                "|---------|-----------|----------------|------------|",
            ]
            for size in ["small", "medium", "large"]:
                tp_key = f"{size}_throughput_mb_s"
                tp = throughput.get(tp_key, 0)
                f_data = (finals or {}).get(size, {})
                f_rps = f_data.get("rps", 0)
                f_cpu = f_data.get("cpu_pct", 0)
                # Determine bottleneck
                if f_cpu > 90:
                    bottleneck = "CPU saturated"
                elif tp > 4000:
                    bottleneck = "Memory bus / NIC bandwidth"
                elif tp > 1000:
                    bottleneck = "Network throughput"
                else:
                    bottleneck = "Application tuning"
                lines.append(f"| {size} | {f_rps:.0f} | {tp:.0f} MB/s | {bottleneck} |")
            lines += [
                "",
                "**Note:** Medium and large file performance is typically bounded by hardware "
                "(NIC bandwidth, memory bus, disk I/O) rather than software configuration. "
                "Small file performance benefits most from nginx/kernel tuning because "
                "per-request overhead (syscalls, connection handling) dominates over "
                "data transfer.",
                "",
                "---",
                "",
            ]

    # ── Sustained Stability Test ──────────────────────────────────────────────
    if stability:
        lines += [
            "## Sustained Stability Test",
            "",
            f"- **Duration:** {stability.get('duration_sec', 0) // 60} minutes",
            f"- **Samples:** {stability.get('sample_count', 0)}",
            f"- **Mean RPS:** {stability.get('mean_rps', 0):.1f}",
            f"- **Std Dev:** {stability.get('stdev_rps', 0):.1f}",
            f"- **Coefficient of Variation:** {stability.get('cv_pct', 0):.1f}%",
            "",
        ]
        samples = stability.get("samples", [])
        if samples:
            lines += [
                "| Sample | RPS |",
                "|--------|-----|",
            ]
            for i, s in enumerate(samples, 1):
                lines.append(f"| {i} | {s:.1f} |")
        lines += ["", "---", ""]

    # ── Applied Fixes ─────────────────────────────────────────────────────────
    lines += ["## Applied Fixes", ""]

    if fixes:
        lines += [
            "| Parameter | Old Value | New Value | Before RPS | After RPS | Impact |",
            "|-----------|-----------|-----------|------------|-----------|--------|",
        ]
        for f in fixes:
            impact = f.get("impact_pct") or 0.0
            lines.append(
                f"| `{f.get('parameter', '')}` "
                f"| `{f.get('before_value', '')}` "
                f"| `{f.get('after_value', '')}` "
                f"| {f.get('before_rps') or 0:.1f} "
                f"| {f.get('after_rps') or 0:.1f} "
                f"| **{impact:+.1f}%** |"
            )
    else:
        lines.append("No fixes were applied.")

    lines += ["", "---", ""]

    # ── Decision Log ──────────────────────────────────────────────────────────
    lines += ["## Decision Log", ""]

    for f in fixes + findings:
        reasoning = f.get("reasoning", "")
        if not reasoning or reasoning == "proven fix applied":
            reasoning = _template_reasoning(f.get("parameter", ""))
        lines += [
            f"### {f.get('parameter', 'N/A')} ({f.get('type', '')})",
            f"**Reasoning:** {reasoning}",
            "",
        ]

    lines += ["---", ""]

    # ── Hypothesis Queue ──────────────────────────────────────────────────────
    lines += [
        "## Hypothesis Queue Summary",
        "",
        "| Hypothesis | Priority | Status | Outcome |",
        "|-----------|----------|--------|---------|",
    ]
    for q in queue:
        lines.append(
            f"| {q.get('name', '')} | P{q.get('priority', '')} "
            f"| {q.get('status', '')} | {q.get('outcome', '') or '—'} |"
        )

    lines += ["", "---", ""]

    # ── Token Consumption ─────────────────────────────────────────────────────
    lines += [
        "## Token Consumption",
        "",
        "| | Count |",
        "|--|--|",
        f"| Input tokens | {token_counter.input_tokens:,} |",
        f"| Output tokens | {token_counter.output_tokens:,} |",
        f"| Total tokens | {token_counter.total:,} |",
        f"| Tool calls | {token_counter.tool_calls:,} |",
        "",
    ]

    by_tool = token_counter.tool_token_rows()
    if by_tool:
        lines += [
            "### Token Attribution by Tool",
            "",
            "| Tool | Calls | Call In | Call Out | Post In | Post Out | Total |",
            "|------|------:|--------:|---------:|--------:|---------:|------:|",
        ]
        for row in by_tool:
            lines.append(
                f"| `{row['tool']}` | {row['calls']} | {row['call_input_tokens']:,} | "
                f"{row['call_output_tokens']:,} | {row['post_input_tokens']:,} | "
                f"{row['post_output_tokens']:,} | **{row['total_tokens']:,}** |"
            )
        lines += ["", "---", ""]

    # ── Token History (across sessions) ──────────────────────────────────────
    if token_history:
        lines += [
            "### Token Usage History (all sessions)",
            "",
            "| Session | Date | Input | Output | Total | Tool Calls |",
            "|---------|------|-------|--------|-------|------------|",
        ]
        for th in token_history:
            lines.append(
                f"| {th.get('session_id', '')[:8]} "
                f"| {th.get('created_at', '')[:19]} "
                f"| {th.get('input_tokens', 0):,} "
                f"| {th.get('output_tokens', 0):,} "
                f"| {th.get('total_tokens', 0):,} "
                f"| {th.get('tool_calls', 0)} |"
            )
        lines += ["", "---", ""]

    return "\n".join(lines)


REASONING_TEMPLATES = {
    "worker_connections": "Increased worker_connections to handle more concurrent connections per worker process.",
    "open_file_cache": "Enabled open_file_cache to eliminate repeated stat() and open() syscalls on static files.",
    "access_log": "Disabled access logging to remove per-request disk I/O overhead.",
    "tcp_nodelay": "Enabled tcp_nodelay to disable Nagle's algorithm, reducing latency for small responses.",
    "worker_rlimit_nofile": "Increased file descriptor limit to support higher concurrency.",
    "keepalive_requests": "Increased keepalive_requests for better connection reuse under high load.",
    "gzip": "Enabled gzip compression at level 1 for bandwidth reduction with minimal CPU cost.",
    "listen_backlog": "Aligned listen backlog with somaxconn to prevent connection queue drops.",
    "net.core.somaxconn": "Increased TCP listen backlog to handle burst connections.",
    "net.ipv4.tcp_max_syn_backlog": "Increased SYN queue to prevent drops under high connection rates.",
    "net.core.netdev_max_backlog": "Increased NIC packet buffer to prevent kernel-level packet drops.",
    "transparent_hugepage": "Disabled THP to eliminate memory compaction latency spikes.",
    "selinux": "Set SELinux to permissive to reduce syscall overhead on file access.",
    "net.ipv4.tcp_tw_reuse": "Enabled TIME_WAIT socket reuse for faster connection recycling.",
    "net.core.rmem_max": "Increased socket receive buffer maximum for better TCP throughput.",
    "net.core.wmem_max": "Increased socket send buffer maximum for better TCP throughput.",
}


def _template_reasoning(parameter: str) -> str:
    """Generate reasoning text from template based on parameter name."""
    # Check for exact match
    if parameter in REASONING_TEMPLATES:
        return REASONING_TEMPLATES[parameter]
    # Check if any key is a substring of the parameter (for batch names)
    for key, reason in REASONING_TEMPLATES.items():
        if key in parameter:
            return reason
    return "Applied proven performance tuning parameter."


def _clean(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "embedding"}
