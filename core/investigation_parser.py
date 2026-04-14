"""Parsers for SRE investigation agent LLM responses."""

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("slayMetrics.investigator")


@dataclass
class PlannedHypothesis:
    """One hypothesis from the investigation plan."""
    priority: int
    hypothesis: str
    estimated_impact: str
    evidence_so_far: str
    commands_to_verify: list[str] = field(default_factory=list)


@dataclass
class InvestigationPlan:
    """Upfront investigation plan from the planning LLM call."""
    system_blueprint: dict = field(default_factory=dict)
    hypotheses: list[PlannedHypothesis] = field(default_factory=list)


@dataclass
class InvestigationResult:
    """Parsed output from one investigation turn."""
    done: bool
    commands: list[str] = field(default_factory=list)
    hypothesis: str = ""
    evidence: str = ""
    plan: str = ""
    reasoning: str = ""
    findings: str = ""
    layer: str = ""


def _strip_code_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
    return raw


def format_structured_findings(findings: dict) -> str:
    """Convert structured findings dict into readable text for domain analyzers."""
    sections = []
    severity = findings.get("severity", "unknown")

    bp = findings.get("system_blueprint")
    if bp and isinstance(bp, dict):
        sections.append(
            f"[SYSTEM BLUEPRINT]\n"
            f"  CPU: {bp.get('cpu_cores', '?')} cores | Memory: {bp.get('memory_gb', '?')}GB | "
            f"NIC: {bp.get('nic_speed', '?')} | Disk: {bp.get('disk_type', '?')}\n"
            f"  Theoretical max small RPS: {bp.get('theoretical_max_rps_small', '?')}\n"
            f"  Actual small RPS: {bp.get('actual_rps_small', '?')}\n"
            f"  Capacity utilization: {bp.get('capacity_utilization', '?')}"
        )

    ranking = findings.get("bottleneck_ranking")
    if ranking and isinstance(ranking, list):
        items = []
        for i, b in enumerate(ranking, 1):
            if isinstance(b, dict):
                items.append(f"  {i}. [{b.get('severity', '?').upper()}] "
                             f"{b.get('issue', '?')} — {b.get('impact', '?')}")
            else:
                items.append(f"  {i}. {b}")
        sections.append("[BOTTLENECK RANKING]\n" + "\n".join(items))

    chain = findings.get("fix_dependency_chain")
    if chain and isinstance(chain, list):
        sections.append("[FIX DEPENDENCY CHAIN]\n" + "\n".join(f"  {c}" for c in chain))

    plan = findings.get("attack_plan")
    if plan and isinstance(plan, list):
        items = []
        for p in plan:
            if isinstance(p, dict):
                fixes = ", ".join(p.get("fixes", []))
                items.append(f"  Phase {p.get('phase', '?')}: {p.get('label', '?')} → {fixes}")
            else:
                items.append(f"  {p}")
        sections.append("[ATTACK PLAN]\n" + "\n".join(items))

    for key, title in [("cross_layer_violations", "CROSS-LAYER VIOLATIONS"),
                       ("systemd_sabotage", "SYSTEMD SABOTAGE"),
                       ("effective_nginx_values", "EFFECTIVE NGINX VALUES")]:
        val = findings.get(key)
        if not val:
            continue
        if isinstance(val, dict):
            items = [f"  {k}: {v}" for k, v in val.items()]
        elif isinstance(val, list):
            items = [f"  - {v}" for v in val]
        else:
            items = [f"  {val}"]
        sections.append(f"[{title}]\n" + "\n".join(items))

    header = f"=== SRE Investigation Report (severity: {severity}) ==="
    return header + "\n\n" + "\n\n".join(sections)


def _extract_from_reasoning(reasoning: str, field_name: str) -> str:
    """Extract hypothesis/evidence/plan from reasoning text."""
    patterns = {
        "hypothesis": r"[Hh]ypothesis:\s*(.+?)(?=\s*(?:Test:|Evidence:|Observation:|Plan:|$))",
        "evidence": r"(?:Observation|Evidence):\s*(.+?)(?=\s*(?:Hypothesis:|Test:|Plan:|$))",
        "plan": r"(?:Test|Plan):\s*(.+?)(?=\s*(?:Hypothesis:|Observation:|Evidence:|$))",
    }
    pattern = patterns.get(field_name)
    if not pattern:
        return ""
    match = re.search(pattern, reasoning, re.DOTALL)
    return match.group(1).strip()[:300] if match else ""


def parse_response(raw: str) -> InvestigationResult:
    """Parse LLM JSON response into InvestigationResult."""
    raw = _strip_code_fence(raw)
    try:
        data = json.loads(raw)
        findings_raw = data.get("findings", "")
        if isinstance(findings_raw, dict):
            findings_text = format_structured_findings(findings_raw)
        else:
            findings_text = str(findings_raw)

        reasoning = data.get("reasoning", "")
        hypothesis = data.get("hypothesis", "")
        evidence = data.get("evidence", "")
        plan = data.get("plan", "")

        if not hypothesis and reasoning:
            hypothesis = _extract_from_reasoning(reasoning, "hypothesis")
        if not evidence and reasoning:
            evidence = _extract_from_reasoning(reasoning, "evidence")
        if not plan and reasoning:
            plan = _extract_from_reasoning(reasoning, "plan")

        return InvestigationResult(
            done=bool(data.get("done", False)),
            commands=data.get("commands", []),
            hypothesis=hypothesis, evidence=evidence, plan=plan,
            reasoning=reasoning, findings=findings_text,
            layer=data.get("layer", ""),
        )
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Failed to parse investigation response, treating as done")
        return InvestigationResult(done=True, findings="(parse error — investigation ended)")


def parse_plan(raw: str) -> InvestigationPlan:
    """Parse the planning LLM response into an InvestigationPlan."""
    raw = _strip_code_fence(raw)
    try:
        data = json.loads(raw)
        blueprint = data.get("system_blueprint", {})
        raw_plan = data.get("investigation_plan", [])
        hypotheses = []
        for item in raw_plan:
            if isinstance(item, dict):
                hypotheses.append(PlannedHypothesis(
                    priority=item.get("priority", 99),
                    hypothesis=item.get("hypothesis", ""),
                    estimated_impact=item.get("estimated_impact", ""),
                    evidence_so_far=item.get("evidence_so_far", ""),
                    commands_to_verify=item.get("commands_to_verify", []),
                ))
        hypotheses.sort(key=lambda h: h.priority)
        return InvestigationPlan(system_blueprint=blueprint, hypotheses=hypotheses)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Failed to parse investigation plan")
        return InvestigationPlan()
