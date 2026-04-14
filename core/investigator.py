"""Autonomous SRE investigation agent.

Uses a DSPy-driven multi-turn loop to investigate the DUT's performance
stack via SSH. Each step() call is one LLM turn that can request diagnostic
commands and accumulate findings across 5 layers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import dspy

from .config import Config
from .analyzer_utils import extract_tokens

logger = logging.getLogger("slayMetrics.investigator")


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


def _format_structured_findings(findings: dict) -> str:
    """Convert structured findings dict into readable text for domain analyzers."""
    sections = []
    severity = findings.get("severity", "unknown")

    # System blueprint
    bp = findings.get("system_blueprint")
    if bp and isinstance(bp, dict):
        util = bp.get("capacity_utilization", "?")
        sections.append(
            f"[SYSTEM BLUEPRINT]\n"
            f"  CPU: {bp.get('cpu_cores', '?')} cores | Memory: {bp.get('memory_gb', '?')}GB | "
            f"NIC: {bp.get('nic_speed', '?')} | Disk: {bp.get('disk_type', '?')}\n"
            f"  Theoretical max small RPS: {bp.get('theoretical_max_rps_small', '?')}\n"
            f"  Actual small RPS: {bp.get('actual_rps_small', '?')}\n"
            f"  Capacity utilization: {util}"
        )

    # Bottleneck ranking
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

    # Fix dependency chain
    chain = findings.get("fix_dependency_chain")
    if chain and isinstance(chain, list):
        sections.append("[FIX DEPENDENCY CHAIN]\n" + "\n".join(f"  {c}" for c in chain))

    # Attack plan
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

    # Remaining sections (cross_layer_violations, systemd_sabotage, etc.)
    extra_labels = {
        "cross_layer_violations": "CROSS-LAYER VIOLATIONS",
        "systemd_sabotage": "SYSTEMD SABOTAGE",
        "effective_nginx_values": "EFFECTIVE NGINX VALUES",
    }
    for key, title in extra_labels.items():
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


def _extract_from_reasoning(reasoning: str, field: str, fallback: str = "") -> str:
    """Extract a section from reasoning text when the LLM embeds it inline.

    Looks for patterns like "Hypothesis: ..." or "Test: ..." in the reasoning.
    """
    import re
    patterns = {
        "hypothesis": r"[Hh]ypothesis:\s*(.+?)(?=\s*(?:Test:|Evidence:|Observation:|Plan:|$))",
        "evidence": r"(?:Observation|Evidence):\s*(.+?)(?=\s*(?:Hypothesis:|Test:|Plan:|$))",
        "plan": r"(?:Test|Plan):\s*(.+?)(?=\s*(?:Hypothesis:|Observation:|Evidence:|$))",
    }
    pattern = patterns.get(field)
    if not pattern:
        return fallback
    match = re.search(pattern, reasoning, re.DOTALL)
    if match:
        return match.group(1).strip()[:300]
    return fallback


def _parse_response(raw: str) -> InvestigationResult:
    """Parse LLM JSON response into InvestigationResult."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
    try:
        data = json.loads(raw)
        findings_raw = data.get("findings", "")
        # When done=true, findings may be a structured dict — format it
        if isinstance(findings_raw, dict):
            findings_text = _format_structured_findings(findings_raw)
        else:
            findings_text = str(findings_raw)

        reasoning = data.get("reasoning", "")
        hypothesis = data.get("hypothesis", "")
        evidence = data.get("evidence", "")
        plan = data.get("plan", "")

        # If model didn't use separate fields, extract from reasoning text
        if not hypothesis and reasoning:
            hypothesis = _extract_from_reasoning(reasoning, "hypothesis")
        if not evidence and reasoning:
            evidence = _extract_from_reasoning(reasoning, "evidence")
        if not plan and reasoning:
            plan = _extract_from_reasoning(reasoning, "plan")

        return InvestigationResult(
            done=bool(data.get("done", False)),
            commands=data.get("commands", []),
            hypothesis=hypothesis,
            evidence=evidence,
            plan=plan,
            reasoning=reasoning,
            findings=findings_text,
            layer=data.get("layer", ""),
        )
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Failed to parse investigation response, treating as done")
        return InvestigationResult(done=True, findings="(parse error — investigation ended)")


class SREInvestigator:
    """DSPy-driven interactive investigator for the DUT performance stack."""

    def __init__(self, config: Config, prompts_dir: Path):
        self.config = config
        self.prompts_dir = prompts_dir
        self._module: dspy.Module | None = None

    def _build(self) -> dspy.Module:
        instructions = (self.prompts_dir / "investigation.md").read_text()

        class Sig(dspy.Signature):
            audit_baseline: str = dspy.InputField(
                desc="Static audit output from omega_master_audit.sh (5-group baseline)"
            )
            benchmark_results: str = dspy.InputField(
                desc="Benchmark RPS results per workload"
            )
            live_audit_output: str = dspy.InputField(
                desc="Dynamic runtime metrics from live sampler during benchmark"
            )
            previous_findings: str = dspy.InputField(
                desc=(
                    "Commands already run and their outputs from prior iterations. "
                    "Empty on first iteration. Build on these — do not repeat."
                )
            )
            performance_rules: str = dspy.InputField(
                desc="Mandatory performance rules — constraint chains, fix ordering, hard rules. MUST follow."
            )
            response_json: str = dspy.OutputField(
                desc=(
                    'JSON: {"layer": "1-5 or cross-layer", "commands": ["cmd1", ...], '
                    '"reasoning": "why", "findings": "summary so far", "done": bool}'
                )
            )

        Sig.__doc__ = instructions
        return dspy.Predict(Sig)

    def step(
        self,
        audit_baseline: str,
        benchmark_results: str,
        live_audit_output: str,
        previous_findings: str,
        performance_rules: str = "",
    ) -> tuple[InvestigationResult, int, int, float]:
        """Run one investigation turn.

        Returns (result, input_tokens, output_tokens, elapsed_seconds).
        """
        if self._module is None:
            self._module = self._build()
        logger.info("Investigation step — running inference...")
        t0 = datetime.now()
        pred = self._module(
            audit_baseline=audit_baseline,
            benchmark_results=benchmark_results,
            live_audit_output=live_audit_output,
            previous_findings=previous_findings or "First iteration — no prior findings.",
            performance_rules=performance_rules,
        )
        elapsed = (datetime.now() - t0).total_seconds()
        result = _parse_response(pred.response_json)
        in_tok, out_tok = extract_tokens()
        logger.info(
            "Investigation step done in %.1fs — layer=%s, done=%s, commands=%d",
            elapsed, result.layer, result.done, len(result.commands),
        )
        if result.reasoning:
            logger.info("  Reasoning: %s", result.reasoning[:200])
        return result, in_tok, out_tok, elapsed
