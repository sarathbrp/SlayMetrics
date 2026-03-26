-- SlayMetricsAgent — TiDB Schema
-- Run once: mysql -h 127.0.0.1 -P 4000 -u root < schema.sql

CREATE DATABASE IF NOT EXISTS perfagent;
USE perfagent;

-- Profile: one row per agent session/service
CREATE TABLE IF NOT EXISTS profile (
    id              VARCHAR(64)     PRIMARY KEY,
    session_id      VARCHAR(64)     NOT NULL,
    service         VARCHAR(64)     NOT NULL,
    host            VARCHAR(256)    NOT NULL,
    rhel_version    VARCHAR(32),
    kernel_version  VARCHAR(64),
    cpu_cores       INT,
    ram_gb          INT,
    baseline_rps    FLOAT,
    best_rps        FLOAT,
    target_rps      FLOAT,
    llm_profile     VARCHAR(64),
    status          ENUM('running', 'completed', 'escalated') DEFAULT 'running',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Facts: confirmed findings and applied fixes
CREATE TABLE IF NOT EXISTS facts (
    id              VARCHAR(64)     PRIMARY KEY,
    session_id      VARCHAR(64)     NOT NULL,
    type            ENUM('finding', 'fix', 'negative', 'escalation', 'knowledge') NOT NULL,
    parameter       VARCHAR(256),
    before_value    TEXT,
    after_value     TEXT,
    before_rps      FLOAT,
    after_rps       FLOAT,
    impact_pct      FLOAT,
    reasoning       TEXT,
    status          ENUM('applied', 'reverted', 'pending') DEFAULT 'applied',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    embedding       VECTOR(1536)
);

-- Context: raw observations, command outputs, metrics
CREATE TABLE IF NOT EXISTS context (
    id              VARCHAR(64)     PRIMARY KEY,
    session_id      VARCHAR(64)     NOT NULL,
    type            ENUM('metric', 'log', 'command_output', 'benchmark', 'system_check') NOT NULL,
    source          VARCHAR(256),
    content         TEXT,
    summary         TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    embedding       VECTOR(1536)
);

-- Hypothesis queue: what to try next
CREATE TABLE IF NOT EXISTS hypothesis_queue (
    id              VARCHAR(64)     PRIMARY KEY,
    session_id      VARCHAR(64)     NOT NULL,
    name            VARCHAR(256)    NOT NULL,
    priority        INT             NOT NULL DEFAULT 2,
    status          ENUM('pending', 'running', 'done', 'skipped') DEFAULT 'pending',
    outcome         TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_facts_session     ON facts(session_id);
CREATE INDEX IF NOT EXISTS idx_context_session   ON context(session_id);
CREATE INDEX IF NOT EXISTS idx_queue_session     ON hypothesis_queue(session_id, status, priority);
CREATE INDEX IF NOT EXISTS idx_profile_session   ON profile(session_id);
