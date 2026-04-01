"""Parse hypothesis markdown files and report JSON into dashboard data."""

from __future__ import annotations

import ast
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PRECISE_BUNDLE_MAP: dict[str, str] = {
    "worker_processes": "nginx core concurrency",
    "worker_connections": "nginx core concurrency",
    "worker_rlimit_nofile": "nginx core concurrency",
    "worker_cpu_affinity": "nginx core concurrency",
    "multi_accept": "nginx core concurrency",
    "accept_mutex": "nginx core concurrency",
    "sendfile": "nginx io path",
    "directio": "nginx io path",
    "aio": "nginx io path",
    "output_buffers": "nginx io path",
    "postpone_output": "nginx io path",
    "tcp_nopush": "nginx socket behavior",
    "tcp_nodelay": "nginx socket behavior",
    "keepalive_requests": "nginx socket behavior",
    "keepalive_timeout": "nginx socket behavior",
    "listen_backlog": "nginx socket behavior",
    "reset_timedout_connection": "nginx socket behavior",
    "client_body_timeout": "nginx socket behavior",
    "client_header_timeout": "nginx socket behavior",
    "send_timeout": "nginx socket behavior",
    "access_log": "nginx logging/cache",
    "error_log_level": "nginx logging/cache",
    "open_file_cache": "nginx logging/cache",
    "open_file_cache_valid": "nginx logging/cache",
    "open_file_cache_min_uses": "nginx logging/cache",
    "gzip": "nginx throttling/compression",
    "gzip_comp_level": "nginx throttling/compression",
    "limit_rate": "nginx throttling/compression",
    "limit_rate_after": "nginx throttling/compression",
    "limit_req": "nginx throttling/compression",
    "limit_conn": "nginx throttling/compression",
    "net.core.somaxconn": "kernel backlog/queues",
    "net.ipv4.tcp_max_syn_backlog": "kernel backlog/queues",
    "net.core.netdev_max_backlog": "kernel backlog/queues",
    "net.core.rmem_max": "kernel socket buffers",
    "net.core.wmem_max": "kernel socket buffers",
    "net.core.rmem_default": "kernel socket buffers",
    "net.core.wmem_default": "kernel socket buffers",
    "net.ipv4.tcp_rmem": "kernel socket buffers",
    "net.ipv4.tcp_wmem": "kernel socket buffers",
    "net.ipv4.tcp_tw_reuse": "kernel tcp lifecycle",
    "net.ipv4.tcp_max_tw_buckets": "kernel tcp lifecycle",
    "net.ipv4.tcp_fin_timeout": "kernel tcp lifecycle",
    "net.ipv4.tcp_slow_start_after_idle": "kernel tcp lifecycle",
    "net.ipv4.tcp_max_orphans": "kernel tcp lifecycle",
    "net.ipv4.tcp_orphan_retries": "kernel tcp lifecycle",
    "net.ipv4.tcp_keepalive_time": "kernel tcp lifecycle",
    "net.ipv4.tcp_keepalive_intvl": "kernel tcp lifecycle",
    "net.ipv4.tcp_keepalive_probes": "kernel tcp lifecycle",
    "net.ipv4.ip_local_port_range": "kernel tcp lifecycle",
    "vm.swappiness": "kernel memory policy",
    "vm.vfs_cache_pressure": "kernel memory policy",
    "vm.dirty_ratio": "kernel memory policy",
    "vm.dirty_background_ratio": "kernel memory policy",
    "vm.dirty_expire_centisecs": "kernel memory policy",
    "vm.dirty_writeback_centisecs": "kernel memory policy",
    "transparent_hugepage": "kernel memory policy",
    "selinux": "system runtime controls",
    "cpu_governor": "system runtime controls",
    "irqbalance": "system runtime controls",
    "systemd_nofile": "resource limits/cgroups",
    "systemd_nproc": "resource limits/cgroups",
    "cgroup_cpu": "resource limits/cgroups",
    "cgroup_memory": "resource limits/cgroups",
    "cgroup_io_weight": "resource limits/cgroups",
    "cgroup_cpu_weight": "resource limits/cgroups",
    "numa_policy": "resource limits/cgroups",
    "kill_background_hogs": "resource limits/cgroups",
    "iptables_drop_rules": "network controls",
    "conntrack_max": "network controls",
    "tc_rules": "network controls",
    "io_scheduler": "storage controls",
    "readahead": "storage controls",
    "kill_io_hogs": "storage controls",
}

REFERENCE_SESSION_OVERRIDE = os.environ.get(
    "SLAYMETRICS_REFERENCE_SESSION",
    "b27d6d9f",
).strip()
IGNORED_SESSION_IDS = {"fc1e49d9"}
LEADERBOARD_LIMIT = 30


def discover_sessions(data_dir: str) -> list[dict]:
    """Scan report/ and hypothesis/ to build session list."""
    report_dir = Path(data_dir) / "report"
    hypo_dir = Path(data_dir) / "hypothesis"
    sessions: dict[str, dict] = {}

    # Scan report JSON files
    if report_dir.exists():
        for f in sorted(report_dir.glob("report_*_*.json"), reverse=True):
            parts = f.stem.split("_")
            if len(parts) >= 4:
                session_id = parts[-1]
                timestamp = f"20{parts[1]}_{parts[2]}" if len(parts[1]) == 6 else parts[1]
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                sessions[session_id] = {
                    "session_id": session_id,
                    "timestamp": data.get("generated_at", timestamp),
                    "report_file": str(f.name),
                    "has_report": True,
                    "baseline_rps": data.get("baseline_rps", 0),
                    "best_rps": data.get("best_rps", 0),
                    "improvement_pct": data.get("total_improvement_pct", 0),
                    "tokens": data.get("tokens", {}),
                    "profile": data.get("profile", {}),
                }

    # Scan hypothesis folders
    if hypo_dir.exists():
        for d in hypo_dir.iterdir():
            if d.is_dir() and len(d.name) == 8:
                sid = d.name
                iterations = len(list(d.glob("iter*_00_summary.md")))
                if sid not in sessions:
                    sessions[sid] = {
                        "session_id": sid,
                        "timestamp": "",
                        "has_report": False,
                        "baseline_rps": 0,
                        "best_rps": 0,
                        "improvement_pct": 0,
                        "tokens": {},
                        "profile": {},
                    }
                sessions[sid]["iterations"] = iterations
                sessions[sid]["has_hypothesis"] = True

    result = sorted(
        sessions.values(),
        key=lambda s: s.get("timestamp", ""),
        reverse=True,
    )
    return result


def load_session(data_dir: str, session_id: str) -> dict:
    """Load full session data: report + hypothesis iterations."""
    report_dir = Path(data_dir) / "report"
    hypo_dir = Path(data_dir) / "hypothesis" / session_id

    session: dict = {"session_id": session_id, "iterations": [], "report": None}

    # Load report JSON
    for f in report_dir.glob(f"report_*_{session_id}.json"):
        try:
            session["report"] = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
        break

    # Load iteration data
    if hypo_dir.exists():
        iter_nums = set()
        for f in hypo_dir.glob("iter*_00_summary.md"):
            m = re.match(r"iter(\d+)_00_summary\.md", f.name)
            if m:
                iter_nums.add(int(m.group(1)))

        for n in sorted(iter_nums):
            iteration = {"iteration": n}

            # Summary
            summary_file = hypo_dir / f"iter{n}_00_summary.md"
            if summary_file.exists():
                iteration["summary"] = _parse_summary_md(summary_file.read_text(encoding="utf-8"))

            # Expert analyses
            for agent, num in [
                ("nginx_expert", "01"),
                ("rhel_expert", "02"),
                ("synthesizer", "03"),
                ("apply_planner", "04"),
            ]:
                agent_file = hypo_dir / f"iter{n}_{num}_{agent}.md"
                if agent_file.exists():
                    content = agent_file.read_text(encoding="utf-8")
                    iteration[agent] = _parse_agent_md(content)

            session["iterations"].append(iteration)

    return session


def load_comparison(data_dir: str, session_ids: list[str]) -> list[dict]:
    """Load summary data for multiple sessions for comparison."""
    results = []
    for sid in session_ids:
        session = load_session(data_dir, sid)
        report = session.get("report") or {}
        results.append(
            {
                "session_id": sid,
                "timestamp": report.get("generated_at", ""),
                "iterations": len(session.get("iterations", [])),
                "baseline_rps": report.get("baseline_rps", 0),
                "best_rps": report.get("best_rps", 0),
                "improvement_pct": report.get("total_improvement_pct", 0),
                "tokens": report.get("tokens", {}),
                "baselines_by_size": report.get("baselines_by_size", {}),
                "finals_by_size": report.get("finals_by_size", {}),
                "fixes_applied": report.get("fixes_applied", []),
                "profile": report.get("profile", {}),
                "iteration_summaries": [
                    it.get("summary", {}) for it in session.get("iterations", [])
                ],
            }
        )
    return results


def load_parameter_summary(data_dir: str) -> dict:
    """Build a cross-session parameter dataset from all hypothesis folders."""
    hypo_root = Path(data_dir) / "hypothesis"
    report_root = Path(data_dir) / "report"
    parameter_sessions: dict[str, dict[str, dict]] = defaultdict(dict)
    session_rows: list[dict] = []
    session_states: dict[str, dict[str, dict]] = {}
    session_precise_applied: dict[str, dict[str, str]] = {}
    session_param_sources: dict[str, dict[str, set[str]]] = {}

    if not hypo_root.exists():
        return {"sessions": [], "parameters": [], "matrix": []}

    for session_dir in sorted(hypo_root.iterdir()):
        if not session_dir.is_dir() or len(session_dir.name) != 8:
            continue

        session_id = session_dir.name
        session = load_session(data_dir, session_id)
        report = session.get("report") or _load_report_json(report_root, session_id) or {}
        improvement = _resolve_session_improvement(session)
        timestamp = _resolve_session_timestamp(session_dir, report)

        session_state: dict[str, dict] = {}
        recommended = _parse_recommendations_file(session_dir / "05_recommendations.md")
        rejected = _parse_rejection_file(session_dir / "06_rejections.md")
        applied = _collect_applied_changes(session.get("iterations", []))

        for item in recommended:
            parameter = item["parameter"]
            state = session_state.setdefault(
                parameter,
                {
                    "parameter": parameter,
                    "scope": item.get("scope", "unknown"),
                    "recommended": False,
                    "applied": False,
                    "rejected": False,
                    "values": set(),
                    "titles": set(),
                },
            )
            state["recommended"] = True
            if item.get("value"):
                state["values"].add(str(item["value"]))
            if item.get("title"):
                state["titles"].add(str(item["title"]))

        for item in rejected:
            parameter = item["parameter"]
            state = session_state.setdefault(
                parameter,
                {
                    "parameter": parameter,
                    "scope": item.get("scope", "unknown"),
                    "recommended": True,
                    "applied": False,
                    "rejected": False,
                    "values": set(),
                    "titles": set(),
                },
            )
            state["recommended"] = True
            state["rejected"] = True
            if item.get("scope") and state.get("scope") == "unknown":
                state["scope"] = item["scope"]
            if item.get("value"):
                state["values"].add(str(item["value"]))
            if item.get("title"):
                state["titles"].add(str(item["title"]))

        for item in applied:
            parameter = item["parameter"]
            state = session_state.setdefault(
                parameter,
                {
                    "parameter": parameter,
                    "scope": item.get("scope", "unknown"),
                    "recommended": False,
                    "applied": False,
                    "rejected": False,
                    "values": set(),
                    "titles": set(),
                },
            )
            state["applied"] = True
            if item.get("scope") and state.get("scope") == "unknown":
                state["scope"] = item["scope"]
            if item.get("value"):
                state["values"].add(str(item["value"]))
            if item.get("title"):
                state["titles"].add(str(item["title"]))

        session_rows.append(
            {
                "session_id": session_id,
                "timestamp": timestamp,
                "improvement_pct": improvement,
                "iterations": len(session.get("iterations", [])),
                "parameter_count": len(session_state),
                "applied_count": sum(1 for item in session_state.values() if item["applied"]),
                "rejected_count": sum(1 for item in session_state.values() if item["rejected"]),
                "best_small_rps": _resolve_best_workload_rps(session, "small"),
                "best_homepage_rps": _resolve_best_workload_rps(session, "homepage"),
            }
        )
        session_states[session_id] = session_state
        session_precise_applied[session_id] = _collect_precise_applied(
            session.get("iterations", [])
        )
        session_param_sources[session_id] = _collect_param_sources(session.get("iterations", []))

        for parameter, state in session_state.items():
            parameter_sessions[parameter][session_id] = {
                "state": _resolve_parameter_state(state),
                "scope": state.get("scope", "unknown"),
                "improvement_pct": improvement,
                "values": sorted(state["values"]),
                "titles": sorted(state["titles"]),
            }

    parameters: list[dict] = []
    for parameter, session_map in parameter_sessions.items():
        rows = list(session_map.values())
        sessions_seen = len(rows)
        applied_rows = [row for row in rows if row["state"] == "applied"]
        rejected_rows = [row for row in rows if row["state"] == "rejected"]
        recommended_rows = [
            row for row in rows if row["state"] in {"recommended", "applied", "rejected"}
        ]
        applied_improvements = [row["improvement_pct"] for row in applied_rows]
        rejected_improvements = [row["improvement_pct"] for row in rejected_rows]
        acceptance_rate = len(applied_rows) / len(recommended_rows) if recommended_rows else 0.0
        avg_improvement = _avg(applied_improvements)
        score = (
            len(applied_rows) * 3.0
            + len([row for row in applied_rows if row["improvement_pct"] > 0]) * 2.0
            - len(rejected_rows) * 2.0
            + avg_improvement / 25.0
        )
        parameters.append(
            {
                "parameter": parameter,
                "scope": _majority_scope(rows),
                "sessions_seen": sessions_seen,
                "recommended_sessions": len(recommended_rows),
                "applied_sessions": len(applied_rows),
                "rejected_sessions": len(rejected_rows),
                "acceptance_rate": round(acceptance_rate, 3),
                "avg_improvement_when_applied": round(avg_improvement, 2),
                "avg_improvement_when_rejected": round(_avg(rejected_improvements), 2),
                "score": round(score, 2),
                "temperature": _classify_parameter(
                    acceptance_rate=acceptance_rate,
                    avg_improvement=avg_improvement,
                    rejected_count=len(rejected_rows),
                    applied_count=len(applied_rows),
                ),
                "sessions": {
                    session_id: value["state"] for session_id, value in sorted(session_map.items())
                },
                "values": _collect_unique_values(rows),
                "source_agents": _collect_source_agents(
                    session_id_map=session_map,
                    session_param_sources=session_param_sources,
                    parameter=parameter,
                ),
            }
        )

    parameters.sort(
        key=lambda item: (
            -item["score"],
            -item["sessions_seen"],
            item["parameter"],
        )
    )
    session_rows.sort(key=lambda item: item.get("timestamp", ""), reverse=True)

    matrix = []
    for item in parameters:
        matrix.append(
            {
                "parameter": item["parameter"],
                "scope": item["scope"],
                "score": item["score"],
                "sessions": [
                    {
                        "session_id": session["session_id"],
                        "state": item["sessions"].get(session["session_id"], "none"),
                        "improvement_pct": session["improvement_pct"],
                    }
                    for session in session_rows
                ],
            }
        )

    return {
        "sessions": session_rows,
        "parameters": parameters,
        "matrix": matrix,
        "winning_gaps": _build_winning_gaps(
            session_rows, session_states, session_precise_applied, session_param_sources
        ),
        "summary": {
            "session_count": len(session_rows),
            "parameter_count": len(parameters),
            "hot_count": sum(1 for item in parameters if item["temperature"] == "hot"),
            "cold_count": sum(1 for item in parameters if item["temperature"] == "cold"),
        },
    }


def _parse_summary_md(text: str) -> dict:
    """Parse iter{N}_00_summary.md into structured data."""
    result: dict = {
        "benchmarks": [],
        "applied_changes": [],
        "regressions": [],
        "decision": "",
    }

    # Parse benchmark table
    in_bench = False
    for line in text.splitlines():
        line = line.strip()
        if "Baseline RPS" in line and "Current RPS" in line:
            in_bench = True
            continue
        if in_bench and line.startswith("|") and "---" not in line:
            cols = [c.strip() for c in line.split("|")[1:-1]]
            if len(cols) >= 6:
                result["benchmarks"].append(
                    {
                        "workload": cols[0],
                        "baseline_rps": _safe_float(cols[1]),
                        "current_rps": _safe_float(cols[2]),
                        "change": cols[3],
                        "p99_ms": _safe_float(cols[4]),
                        "status": cols[5],
                    }
                )
        elif in_bench and not line.startswith("|"):
            in_bench = False

        # Parse applied changes
        if line.startswith("- ["):
            m = re.match(r"- \[(\w+)\] (.+?): (\{.+\})", line)
            if m:
                result["applied_changes"].append(
                    {
                        "scope": m.group(1),
                        "title": m.group(2),
                        "changes": _parse_changes_literal(m.group(3)),
                        "raw_changes": m.group(3),
                    }
                )

        # Parse regressions
        if line.startswith("- ") and "vs baseline" in line:
            result["regressions"].append(line[2:])

    # Parse decision (outside loop — operates on full text)
    if "## Decision" in text:
        decision_lines = text.split("## Decision")[-1].strip().split("\n")
        result["decision"] = (
            decision_lines[1].strip()
            if len(decision_lines) > 1
            else decision_lines[0].strip()
            if decision_lines
            else ""
        )

    # Extract nginx/system applied booleans
    for line in text.splitlines():
        if "Nginx applied:" in line:
            result["nginx_applied"] = "True" in line
        if "System applied:" in line:
            result["system_applied"] = "True" in line

    return result


def _parse_agent_md(text: str) -> dict:
    """Parse agent markdown file to extract summary and JSON payload."""
    result: dict = {"summary": "", "payload": {}}

    # Extract summary
    if "## Summary" in text:
        summary_section = text.split("## Summary")[1]
        if "## Payload" in summary_section:
            summary_section = summary_section.split("## Payload")[0]
        result["summary"] = summary_section.strip()

    # Extract JSON payload from code block
    json_match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    if json_match:
        try:
            result["payload"] = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            result["payload"] = {}

    return result


def _safe_float(val: str) -> float:
    try:
        return float(val.replace(",", "").replace("%", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _parse_changes_literal(raw: str) -> dict:
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_report_json(report_dir: Path, session_id: str) -> dict | None:
    for report_file in report_dir.glob(f"report_*_{session_id}.json"):
        try:
            return json.loads(report_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _collect_applied_changes(iterations: list[dict]) -> list[dict]:
    results: list[dict] = []
    for iteration in iterations:
        summary = iteration.get("summary") or {}
        for item in summary.get("applied_changes", []):
            for parameter, value in (item.get("changes") or {}).items():
                results.append(
                    {
                        "parameter": str(parameter),
                        "value": str(value),
                        "scope": item.get("scope", "unknown"),
                        "title": item.get("title", ""),
                    }
                )
    return results


def _parse_recommendations_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    blocks = _extract_json_code_blocks(path.read_text(encoding="utf-8"))
    items: list[dict] = []
    for block in blocks:
        if isinstance(block, list):
            for entry in block:
                if not isinstance(entry, dict):
                    continue
                scope = str(entry.get("scope", "unknown"))
                title = str(entry.get("title", ""))
                for parameter, value in (entry.get("changes") or {}).items():
                    items.append(
                        {
                            "parameter": str(parameter),
                            "value": str(value),
                            "scope": scope,
                            "title": title,
                        }
                    )
    return items


def _parse_rejection_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items: list[dict] = []
    for block in _extract_json_code_blocks(path.read_text(encoding="utf-8")):
        if not isinstance(block, dict):
            continue
        scope = str(block.get("scope") or block.get("raw", {}).get("scope") or "unknown")
        title = str(block.get("raw", {}).get("title", ""))
        changes = block.get("normalized_changes") or block.get("raw", {}).get("changes") or {}
        if not isinstance(changes, dict):
            continue
        for parameter, value in changes.items():
            items.append(
                {
                    "parameter": str(parameter),
                    "value": str(value),
                    "scope": scope,
                    "title": title,
                }
            )
    return items


def _extract_json_code_blocks(text: str) -> list[dict | list]:
    blocks = []
    for match in re.finditer(r"```json\s*\n(.*?)\n```", text, re.DOTALL):
        try:
            blocks.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return blocks


def _resolve_session_improvement(session: dict) -> float:
    report = session.get("report") or {}
    if report.get("total_improvement_pct") is not None:
        return _safe_float(str(report.get("total_improvement_pct")))

    best_change = None
    for iteration in session.get("iterations", []):
        for benchmark in (iteration.get("summary") or {}).get("benchmarks", []):
            baseline = float(benchmark.get("baseline_rps") or 0.0)
            current = float(benchmark.get("current_rps") or 0.0)
            if baseline <= 0:
                continue
            change = ((current - baseline) / baseline) * 100.0
            best_change = change if best_change is None else max(best_change, change)
    return round(best_change or 0.0, 2)


def _resolve_best_workload_rps(session: dict, workload: str) -> float:
    report = session.get("report") or {}
    best_from_report = (
        (report.get("best_results_by_size") or {}).get(workload, {}).get("rps")
        or (report.get("finals_by_size") or {}).get(workload, {}).get("rps")
        or (report.get("baselines_by_size") or {}).get(workload, {}).get("rps")
    )
    if best_from_report is not None:
        return float(best_from_report or 0.0)

    best_seen = 0.0
    for iteration in session.get("iterations", []):
        for benchmark in (iteration.get("summary") or {}).get("benchmarks", []):
            if benchmark.get("workload") != workload:
                continue
            best_seen = max(best_seen, float(benchmark.get("current_rps") or 0.0))
    return best_seen


def _resolve_parameter_state(state: dict) -> str:
    if state.get("applied"):
        return "applied"
    if state.get("rejected"):
        return "rejected"
    if state.get("recommended"):
        return "recommended"
    return "none"


def _avg(values: list[float]) -> float:
    usable = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    return sum(usable) / len(usable) if usable else 0.0


def _majority_scope(rows: list[dict]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get("scope", "unknown"))] += 1
    return max(counts.items(), key=lambda item: item[1])[0] if counts else "unknown"


def _collect_unique_values(rows: list[dict]) -> list[str]:
    values: set[str] = set()
    for row in rows:
        for value in row.get("values", []):
            values.add(value)
    return sorted(values)


def _classify_parameter(
    *,
    acceptance_rate: float,
    avg_improvement: float,
    rejected_count: int,
    applied_count: int,
) -> str:
    if applied_count >= 2 and acceptance_rate >= 0.6 and avg_improvement > 0:
        return "hot"
    if rejected_count >= max(applied_count, 1) and acceptance_rate < 0.4:
        return "cold"
    return "mixed"


def _build_winning_gaps(
    session_rows: list[dict],
    session_states: dict[str, dict[str, dict]],
    session_precise_applied: dict[str, dict[str, str]],
    session_param_sources: dict[str, dict[str, set[str]]],
) -> dict:
    if not session_rows:
        return {"reference": None, "sessions": []}

    ranked = sorted(
        [item for item in session_rows if item["session_id"] not in IGNORED_SESSION_IDS],
        key=lambda item: (
            -float(item.get("best_small_rps") or 0.0),
            -float(item.get("best_homepage_rps") or 0.0),
            -float(item.get("improvement_pct") or 0.0),
        ),
    )
    reference = ranked[0]
    if REFERENCE_SESSION_OVERRIDE:
        override = next(
            (item for item in ranked if item["session_id"] == REFERENCE_SESSION_OVERRIDE),
            None,
        )
        if override:
            reference = override
    current_rank_map = {
        item["session_id"]: index for index, item in enumerate(ranked[:LEADERBOARD_LIMIT], start=1)
    }
    previous_rank_map = _previous_rank_map(ranked)
    ref_applied = session_precise_applied.get(reference["session_id"], {})
    ref_bundles = _bundle_counts(ref_applied.keys())

    gaps = []
    for session in ranked:
        if session["session_id"] == reference["session_id"]:
            continue
        current_state = session_states.get(session["session_id"], {})
        current_applied = session_precise_applied.get(session["session_id"], {})
        missing_params = sorted(set(ref_applied) - set(current_applied))
        differing_params = sorted(
            parameter
            for parameter, ref_value in ref_applied.items()
            if (parameter in current_applied and str(current_applied[parameter]) != str(ref_value))
        )
        rejected_winners = sorted(
            parameter
            for parameter in set(missing_params) | set(differing_params)
            if _resolve_parameter_state(current_state.get(parameter, {})) == "rejected"
        )
        current_bundle_counts = _bundle_match_counts(ref_applied, current_applied)
        bundle_gaps = []
        for bundle, ref_count in ref_bundles.items():
            current_count = current_bundle_counts.get(bundle, 0)
            if current_count < ref_count:
                bundle_gaps.append(
                    {
                        "bundle": bundle,
                        "present": current_count,
                        "reference": ref_count,
                        "missing": ref_count - current_count,
                    }
                )
        gap_score = len(missing_params) + len(differing_params) * 2 + len(rejected_winners) * 2
        gaps.append(
            {
                "session_id": session["session_id"],
                "best_small_rps": session.get("best_small_rps", 0.0),
                "best_homepage_rps": session.get("best_homepage_rps", 0.0),
                "improvement_pct": session.get("improvement_pct", 0.0),
                "timestamp": session.get("timestamp", ""),
                "leaderboard_rank": current_rank_map.get(session["session_id"]),
                "rank_delta": _rank_delta(
                    current_rank_map.get(session["session_id"]),
                    previous_rank_map.get(session["session_id"]),
                ),
                "is_new_entry": (
                    current_rank_map.get(session["session_id"]) is not None
                    and previous_rank_map.get(session["session_id"]) is None
                ),
                "performance_vs_reference_pct": _ratio_pct(
                    session.get("best_small_rps", 0.0),
                    reference.get("best_small_rps", 0.0),
                ),
                "missing_parameters": missing_params,
                "missing_count": len(missing_params),
                "differing_parameters": [
                    {
                        "parameter": parameter,
                        "reference_value": str(ref_applied.get(parameter, "")),
                        "session_value": str(current_applied.get(parameter, "")),
                    }
                    for parameter in differing_params
                ],
                "differing_count": len(differing_params),
                "rejected_winning_parameters": rejected_winners,
                "bundle_gaps": sorted(
                    bundle_gaps, key=lambda item: (-item["missing"], item["bundle"])
                ),
                "gap_score": gap_score,
                "current_applied_parameters": [
                    {
                        "parameter": parameter,
                        "value": str(value),
                        "bundle": _bundle_for_parameter(parameter),
                        "sources": sorted(
                            session_param_sources.get(session["session_id"], {}).get(
                                parameter, set()
                            )
                        ),
                    }
                    for parameter, value in sorted(current_applied.items())
                ],
            }
        )

    gaps.sort(
        key=lambda item: (
            item["gap_score"],
            item["performance_vs_reference_pct"],
        )
    )
    return {
        "reference": {
            "session_id": reference["session_id"],
            "best_small_rps": reference.get("best_small_rps", 0.0),
            "best_homepage_rps": reference.get("best_homepage_rps", 0.0),
            "improvement_pct": reference.get("improvement_pct", 0.0),
            "timestamp": reference.get("timestamp", ""),
            "leaderboard_rank": current_rank_map.get(reference["session_id"], 1),
            "rank_delta": _rank_delta(
                current_rank_map.get(reference["session_id"]),
                previous_rank_map.get(reference["session_id"]),
            ),
            "is_new_entry": (
                current_rank_map.get(reference["session_id"]) is not None
                and previous_rank_map.get(reference["session_id"]) is None
            ),
            "selection_mode": "override"
            if reference["session_id"] == REFERENCE_SESSION_OVERRIDE
            else "top_rps",
            "applied_parameters": [
                {
                    "parameter": parameter,
                    "value": str(value),
                    "bundle": _bundle_for_parameter(parameter),
                    "sources": sorted(
                        session_param_sources.get(reference["session_id"], {}).get(
                            parameter,
                            set(),
                        )
                    ),
                }
                for parameter, value in sorted(ref_applied.items())
            ],
            "bundle_counts": [
                {"bundle": bundle, "count": count} for bundle, count in sorted(ref_bundles.items())
            ],
        },
        "sessions": gaps,
    }


def _bundle_counts(parameters: object) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for parameter in parameters:
        counts[_bundle_for_parameter(str(parameter))] += 1
    return counts


def _bundle_match_counts(
    reference_values: dict[str, object],
    current_values: dict[str, object],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for parameter, ref_value in reference_values.items():
        if parameter in current_values and str(current_values[parameter]) == str(ref_value):
            counts[_bundle_for_parameter(parameter)] += 1
    return counts


def _bundle_for_parameter(parameter: str) -> str:
    return PRECISE_BUNDLE_MAP.get(parameter, "other")


def _ratio_pct(value: object, reference: object) -> float:
    ref = float(reference or 0.0)
    cur = float(value or 0.0)
    if ref <= 0:
        return 0.0
    return round(cur / ref * 100.0, 1)


def _resolve_session_timestamp(session_dir: Path, report: dict) -> str:
    generated_at = str(report.get("generated_at") or "").strip()
    if generated_at:
        return generated_at
    latest_mtime = session_dir.stat().st_mtime
    for path in session_dir.rglob("*"):
        try:
            latest_mtime = max(latest_mtime, path.stat().st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat()


def _previous_rank_map(ranked: list[dict]) -> dict[str, int]:
    latest = max(ranked, key=lambda item: str(item.get("timestamp", "")), default=None)
    if not latest:
        return {}
    if not latest.get("timestamp"):
        return {}
    previous = [item for item in ranked if item["session_id"] != latest["session_id"]]
    previous.sort(
        key=lambda item: (
            -float(item.get("best_small_rps") or 0.0),
            -float(item.get("best_homepage_rps") or 0.0),
            -float(item.get("improvement_pct") or 0.0),
        ),
    )
    return {
        item["session_id"]: index
        for index, item in enumerate(previous[:LEADERBOARD_LIMIT], start=1)
    }


def _rank_delta(current_rank: int | None, previous_rank: int | None) -> int | None:
    if current_rank is None:
        return None
    if previous_rank is None:
        return None
    return previous_rank - current_rank


def _primary_value(state: dict) -> str:
    values = state.get("values", [])
    if values:
        return str(sorted(values)[0])
    return ""


def _collect_precise_applied(iterations: list[dict]) -> dict[str, str]:
    applied: dict[str, str] = {}
    for iteration in iterations:
        summary = iteration.get("summary") or {}
        for item in summary.get("applied_changes", []):
            for parameter, value in (item.get("changes") or {}).items():
                applied[str(parameter)] = str(value)

        planner = (iteration.get("apply_planner") or {}).get("payload") or {}
        if not isinstance(planner, dict):
            continue
        nginx_applied = bool(summary.get("nginx_applied"))
        system_applied = bool(summary.get("system_applied"))
        for category, changes in planner.items():
            if not isinstance(changes, dict):
                continue
            normalized_category = str(category).strip().lower()
            if normalized_category in {"nginx", "webserver"} and not nginx_applied:
                continue
            if (
                normalized_category in {"system", "kernel", "resource_limits", "network", "storage"}
                and not system_applied
            ):
                continue
            for parameter, value in changes.items():
                key = _normalize_precise_param(str(parameter))
                if key:
                    applied[key] = str(value)
    return applied


def _normalize_precise_param(parameter: str) -> str:
    aliases = {
        "systemd_nofile": "nofile",
        "LimitNOFILE": "nofile",
        "IOWeight": "cgroup_io_weight",
        "CPUWeight": "cgroup_cpu_weight",
        "tc_qdisc": "tc_rules",
    }
    return aliases.get(parameter, parameter)


def _collect_param_sources(iterations: list[dict]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for iteration in iterations:
        nginx_payload = (iteration.get("nginx_expert") or {}).get("payload") or {}
        for parameter in _extract_nginx_expert_params(nginx_payload):
            result[parameter].add("nginx_expert")
        rhel_payload = (iteration.get("rhel_expert") or {}).get("payload") or {}
        for parameter in _extract_rhel_expert_params(rhel_payload):
            result[parameter].add("rhel_expert")
        synth_payload = (iteration.get("synthesizer") or {}).get("payload") or {}
        for parameter in _extract_synthesizer_params(synth_payload):
            result[parameter].add("synthesizer")
    return result


def _extract_nginx_expert_params(payload: dict) -> set[str]:
    params: set[str] = set()
    for item in payload.get("recommendations", []) if isinstance(payload, dict) else []:
        if isinstance(item, dict) and item.get("setting"):
            params.add(_normalize_precise_param(str(item["setting"])))
    return params


def _extract_rhel_expert_params(payload: dict) -> set[str]:
    params: set[str] = set()
    known = sorted(PRECISE_BUNDLE_MAP.keys(), key=len, reverse=True)
    aliases = {
        "LimitNOFILE": "nofile",
        "IOWeight": "cgroup_io_weight",
        "CPUWeight": "cgroup_cpu_weight",
        "fq_codel": "tc_rules",
    }
    for item in payload.get("recommendations", []) if isinstance(payload, dict) else []:
        text = ""
        if isinstance(item, dict):
            text = str(item.get("action", ""))
        for key in known:
            if key in text:
                params.add(_normalize_precise_param(key))
        for raw, normalized in aliases.items():
            if raw in text:
                params.add(normalized)
    return params


def _extract_synthesizer_params(payload: dict) -> set[str]:
    params: set[str] = set()
    known = sorted(
        list(PRECISE_BUNDLE_MAP.keys()) + ["LimitNOFILE", "IOWeight", "CPUWeight", "tc_qdisc"],
        key=len,
        reverse=True,
    )
    for item in payload.get("recommendations", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        changes = item.get("changes")
        if isinstance(changes, dict):
            for key in changes:
                params.add(_normalize_precise_param(str(key)))
            continue
        if item.get("directive"):
            params.add(_normalize_precise_param(str(item["directive"])))
        commands = item.get("commands")
        if isinstance(commands, list):
            blob = "\n".join(str(cmd) for cmd in commands)
            for key in known:
                if key in blob:
                    params.add(_normalize_precise_param(key))
    return params


def _collect_source_agents(
    *,
    session_id_map: dict[str, dict],
    session_param_sources: dict[str, dict[str, set[str]]],
    parameter: str,
) -> list[str]:
    agents: set[str] = set()
    for session_id in session_id_map:
        agents.update(session_param_sources.get(session_id, {}).get(parameter, set()))
    return sorted(agents)
