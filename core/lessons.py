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

    Reads from context table (type='benchmark', source like 'iterN_workload')
    since the agent stores benchmark results there, not in the benchmarks table.
    Uses the highest iteration number per session as the final result.
    """
    with memory._cursor() as cur:
        # Extract RPS from JSON content in context table, using the last
        # iteration's results per session per workload.
        cur.execute(
            """
            SELECT
                c.session_id,
                s.total_tokens AS tokens,
                MAX(CAST(SUBSTRING_INDEX(c.source, '_', 1) AS UNSIGNED)) AS iterations,
                MAX(CASE WHEN c.source LIKE '%%_small'
                    THEN JSON_EXTRACT(c.content, '$.rps') END) AS small_rps,
                MAX(CASE WHEN c.source LIKE '%%_medium'
                    THEN JSON_EXTRACT(c.content, '$.rps') END) AS medium_rps,
                MAX(CASE WHEN c.source LIKE '%%_large'
                    THEN JSON_EXTRACT(c.content, '$.rps') END) AS large_rps
            FROM context c
            JOIN sessions s ON s.id = c.session_id
            WHERE c.type = 'benchmark'
              AND c.source NOT LIKE 'baseline%%'
              AND s.status = 'completed'
            GROUP BY c.session_id, s.total_tokens
            HAVING small_rps IS NOT NULL
               AND CAST(medium_rps AS DECIMAL(20,2)) >= %s
               AND CAST(large_rps AS DECIMAL(20,2)) >= %s
            ORDER BY CAST(small_rps AS DECIMAL(20,2)) DESC
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
