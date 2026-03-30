"""Lessons learned — query best runs from TiDB and merge proven params.

No new tables needed. Uses existing benchmarks + sessions tables:
  - benchmarks: per-workload RPS per session (phase='final')
  - sessions: status, total_tokens, iterations via fixes_applied

Top 3 leaderboard is a SQL query, not a separate table.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Qualification thresholds — a run must meet ALL to enter the leaderboard
QUALIFY_MEDIUM_RPS = 1300.0
QUALIFY_LARGE_RPS = 180.0
LEADERBOARD_SIZE = 3


def get_top_runs(memory, system_id: str | None = None) -> list[dict[str, Any]]:
    """Return top N runs ranked by small-file RPS.

    Each entry: {session_id, small_rps, medium_rps, large_rps, tokens, iterations}
    Only includes runs where medium >= threshold AND large >= threshold.

    Reads from benchmarks table (phase='final') — permanent, not session-scoped.
    """
    with memory._cursor() as cur:
        cur.execute(
            """
            SELECT
                s.id                                        AS session_id,
                s.total_tokens                              AS tokens,
                s.fixes_applied                             AS iterations,
                MAX(CASE WHEN b.payload_size='small'  THEN b.rps END) AS small_rps,
                MAX(CASE WHEN b.payload_size='medium' THEN b.rps END) AS medium_rps,
                MAX(CASE WHEN b.payload_size='large'  THEN b.rps END) AS large_rps
            FROM sessions s
            JOIN benchmarks b ON b.session_id = s.id AND b.phase = 'final'
            WHERE s.status = 'completed'
            GROUP BY s.id, s.total_tokens, s.fixes_applied
            HAVING MAX(CASE WHEN b.payload_size='small' THEN b.rps END) IS NOT NULL
               AND MAX(CASE WHEN b.payload_size='medium' THEN b.rps END) >= %s
               AND MAX(CASE WHEN b.payload_size='large'  THEN b.rps END) >= %s
            ORDER BY small_rps DESC
            LIMIT %s
            """,
            (QUALIFY_MEDIUM_RPS, QUALIFY_LARGE_RPS, LEADERBOARD_SIZE),
        )
        rows = cur.fetchall()

    return [
        {
            "session_id": r["session_id"],
            "small_rps": float(r["small_rps"] or 0),
            "medium_rps": float(r["medium_rps"] or 0),
            "large_rps": float(r["large_rps"] or 0),
            "tokens": int(r["tokens"] or 0),
            "iterations": int(r["iterations"] or 0),
        }
        for r in rows
    ]


def get_best_run_params(memory, system_id: str | None = None) -> dict[str, str]:
    """Return the applied params snapshot from the #1 best run.

    Reads from knowledge table: all 'fix' entries from the best session.
    Returns {parameter: after_value} dict.
    """
    top = get_top_runs(memory, system_id)
    if not top:
        return {}

    best_session = top[0]["session_id"]
    with memory._cursor() as cur:
        cur.execute(
            """
            SELECT parameter, after_value
            FROM knowledge
            WHERE discovered_by = %s
              AND type = 'fix'
              AND status = 'active'
              AND parameter IS NOT NULL
              AND after_value IS NOT NULL
            """,
            (best_session,),
        )
        rows = cur.fetchall()

    return {r["parameter"]: r["after_value"] for r in rows}


def get_prior_knowledge_text(memory, system_id: str | None = None, limit: int = 10) -> str:
    """Build a text summary of past fixes — what worked AND what didn't.

    Framed as historical context, NOT prescription. The LLM should use its
    own inspection of the CURRENT system state to decide what to apply.
    Prior knowledge helps avoid known mistakes but does not lock values.
    """
    top = get_top_runs(memory, system_id)
    if not top:
        return ""

    best = top[0]
    best_session = best["session_id"]

    with memory._cursor() as cur:
        # What worked (from best run)
        cur.execute(
            """
            SELECT parameter, before_value, after_value, reasoning, impact_pct
            FROM knowledge
            WHERE discovered_by = %s
              AND type = 'fix'
              AND status = 'active'
              AND parameter IS NOT NULL
              AND after_value IS NOT NULL
            ORDER BY ABS(COALESCE(impact_pct, 0)) DESC
            LIMIT %s
            """,
            (best_session, limit),
        )
        good_rows = cur.fetchall()

        # What caused regressions (from ANY session — deprecated/negative fixes)
        cur.execute(
            """
            SELECT parameter, before_value, after_value, reasoning
            FROM knowledge
            WHERE type IN ('fix', 'negative')
              AND status = 'deprecated'
              AND parameter IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 5
            """,
        )
        bad_rows = cur.fetchall()

    if not good_rows and not bad_rows:
        return ""

    lines = [
        "Historical context (reference only — diagnose the CURRENT system, "
        "do not blindly copy past values):",
        f"Best prior run: {best_session} (small={best['small_rps']:.0f} RPS)",
        "What worked:",
    ]
    for r in good_rows:
        param = r["parameter"]
        before = r["before_value"] or "?"
        after = r["after_value"] or "?"
        reasoning = r["reasoning"] or ""
        if len(reasoning) > 120:
            reasoning = reasoning[:117] + "..."
        impact = r["impact_pct"]
        impact_str = f" ({impact:+.0f}%)" if impact else ""
        lines.append(f"  {param}: {before}→{after}{impact_str}")
        if reasoning and reasoning != "config-driven tuning":
            lines.append(f"    context: {reasoning}")

    if bad_rows:
        lines.append("What caused regressions (AVOID):")
        for r in bad_rows:
            param = r["parameter"]
            after = r["after_value"] or "?"
            reasoning = r["reasoning"] or ""
            if len(reasoning) > 100:
                reasoning = reasoning[:97] + "..."
            lines.append(f"  {param}={after} — {reasoning}")

    return "\n".join(lines)


def merge_targets(
    config_targets: dict[str, dict[str, str]],
    proven_params: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Merge proven params from best run into config targets.

    proven_params keys are like "webserver.worker_processes" or
    "kernel.net.core.somaxconn". They override config_targets when present.
    """
    merged = {cat: dict(params) for cat, params in config_targets.items()}

    for full_key, value in proven_params.items():
        # Split "webserver.worker_processes" → ("webserver", "worker_processes")
        parts = full_key.split(".", 1)
        if len(parts) == 2:
            category, param = parts
        else:
            # Try to find which category owns this param
            param = full_key
            category = None
            for cat, params in merged.items():
                if param in params:
                    category = cat
                    break
            if not category:
                continue

        if category in merged:
            merged[category][param] = value

    return merged


def qualifies(results: dict[str, Any]) -> bool:
    """Check if a run qualifies for the leaderboard."""
    medium = float(results.get("medium", {}).get("rps", 0) or 0)
    large = float(results.get("large", {}).get("rps", 0) or 0)
    return medium >= QUALIFY_MEDIUM_RPS and large >= QUALIFY_LARGE_RPS


def check_leaderboard(
    memory, results: dict[str, Any], system_id: str | None = None
) -> dict[str, Any]:
    """Check if current run would enter the top 3.

    Returns: {
        "qualifies": bool,
        "rank": int | None,        # 1, 2, 3 or None
        "beats_best": bool,
        "top_runs": [...],
        "current_small": float,
    }
    """
    if not qualifies(results):
        return {
            "qualifies": False,
            "rank": None,
            "beats_best": False,
            "top_runs": [],
            "current_small": 0,
        }

    current_small = float(results.get("small", {}).get("rps", 0) or 0)
    top = get_top_runs(memory, system_id)

    # Determine rank
    rank = None
    for i, run in enumerate(top):
        if current_small > run["small_rps"]:
            rank = i + 1
            break
    if rank is None and len(top) < LEADERBOARD_SIZE:
        rank = len(top) + 1

    return {
        "qualifies": rank is not None,
        "rank": rank,
        "beats_best": rank == 1,
        "top_runs": top,
        "current_small": current_small,
    }
