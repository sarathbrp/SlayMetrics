from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pymysql
import pymysql.cursors

from memory.embeddings import EmbeddingProvider


@dataclass
class Fact:
    id: str
    session_id: str
    type: str
    parameter: str
    before_value: str
    after_value: str
    before_rps: float | None
    after_rps: float | None
    impact_pct: float | None
    reasoning: str
    status: str
    created_at: datetime


@dataclass
class ContextEntry:
    id: str
    session_id: str
    type: str
    source: str
    content: str
    summary: str
    created_at: datetime


class TiDBStore:
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

    # ── Connection ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._conn = pymysql.connect(**self._conn_kwargs)

    def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _cursor(self):
        if not self._conn or not self._conn.open:
            self.connect()
        return self._conn.cursor()

    # ── Profile ─────────────────────────────────────────────────────────────

    def create_session(self, session_id: str, service: str, host: str,
                       llm_profile: str) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO profile (id, session_id, service, host, llm_profile)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE updated_at = NOW()
            """, (str(uuid.uuid4()), session_id, service, host, llm_profile))

    def update_profile(self, session_id: str, **kwargs) -> None:
        if not kwargs:
            return
        cols = ", ".join(f"{k} = %s" for k in kwargs)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE profile SET {cols}, updated_at = NOW() WHERE session_id = %s",
                (*kwargs.values(), session_id),
            )

    def get_profile(self, session_id: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM profile WHERE session_id = %s LIMIT 1",
                        (session_id,))
            return cur.fetchone()

    # ── Facts ────────────────────────────────────────────────────────────────

    def save_fact(self, session_id: str, type: str, parameter: str,
                  reasoning: str, before_value: str = "", after_value: str = "",
                  before_rps: float | None = None, after_rps: float | None = None,
                  impact_pct: float | None = None, status: str = "applied") -> str:
        fid = str(uuid.uuid4())
        text = f"{parameter} {reasoning} {before_value} {after_value}"
        embedding = self._embedder.embed(text)
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO facts
                    (id, session_id, type, parameter, before_value, after_value,
                     before_rps, after_rps, impact_pct, reasoning, status, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (fid, session_id, type, parameter, before_value, after_value,
                  before_rps, after_rps, impact_pct, reasoning, status,
                  json.dumps(embedding)))
        return fid

    def get_facts(self, session_id: str, type: str | None = None) -> list[dict]:
        with self._cursor() as cur:
            if type:
                cur.execute(
                    "SELECT * FROM facts WHERE session_id=%s AND type=%s ORDER BY created_at",
                    (session_id, type),
                )
            else:
                cur.execute(
                    "SELECT * FROM facts WHERE session_id=%s ORDER BY created_at",
                    (session_id,),
                )
            return cur.fetchall()

    def get_all_fixes_for_host(self, host: str) -> list[dict]:
        """Get all fixes across ALL sessions for a given host — cross-session learning."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT f.session_id, f.type, f.parameter, f.before_value,
                       f.after_value, f.before_rps, f.after_rps,
                       f.impact_pct, f.reasoning, f.status, f.created_at
                FROM facts f
                JOIN profile p ON f.session_id = p.session_id
                WHERE p.host = %s AND f.type = 'fix'
                ORDER BY f.created_at DESC
            """, (host,))
            return cur.fetchall()

    # ── Context ──────────────────────────────────────────────────────────────

    def save_context(self, session_id: str, type: str, source: str,
                     content: str, summary: str = "") -> str:
        cid = str(uuid.uuid4())
        source = source[:250]  # VARCHAR(256) safety
        text = f"{source} {summary or content[:500]}"
        embedding = self._embedder.embed(text)
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO context
                    (id, session_id, type, source, content, summary, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (cid, session_id, type, source, content,
                  summary or content[:500], json.dumps(embedding)))
        return cid

    # ── Vector search ────────────────────────────────────────────────────────

    def semantic_search(self, query: str, session_id: str | None = None,
                        top_k: int = 3) -> list[dict]:
        embedding = self._embedder.embed(query)
        vec_str = json.dumps(embedding)
        with self._cursor() as cur:
            if session_id:
                cur.execute("""
                    SELECT parameter, reasoning, before_value, after_value,
                           impact_pct, type,
                           VEC_COSINE_DISTANCE(embedding, %s) AS score
                    FROM facts
                    WHERE (session_id = %s OR type = 'knowledge')
                      AND embedding IS NOT NULL
                    ORDER BY score ASC
                    LIMIT %s
                """, (vec_str, session_id, top_k))
            else:
                cur.execute("""
                    SELECT parameter, reasoning, before_value, after_value,
                           impact_pct, type,
                           VEC_COSINE_DISTANCE(embedding, %s) AS score
                    FROM facts
                    WHERE embedding IS NOT NULL
                    ORDER BY score ASC
                    LIMIT %s
                """, (vec_str, top_k))
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
                cur.execute("""
                    INSERT INTO hypothesis_queue (id, session_id, name, priority)
                    VALUES (%s, %s, %s, %s)
                """, (str(uuid.uuid4()), session_id, h["name"], h["priority"]))

    def next_hypothesis(self, session_id: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute("""
                SELECT * FROM hypothesis_queue
                WHERE session_id = %s AND status = 'pending'
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
            """, (session_id,))
            return cur.fetchone()

    def mark_hypothesis(self, session_id: str, name: str,
                        status: str, outcome: str = "") -> None:
        with self._cursor() as cur:
            cur.execute("""
                UPDATE hypothesis_queue
                SET status = %s, outcome = %s, updated_at = NOW()
                WHERE session_id = %s AND name = %s
            """, (status, outcome, session_id, name))

    def pending_count(self, session_id: str) -> int:
        with self._cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as cnt FROM hypothesis_queue
                WHERE session_id = %s AND status = 'pending'
            """, (session_id,))
            return cur.fetchone()["cnt"]

    def get_queue(self, session_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT * FROM hypothesis_queue WHERE session_id = %s
                ORDER BY priority, created_at
            """, (session_id,))
            return cur.fetchall()

    # ── Session resume ───────────────────────────────────────────────────────

    def get_token_history(self) -> list[dict]:
        """Get token usage across all sessions for tracking consumption over time."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT session_id, source, content, created_at
                FROM context
                WHERE type = 'metric' AND source = 'token_usage'
                ORDER BY created_at ASC
            """)
            rows = cur.fetchall()
        history = []
        for row in rows:
            try:
                data = json.loads(row.get("content", "{}"))
                data["created_at"] = str(row.get("created_at", ""))
                data["session_id"] = row.get("session_id", "")
                history.append(data)
            except (json.JSONDecodeError, TypeError):
                pass
        return history

    def session_exists(self, session_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM profile WHERE session_id = %s",
                (session_id,),
            )
            return cur.fetchone()["cnt"] > 0


def from_config(cfg: dict, embedder) -> TiDBStore:
    return TiDBStore(cfg, embedder)
