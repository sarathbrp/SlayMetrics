"""Parse hypothesis markdown files and report JSON into dashboard data."""

from __future__ import annotations

import json
import re
from pathlib import Path


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

    result = sorted(sessions.values(), key=lambda s: s.get("timestamp", ""), reverse=True)
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
                        "changes": m.group(3),
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
