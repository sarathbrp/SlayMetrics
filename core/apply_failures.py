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

# SQL to create the table (run once via schema.sql or manually)
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS apply_failures (
    id              VARCHAR(64)     PRIMARY KEY,
    session_id      VARCHAR(64)     NOT NULL,
    iteration       INT             NOT NULL,
    category        VARCHAR(32)     NOT NULL,
    parameter       VARCHAR(256)    NOT NULL,
    attempted_value TEXT,
    failure_reason  VARCHAR(64)     NOT NULL,
    llm_param_name  VARCHAR(256),
    config_param_name VARCHAR(256),
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_af_param (parameter),
    INDEX idx_af_session (session_id),
    INDEX idx_af_reason (failure_reason)
);
"""

# Failure reasons
REASON_VERIFY_MISMATCH = "verify_mismatch"  # applied but didn't stick
REASON_APPLY_FAILED = "apply_failed"  # apply handler returned failure
REASON_RECOMMEND_REJECTED = "recommend_rejected"  # LLM used wrong param name
REASON_BLOCKED = "blocked"  # blocked by guardrails


def ensure_table(memory) -> None:
    """Create apply_failures table if it doesn't exist."""
    try:
        with memory._cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
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
        with memory._cursor() as cur:
            cur.execute(
                """
                INSERT INTO apply_failures
                    (id, session_id, iteration, category, parameter,
                     attempted_value, failure_reason, llm_param_name, config_param_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        with memory._cursor() as cur:
            cur.execute(
                """
                SELECT parameter, failure_reason, COUNT(*) as fail_count,
                       GROUP_CONCAT(DISTINCT llm_param_name) as llm_names,
                       GROUP_CONCAT(DISTINCT config_param_name) as config_names
                FROM apply_failures
                GROUP BY parameter, failure_reason
                ORDER BY fail_count DESC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall() or []
    except Exception:
        return []
