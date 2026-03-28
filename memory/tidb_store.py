from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql
import pymysql.cursors

from memory.embeddings import EmbeddingProvider

# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class System:
    id: str
    host: str
    service: str
    service_type: str | None
    rhel_version: str | None
    kernel_version: str | None
    cpu_cores: int | None
    ram_gb: int | None
    numa_nodes: int | None
    current_rps: float | None
    best_rps: float | None
    tuning_state: dict | None
    created_at: datetime
    updated_at: datetime


@dataclass
class Session:
    id: str
    system_id: str
    llm_profile: str | None
    trigger_type: str
    status: str
    total_tokens: int
    fixes_applied: int
    rps_start: float | None
    rps_end: float | None
    rps_delta_pct: float | None
    started_at: datetime
    completed_at: datetime | None


@dataclass
class Knowledge:
    id: str
    discovered_by: str
    system_id: str | None
    service_type: str | None
    scope: str
    type: str
    parameter: str
    condition: str | None
    before_value: str
    after_value: str
    recommendation: str | None
    impact_pct: float | None
    confidence: float
    validations: int
    superseded_by: str | None
    status: str
    reasoning: str
    created_at: datetime


@dataclass
class Validation:
    id: str
    knowledge_id: str
    session_id: str
    system_id: str
    outcome: str
    before_rps: float | None
    after_rps: float | None
    impact_pct: float | None
    notes: str | None
    created_at: datetime


@dataclass
class Benchmark:
    id: str
    session_id: str
    system_id: str
    iteration_num: int
    phase: str
    payload_size: str
    rps: float | None
    latency_avg_ms: float | None
    latency_p99_ms: float | None
    cpu_pct: float | None
    mem_pct: float | None
    errors: int
    fix_id: str | None
    raw_output: str | None
    created_at: datetime


@dataclass
class ContextEntry:
    id: str
    session_id: str
    system_id: str
    type: str
    source: str
    content: str
    summary: str
    iteration_num: int | None
    created_at: datetime


# ── Helpers ──────────────────────────────────────────────────────────────────


def _coerce_optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# ── TiDBStore ────────────────────────────────────────────────────────────────


class TiDBStore:
    """Production-grade memory store backed by TiDB.

    Operates on a knowledge-scoped schema: systems persist across sessions,
    knowledge accumulates with confidence scoring, benchmarks are structured.

    Maintains backward-compatible method signatures (session_id-centric) so
    existing agent code works without modification. Internally maps session_id
    → system_id via the sessions table.
    """

    SYSTEM_FIELDS = {
        "service",
        "service_type",
        "host",
        "rhel_version",
        "kernel_version",
        "cpu_cores",
        "ram_gb",
        "numa_nodes",
        "current_rps",
        "best_rps",
        "tuning_state",
    }
    CONTEXT_TYPE_MAP = {
        "metric": "metric",
        "log": "log",
        "command_output": "command_output",
        "benchmark": "benchmark",
        "system_check": "system_check",
        "telemetry": "metric",
        "rca": "command_output",
        "recommendation": "command_output",
    }

    def __init__(self, cfg: dict, embedder: EmbeddingProvider):
        m = cfg["memory"]
        self._conn_kwargs = dict(
            host=m["host"],
            port=int(m.get("port", 4000)),
            user=m["user"],
            password=os.environ.get(m.get("password_env", ""), "") or "",
            database=m["database"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
        self._embedder = embedder
        self._conn: pymysql.Connection | None = None
        # Cache system_id for the current session to avoid repeated lookups
        self._system_id_cache: dict[str, str] = {}

    # ── Connection ───────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._conn = pymysql.connect(**self._conn_kwargs)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist by running schema.sql."""
        if not isinstance(self._conn, pymysql.Connection):
            return  # skip for test fakes
        schema_path = Path(__file__).resolve().parent.parent / "schema.sql"
        if not schema_path.exists():
            return
        sql = schema_path.read_text(encoding="utf-8")
        with self._conn.cursor() as cur:
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    try:
                        cur.execute(stmt)
                    except Exception:
                        pass  # table already exists or other DDL conflict

    def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _cursor(self):
        if not self._conn or not self._conn.open:
            self.connect()
        return self._conn.cursor()

    # ── System registry ──────────────────────────────────────────────────────

    def _get_or_create_system(self, host: str, service: str) -> str:
        """Find existing system by host+service or create one. Returns system_id."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT id FROM systems WHERE host = %s AND service = %s LIMIT 1",
                (host, service),
            )
            row = cur.fetchone()
            if row:
                return row["id"]

            system_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO systems (id, host, service)
                VALUES (%s, %s, %s)
                """,
                (system_id, host, service),
            )
            return system_id

    def get_system(self, system_id: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM systems WHERE id = %s LIMIT 1", (system_id,))
            return cur.fetchone()

    def get_system_by_host(self, host: str, service: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM systems WHERE host = %s AND service = %s LIMIT 1",
                (host, service),
            )
            return cur.fetchone()

    def update_system(self, system_id: str, **kwargs) -> None:
        filtered = {k: v for k, v in kwargs.items() if k in self.SYSTEM_FIELDS}
        if not filtered:
            return
        # JSON-encode tuning_state if present
        if "tuning_state" in filtered and isinstance(filtered["tuning_state"], dict):
            filtered["tuning_state"] = json.dumps(filtered["tuning_state"])
        cols = ", ".join(f"{k} = %s" for k in filtered)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE systems SET {cols}, updated_at = NOW() WHERE id = %s",
                (*filtered.values(), system_id),
            )

    def _system_id_for_session(self, session_id: str) -> str | None:
        """Look up system_id for a session, with caching."""
        if session_id in self._system_id_cache:
            return self._system_id_cache[session_id]
        with self._cursor() as cur:
            cur.execute(
                "SELECT system_id FROM sessions WHERE id = %s LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
        if row:
            self._system_id_cache[session_id] = row["system_id"]
            return row["system_id"]
        return None

    # ── Sessions ─────────────────────────────────────────────────────────────

    def create_session(self, session_id: str, service: str, host: str, llm_profile: str) -> str:
        """Create a session, auto-registering the system if needed.

        Backward-compatible with old callers that expect (session_id, service, host, llm_profile).
        Returns the system_id.
        """
        system_id = self._get_or_create_system(host, service)
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (id, system_id, llm_profile)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE started_at = NOW()
                """,
                (session_id, system_id, llm_profile),
            )
        self._system_id_cache[session_id] = system_id
        return system_id

    def complete_session(
        self,
        session_id: str,
        total_tokens: int = 0,
        fixes_applied: int = 0,
        rps_start: float | None = None,
        rps_end: float | None = None,
    ) -> None:
        """Mark session as completed with summary stats."""
        delta = None
        if rps_start and rps_end and rps_start > 0:
            delta = (rps_end - rps_start) / rps_start * 100
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET status = 'completed', total_tokens = %s, fixes_applied = %s,
                    rps_start = %s, rps_end = %s, rps_delta_pct = %s,
                    completed_at = NOW()
                WHERE id = %s
                """,
                (total_tokens, fixes_applied, rps_start, rps_end, delta, session_id),
            )

    def get_session(self, session_id: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM sessions WHERE id = %s LIMIT 1", (session_id,))
            return cur.fetchone()

    def session_exists(self, session_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM sessions WHERE id = %s",
                (session_id,),
            )
            return cur.fetchone()["cnt"] > 0

    def get_latest_session_for_host(
        self, host: str, exclude_session_id: str | None = None
    ) -> str | None:
        """Get latest session for a host (cross-session learning)."""
        query = """
            SELECT s.id as session_id
            FROM sessions s
            JOIN systems sys ON s.system_id = sys.id
            WHERE sys.host = %s
        """
        params: list[object] = [host]
        if exclude_session_id:
            query += " AND s.id <> %s"
            params.append(exclude_session_id)
        query += " ORDER BY s.started_at DESC LIMIT 1"
        with self._cursor() as cur:
            cur.execute(query, tuple(params))
            row = cur.fetchone()
        return row["session_id"] if row else None

    # ── Profile (backward-compatible facade) ─────────────────────────────────
    # These map old profile-centric calls onto systems + sessions.

    def update_profile(self, session_id: str, **kwargs) -> None:
        """Backward-compatible: updates the system record for this session."""
        system_id = self._system_id_for_session(session_id)
        if not system_id:
            return
        # Map old profile fields to system fields
        remap = {
            "baseline_rps": "current_rps",
            "best_rps": "best_rps",
            "status": None,  # session status, not system
            "llm_profile": None,  # session field, not system
        }
        system_kwargs = {}
        for k, v in kwargs.items():
            mapped = remap.get(k, k)  # unmapped keys pass through
            if mapped is not None:
                system_kwargs[mapped] = v
        if system_kwargs:
            self.update_system(system_id, **system_kwargs)

        # Update session-level fields
        session_updates = {}
        if "status" in kwargs:
            session_updates["status"] = kwargs["status"]
        if "llm_profile" in kwargs:
            session_updates["llm_profile"] = kwargs["llm_profile"]
        if "baseline_rps" in kwargs:
            session_updates["rps_start"] = kwargs["baseline_rps"]
        if session_updates:
            cols = ", ".join(f"{k} = %s" for k in session_updates)
            with self._cursor() as cur:
                cur.execute(
                    f"UPDATE sessions SET {cols} WHERE id = %s",
                    (*session_updates.values(), session_id),
                )

    def get_profile(self, session_id: str) -> dict | None:
        """Backward-compatible: returns a merged system+session dict.

        Callers expect keys like: service, host, rhel_version, cpu_cores, ram_gb,
        baseline_rps, best_rps, llm_profile, status.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    sys.id as system_id,
                    sys.host, sys.service, sys.service_type,
                    sys.rhel_version, sys.kernel_version,
                    sys.cpu_cores, sys.ram_gb, sys.numa_nodes,
                    sys.current_rps as baseline_rps,
                    sys.best_rps,
                    sys.tuning_state,
                    s.id as session_id,
                    s.llm_profile, s.status,
                    s.total_tokens, s.fixes_applied,
                    s.rps_start, s.rps_end, s.rps_delta_pct,
                    s.started_at, s.completed_at
                FROM sessions s
                JOIN systems sys ON s.system_id = sys.id
                WHERE s.id = %s
                LIMIT 1
                """,
                (session_id,),
            )
            return cur.fetchone()

    # ── Knowledge (replaces facts) ───────────────────────────────────────────

    def save_fact(
        self,
        session_id: str,
        type: str,
        parameter: str,
        reasoning: str,
        before_value: str = "",
        after_value: str = "",
        before_rps: float | None = None,
        after_rps: float | None = None,
        impact_pct: float | None = None,
        status: str = "applied",
        scope: str = "system",
        condition: str | None = None,
        confidence: float = 0.5,
    ) -> str:
        """Save a knowledge entry. Backward-compatible signature with new optional params."""
        kid = str(uuid.uuid4())
        system_id = self._system_id_for_session(session_id)

        # Determine service_type from system
        service_type = None
        if system_id:
            sys_row = self.get_system(system_id)
            if sys_row:
                service_type = sys_row.get("service_type") or sys_row.get("service")

        text = f"{parameter} {reasoning} {before_value} {after_value}"
        embedding = self._embedder.embed(text)

        # Map old status to knowledge status
        k_status = "active"
        if status in ("reverted", "regressed", "negative"):
            k_status = "deprecated"

        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO knowledge
                    (id, discovered_by, system_id, service_type, scope, type,
                     parameter, condition, before_value, after_value,
                     impact_pct, confidence, status, reasoning,
                     embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    kid,
                    session_id,
                    system_id,
                    service_type,
                    scope,
                    type,
                    parameter,
                    condition,
                    before_value,
                    after_value,
                    _coerce_optional_float(impact_pct),
                    confidence,
                    k_status,
                    reasoning,
                    json.dumps(embedding),
                ),
            )

        # Also record validation for this knowledge entry
        if system_id and type == "fix":
            outcome = "confirmed" if status == "applied" else "contradicted"
            self.save_validation(
                knowledge_id=kid,
                session_id=session_id,
                system_id=system_id,
                outcome=outcome,
                before_rps=before_rps,
                after_rps=after_rps,
                impact_pct=impact_pct,
            )

        return kid

    def get_facts(self, session_id: str, type: str | None = None) -> list[dict]:
        """Backward-compatible: get knowledge entries for this session."""
        with self._cursor() as cur:
            if type:
                cur.execute(
                    """SELECT id, discovered_by as session_id, type, parameter,
                              before_value, after_value, impact_pct,
                              confidence, status, reasoning, created_at
                       FROM knowledge
                       WHERE discovered_by = %s AND type = %s
                       ORDER BY created_at""",
                    (session_id, type),
                )
            else:
                cur.execute(
                    """SELECT id, discovered_by as session_id, type, parameter,
                              before_value, after_value, impact_pct,
                              confidence, status, reasoning, created_at
                       FROM knowledge
                       WHERE discovered_by = %s
                       ORDER BY created_at""",
                    (session_id,),
                )
            return cur.fetchall()

    def get_all_fixes_for_host(self, host: str) -> list[dict]:
        """Get all fixes across ALL sessions for a host — cross-session learning."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT k.discovered_by as session_id, k.type, k.parameter,
                       k.before_value, k.after_value, k.impact_pct,
                       k.confidence, k.reasoning, k.status, k.created_at
                FROM knowledge k
                JOIN sessions s ON k.discovered_by = s.id
                JOIN systems sys ON s.system_id = sys.id
                WHERE sys.host = %s AND k.type = 'fix' AND k.status = 'active'
                ORDER BY k.confidence DESC, k.created_at DESC
                """,
                (host,),
            )
            return cur.fetchall()

    def get_knowledge_for_service(
        self, service_type: str, min_confidence: float = 0.0
    ) -> list[dict]:
        """Cross-system learning: get all active knowledge for a service type.

        Returns universal + service_type scoped knowledge, ordered by confidence.
        This is the query that makes the product pitch real.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, scope, type, parameter, condition, recommendation,
                       impact_pct, confidence, validations, reasoning, created_at
                FROM knowledge
                WHERE status = 'active'
                  AND (scope = 'universal' OR (scope = 'service_type' AND service_type = %s))
                  AND confidence >= %s
                ORDER BY confidence DESC, validations DESC
                """,
                (service_type, min_confidence),
            )
            return cur.fetchall()

    def promote_knowledge(self, knowledge_id: str, new_scope: str) -> None:
        """Widen the scope of a knowledge entry (e.g., system → service_type)."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE knowledge SET scope = %s WHERE id = %s",
                (new_scope, knowledge_id),
            )

    def supersede_knowledge(self, old_id: str, new_id: str) -> None:
        """Mark a knowledge entry as superseded by a newer one."""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE knowledge
                SET status = 'superseded', superseded_by = %s
                WHERE id = %s
                """,
                (new_id, old_id),
            )

    # ── Validations ──────────────────────────────────────────────────────────

    def save_validation(
        self,
        knowledge_id: str,
        session_id: str,
        system_id: str,
        outcome: str,
        before_rps: float | None = None,
        after_rps: float | None = None,
        impact_pct: float | None = None,
        notes: str | None = None,
    ) -> str:
        vid = str(uuid.uuid4())
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO validations
                    (id, knowledge_id, session_id, system_id, outcome,
                     before_rps, after_rps, impact_pct, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    vid,
                    knowledge_id,
                    session_id,
                    system_id,
                    outcome,
                    _coerce_optional_float(before_rps),
                    _coerce_optional_float(after_rps),
                    _coerce_optional_float(impact_pct),
                    notes,
                ),
            )

        # Update confidence on the knowledge entry
        self._update_knowledge_confidence(knowledge_id, outcome)
        return vid

    def _update_knowledge_confidence(self, knowledge_id: str, outcome: str) -> None:
        """Adjust confidence based on validation outcome.

        Confirmed: confidence += 0.1 (max 1.0), validations += 1
        Contradicted: confidence -= 0.15 (min 0.0)
        Partial: confidence += 0.03
        """
        delta = {"confirmed": 0.1, "contradicted": -0.15, "partial": 0.03}.get(outcome, 0)
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE knowledge
                SET confidence = GREATEST(0, LEAST(1, confidence + %s)),
                    validations = validations + 1,
                    last_validated = NOW()
                WHERE id = %s
                """,
                (delta, knowledge_id),
            )

    def get_validations(self, knowledge_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT v.*, sys.host, sys.service
                FROM validations v
                JOIN systems sys ON v.system_id = sys.id
                WHERE v.knowledge_id = %s
                ORDER BY v.created_at DESC
                """,
                (knowledge_id,),
            )
            return cur.fetchall()

    # ── Benchmarks ───────────────────────────────────────────────────────────

    def save_benchmark(
        self,
        session_id: str,
        iteration_num: int,
        phase: str,
        payload_size: str,
        rps: float | None = None,
        latency_avg_ms: float | None = None,
        latency_p99_ms: float | None = None,
        cpu_pct: float | None = None,
        mem_pct: float | None = None,
        errors: int = 0,
        fix_id: str | None = None,
        raw_output: str | None = None,
    ) -> str:
        bid = str(uuid.uuid4())
        system_id = self._system_id_for_session(session_id) or ""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO benchmarks
                    (id, session_id, system_id, iteration_num, phase, payload_size,
                     rps, latency_avg_ms, latency_p99_ms, cpu_pct, mem_pct,
                     errors, fix_id, raw_output)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    bid,
                    session_id,
                    system_id,
                    iteration_num,
                    phase,
                    payload_size,
                    _coerce_optional_float(rps),
                    _coerce_optional_float(latency_avg_ms),
                    _coerce_optional_float(latency_p99_ms),
                    _coerce_optional_float(cpu_pct),
                    _coerce_optional_float(mem_pct),
                    errors,
                    fix_id,
                    raw_output,
                ),
            )
        return bid

    def get_benchmarks(
        self,
        session_id: str | None = None,
        system_id: str | None = None,
        phase: str | None = None,
        payload_size: str | None = None,
    ) -> list[dict]:
        """Query benchmarks with optional filters."""
        query = "SELECT * FROM benchmarks WHERE 1=1"
        params: list[object] = []
        if session_id:
            query += " AND session_id = %s"
            params.append(session_id)
        if system_id:
            query += " AND system_id = %s"
            params.append(system_id)
        if phase:
            query += " AND phase = %s"
            params.append(phase)
        if payload_size:
            query += " AND payload_size = %s"
            params.append(payload_size)
        query += " ORDER BY iteration_num ASC, payload_size ASC"
        with self._cursor() as cur:
            cur.execute(query, tuple(params))
            return cur.fetchall()

    def get_benchmark_comparison(self, session_id: str) -> dict:
        """Get baseline vs final benchmarks for report generation.

        Returns: {
            "small": {"baseline_rps": ..., "final_rps": ..., "delta_pct": ...},
            "medium": {...},
            "large": {...}
        }
        """
        result = {}
        for size in ("small", "medium", "large"):
            with self._cursor() as cur:
                cur.execute(
                    """
                    SELECT phase, rps, latency_p99_ms, cpu_pct, mem_pct
                    FROM benchmarks
                    WHERE session_id = %s AND payload_size = %s
                      AND phase IN ('baseline', 'final')
                    ORDER BY phase ASC
                    """,
                    (session_id, size),
                )
                rows = cur.fetchall()
            baseline: dict[str, Any] = next((r for r in rows if r["phase"] == "baseline"), {})
            final: dict[str, Any] = next((r for r in rows if r["phase"] == "final"), {})
            b_rps = baseline.get("rps") or 0
            f_rps = final.get("rps") or 0
            result[size] = {
                "baseline_rps": b_rps,
                "baseline_p99": baseline.get("latency_p99_ms"),
                "final_rps": f_rps,
                "final_p99": final.get("latency_p99_ms"),
                "delta_pct": ((f_rps - b_rps) / b_rps * 100) if b_rps else 0,
                "baseline_cpu": baseline.get("cpu_pct"),
                "final_cpu": final.get("cpu_pct"),
            }
        return result

    def get_performance_trend(self, system_id: str, payload_size: str = "small") -> list[dict]:
        """Get historical benchmark data for trend analysis (product feature).

        Returns chronological list of scheduled/baseline benchmarks for drift detection.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT rps, latency_p99_ms, cpu_pct, phase, created_at
                FROM benchmarks
                WHERE system_id = %s AND payload_size = %s
                  AND phase IN ('baseline', 'scheduled', 'final')
                ORDER BY created_at ASC
                """,
                (system_id, payload_size),
            )
            return cur.fetchall()

    # ── Context ──────────────────────────────────────────────────────────────

    def save_context(
        self,
        session_id: str,
        type: str,
        source: str,
        content: str,
        summary: str = "",
        iteration_num: int | None = None,
    ) -> str:
        """Save a context entry. Backward-compatible + new iteration_num param."""
        cid = str(uuid.uuid4())
        system_id = self._system_id_for_session(session_id) or ""
        logical_type = type
        storage_type = self.CONTEXT_TYPE_MAP.get(type, "command_output")
        source = source[:250]
        if logical_type not in {"metric", "log", "command_output", "benchmark", "system_check"}:
            source = f"{logical_type}:{source}"[:250]

        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO context
                    (id, session_id, system_id, type, source, content, summary, iteration_num)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    cid,
                    session_id,
                    system_id,
                    storage_type,
                    source,
                    content,
                    summary or content[:500],
                    iteration_num,
                ),
            )
        return cid

    def get_contexts(
        self,
        session_id: str,
        type: str | None = None,
        source_prefix: str | None = None,
        limit: int | None = None,
        recent_iterations: int | None = None,
    ) -> list[dict]:
        """Get context entries with optional recency filter."""
        logical_type = type
        storage_type = self.CONTEXT_TYPE_MAP.get(type, type) if type else None
        query = "SELECT * FROM context WHERE session_id = %s"
        params: list[object] = [session_id]
        if storage_type:
            query += " AND type = %s"
            params.append(storage_type)
        if source_prefix:
            query += " AND source LIKE %s"
            params.append(f"{source_prefix}%")
        if recent_iterations is not None:
            query += (
                " AND iteration_num >= ("
                "SELECT COALESCE(MAX(iteration_num), 0) - %s"
                " FROM context WHERE session_id = %s)"
            )
            params.extend([recent_iterations, session_id])
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        with self._cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
        if logical_type in {"telemetry", "rca", "recommendation"}:
            prefix = f"{logical_type}:"
            rows = [row for row in rows if str(row.get("source", "")).startswith(prefix)]
            for row in rows:
                row["type"] = logical_type
                row["source"] = str(row.get("source", ""))[len(prefix) :]
        return rows

    def cleanup_context(self, session_id: str, keep_last_n: int = 50) -> int:
        """Remove old context entries, keeping the most recent N per session.

        Returns number of rows deleted.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                DELETE FROM context
                WHERE session_id = %s
                  AND id NOT IN (
                      SELECT id FROM (
                          SELECT id FROM context
                          WHERE session_id = %s
                          ORDER BY created_at DESC
                          LIMIT %s
                      ) AS keep_rows
                  )
                """,
                (session_id, session_id, keep_last_n),
            )
            return cur.rowcount

    # ── Vector search ────────────────────────────────────────────────────────

    def semantic_search(
        self, query: str, session_id: str | None = None, top_k: int = 3
    ) -> list[dict]:
        """Semantic search over knowledge base.

        Searches active knowledge entries, prioritizing high-confidence entries.
        If session_id is provided, also includes session-specific entries.
        """
        embedding = self._embedder.embed(query)
        vec_str = json.dumps(embedding)
        with self._cursor() as cur:
            if session_id:
                cur.execute(
                    """
                    SELECT parameter, reasoning, before_value, after_value,
                           impact_pct, type, confidence, scope,
                           VEC_COSINE_DISTANCE(embedding, %s) AS score
                    FROM knowledge
                    WHERE (discovered_by = %s OR type = 'knowledge'
                           OR scope IN ('universal', 'service_type'))
                      AND status = 'active'
                      AND embedding IS NOT NULL
                    ORDER BY score ASC
                    LIMIT %s
                    """,
                    (vec_str, session_id, top_k),
                )
            else:
                cur.execute(
                    """
                    SELECT parameter, reasoning, before_value, after_value,
                           impact_pct, type, confidence, scope,
                           VEC_COSINE_DISTANCE(embedding, %s) AS score
                    FROM knowledge
                    WHERE status = 'active' AND embedding IS NOT NULL
                    ORDER BY score ASC
                    LIMIT %s
                    """,
                    (vec_str, top_k),
                )
            return cur.fetchall()

    # ── Hypothesis queue ─────────────────────────────────────────────────────

    def populate_queue(self, session_id: str, hypotheses: list[dict]) -> None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM hypothesis_queue WHERE session_id=%s",
                (session_id,),
            )
            if cur.fetchone()["cnt"] > 0:
                return  # already populated — preserve existing state on restart
            for h in hypotheses:
                source = h.get("source", "llm")
                knowledge_ref = h.get("knowledge_ref")
                cur.execute(
                    """
                    INSERT INTO hypothesis_queue
                        (id, session_id, name, priority, source, knowledge_ref)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        session_id,
                        h["name"],
                        h["priority"],
                        source,
                        knowledge_ref,
                    ),
                )

    def next_hypothesis(self, session_id: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM hypothesis_queue
                WHERE session_id = %s AND status = 'pending'
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """,
                (session_id,),
            )
            return cur.fetchone()

    def mark_hypothesis(self, session_id: str, name: str, status: str, outcome: str = "") -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE hypothesis_queue
                SET status = %s, outcome = %s, updated_at = NOW()
                WHERE session_id = %s AND name = %s
                """,
                (status, outcome, session_id, name),
            )

    def pending_count(self, session_id: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) as cnt FROM hypothesis_queue
                WHERE session_id = %s AND status = 'pending'
                """,
                (session_id,),
            )
            return cur.fetchone()["cnt"]

    def get_queue(self, session_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM hypothesis_queue WHERE session_id = %s
                ORDER BY priority, created_at
                """,
                (session_id,),
            )
            return cur.fetchall()

    # ── Token history ────────────────────────────────────────────────────────

    def get_token_history(self) -> list[dict]:
        """Get token usage across all sessions."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT id as session_id, total_tokens, fixes_applied,
                       rps_delta_pct, started_at, completed_at
                FROM sessions
                WHERE status = 'completed'
                ORDER BY started_at ASC
            """)
            return cur.fetchall()

    # ── Knowledge promotion pipeline ─────────────────────────────────────────

    def run_knowledge_promotion(self, min_validations: int = 3) -> list[str]:
        """Promote system-scoped knowledge to service_type scope when validated enough.

        Returns list of promoted knowledge IDs.
        """
        promoted = []
        with self._cursor() as cur:
            # Find system-scoped knowledge with enough cross-system validations
            cur.execute(
                """
                SELECT k.id, k.parameter,
                       COUNT(DISTINCT v.system_id) as unique_systems
                FROM knowledge k
                JOIN validations v ON v.knowledge_id = k.id AND v.outcome = 'confirmed'
                WHERE k.scope = 'system' AND k.status = 'active'
                GROUP BY k.id, k.parameter
                HAVING unique_systems >= %s
                """,
                (min_validations,),
            )
            candidates = cur.fetchall()

        for row in candidates:
            self.promote_knowledge(row["id"], "service_type")
            promoted.append(row["id"])

        return promoted


def from_config(cfg: dict, embedder) -> TiDBStore:
    return TiDBStore(cfg, embedder)
