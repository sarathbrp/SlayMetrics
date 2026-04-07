"""Track parameters that fail to apply — persistent learning signal.

This table accumulates over time. Review periodically to:
  - Fix param name mismatches (LLM says 'irqbalance.enabled', config says 'irqbalance')
  - Add missing apply handlers
  - Improve LLM prompts to use correct param names
  - Identify systemic issues (e.g. SELinux can't be changed without reboot)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

# SQL to create the table and indexes (SQLite-compatible)
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS apply_failures (
    id              TEXT            PRIMARY KEY,
    session_id      TEXT            NOT NULL,
    iteration       INTEGER         NOT NULL,
    category        TEXT            NOT NULL,
    parameter       TEXT            NOT NULL,
    attempted_value TEXT,
    failure_reason  TEXT            NOT NULL,
    llm_param_name  TEXT,
    config_param_name TEXT,
    created_at      TEXT            NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_af_param   ON apply_failures(parameter);
CREATE INDEX IF NOT EXISTS idx_af_session ON apply_failures(session_id);
CREATE INDEX IF NOT EXISTS idx_af_reason  ON apply_failures(failure_reason);
"""

# Failure reasons
REASON_VERIFY_MISMATCH = "verify_mismatch"  # applied but didn't stick
REASON_APPLY_FAILED = "apply_failed"  # apply handler returned failure
REASON_RECOMMEND_REJECTED = "recommend_rejected"  # LLM used wrong param name
REASON_BLOCKED = "blocked"  # blocked by guardrails


def ensure_table(memory) -> None:
    """Create apply_failures table if it doesn't exist."""
    try:
        conn = getattr(memory, "_conn", None)
        if conn is not None and hasattr(conn, "executescript"):
            conn.executescript(CREATE_TABLE_SQL)
            if hasattr(conn, "commit"):
                conn.commit()
            return

        cur = memory._cursor()
        for stmt in CREATE_TABLE_SQL.split(";"):
            sql = stmt.strip()
            if sql:
                cur.execute(sql)
        conn = getattr(memory, "_conn", None)
        if conn is not None and hasattr(conn, "commit"):
            conn.commit()
    except Exception:
        pass  # table already exists or DB not available


def record_failure(
    memory,
    session_id: str,
    iteration: int,
    category: str,
    parameter: str,
    attempted_value: str = "",
    failure_reason: str = REASON_APPLY_FAILED,
    llm_param_name: str = "",
    config_param_name: str = "",
) -> None:
    """Record a single apply failure."""
    try:
        ensure_table(memory)
        cur = memory._cursor()
        cur.execute(
            """
            INSERT INTO apply_failures
                (id, session_id, iteration, category, parameter,
                 attempted_value, failure_reason, llm_param_name, config_param_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                session_id,
                iteration,
                category,
                parameter,
                attempted_value,
                failure_reason,
                llm_param_name,
                config_param_name,
            ),
        )
        conn = getattr(memory, "_conn", None)
        if conn is not None and hasattr(conn, "commit"):
            conn.commit()
    except Exception as e:
        log.warning("Failed to record apply failure: %s", e)


def record_verify_mismatches(
    memory, session_id: str, iteration: int, mismatches: list[dict[str, str]]
) -> None:
    """Record all verify mismatches from a single iteration."""
    for m in mismatches:
        record_failure(
            memory,
            session_id=session_id,
            iteration=iteration,
            category=m.get("scope", "unknown"),
            parameter=m.get("param", ""),
            attempted_value=m.get("expected", ""),
            failure_reason=REASON_VERIFY_MISMATCH,
        )


def record_rejected_recommendations(
    memory, session_id: str, iteration: int, rejected: list[dict[str, Any]]
) -> None:
    """Record recommendations the LLM suggested but were rejected."""
    for r in rejected:
        changes = r.get("changes", {})
        for param, value in changes.items() if isinstance(changes, dict) else []:
            record_failure(
                memory,
                session_id=session_id,
                iteration=iteration,
                category=r.get("scope", "unknown"),
                parameter=param,
                attempted_value=str(value),
                failure_reason=REASON_RECOMMEND_REJECTED,
                llm_param_name=param,
            )


def get_top_failures(memory, limit: int = 20) -> list[dict[str, Any]]:
    """Get most frequently failing parameters across all sessions."""
    try:
        ensure_table(memory)
        cur = memory._cursor()
        cur.execute(
            """
            SELECT parameter, failure_reason, COUNT(*) as fail_count,
                   GROUP_CONCAT(DISTINCT llm_param_name) as llm_names,
                   GROUP_CONCAT(DISTINCT config_param_name) as config_names
            FROM apply_failures
            GROUP BY parameter, failure_reason
            ORDER BY fail_count DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall() or []
        return [dict(row) if not isinstance(row, dict) else row for row in rows]
    except Exception:
        return []
