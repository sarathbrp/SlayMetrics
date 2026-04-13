"""Shared utilities for domain analyzers and investigation nodes."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import dspy

logger = logging.getLogger("slayMetrics.analyzer")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]")


def extract_audit_groups(audit_output: str, groups: list[int]) -> str:
    """Return only the requested audit groups from omega_master_audit.sh output."""
    clean = ANSI_RE.sub("", audit_output)
    lines = clean.splitlines()
    result: list[str] = []
    include = False
    for line in lines:
        for g in range(1, 6):
            if f"[{g}/5]" in line:
                include = g in groups
                break
        if include:
            result.append(line)
    return "\n".join(result)


def extract_tokens() -> tuple[int, int]:
    """Extract input/output token counts from the most recent DSPy LLM call."""
    history = dspy.settings.lm.history
    if not history:
        return 0, 0
    usage = history[-1].get("usage", {})
    return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def parse_fixes_json(raw: str) -> tuple[list[dict], str]:
    """Parse the LLM JSON output -> (fixes, summary)."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
    data = json.loads(raw)
    fixes = data.get("fixes", []) if isinstance(data, dict) else data
    summary = data.get("summary", "") if isinstance(data, dict) else ""
    return fixes, summary


def save_prompt(save_dir: Path, name: str, inputs: dict,
                fixes: list, summary: str, in_tok: int, out_tok: int) -> None:
    """Save LLM prompt + response to session folder for debugging."""
    try:
        history = dspy.settings.lm.history
        raw_messages = history[-1] if history else {}
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"prompt_{name}.json"
        payload = {
            "timestamp": datetime.now().isoformat(),
            "domain": name,
            "inputs": {k: v[:2000] + "…" if isinstance(v, str) and len(v) > 2000 else v
                       for k, v in inputs.items()},
            "fixes": fixes,
            "summary": summary,
            "tokens": {"input": in_tok, "output": out_tok},
            "raw_messages": raw_messages,
        }
        path.write_text(json.dumps(payload, indent=2, default=str))
        logger.debug("Prompt saved to %s", path)
    except Exception as e:
        logger.warning("Failed to save prompt for %s: %s", name, e)
