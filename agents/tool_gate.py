"""Tool approval gate — user control over apply operations.

Single chokepoint for all apply operations. Supports three modes:
- auto: execute immediately (explicit opt-in)
- interactive: prompt user per category before executing
- dry_run: log plan, skip execution
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core import log as logger


@dataclass
class ToolAction:
    """Describes an apply operation pending approval."""

    scope: str  # "webserver", "kernel", "resource_limits", "network", "storage", "preflight"
    changes: dict[str, str]  # {param: value} or {cmd_N: command_str}
    executor: Callable[[], dict[str, Any]]  # runs the actual apply function
    description: str = ""  # human-readable summary
    before_values: dict[str, str] = field(default_factory=dict)  # current values for display


@dataclass
class ToolDecision:
    """Result of the approval check."""

    approved: bool
    mode: str  # "auto", "interactive", "dry_run"
    reason: str  # "auto-approved", "user-approved", "user-denied", "dry-run"


@dataclass
class ToolResult:
    """Complete outcome of a gated tool call."""

    action: ToolAction
    decision: ToolDecision
    result: dict[str, Any] | None = None  # executor output, None if denied/dry-run


def gate_and_execute(
    action: ToolAction,
    config: dict[str, Any],
    session_id: str,
) -> ToolResult:
    """Resolve approval mode, optionally prompt user, execute or skip, write audit."""
    mode = _resolve_mode(action.scope, config)

    if mode == "dry_run":
        decision = ToolDecision(approved=False, mode="dry_run", reason="dry-run")
        logger.status(
            "gate",
            f"[DRY RUN] {action.scope}: {len(action.changes)} changes — skipped",
        )
        _log_changes(action)
        result = ToolResult(action=action, decision=decision)
        _write_audit(session_id, result, config)
        return result

    if mode == "interactive":
        decision = _prompt_user(action)
    else:
        decision = ToolDecision(approved=True, mode="auto", reason="auto-approved")

    if not decision.approved:
        logger.status("gate", f"[DENIED] {action.scope}: {decision.reason}")
        result = ToolResult(action=action, decision=decision)
        _write_audit(session_id, result, config)
        return result

    # Execute
    try:
        exec_result = action.executor()
    except Exception as e:
        exec_result = {"applied": {}, "failed": action.changes, "error": str(e)}

    result = ToolResult(action=action, decision=decision, result=exec_result)
    _write_audit(session_id, result, config)
    return result


def _resolve_mode(scope: str, config: dict[str, Any]) -> str:
    """Resolve approval mode: explicit scope override → global → safe fallback.

    Scope override only wins if it's explicitly non-auto (e.g. 'interactive').
    This lets CLI --approval-mode override config.yaml scope defaults.
    """
    # Backward compatibility for callers/tests that do not provide tools config.
    if "tools" not in config:
        return "interactive"

    tools_cfg = config.get("tools") or {}
    scopes = tools_cfg.get("scopes") or {}
    scope_cfg = scopes.get(scope) or {}

    global_mode = tools_cfg.get("approval_mode", "interactive")
    scope_mode = scope_cfg.get("mode")

    # Scope override only applies if explicitly set to something other than auto
    if scope_mode and scope_mode.strip().lower() != "auto":
        mode = scope_mode
    else:
        mode = global_mode

    mode = mode.strip().lower()

    # Auto apply must be explicitly allowed.
    if mode == "auto" and not bool(tools_cfg.get("allow_auto_apply", False)):
        mode = "interactive"

    # In headless mode, never auto-approve. Downgrade to dry-run.
    if mode == "interactive" and not sys.stdin.isatty():
        mode = "dry_run"

    if mode == "interactive":
        logger.status("gate", f"[INTERACTIVE] {scope}: awaiting approval")

    return mode


def _prompt_user(action: ToolAction) -> ToolDecision:
    """Show changes and prompt for approval."""
    print()
    print(f"┌─ Apply: {action.scope} ({len(action.changes)} changes) " + "─" * 40)

    for param, value in action.changes.items():
        before = action.before_values.get(param, "")
        if before:
            print(f"│  {param:<40s} {before} → {value}")
        else:
            print(f"│  {param:<40s} = {value}")

    print("├" + "─" * 60)
    print("│  [Y] Approve  [n] Deny  [d] Dry-run (show only)")
    print("└" + "─" * 60)

    try:
        choice = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = "n"

    if choice in ("n", "no"):
        return ToolDecision(approved=False, mode="interactive", reason="user-denied")
    if choice in ("d", "dry", "dry-run", "dry_run"):
        return ToolDecision(approved=False, mode="interactive", reason="dry-run")

    # Default to approve (Y or empty)
    return ToolDecision(approved=True, mode="interactive", reason="user-approved")


def _log_changes(action: ToolAction) -> None:
    """Log changes to console for dry-run visibility."""
    for param, value in action.changes.items():
        logger.log("gate", f"  {param} = {value}", "detail")


def _write_audit(
    session_id: str,
    result: ToolResult,
    config: dict[str, Any],
) -> None:
    """Append audit entry to JSONL file."""
    tools_cfg = config.get("tools") or {}
    if not tools_cfg.get("audit_log", True):
        return

    report_dir = Path("report")
    report_dir.mkdir(exist_ok=True)
    audit_path = report_dir / f"audit_{session_id}.jsonl"

    applied_count = 0
    failed_count = 0
    if result.result:
        applied = result.result.get("applied", {})
        failed = result.result.get("failed", {})
        applied_count = len(applied) if isinstance(applied, (dict, list)) else 0
        failed_count = len(failed) if isinstance(failed, (dict, list)) else 0

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "scope": result.action.scope,
        "mode": result.decision.mode,
        "approved": result.decision.approved,
        "reason": result.decision.reason,
        "change_count": len(result.action.changes),
        "changes": result.action.changes,
        "applied": applied_count,
        "failed": failed_count,
    }

    try:
        with open(audit_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # best-effort audit
