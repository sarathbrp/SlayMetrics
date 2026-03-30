-- SlayMetricsAgent — TiDB Schema (Production-Grade)
-- Run once: mysql -h 127.0.0.1 -P 4000 -u root < schema.sql
--
-- Architecture: knowledge-scoped, not session-scoped
--   systems    → persistent identity per host+service (survives across sessions)
--   sessions   → each agent run against a system
--   knowledge  → learned tuning facts with scope, confidence, lineage
--   validations→ audit trail: every time knowledge is confirmed or contradicted
--   benchmarks → structured performance data (never expires)
--   context    → hot working memory (session-scoped, cleaned up after)
--   hypothesis_queue → per-session work queue with provenance

CREATE DATABASE IF NOT EXISTS perfagent;
USE perfagent;

-- ─── Systems ────────────────────────────────────────────────────────────────
-- One row per host+service. Persists across sessions.
-- Replaces the old per-session `profile` table.
CREATE TABLE IF NOT EXISTS systems (
    id              VARCHAR(64)     PRIMARY KEY,
    host            VARCHAR(256)    NOT NULL,
    service         VARCHAR(64)     NOT NULL,
    service_type    VARCHAR(64),                        -- webserver, database, cache
    rhel_version    VARCHAR(128),
    kernel_version  VARCHAR(64),
    cpu_cores       INT,
    ram_gb          INT,
    numa_nodes      INT,
    current_rps     FLOAT,                              -- latest known RPS
    best_rps        FLOAT,                              -- best RPS ever achieved
    tuning_state    JSON,                               -- current applied config snapshot
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_host_service (host, service)
);

-- ─── Sessions ───────────────────────────────────────────────────────────────
-- Each agent run against a system. Tracks token usage and outcome.
CREATE TABLE IF NOT EXISTS sessions (
    id              VARCHAR(64)     PRIMARY KEY,
    system_id       VARCHAR(64)     NOT NULL,
    llm_profile     VARCHAR(64),
    trigger_type    ENUM('manual', 'scheduled', 'alert') DEFAULT 'manual',
    status          ENUM('running', 'completed', 'failed', 'escalated') DEFAULT 'running',
    total_tokens    INT             DEFAULT 0,
    fixes_applied   INT             DEFAULT 0,
    rps_start       FLOAT,                              -- baseline RPS at session start
    rps_end         FLOAT,                              -- final RPS at session end
    rps_delta_pct   FLOAT,                              -- overall improvement %
    started_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP       NULL,
    FOREIGN KEY (system_id) REFERENCES systems(id)
);

-- ─── Knowledge ──────────────────────────────────────────────────────────────
-- Learned tuning facts with scope and confidence.
-- Replaces the old `facts` table. Key differences:
--   - scope: controls reusability across systems/services
--   - confidence: increases with validations, decreases on contradictions
--   - superseded_by: handles fact evolution without deletion
--   - condition: machine-readable applicability ("cpu_cores >= 16")
CREATE TABLE IF NOT EXISTS knowledge (
    id              VARCHAR(64)     PRIMARY KEY,
    discovered_by   VARCHAR(64)     NOT NULL,           -- session that found this
    system_id       VARCHAR(64),                        -- NULL = cross-system knowledge
    service_type    VARCHAR(64),                        -- nginx, postgres, NULL = universal
    scope           ENUM('universal', 'service_type', 'platform', 'system') NOT NULL,
    type            ENUM('finding', 'fix', 'negative', 'constraint', 'knowledge') NOT NULL,
    parameter       VARCHAR(256),
    condition       TEXT,                               -- applicability predicate
    before_value    TEXT,
    after_value     TEXT,
    recommendation  TEXT,
    impact_pct      FLOAT,
    confidence      FLOAT           DEFAULT 0.5,        -- 0-1, grows with validations
    validations     INT             DEFAULT 1,
    last_validated  TIMESTAMP       NULL,
    superseded_by   VARCHAR(64),                        -- FK to knowledge.id
    status          ENUM('active', 'superseded', 'deprecated') DEFAULT 'active',
    reasoning       TEXT,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    embedding       VECTOR(1536)
);

-- ─── Validations ────────────────────────────────────────────────────────────
-- Audit trail: every time knowledge is tested on a system.
-- "This fix was confirmed on 3 different 16-core RHEL 9 boxes."
CREATE TABLE IF NOT EXISTS validations (
    id              VARCHAR(64)     PRIMARY KEY,
    knowledge_id    VARCHAR(64)     NOT NULL,
    session_id      VARCHAR(64)     NOT NULL,
    system_id       VARCHAR(64)     NOT NULL,
    outcome         ENUM('confirmed', 'contradicted', 'partial') NOT NULL,
    before_rps      FLOAT,
    after_rps       FLOAT,
    impact_pct      FLOAT,
    notes           TEXT,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge(id),
    FOREIGN KEY (session_id)   REFERENCES sessions(id),
    FOREIGN KEY (system_id)    REFERENCES systems(id)
);

-- ─── Benchmarks ─────────────────────────────────────────────────────────────
-- Structured performance data. Never expires — your evidence base.
-- Replaces benchmark data that was stored as TEXT in old `context` table.
CREATE TABLE IF NOT EXISTS benchmarks (
    id              VARCHAR(64)     PRIMARY KEY,
    session_id      VARCHAR(64)     NOT NULL,
    system_id       VARCHAR(64)     NOT NULL,
    iteration_num   INT             NOT NULL,
    phase           ENUM('baseline', 'post_fix', 'final', 'stability', 'scheduled') NOT NULL,
    payload_size    ENUM('homepage', 'small', 'medium', 'large', 'mixed') NOT NULL,
    rps             FLOAT,
    latency_avg_ms  FLOAT,
    latency_p99_ms  FLOAT,
    cpu_pct         FLOAT,
    mem_pct         FLOAT,
    errors          INT             DEFAULT 0,
    fix_id          VARCHAR(64),                        -- links to knowledge.id
    raw_output      TEXT,                               -- full wrk2/benchmark.sh output
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (system_id)  REFERENCES systems(id)
);

-- ─── Context ────────────────────────────────────────────────────────────────
-- Session-scoped working memory. Cleaned up after session ends.
-- Changes from old schema:
--   - Added iteration_num for cheap recency filtering
--   - Dropped embedding on hot context (expensive, low-value for ephemeral data)
CREATE TABLE IF NOT EXISTS context (
    id              VARCHAR(64)     PRIMARY KEY,
    session_id      VARCHAR(64)     NOT NULL,
    system_id       VARCHAR(64)     NOT NULL,
    type            ENUM('metric', 'log', 'command_output', 'benchmark', 'system_check') NOT NULL,
    source          VARCHAR(256),
    content         TEXT,
    summary         TEXT,
    iteration_num   INT,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (system_id)  REFERENCES systems(id)
);

-- ─── Hypothesis Queue ───────────────────────────────────────────────────────
-- Per-session work queue. Changes from old schema:
--   - source: where did this hypothesis come from?
--   - knowledge_ref: if sourced from knowledge table, link it
CREATE TABLE IF NOT EXISTS hypothesis_queue (
    id              VARCHAR(64)     PRIMARY KEY,
    session_id      VARCHAR(64)     NOT NULL,
    name            VARCHAR(256)    NOT NULL,
    priority        INT             NOT NULL DEFAULT 2,
    source          ENUM('llm', 'knowledge_base', 'rule', 'adapter') DEFAULT 'llm',
    knowledge_ref   VARCHAR(64),                        -- FK to knowledge.id if applicable
    status          ENUM('pending', 'running', 'done', 'skipped') DEFAULT 'pending',
    outcome         TEXT,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (knowledge_ref) REFERENCES knowledge(id)
);

-- ─── Apply Failures ─────────────────────────────────────────────────────────
-- Tracks parameters that fail to apply. Persists across sessions.
-- Review periodically to fix param name mismatches, missing handlers, etc.
CREATE TABLE IF NOT EXISTS apply_failures (
    id              VARCHAR(64)     PRIMARY KEY,
    session_id      VARCHAR(64)     NOT NULL,
    iteration       INT             NOT NULL,
    category        VARCHAR(32)     NOT NULL,       -- webserver, kernel, etc.
    parameter       VARCHAR(256)    NOT NULL,
    attempted_value TEXT,
    failure_reason  VARCHAR(64)     NOT NULL,       -- verify_mismatch, apply_failed, recommend_rejected, blocked
    llm_param_name  VARCHAR(256),                   -- what the LLM called it
    config_param_name VARCHAR(256),                 -- what config.yaml calls it
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- ─── Indexes ────────────────────────────────────────────────────────────────

-- Systems
CREATE INDEX IF NOT EXISTS idx_systems_host       ON systems(host);

-- Sessions
CREATE INDEX IF NOT EXISTS idx_sessions_system    ON sessions(system_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_started   ON sessions(started_at);

-- Knowledge
CREATE INDEX IF NOT EXISTS idx_knowledge_session  ON knowledge(discovered_by);
CREATE INDEX IF NOT EXISTS idx_knowledge_system   ON knowledge(system_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_scope    ON knowledge(scope, service_type, status);
CREATE INDEX IF NOT EXISTS idx_knowledge_param    ON knowledge(parameter);

-- Validations
CREATE INDEX IF NOT EXISTS idx_validations_knowledge ON validations(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_validations_system    ON validations(system_id);

-- Benchmarks
CREATE INDEX IF NOT EXISTS idx_benchmarks_session ON benchmarks(session_id, iteration_num);
CREATE INDEX IF NOT EXISTS idx_benchmarks_system  ON benchmarks(system_id, phase);
CREATE INDEX IF NOT EXISTS idx_benchmarks_payload ON benchmarks(system_id, payload_size, created_at);

-- Context
CREATE INDEX IF NOT EXISTS idx_context_session    ON context(session_id, iteration_num);
CREATE INDEX IF NOT EXISTS idx_context_type       ON context(session_id, type);

-- Hypothesis queue
CREATE INDEX IF NOT EXISTS idx_queue_session      ON hypothesis_queue(session_id, status, priority);
