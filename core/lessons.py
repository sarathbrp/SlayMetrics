"""Lessons learned — query best runs from database and merge proven params.

No new tables needed. Uses existing benchmarks + sessions tables:
  - benchmarks: per-workload RPS per session (phase='final')
  - sessions: status, total_tokens, iterations via fixes_applied

Top 3 leaderboard is a SQL query, not a separate table.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)

# Qualification thresholds — a run must meet ALL to enter the leaderboard
QUALIFY_MEDIUM_RPS = 1300.0
QUALIFY_LARGE_RPS = 180.0
LEADERBOARD_SIZE = 3

OPTIMIZATION_GROUPS: dict[str, dict[str, Any]] = {
    "accept_path": {
        "description": "Connection admission and queue depth",
        "risk": "low",
        "params": (
            "webserver.worker_connections",
            "webserver.listen_backlog",
            "kernel.net.core.somaxconn",
            "kernel.net.ipv4.tcp_max_syn_backlog",
            "kernel.net.core.netdev_max_backlog",
        ),
    },
    "nginx_worker_model": {
        "description": "Worker parallelism and accept loop behavior",
        "risk": "low",
        "params": (
            "webserver.worker_processes",
            "webserver.worker_cpu_affinity",
            "webserver.multi_accept",
            "webserver.accept_mutex",
        ),
    },
    "http_connection_reuse": {
        "description": "Keepalive and request lifecycle tuning",
        "risk": "low",
        "params": (
            "webserver.keepalive_requests",
            "webserver.keepalive_timeout",
            "webserver.tcp_nodelay",
            "webserver.tcp_nopush",
            "webserver.reset_timedout_connection",
        ),
    },
    "fd_and_file_cache": {
        "description": "Descriptor limits and static file cache",
        "risk": "low",
        "params": (
            "webserver.worker_rlimit_nofile",
            "webserver.open_file_cache",
            "webserver.open_file_cache_valid",
            "webserver.open_file_cache_min_uses",
        ),
    },
    "logging_and_rate_limits": {
        "description": "Request-path overhead and throttling controls",
        "risk": "medium",
        "params": (
            "webserver.access_log",
            "webserver.error_log_level",
            "webserver.limit_req",
            "webserver.limit_conn",
            "webserver.limit_rate",
            "webserver.limit_rate_after",
        ),
    },
    "socket_buffers": {
        "description": "Socket buffer sizing for high-throughput connections",
        "risk": "medium",
        "params": (
            "kernel.net.core.rmem_max",
            "kernel.net.core.wmem_max",
            "kernel.net.core.rmem_default",
            "kernel.net.core.wmem_default",
            "kernel.net.ipv4.tcp_rmem",
            "kernel.net.ipv4.tcp_wmem",
        ),
    },
    "tcp_lifecycle": {
        "description": "TCP connection turnover and idle behavior",
        "risk": "medium",
        "params": (
            "kernel.net.ipv4.tcp_tw_reuse",
            "kernel.net.ipv4.tcp_fin_timeout",
            "kernel.net.ipv4.tcp_max_tw_buckets",
            "kernel.net.ipv4.tcp_slow_start_after_idle",
            "kernel.net.ipv4.tcp_max_orphans",
        ),
    },
    "memory_writeback": {
        "description": "Memory pressure and writeback tuning",
        "risk": "medium",
        "params": (
            "kernel.vm.swappiness",
            "kernel.vm.vfs_cache_pressure",
            "kernel.vm.dirty_ratio",
            "kernel.vm.dirty_background_ratio",
            "kernel.vm.dirty_expire_centisecs",
            "kernel.vm.dirty_writeback_centisecs",
        ),
    },
    "platform_latency": {
        "description": "Platform-level latency knobs",
        "risk": "high",
        "params": (
            "kernel.irqbalance",
            "kernel.transparent_hugepage",
            "kernel.cpu_governor",
            "kernel.selinux",
        ),
    },
}

GROUP_RISK_PENALTY = {"low": 1.0, "medium": 3.0, "high": 5.0}


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
               AND MAX(CASE WHEN b.payload_size='medium' THEN b.rps END) >= ?
               AND MAX(CASE WHEN b.payload_size='large'  THEN b.rps END) >= ?
            ORDER BY small_rps DESC
            LIMIT ?
            """,
            (QUALIFY_MEDIUM_RPS, QUALIFY_LARGE_RPS, LEADERBOARD_SIZE),
        )
        rows = cur.fetchall()

    results = [
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
    for i, r in enumerate(results):
        log.info(
            "leaderboard #%d: ? small=%.0f med=%.0f large=%.0f tokens=%d",
            i + 1,
            r["session_id"],
            r["small_rps"],
            r["medium_rps"],
            r["large_rps"],
            r["tokens"],
        )
    if not results:
        log.info("leaderboard: empty — no qualifying runs")
    return results


def get_best_run_params(memory, system_id: str | None = None) -> dict[str, str]:
    """Return the applied params snapshot from the #1 best run.

    Reads from validations + knowledge so reused logical fix facts still map
    back to the session where they were confirmed.
    Returns {parameter: after_value} dict.
    """
    top = get_top_runs(memory, system_id)
    if not top:
        return {}

    best_session = top[0]["session_id"]
    with memory._cursor() as cur:
        cur.execute(
            """
            SELECT k.parameter, k.after_value
            FROM validations v
            JOIN knowledge k ON k.id = v.knowledge_id
            WHERE v.session_id = ?
              AND v.outcome IN ('confirmed', 'partial')
              AND k.type = 'fix'
              AND k.status = 'active'
              AND k.parameter IS NOT NULL
              AND k.after_value IS NOT NULL
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
            WHERE discovered_by = ?
              AND type = 'fix'
              AND status = 'active'
              AND parameter IS NOT NULL
              AND after_value IS NOT NULL
            ORDER BY ABS(COALESCE(impact_pct, 0)) DESC
            LIMIT ?
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


def get_ranked_optimization_groups(
    memory,
    current_state: dict[str, str],
    *,
    system_id: str | None = None,
    top_n: int = LEADERBOARD_SIZE,
) -> list[dict[str, Any]]:
    """Rank optimization groups using top-run prevalence and validation history."""
    top_runs = get_top_runs(memory, system_id)[:top_n]
    if not top_runs:
        return []

    session_ids = [run["session_id"] for run in top_runs]
    session_weight = {run["session_id"]: max(top_n - idx, 1) for idx, run in enumerate(top_runs)}
    evidence = _get_fix_evidence_for_sessions(memory, session_ids)
    evidence_by_param: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        evidence_by_param[row["parameter"]].append(row)

    ranked: list[dict[str, Any]] = []
    for name, spec in OPTIMIZATION_GROUPS.items():
        candidate_params: dict[str, str] = {}
        current_params: dict[str, str] = {}
        reasons: list[str] = []
        score = 0.0
        evidence_params = 0
        matched_params = 0
        support_hits = 0
        confirm_total = 0
        contradict_total = 0
        prevalence_bucket = {3: 0, 2: 0, 1: 0}

        for full_param in spec["params"]:
            param_rows = evidence_by_param.get(full_param) or []
            if not param_rows:
                continue
            evidence_params += 1
            choice = _choose_param_value(param_rows, session_weight)
            if not choice:
                continue

            current_value = current_state.get(full_param, "unknown")
            current_params[full_param] = current_value
            normalized_current = _normalize_value(current_value)
            normalized_target = _normalize_value(choice["value"])

            if normalized_current == normalized_target:
                matched_params += 1
                continue

            candidate_params[full_param] = choice["value"]
            prevalence = int(choice["session_count"])
            support_hits += prevalence
            confirm_total += int(choice["confirmed"])
            contradict_total += int(choice["contradicted"])
            prevalence_bucket[prevalence] = prevalence_bucket.get(prevalence, 0) + 1
            score += _prevalence_score(prevalence, top_n)
            score += min(float(choice["confirmed"]), 5.0)
            score -= float(choice["contradicted"]) * 2.0
            score += 1.0

        if not candidate_params:
            continue

        completeness = (
            (matched_params + len(candidate_params)) / evidence_params if evidence_params else 0
        )
        if completeness >= 0.8:
            score += 3.0
        elif completeness >= 0.5:
            score += 1.5
        if len(candidate_params) == 1:
            score -= 1.0

        risk = spec["risk"]
        score -= GROUP_RISK_PENALTY.get(risk, 0.0)
        reasons.append(f"{len(candidate_params)} params differ from the current healthy state")
        reasons.append(
            f"top-run prevalence: {prevalence_bucket.get(3, 0)} core, "
            f"{prevalence_bucket.get(2, 0)} conditional, {prevalence_bucket.get(1, 0)} experimental"
        )
        reasons.append(
            f"validation strength: {confirm_total} confirmations, {contradict_total} contradictions"
        )
        reasons.append(f"group completeness after apply: {completeness:.0%}")
        reasons.append(f"risk penalty: {risk}")

        ranked.append(
            {
                "name": name,
                "description": spec["description"],
                "risk": risk,
                "score": round(score, 2),
                "reasons": reasons,
                "changes": _categorize_changes(candidate_params),
                "current": current_params,
                "evidence_params": evidence_params,
                "support_hits": support_hits,
            }
        )

    ranked.sort(key=lambda item: (-item["score"], item["name"]))
    return ranked


def _get_fix_evidence_for_sessions(memory, session_ids: list[str]) -> list[dict[str, Any]]:
    if not session_ids:
        return []
    placeholders = ",".join(["?"] * len(session_ids))
    with memory._cursor() as cur:
        cur.execute(
            f"""
            SELECT
                v.session_id AS session_id,
                k.parameter AS parameter,
                k.after_value AS after_value,
                SUM(CASE WHEN v_all.outcome = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_count,
                SUM(CASE WHEN v_all.outcome = 'contradicted'
                    THEN 1 ELSE 0 END) AS contradicted_count
            FROM validations v
            JOIN knowledge k ON k.id = v.knowledge_id
            LEFT JOIN validations v_all ON v_all.knowledge_id = k.id
            WHERE v.session_id IN ({placeholders})
              AND v.outcome IN ('confirmed', 'partial')
              AND k.type = 'fix'
              AND k.status = 'active'
              AND k.parameter IS NOT NULL
              AND k.after_value IS NOT NULL
            GROUP BY v.session_id, k.id, k.parameter, k.after_value
            """,
            tuple(session_ids),
        )
        rows = cur.fetchall()
    return [
        {
            "session_id": row["session_id"],
            "parameter": row["parameter"],
            "after_value": row["after_value"],
            "confirmed_count": int(row["confirmed_count"] or 0),
            "contradicted_count": int(row["contradicted_count"] or 0),
        }
        for row in rows
    ]


def _choose_param_value(
    rows: list[dict[str, Any]], session_weight: dict[str, int]
) -> dict[str, Any] | None:
    by_value: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row["after_value"])
        entry = by_value.setdefault(
            value,
            {
                "value": value,
                "sessions": set(),
                "rank_weight": 0,
                "confirmed": 0,
                "contradicted": 0,
            },
        )
        session_id = row["session_id"]
        entry["sessions"].add(session_id)
        entry["rank_weight"] += session_weight.get(session_id, 1)
        entry["confirmed"] += int(row["confirmed_count"] or 0)
        entry["contradicted"] += int(row["contradicted_count"] or 0)
    if not by_value:
        return None

    chosen = sorted(
        by_value.values(),
        key=lambda item: (
            -len(item["sessions"]),
            -item["rank_weight"],
            -(item["confirmed"] - item["contradicted"]),
            item["value"],
        ),
    )[0]
    return {
        "value": chosen["value"],
        "session_count": len(chosen["sessions"]),
        "confirmed": chosen["confirmed"],
        "contradicted": chosen["contradicted"],
    }


def _prevalence_score(prevalence: int, top_n: int) -> float:
    if prevalence >= top_n:
        return 5.0
    if prevalence >= 2:
        return 2.0
    return 0.5


def _categorize_changes(changes: dict[str, str]) -> dict[str, dict[str, str]]:
    categorized: dict[str, dict[str, str]] = {}
    for full_param, value in changes.items():
        if "." not in full_param:
            continue
        category, param = full_param.split(".", 1)
        categorized.setdefault(category, {})[param] = value
    return categorized


def _normalize_value(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def compute_delta(
    memory,
    current_session_id: str,
    system_id: str | None = None,
) -> dict[str, dict[str, str]]:
    """Compare current session's applied params vs #1's params.

    Returns categorized delta: {category: {param: value}} for params
    that #1 had but current session is missing or has different values.
    Used for deterministic iter2 — apply only what LLM missed.
    """
    top = get_top_runs(memory, system_id)
    if not top:
        log.info("compute_delta: no top runs — nothing to compare")
        return {}

    best_session = top[0]["session_id"]
    proven = get_best_run_params(memory, system_id)
    if not proven:
        log.info("compute_delta: no proven params for #1 (?)", best_session)
        return {}

    log.info(
        "compute_delta: #1=? has %d proven params, comparing to session ?",
        best_session,
        len(proven),
        current_session_id,
    )

    # Get current session's applied params
    with memory._cursor() as cur:
        cur.execute(
            """
            SELECT parameter, after_value
            FROM knowledge
            WHERE discovered_by = ?
              AND type = 'fix'
              AND status = 'active'
              AND parameter IS NOT NULL
              AND after_value IS NOT NULL
            """,
            (current_session_id,),
        )
        rows = cur.fetchall()

    current = {r["parameter"]: r["after_value"] for r in rows}
    log.info(
        "compute_delta: current session ? has %d applied params",
        current_session_id,
        len(current),
    )

    # Diff: params in proven but missing or different in current
    delta: dict[str, dict[str, str]] = {}
    for full_key, proven_value in proven.items():
        current_value = current.get(full_key)
        if current_value is None:
            parts = full_key.split(".", 1)
            cat, param = parts if len(parts) == 2 else ("kernel", full_key)
            delta.setdefault(cat, {})[param] = proven_value
            log.info("  delta MISSING: ? (proven=?)", full_key, proven_value)
        elif current_value != proven_value:
            parts = full_key.split(".", 1)
            cat, param = parts if len(parts) == 2 else ("kernel", full_key)
            delta.setdefault(cat, {})[param] = proven_value
            log.info(
                "  delta DIFFERS: ? current=? proven=?",
                full_key,
                current_value,
                proven_value,
            )

    log.info(
        "compute_delta: %d params differ (?)",
        sum(len(v) for v in delta.values()),
        ", ".join(f"{c}={len(v)}" for c, v in delta.items()),
    )
    return delta


def apply_delta(deps, delta: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Apply only the delta params deterministically — no LLM, 0 tokens.

    Reuses existing apply functions from agents/tools_apply.py.
    """
    from agents.tools_apply import apply_kernel, apply_network, apply_resource_limits, apply_storage
    from core import log as logger

    ssh = deps.ssh
    results: dict[str, Any] = {}

    # Webserver (nginx) — apply via adapter
    web_delta = delta.get("webserver", {})
    if web_delta:
        logger.status("delta", f"Applying {len(web_delta)} nginx delta params")
        applied, failed = [], []
        for param, value in web_delta.items():
            if deps.adapter.apply_config(param, value):
                applied.append(param)
            else:
                failed.append(param)
        # Reload nginx
        reload_result = ssh.execute("nginx -t 2>&1 && nginx -s reload 2>&1")
        reload_ok = "syntax is ok" in reload_result.stdout or reload_result.exit_code == 0
        results["webserver"] = {"applied": applied, "failed": failed, "reload": reload_ok}

    # Kernel (sysctls + THP + SELinux + IRQ + governor)
    kern_delta = delta.get("kernel", {})
    if kern_delta:
        logger.status("delta", f"Applying {len(kern_delta)} kernel delta params")
        results["kernel"] = apply_kernel(ssh, kern_delta)

    # Resource limits
    res_delta = delta.get("resource_limits", {})
    if res_delta:
        logger.status("delta", f"Applying {len(res_delta)} resource_limits delta params")
        results["resource_limits"] = apply_resource_limits(ssh, res_delta)

    # Network
    net_delta = delta.get("network", {})
    if net_delta:
        logger.status("delta", f"Applying {len(net_delta)} network delta params")
        results["network"] = apply_network(ssh, net_delta)

    # Storage
    stor_delta = delta.get("storage", {})
    if stor_delta:
        logger.status("delta", f"Applying {len(stor_delta)} storage delta params")
        results["storage"] = apply_storage(ssh, stor_delta)

    return results
