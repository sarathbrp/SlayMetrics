from __future__ import annotations

import json
import os
from datetime import datetime

from agents import TokenCounter
from memory.tidb_store import TiDBStore


def generate(session_id: str, memory: TiDBStore,
             token_counter: TokenCounter, output_dir: str = "report",
             baselines: dict | None = None, finals: dict | None = None,
             stability: dict | None = None) -> str:
    os.makedirs(output_dir, exist_ok=True)

    profile = memory.get_profile(session_id) or {}
    facts = memory.get_facts(session_id)
    queue = memory.get_queue(session_id)

    fixes = [f for f in facts if f.get("type") == "fix"]
    findings = [f for f in facts if f.get("type") == "finding"]
    negatives = [f for f in facts if f.get("type") == "negative"]

    baseline_rps = profile.get("baseline_rps", 0.0) or 0.0
    best_rps = profile.get("best_rps", 0.0) or 0.0
    total_improvement = (
        ((best_rps - baseline_rps) / baseline_rps * 100) if baseline_rps else 0.0
    )

    # ── Markdown report ──────────────────────────────────────────────────────
    md = _md_report(profile, fixes, findings, negatives, queue,
                    baseline_rps, best_rps, total_improvement, token_counter,
                    baselines=baselines, finals=finals, stability=stability)

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
        "fixes_applied": [_clean(f) for f in fixes],
        "findings": [_clean(f) for f in findings],
        "negatives": [_clean(f) for f in negatives],
        "hypothesis_queue": [_clean(q) for q in queue],
        "tokens": {
            "input": token_counter.input_tokens,
            "output": token_counter.output_tokens,
            "total": token_counter.total,
            "tool_calls": token_counter.tool_calls,
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


def _md_report(profile, fixes, findings, negatives, queue,
               baseline_rps, best_rps, total_improvement, token_counter,
               baselines=None, finals=None, stability=None) -> str:
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
            "| Payload | Baseline CPU% | Baseline Mem MB | Final CPU% | Final Mem MB | CPU Change |",
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
        lines += [
            f"### {f.get('parameter', 'N/A')} ({f.get('type', '')})",
            f"**Reasoning:** {f.get('reasoning', '')}",
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

    return "\n".join(lines)


def _clean(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "embedding"}
