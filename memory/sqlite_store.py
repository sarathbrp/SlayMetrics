"""SQLite-backed memory store.

Zero-config, single file, no network dependency.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.embeddings import EmbeddingProvider


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Compute cosine distance between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


def _dict_from_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _dicts_from_rows(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS systems (
    id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    service TEXT NOT NULL DEFAULT 'nginx',
    service_type TEXT,
    rhel_version TEXT,
    kernel_version TEXT,
    cpu_cores INTEGER,
    ram_gb INTEGER,
    numa_nodes INTEGER,
    current_rps REAL,
    best_rps REAL,
    tuning_state TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    system_id TEXT NOT NULL,
    llm_profile TEXT,
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'running',
    total_tokens INTEGER NOT NULL DEFAULT 0,
    fixes_applied INTEGER NOT NULL DEFAULT 0,
    rps_start REAL,
    rps_end REAL,
    rps_delta_pct REAL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    completed_at TEXT,
    FOREIGN KEY (system_id) REFERENCES systems(id)
);

CREATE TABLE IF NOT EXISTS knowledge (
    id TEXT PRIMARY KEY,
    discovered_by TEXT NOT NULL,
    system_id TEXT,
    service_type TEXT,
    scope TEXT NOT NULL DEFAULT 'system',
    type TEXT NOT NULL DEFAULT 'fix',
    parameter TEXT NOT NULL,
    condition TEXT,
    before_value TEXT DEFAULT '',
    after_value TEXT DEFAULT '',
    recommendation TEXT,
    impact_pct REAL,
    confidence REAL NOT NULL DEFAULT 0.5,
    validations INTEGER NOT NULL DEFAULT 0,
    last_validated TEXT,
    superseded_by TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    reasoning TEXT,
    embedding TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS validations (
    id TEXT PRIMARY KEY,
    knowledge_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    system_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    before_rps REAL,
    after_rps REAL,
    impact_pct REAL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    FOREIGN KEY (knowledge_id) REFERENCES knowledge(id)
);

CREATE TABLE IF NOT EXISTS benchmarks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    system_id TEXT NOT NULL DEFAULT '',
    iteration_num INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'baseline',
    payload_size TEXT NOT NULL DEFAULT 'small',
    rps REAL,
    latency_avg_ms REAL,
    latency_p99_ms REAL,
    cpu_pct REAL,
    mem_pct REAL,
    errors INTEGER NOT NULL DEFAULT 0,
    fix_id TEXT,
    raw_output TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS context (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    system_id TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    iteration_num INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS hypothesis_queue (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'llm',
    knowledge_ref TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    outcome TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_system ON sessions(system_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_discovered ON knowledge(discovered_by);
CREATE INDEX IF NOT EXISTS idx_knowledge_system ON knowledge(system_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_param ON knowledge(parameter);
CREATE INDEX IF NOT EXISTS idx_context_session ON context(session_id);
CREATE INDEX IF NOT EXISTS idx_benchmarks_session ON benchmarks(session_id);
CREATE INDEX IF NOT EXISTS idx_hypothesis_session ON hypothesis_queue(session_id);
CREATE INDEX IF NOT EXISTS idx_validations_knowledge ON validations(knowledge_id);
"""


class SQLiteStore:
    """SQLite-backed memory store."""

    SYSTEM_FIELDS = {
        "service", "service_type", "host", "rhel_version", "kernel_version",
        "cpu_cores", "ram_gb", "numa_nodes", "current_rps", "best_rps", "tuning_state",
    }
    CONTEXT_TYPE_MAP = {
        "metric": "metric", "log": "log", "command_output": "command_output",
        "benchmark": "benchmark", "system_check": "system_check",
        "telemetry": "metric", "rca": "command_output", "recommendation": "command_output",
    }

    def __init__(self, cfg: dict, embedder: EmbeddingProvider):
        m = cfg.get("memory") or {}
        self._db_path = m.get("path", "data/slaymetrics.db")
        self._embedder = embedder
        self._conn: sqlite3.Connection | None = None
        self._system_id_cache: dict[str, str] = {}

    # ── Connection ───────────────────────────────────────────────────────────

    def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        if not self._conn:
            return
        self._conn.executescript(SCHEMA_SQL)

    def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _cursor(self):
        if not self._conn:
            self.connect()
        return self._conn.cursor()

    # ── System registry ──────────────────────────────────────────────────────

    def _get_or_create_system(self, host: str, service: str) -> str:
        cur = self._cursor()
        cur.execute(
            "SELECT id FROM systems WHERE host = ? AND service = ? LIMIT 1",
            (host, service),
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        system_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO systems (id, host, service) VALUES (?, ?, ?)",
            (system_id, host, service),
        )
        self._conn.commit()
        return system_id

    def get_system(self, system_id: str) -> dict | None:
        cur = self._cursor()
        cur.execute("SELECT * FROM systems WHERE id = ? LIMIT 1", (system_id,))
        return _dict_from_row(cur.fetchone())

    def get_system_by_host(self, host: str, service: str) -> dict | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM systems WHERE host = ? AND service = ? LIMIT 1",
            (host, service),
        )
        return _dict_from_row(cur.fetchone())

    def update_system(self, system_id: str, **kwargs) -> None:
        filtered = {k: v for k, v in kwargs.items() if k in self.SYSTEM_FIELDS}
        if not filtered:
            return
        if "tuning_state" in filtered and isinstance(filtered["tuning_state"], dict):
            filtered["tuning_state"] = json.dumps(filtered["tuning_state"])
        cols = ", ".join(f"{k} = ?" for k in filtered)
        cur = self._cursor()
        cur.execute(
            f"UPDATE systems SET {cols}, updated_at = ? WHERE id = ?",
            (*filtered.values(), _now(), system_id),
        )
        self._conn.commit()

    def _system_id_for_session(self, session_id: str) -> str | None:
        if session_id in self._system_id_cache:
            return self._system_id_cache[session_id]
        cur = self._cursor()
        cur.execute("SELECT system_id FROM sessions WHERE id = ? LIMIT 1", (session_id,))
        row = cur.fetchone()
        if row:
            self._system_id_cache[session_id] = row["system_id"]
            return row["system_id"]
        return None

    # ── Sessions ─────────────────────────────────────────────────────────────

    def create_session(self, session_id: str, service: str, host: str, llm_profile: str) -> str:
        system_id = self._get_or_create_system(host, service)
        cur = self._cursor()
        cur.execute(
            """INSERT OR REPLACE INTO sessions (id, system_id, llm_profile, started_at)
               VALUES (?, ?, ?, ?)""",
            (session_id, system_id, llm_profile, _now()),
        )
        self._conn.commit()
        self._system_id_cache[session_id] = system_id
        return system_id

    def complete_session(
        self, session_id: str, total_tokens: int = 0, fixes_applied: int = 0,
        rps_start: float | None = None, rps_end: float | None = None,
    ) -> None:
        delta = None
        if rps_start and rps_end and rps_start > 0:
            delta = (rps_end - rps_start) / rps_start * 100
        cur = self._cursor()
        cur.execute(
            """UPDATE sessions
               SET status = 'completed', total_tokens = ?, fixes_applied = ?,
                   rps_start = ?, rps_end = ?, rps_delta_pct = ?, completed_at = ?
               WHERE id = ?""",
            (total_tokens, fixes_applied, rps_start, rps_end, delta, _now(), session_id),
        )
        self._conn.commit()

    def get_session(self, session_id: str) -> dict | None:
        cur = self._cursor()
        cur.execute("SELECT * FROM sessions WHERE id = ? LIMIT 1", (session_id,))
        return _dict_from_row(cur.fetchone())

    def session_exists(self, session_id: str) -> bool:
        cur = self._cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM sessions WHERE id = ?", (session_id,))
        return cur.fetchone()["cnt"] > 0

    def get_latest_session_for_host(self, host: str, exclude_session_id: str | None = None) -> str | None:
        query = """SELECT s.id as session_id FROM sessions s
                   JOIN systems sys ON s.system_id = sys.id WHERE sys.host = ?"""
        params: list = [host]
        if exclude_session_id:
            query += " AND s.id <> ?"
            params.append(exclude_session_id)
        query += " ORDER BY s.started_at DESC LIMIT 1"
        cur = self._cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        return row["session_id"] if row else None

    # ── Profile (backward-compatible facade) ─────────────────────────────────

    def update_profile(self, session_id: str, **kwargs) -> None:
        system_id = self._system_id_for_session(session_id)
        if not system_id:
            return
        remap = {"baseline_rps": "current_rps", "best_rps": "best_rps", "status": None, "llm_profile": None}
        system_kwargs = {}
        for k, v in kwargs.items():
            mapped = remap.get(k, k)
            if mapped is not None:
                system_kwargs[mapped] = v
        if system_kwargs:
            self.update_system(system_id, **system_kwargs)
        session_updates = {}
        if "status" in kwargs:
            session_updates["status"] = kwargs["status"]
        if "llm_profile" in kwargs:
            session_updates["llm_profile"] = kwargs["llm_profile"]
        if "baseline_rps" in kwargs:
            session_updates["rps_start"] = kwargs["baseline_rps"]
        if session_updates:
            cols = ", ".join(f"{k} = ?" for k in session_updates)
            cur = self._cursor()
            cur.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*session_updates.values(), session_id))
            self._conn.commit()

    def get_profile(self, session_id: str) -> dict | None:
        cur = self._cursor()
        cur.execute(
            """SELECT sys.id as system_id, sys.host, sys.service, sys.service_type,
                      sys.rhel_version, sys.kernel_version, sys.cpu_cores, sys.ram_gb,
                      sys.numa_nodes, sys.current_rps as baseline_rps, sys.best_rps,
                      sys.tuning_state, s.id as session_id, s.llm_profile, s.status,
                      s.total_tokens, s.fixes_applied, s.rps_start, s.rps_end,
                      s.rps_delta_pct, s.started_at, s.completed_at
               FROM sessions s JOIN systems sys ON s.system_id = sys.id
               WHERE s.id = ? LIMIT 1""",
            (session_id,),
        )
        return _dict_from_row(cur.fetchone())

    # ── Knowledge ────────────────────────────────────────────────────────────

    def save_fact(
        self, session_id: str, type: str, parameter: str, reasoning: str,
        before_value: str = "", after_value: str = "", before_rps: float | None = None,
        after_rps: float | None = None, impact_pct: float | None = None,
        status: str = "applied", scope: str = "system", condition: str | None = None,
        confidence: float = 0.5,
    ) -> str:
        system_id = self._system_id_for_session(session_id)
        service_type = None
        if system_id:
            sys_row = self.get_system(system_id)
            if sys_row:
                service_type = sys_row.get("service_type") or sys_row.get("service")

        text = f"{parameter} {reasoning} {before_value} {after_value}"
        embedding = self._embedder.embed(text)
        k_status = "active"
        if status in ("reverted", "regressed", "negative"):
            k_status = "deprecated"

        # Reuse existing fix fact
        if system_id and type == "fix":
            existing_id = self._find_existing_fix_fact(
                system_id=system_id, parameter=parameter,
                before_value=before_value, after_value=after_value, reasoning=reasoning,
            )
            if existing_id:
                outcome = "confirmed" if status == "applied" else "contradicted"
                self._refresh_existing_fix_fact(
                    knowledge_id=existing_id, impact_pct=impact_pct,
                    confidence=confidence, status=k_status,
                )
                self.save_validation(
                    knowledge_id=existing_id, session_id=session_id,
                    system_id=system_id, outcome=outcome,
                    before_rps=before_rps, after_rps=after_rps, impact_pct=impact_pct,
                )
                return existing_id

        kid = str(uuid.uuid4())
        cur = self._cursor()
        cur.execute(
            """INSERT INTO knowledge
                (id, discovered_by, system_id, service_type, scope, type,
                 parameter, condition, before_value, after_value,
                 impact_pct, confidence, status, reasoning, embedding)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kid, session_id, system_id, service_type, scope, type,
             parameter, condition, before_value, after_value,
             _coerce_optional_float(impact_pct), confidence, k_status,
             reasoning, json.dumps(embedding)),
        )
        self._conn.commit()

        if system_id and type == "fix":
            outcome = "confirmed" if status == "applied" else "contradicted"
            self.save_validation(
                knowledge_id=kid, session_id=session_id, system_id=system_id,
                outcome=outcome, before_rps=before_rps, after_rps=after_rps,
                impact_pct=impact_pct,
            )
        return kid

    def _find_existing_fix_fact(self, *, system_id, parameter, before_value, after_value, reasoning) -> str | None:
        cur = self._cursor()
        cur.execute(
            """SELECT id FROM knowledge
               WHERE system_id = ? AND type = 'fix' AND status = 'active'
                 AND parameter = ? AND COALESCE(before_value, '') = ?
                 AND COALESCE(after_value, '') = ? AND COALESCE(reasoning, '') = ?
               ORDER BY created_at DESC LIMIT 1""",
            (system_id, parameter, before_value, after_value, reasoning),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def _refresh_existing_fix_fact(self, *, knowledge_id, impact_pct=None, confidence=None, status="active"):
        cur = self._cursor()
        cur.execute(
            """UPDATE knowledge
               SET impact_pct = COALESCE(?, impact_pct),
                   confidence = MAX(confidence, COALESCE(?, 0.0)),
                   status = ?
               WHERE id = ?""",
            (_coerce_optional_float(impact_pct), _coerce_optional_float(confidence), status, knowledge_id),
        )
        self._conn.commit()

    def _find_fix_fact_by_identity(self, *, system_id, parameter, before_value, after_value) -> str | None:
        cur = self._cursor()
        cur.execute(
            """SELECT id FROM knowledge
               WHERE system_id = ? AND type = 'fix' AND status = 'active'
                 AND parameter = ? AND COALESCE(before_value, '') = ?
                 AND COALESCE(after_value, '') = ?
               ORDER BY created_at DESC LIMIT 1""",
            (system_id, parameter, before_value, after_value),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def save_optimization_validation(self, *, session_id, parameter, before_value="", after_value="",
                                      outcome, reasoning="", before_rps=None, after_rps=None,
                                      impact_pct=None, notes=None, scope="system", confidence=0.5) -> str | None:
        system_id = self._system_id_for_session(session_id)
        if not system_id:
            return None
        service_type = None
        sys_row = self.get_system(system_id)
        if sys_row:
            service_type = sys_row.get("service_type") or sys_row.get("service")

        knowledge_id = self._find_fix_fact_by_identity(
            system_id=system_id, parameter=parameter,
            before_value=before_value, after_value=after_value,
        )
        if not knowledge_id:
            kid = str(uuid.uuid4())
            text = f"{parameter} {reasoning} {before_value} {after_value}"
            embedding = self._embedder.embed(text)
            cur = self._cursor()
            cur.execute(
                """INSERT INTO knowledge
                    (id, discovered_by, system_id, service_type, scope, type,
                     parameter, condition, before_value, after_value,
                     impact_pct, confidence, status, reasoning, embedding)
                   VALUES (?,?,?,?,?,'fix',?,NULL,?,?,?,?,'active',?,?)""",
                (kid, session_id, system_id, service_type, scope, parameter,
                 before_value, after_value, _coerce_optional_float(impact_pct),
                 confidence, reasoning, json.dumps(embedding)),
            )
            self._conn.commit()
            knowledge_id = kid
        else:
            self._refresh_existing_fix_fact(
                knowledge_id=knowledge_id, impact_pct=impact_pct,
                confidence=confidence, status="active",
            )
        self.save_validation(
            knowledge_id=knowledge_id, session_id=session_id, system_id=system_id,
            outcome=outcome, before_rps=before_rps, after_rps=after_rps,
            impact_pct=impact_pct, notes=notes,
        )
        return knowledge_id

    def get_facts(self, session_id: str, type: str | None = None) -> list[dict]:
        cur = self._cursor()
        if type:
            cur.execute(
                """SELECT id, discovered_by as session_id, type, parameter,
                          before_value, after_value, impact_pct,
                          confidence, status, reasoning, created_at
                   FROM knowledge WHERE discovered_by = ? AND type = ?
                   ORDER BY created_at""",
                (session_id, type),
            )
        else:
            cur.execute(
                """SELECT id, discovered_by as session_id, type, parameter,
                          before_value, after_value, impact_pct,
                          confidence, status, reasoning, created_at
                   FROM knowledge WHERE discovered_by = ? ORDER BY created_at""",
                (session_id,),
            )
        return _dicts_from_rows(cur.fetchall())

    def get_all_fixes_for_host(self, host: str) -> list[dict]:
        cur = self._cursor()
        cur.execute(
            """SELECT k.discovered_by as session_id, k.type, k.parameter,
                      k.before_value, k.after_value, k.impact_pct,
                      k.confidence, k.reasoning, k.status, k.created_at
               FROM knowledge k
               JOIN sessions s ON k.discovered_by = s.id
               JOIN systems sys ON s.system_id = sys.id
               WHERE sys.host = ? AND k.type = 'fix' AND k.status = 'active'
               ORDER BY k.confidence DESC, k.created_at DESC""",
            (host,),
        )
        return _dicts_from_rows(cur.fetchall())

    def get_knowledge_for_service(self, service_type: str, min_confidence: float = 0.0) -> list[dict]:
        cur = self._cursor()
        cur.execute(
            """SELECT id, scope, type, parameter, condition, recommendation,
                      impact_pct, confidence, validations, reasoning, created_at
               FROM knowledge
               WHERE status = 'active'
                 AND (scope = 'universal' OR (scope = 'service_type' AND service_type = ?))
                 AND confidence >= ?
               ORDER BY confidence DESC, validations DESC""",
            (service_type, min_confidence),
        )
        return _dicts_from_rows(cur.fetchall())

    def promote_knowledge(self, knowledge_id: str, new_scope: str) -> None:
        cur = self._cursor()
        cur.execute("UPDATE knowledge SET scope = ? WHERE id = ?", (new_scope, knowledge_id))
        self._conn.commit()

    def supersede_knowledge(self, old_id: str, new_id: str) -> None:
        cur = self._cursor()
        cur.execute(
            "UPDATE knowledge SET status = 'superseded', superseded_by = ? WHERE id = ?",
            (new_id, old_id),
        )
        self._conn.commit()

    # ── Validations ──────────────────────────────────────────────────────────

    def save_validation(self, knowledge_id, session_id, system_id, outcome,
                        before_rps=None, after_rps=None, impact_pct=None, notes=None) -> str:
        vid = str(uuid.uuid4())
        cur = self._cursor()
        cur.execute(
            """INSERT INTO validations (id, knowledge_id, session_id, system_id, outcome,
                                        before_rps, after_rps, impact_pct, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (vid, knowledge_id, session_id, system_id, outcome,
             _coerce_optional_float(before_rps), _coerce_optional_float(after_rps),
             _coerce_optional_float(impact_pct), notes),
        )
        self._conn.commit()
        self._update_knowledge_confidence(knowledge_id, outcome)
        return vid

    def _update_knowledge_confidence(self, knowledge_id: str, outcome: str) -> None:
        delta = {"confirmed": 0.1, "contradicted": -0.15, "partial": 0.03}.get(outcome, 0)
        cur = self._cursor()
        cur.execute(
            """UPDATE knowledge
               SET confidence = MAX(0, MIN(1, confidence + ?)),
                   validations = validations + 1, last_validated = ?
               WHERE id = ?""",
            (delta, _now(), knowledge_id),
        )
        if outcome == "contradicted":
            cur.execute(
                """UPDATE knowledge SET status = 'deprecated'
                   WHERE id = ? AND confidence < 0.2 AND status = 'active'""",
                (knowledge_id,),
            )
        self._conn.commit()

    def get_validations(self, knowledge_id: str) -> list[dict]:
        cur = self._cursor()
        cur.execute(
            """SELECT v.*, sys.host, sys.service
               FROM validations v JOIN systems sys ON v.system_id = sys.id
               WHERE v.knowledge_id = ? ORDER BY v.created_at DESC""",
            (knowledge_id,),
        )
        return _dicts_from_rows(cur.fetchall())

    # ── Benchmarks ───────────────────────────────────────────────────────────

    def save_benchmark(self, session_id, iteration_num, phase, payload_size,
                       rps=None, latency_avg_ms=None, latency_p99_ms=None,
                       cpu_pct=None, mem_pct=None, errors=0, fix_id=None, raw_output=None) -> str:
        bid = str(uuid.uuid4())
        system_id = self._system_id_for_session(session_id) or ""
        cur = self._cursor()
        cur.execute(
            """INSERT INTO benchmarks
                (id, session_id, system_id, iteration_num, phase, payload_size,
                 rps, latency_avg_ms, latency_p99_ms, cpu_pct, mem_pct,
                 errors, fix_id, raw_output)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bid, session_id, system_id, iteration_num, phase, payload_size,
             _coerce_optional_float(rps), _coerce_optional_float(latency_avg_ms),
             _coerce_optional_float(latency_p99_ms), _coerce_optional_float(cpu_pct),
             _coerce_optional_float(mem_pct), errors, fix_id, raw_output),
        )
        self._conn.commit()
        return bid

    def get_benchmarks(self, session_id=None, system_id=None, phase=None, payload_size=None) -> list[dict]:
        query = "SELECT * FROM benchmarks WHERE 1=1"
        params: list = []
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if system_id:
            query += " AND system_id = ?"
            params.append(system_id)
        if phase:
            query += " AND phase = ?"
            params.append(phase)
        if payload_size:
            query += " AND payload_size = ?"
            params.append(payload_size)
        query += " ORDER BY iteration_num ASC, payload_size ASC"
        cur = self._cursor()
        cur.execute(query, params)
        return _dicts_from_rows(cur.fetchall())

    def get_benchmark_comparison(self, session_id: str) -> dict:
        result = {}
        for size in ("small", "medium", "large"):
            cur = self._cursor()
            cur.execute(
                """SELECT phase, rps, latency_p99_ms, cpu_pct, mem_pct
                   FROM benchmarks
                   WHERE session_id = ? AND payload_size = ? AND phase IN ('baseline', 'final')
                   ORDER BY phase ASC""",
                (session_id, size),
            )
            rows = _dicts_from_rows(cur.fetchall())
            baseline = next((r for r in rows if r["phase"] == "baseline"), {})
            final = next((r for r in rows if r["phase"] == "final"), {})
            b_rps = baseline.get("rps") or 0
            f_rps = final.get("rps") or 0
            result[size] = {
                "baseline_rps": b_rps, "baseline_p99": baseline.get("latency_p99_ms"),
                "final_rps": f_rps, "final_p99": final.get("latency_p99_ms"),
                "delta_pct": ((f_rps - b_rps) / b_rps * 100) if b_rps else 0,
                "baseline_cpu": baseline.get("cpu_pct"), "final_cpu": final.get("cpu_pct"),
            }
        return result

    def get_performance_trend(self, system_id: str, payload_size: str = "small") -> list[dict]:
        cur = self._cursor()
        cur.execute(
            """SELECT rps, latency_p99_ms, cpu_pct, phase, created_at
               FROM benchmarks
               WHERE system_id = ? AND payload_size = ? AND phase IN ('baseline', 'scheduled', 'final')
               ORDER BY created_at ASC""",
            (system_id, payload_size),
        )
        return _dicts_from_rows(cur.fetchall())

    # ── Context ──────────────────────────────────────────────────────────────

    def save_context(self, session_id, type, source, content, summary="", iteration_num=None) -> str:
        cid = str(uuid.uuid4())
        system_id = self._system_id_for_session(session_id) or ""
        logical_type = type
        storage_type = self.CONTEXT_TYPE_MAP.get(type, "command_output")
        source = source[:250]
        if logical_type not in {"metric", "log", "command_output", "benchmark", "system_check"}:
            source = f"{logical_type}:{source}"[:250]
        cur = self._cursor()
        cur.execute(
            """INSERT INTO context (id, session_id, system_id, type, source, content, summary, iteration_num)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cid, session_id, system_id, storage_type, source, content, summary or content[:500], iteration_num),
        )
        self._conn.commit()
        return cid

    def get_contexts(self, session_id, type=None, source_prefix=None, limit=None, recent_iterations=None) -> list[dict]:
        logical_type = type
        storage_type = self.CONTEXT_TYPE_MAP.get(type, type) if type else None
        query = "SELECT * FROM context WHERE session_id = ?"
        params: list = [session_id]
        if storage_type:
            query += " AND type = ?"
            params.append(storage_type)
        if source_prefix:
            query += " AND source LIKE ?"
            params.append(f"{source_prefix}%")
        if recent_iterations is not None:
            query += (" AND iteration_num >= ("
                      "SELECT COALESCE(MAX(iteration_num), 0) - ?"
                      " FROM context WHERE session_id = ?)")
            params.extend([recent_iterations, session_id])
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        cur = self._cursor()
        cur.execute(query, params)
        rows = _dicts_from_rows(cur.fetchall())
        if logical_type in {"telemetry", "rca", "recommendation"}:
            prefix = f"{logical_type}:"
            rows = [row for row in rows if str(row.get("source", "")).startswith(prefix)]
            for row in rows:
                row["type"] = logical_type
                row["source"] = str(row.get("source", ""))[len(prefix):]
        return rows

    def cleanup_context(self, session_id: str, keep_last_n: int = 50) -> int:
        cur = self._cursor()
        cur.execute(
            """DELETE FROM context WHERE session_id = ? AND id NOT IN (
                SELECT id FROM context WHERE session_id = ?
                ORDER BY created_at DESC LIMIT ?)""",
            (session_id, session_id, keep_last_n),
        )
        deleted = cur.rowcount
        self._conn.commit()
        return deleted

    # ── Vector search ────────────────────────────────────────────────────────

    def semantic_search(self, query: str, session_id: str | None = None, top_k: int = 3) -> list[dict]:
        query_embedding = self._embedder.embed(query)
        cur = self._cursor()
        if session_id:
            cur.execute(
                """SELECT parameter, reasoning, before_value, after_value,
                          impact_pct, type, confidence, scope, embedding
                   FROM knowledge
                   WHERE (discovered_by = ? OR type = 'knowledge'
                          OR scope IN ('universal', 'service_type'))
                     AND status = 'active' AND embedding IS NOT NULL""",
                (session_id,),
            )
        else:
            cur.execute(
                """SELECT parameter, reasoning, before_value, after_value,
                          impact_pct, type, confidence, scope, embedding
                   FROM knowledge
                   WHERE status = 'active' AND embedding IS NOT NULL""",
            )
        rows = cur.fetchall()
        scored = []
        for row in rows:
            row_dict = dict(row)
            try:
                row_embedding = json.loads(row_dict.pop("embedding"))
                score = _cosine_distance(query_embedding, row_embedding)
            except (json.JSONDecodeError, TypeError):
                score = 1.0
            row_dict["score"] = score
            scored.append(row_dict)
        scored.sort(key=lambda x: x["score"])
        return scored[:top_k]

    # ── Hypothesis queue ─────────────────────────────────────────────────────

    def populate_queue(self, session_id: str, hypotheses: list[dict]) -> None:
        cur = self._cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM hypothesis_queue WHERE session_id=?", (session_id,))
        if cur.fetchone()["cnt"] > 0:
            return
        for h in hypotheses:
            cur.execute(
                """INSERT INTO hypothesis_queue (id, session_id, name, priority, source, knowledge_ref)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), session_id, h["name"], h["priority"],
                 h.get("source", "llm"), h.get("knowledge_ref")),
            )
        self._conn.commit()

    def next_hypothesis(self, session_id: str) -> dict | None:
        cur = self._cursor()
        cur.execute(
            """SELECT * FROM hypothesis_queue
               WHERE session_id = ? AND status = 'pending'
               ORDER BY priority ASC, created_at ASC LIMIT 1""",
            (session_id,),
        )
        return _dict_from_row(cur.fetchone())

    def mark_hypothesis(self, session_id: str, name: str, status: str, outcome: str = "") -> None:
        cur = self._cursor()
        cur.execute(
            """UPDATE hypothesis_queue SET status = ?, outcome = ?, updated_at = ?
               WHERE session_id = ? AND name = ?""",
            (status, outcome, _now(), session_id, name),
        )
        self._conn.commit()

    def pending_count(self, session_id: str) -> int:
        cur = self._cursor()
        cur.execute(
            "SELECT COUNT(*) as cnt FROM hypothesis_queue WHERE session_id = ? AND status = 'pending'",
            (session_id,),
        )
        return cur.fetchone()["cnt"]

    def get_queue(self, session_id: str) -> list[dict]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM hypothesis_queue WHERE session_id = ? ORDER BY priority, created_at",
            (session_id,),
        )
        return _dicts_from_rows(cur.fetchall())

    # ── Token history ────────────────────────────────────────────────────────

    def get_token_history(self) -> list[dict]:
        cur = self._cursor()
        cur.execute(
            """SELECT id as session_id, total_tokens, fixes_applied,
                      rps_delta_pct, started_at, completed_at
               FROM sessions WHERE status = 'completed' ORDER BY started_at ASC""",
        )
        return _dicts_from_rows(cur.fetchall())

    # ── Knowledge promotion ──────────────────────────────────────────────────

    def run_knowledge_promotion(self, min_validations: int = 3) -> list[str]:
        promoted = []
        cur = self._cursor()
        cur.execute(
            """SELECT k.id, k.parameter, COUNT(DISTINCT v.system_id) as unique_systems
               FROM knowledge k
               JOIN validations v ON v.knowledge_id = k.id AND v.outcome = 'confirmed'
               WHERE k.scope = 'system' AND k.status = 'active'
               GROUP BY k.id, k.parameter HAVING unique_systems >= ?""",
            (min_validations,),
        )
        candidates = _dicts_from_rows(cur.fetchall())
        for row in candidates:
            self.promote_knowledge(row["id"], "service_type")
            promoted.append(row["id"])
        return promoted


def from_config(cfg: dict, embedder) -> SQLiteStore:
    return SQLiteStore(cfg, embedder)
