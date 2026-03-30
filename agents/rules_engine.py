"""Deterministic rules engine — builds apply plans without LLM tokens.

The inspection tools already compare current values against config.yaml targets
and return ``{needs_fixing: {param: {current, target}}}``.  This module converts
that diff directly into an apply plan in the same format the debate planner
produces, saving 4 LLM calls (~15-19K tokens).

Two modes are supported (selected via ``config.yaml → agent.planner_mode``):

* **deterministic** — zero LLM calls; rules engine builds everything.
* **hybrid** — rules engine builds the plan, then a single LLM validation
  call reviews it for edge cases (~3-5K tokens).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Guardrail: blocked values (mirrors _is_blocked in agent.py)
# ---------------------------------------------------------------------------


def _is_blocked(param: str, value: str, config: dict[str, Any] | None = None) -> bool:
    """Return True if *param=value* is blocked by config.yaml guardrails."""
    blocked = (config or {}).get("tuning", {}).get("blocked_values", {})
    blocked_vals = blocked.get(param)
    if not blocked_vals:
        return False
    return value.strip().lower() in {str(v).lower() for v in blocked_vals}


# ---------------------------------------------------------------------------
# Apply plan builder
# ---------------------------------------------------------------------------

CATEGORY_TARGET_KEYS = {
    "webserver": "webserver_targets",
    "kernel": "kernel_targets",
    "resource_limits": "resource_limits_targets",
    "network": "network_targets",
    "storage": "storage_targets",
}


def build_apply_plan(
    inspection: dict[str, Any], config: dict[str, Any]
) -> dict[str, dict[str, str]]:
    """Build a complete apply plan deterministically from inspection + config.

    Returns the same ``{category: {param: value}}`` structure that the debate
    planner's 4th LLM call (apply_planner) produces.
    """
    tuning = config.get("tuning") or {}
    plan: dict[str, dict[str, str]] = {}

    for category, targets_key in CATEGORY_TARGET_KEYS.items():
        targets = tuning.get(targets_key) or {}
        cat_inspection = inspection.get(category) or {}
        cat_plan: dict[str, str] = {}

        # 1. Everything in needs_fixing → apply the target value
        needs_fixing = cat_inspection.get("needs_fixing") or {}
        for param, info in needs_fixing.items():
            target = str(info.get("target", "") or targets.get(param, "")).strip()
            if target and not _is_blocked(param, target, config):
                cat_plan[param] = target

        # 2. Problems (resource_limits, network, storage) → use config targets
        for problem in cat_inspection.get("problems") or []:
            if not isinstance(problem, dict):
                continue
            param = str(problem.get("param") or problem.get("setting", "")).strip()
            if param and param in targets and param not in cat_plan:
                target = str(targets[param]).strip()
                if not _is_blocked(param, target, config):
                    cat_plan[param] = target

        # 3. Fill any remaining targets the inspection missed
        #    (same safety-net logic as agent.py lines 2401-2420)
        for param, target_val in targets.items():
            target = str(target_val).strip()
            if param not in cat_plan and not _is_blocked(param, target, config):
                cat_plan[param] = target

        plan[category] = cat_plan

    return plan


# ---------------------------------------------------------------------------
# RCA record builder (template-based, for auditability)
# ---------------------------------------------------------------------------


def build_rca_records(inspection: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate structured RCA records from inspection evidence.

    These are deterministic (confidence = 0.99) and provide the same
    schema that the LLM synthesizer would produce.
    """
    records: list[dict[str, Any]] = []
    for category, data in inspection.items():
        if category == "summary":
            continue
        needs_fixing = (data if isinstance(data, dict) else {}).get("needs_fixing") or {}
        for param, info in needs_fixing.items():
            current = str(info.get("current", "unknown"))
            target = str(info.get("target", "unknown"))
            records.append(
                {
                    "symptom": f"{param} misconfigured (current: {current})",
                    "root_cause": f"{param} set to {current}, optimal target is {target}",
                    "confidence": 0.99,
                    "recommendation": f"Set {param} to {target}",
                    "evidence": [f"inspection: {category}.{param} = {current}"],
                }
            )
    return records


# ---------------------------------------------------------------------------
# Recommendation builder (for save_recommendations persistence)
# ---------------------------------------------------------------------------

_SCOPE_MAP = {
    "webserver": "nginx",
    "kernel": "system",
    "resource_limits": "system",
    "network": "system",
    "storage": "system",
}


def build_recommendations(
    inspection: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build structured recommendations matching the LLM output schema."""
    tuning = config.get("tuning") or {}
    recs: list[dict[str, Any]] = []

    for category, targets_key in CATEGORY_TARGET_KEYS.items():
        targets = tuning.get(targets_key) or {}
        cat_inspection = inspection.get(category) or {}
        needs_fixing = cat_inspection.get("needs_fixing") or {}

        for param, info in needs_fixing.items():
            target = str(info.get("target", "") or targets.get(param, "")).strip()
            if not target or _is_blocked(param, target, config):
                continue
            current = str(info.get("current", "unknown"))
            recs.append(
                {
                    "title": f"Set {param} to {target}",
                    "scope": _SCOPE_MAP.get(category, "system"),
                    "changes": {param: target},
                    "rationale": f"Current value {current} is suboptimal; "
                    f"target {target} matches production best practice.",
                    "risk_level": "low",
                }
            )

    return recs


# ---------------------------------------------------------------------------
# Compact plan text (for hybrid LLM validation prompt)
# ---------------------------------------------------------------------------


def compact_plan_text(plan: dict[str, dict[str, str]]) -> str:
    """Render the apply plan as compact ``param=value`` lines.

    Produces ~300 tokens instead of ~2K for the full JSON.
    """
    lines: list[str] = []
    for category, changes in plan.items():
        if not changes:
            continue
        lines.append(f"[{category}]")
        for param, value in changes.items():
            lines.append(f"  {param} = {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summary text (for the report / notes field)
# ---------------------------------------------------------------------------


def build_summary(inspection: dict[str, Any], plan: dict[str, dict[str, str]]) -> str:
    """One-paragraph summary of what the deterministic engine found and planned."""
    total_issues = sum(
        len((v if isinstance(v, dict) else {}).get("needs_fixing") or {})
        for k, v in inspection.items()
        if k != "summary"
    )
    total_fixes = sum(len(v) for v in plan.values())
    categories = [c for c, v in plan.items() if v]
    return (
        f"Deterministic analysis found {total_issues} misconfigured parameters. "
        f"Planned {total_fixes} fixes across {', '.join(categories)}."
    )


# ---------------------------------------------------------------------------
# Hybrid validation prompt builder
# ---------------------------------------------------------------------------


def build_validation_prompt(
    plan: dict[str, dict[str, str]],
    *,
    cpu_cores: int | str = "unknown",
    ram_gb: int | str = "unknown",
    baseline_summary: str = "",
) -> str:
    """Build a compact prompt for the single LLM validation call.

    Input size: ~800 tokens (vs ~9K across 4 debate calls).
    """
    plan_text = compact_plan_text(plan)
    return (
        "You are a performance tuning validator for RHEL + nginx systems. "
        f"Hardware: {cpu_cores} CPU cores, {ram_gb} GB RAM.\n\n"
        "An automated rules engine produced this apply plan from inspection "
        "evidence. Review it for correctness.\n\n"
        f"Apply Plan:\n{plan_text}\n\n"
        f"{f'Baseline: {baseline_summary}' if baseline_summary else ''}\n\n"
        "Tasks:\n"
        "1. Flag changes that could HARM performance on this specific hardware\n"
        "2. Flag any critical missing optimizations\n"
        "3. Provide reasoning in one short paragraph\n\n"
        'Return JSON: {"remove": ["params to drop"], '
        '"add": {"param": "value"}, '
        '"reasoning": "one paragraph"}'
    )


def apply_validation_result(
    plan: dict[str, dict[str, str]],
    validation: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Apply the LLM validator's feedback to the deterministic plan.

    Removes flagged params and adds suggested new ones (if allowed by config).
    Returns a new plan dict.
    """
    plan = {cat: dict(changes) for cat, changes in plan.items()}

    # Remove flagged params
    to_remove = validation.get("remove") or []
    if isinstance(to_remove, list):
        for param in to_remove:
            param = str(param).strip()
            for cat_changes in plan.values():
                cat_changes.pop(param, None)

    # Add suggested params (only if in config allowlist)
    tuning = config.get("tuning") or {}
    to_add = validation.get("add") or {}
    if isinstance(to_add, dict):
        for param, value in to_add.items():
            param = str(param).strip()
            value = str(value).strip()
            if _is_blocked(param, value, config):
                continue
            # Find which category this param belongs to
            for category, targets_key in CATEGORY_TARGET_KEYS.items():
                allowed = tuning.get(targets_key) or {}
                if param in allowed:
                    plan.setdefault(category, {})[param] = value
                    break

    return plan
