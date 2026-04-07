# SlayMetricsAgent — Requirement Matrix

Track implementation status of every requirement from REQUIREMENTS.md.

**Status legend:**
- `[ ]` Not started
- `[~]` In progress
- `[x]` Done
- `[-]` Skipped / out of scope

---

## 1. Project Scaffold

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 1.1 | `main.py` entry point — loads config, wires deps, starts orchestrator | `[x]` | session resume support; CLI overrides are optional |
| 1.2 | `config.yaml` — all config externalized, no hardcoding | `[x]` | all sections, profiles; planner_mode preserved unless CLI overrides |
| 1.3 | `requirements.txt` with pinned deps | `[x]` | |
| 1.4 | Database schema — embedded in sqlite_store.py | `[x]` | Auto-created on connect |
| 1.5 | `README.md` — usage instructions for customer | `[x]` | |

---

## 2. LLM Backend

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 2.1 | `active_profile` in config drives LLM for all agents | `[x]` | config.yaml + main.py |
| 2.2 | `granite-local` profile — vLLM + Granite 3.1 8B LAB v2.1 | `[x]` | default profile |
| 2.3 | `claude-remote` profile — Claude Opus 4.6 via Anthropic API | `[x]` | |
| 2.4 | `ollama-local` profile — any Ollama model | `[x]` | |
| 2.5 | `get_model()` factory reads profile, returns `AnthropicModel` or `OpenAIModel` | `[x]` | main.py |
| 2.6 | All agents share same model instance from active profile | `[x]` | model passed to all agent.run() calls |

---

## 3. Memory Layer (SQLite — Knowledge-Scoped)

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 3.1 | SQLite database (automatic, no setup) | `[x]` | Schema embedded in sqlite_store.py |
| 3.2 | `systems` table — persistent host+service identity | `[x]` | Replaces old `profile` |
| 3.3 | `sessions` table — per-run metadata, tokens, outcome | `[x]` | New; completion now finalized via `complete_session()` |
| 3.4 | `knowledge` table — scoped facts with confidence scoring | `[x]` | Replaces old `facts`; fix facts are deduped per logical system-level change |
| 3.5 | `validations` table — audit trail for knowledge | `[x]` | New; repeated fix confirmations accrue to reused knowledge rows, and optimization keep/revert outcomes are stored as validations without deprecating facts |
| 3.6 | `benchmarks` table — structured perf data (never expires) | `[x]` | New, replaces TEXT blobs |
| 3.7 | `context` table — session-scoped working memory with iteration_num | `[x]` | Updated, no embeddings |
| 3.8 | `hypothesis_queue` table — with source provenance + knowledge_ref | `[x]` | Updated |
| 3.9 | `SQLiteStore` class — read/write for all 7 tables | `[x]` | memory/sqlite_store.py |
| 3.10 | Vector search over knowledge (semantic symptom recall) | `[x]` | Cosine distance in Python |
| 3.11 | `embeddings.py` — text → vector (Claude embeddings or local) | `[x]` | Claude + LocalEmbeddings fallback |
| 3.12 | Agent survives restart — resumes from SQLite state | `[x]` | `populate_queue` skips if exists |
| 3.13 | Cross-system learning — knowledge for service type | `[x]` | `get_knowledge_for_service()` |
| 3.14 | Confidence scoring — grows with validations | `[x]` | Auto-updates on save_validation |
| 3.15 | Knowledge promotion pipeline — system → service_type | `[x]` | `run_knowledge_promotion()` |
| 3.16 | Backward-compatible API — old callers work unchanged | `[x]` | Facade methods in SQLiteStore |

---

## 4. SSH Layer

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 4.1 | `SSHClient` wrapper using Paramiko | `[x]` | tools/ssh.py |
| 4.2 | SSH key auth from config | `[x]` | |
| 4.3 | Command timeout support | `[x]` | |
| 4.4 | Stdout + stderr captured and returned | `[x]` | `SSHResult` dataclass |

---

## 5. PydanticAI Agents

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 5.1 | `AgentDeps` dataclass — adapter, memory, ssh, session_id, token_counter | `[x]` | agents/__init__.py |
| 5.2 | Orchestrator agent — main hypothesis loop, `AnalysisResult` output | `[x]` | core/orchestrator.py |
| 5.3 | Collector sub-agent — SSH + metrics, `CollectionResult` output | `[x]` | agents/collector.py |
| 5.4 | Analyzer sub-agent — interprets context, `AnalysisResult` output | `[x]` | agents/analyzer.py |
| 5.5 | Remediation sub-agent — applies fix, `RemediationResult` output | `[x]` | agents/remediation.py |
| 5.6 | Benchmark sub-agent — wrk2/pgbench, `BenchmarkResult` output | `[x]` | agents/benchmark.py |
| 5.7 | Each sub-agent born fresh, writes to SQLite, exits — no context accumulation | `[x]` | each agent is a fresh Agent() call |

---

## 6. Agent Tools (`@agent.tool`)

| # | Tool | Status | Notes |
|---|------|--------|-------|
| 6.1 | `run_command(command, reason)` — SSH execute + log to Context | `[x]` | collector, analyzer, remediation |
| 6.2 | `run_benchmark(duration)` — wrk2/pgbench → `BenchmarkResult` | `[x]` | benchmark, remediation agents |
| 6.3 | `apply_config_change(param, value, reason)` — write config + log decision | `[x]` | remediation agent |
| 6.4 | `reload_service(reason)` — systemctl reload | `[x]` | remediation agent |
| 6.5 | `query_memory(symptom)` — vector search facts + context | `[x]` | analyzer agent |
| 6.6 | `save_finding(finding, outcome)` — persist to Facts table | `[x]` | remediation agent |
| 6.7 | `get_hypothesis_queue()` — return pending hypotheses | `[x]` | decision_engine + orchestrator |
| 6.8 | `mark_hypothesis_done(name, outcome)` — update queue status | `[x]` | decision_engine |
| 6.9 | `escalate(reason, summary)` — human handoff, generate report | `[x]` | orchestrator escalation block |

---

## 7. Service Adapters

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 7.1 | `ServiceAdapter` ABC — `get_config`, `apply_config`, `benchmark`, `get_metrics`, `get_logs`, `reload`, `get_hypothesis_queue` | `[x]` | adapters/base.py |
| 7.2 | `NginxAdapter` — nginx.conf, wrk2, systemctl nginx | `[x]` | adapters/nginx.py |
| 7.3 | `PostgresAdapter` — postgresql.conf, pgbench | `[x]` | adapters/postgres.py |
| 7.4 | `RedisAdapter` — redis.conf, redis-benchmark | `[x]` | adapters/redis.py |
| 7.5 | Adapter auto-loaded from `service.name` in config | `[x]` | adapters/__init__.py |

---

## 8. RHEL System Checks

| # | Check | Status | Notes |
|---|-------|--------|-------|
| 8.1 | CPU frequency governor (`scaling_governor`) | `[x]` | rhel/system_checks.py |
| 8.2 | Transparent hugepages | `[x]` | |
| 8.3 | SELinux mode (`getenforce`) | `[x]` | |
| 8.4 | TCP backlog (`net.core.somaxconn`) | `[x]` | inspection bundle now feeds offline debate eval golden-range checks |
| 8.5 | IRQ affinity (`/proc/interrupts` + NIC) | `[x]` | inspection metadata now also captures CPU budget context (`os_cpu_count`, cgroup quota, cpuset) for offline Nginx worker evals |
| 8.6 | Filesystem mount options (`findmnt`) | `[x]` | noatime |
| 8.7 | NUMA topology (`numactl --hardware`) | `[x]` | |
| 8.8 | Open file limits (`ulimit -n`, `fs.file-max`) | `[x]` | |
| 8.9 | NIC offloading (`ethtool -k`) | `[x]` | |
| 8.10 | Kernel version (`uname -r`) | `[x]` | |

---

## 9. Nginx Hypothesis Queue

| # | Hypothesis | Priority | Status | Notes |
|---|-----------|----------|--------|-------|
| 9.1 | `sendfile` enabled | P1 | `[x]` | NginxAdapter.get_hypothesis_queue() |
| 9.2 | CPU governor set to `performance` | P1 | `[x]` | |
| 9.3 | `tcp_nopush` + `tcp_nodelay` on | P1 | `[x]` | |
| 9.4 | `worker_processes` matches CPU cores | P1 | `[x]` | |
| 9.5 | `open_file_cache` enabled | P2 | `[x]` | |
| 9.6 | Transparent hugepages disabled | P2 | `[x]` | |
| 9.7 | SELinux tuned or permissive | P2 | `[x]` | |
| 9.8 | `net.core.somaxconn` backlog increased | P2 | `[x]` | |
| 9.9 | IRQ affinity tuned | P3 | `[x]` | |
| 9.10 | NUMA binding configured | P3 | `[x]` | |
| 9.11 | Filesystem `noatime` mount option | P3 | `[x]` | |
| 9.12 | NIC offload enabled | P3 | `[x]` | |
| 9.13 | `gzip` compression tuned | P3 | `[x]` | |

---

## 10. Decision Engine

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 10.1 | Hypothesis queue pre-populated from adapter on first run | `[x]` | decision_engine.populate() |
| 10.2 | Queue ordered by priority (P1 → P2 → P3) | `[x]` | SQLite ORDER BY priority |
| 10.3 | Vector memory checked before testing each hypothesis | `[x]` | analyzer.query_memory() |
| 10.4 | One change at a time — benchmark between every change | `[x]` | remediation agent + ranked optimization mode enforce one group per iteration |
| 10.5 | Hypothesis marked done after test (pass or fail) | `[x]` | engine.mark_done/skipped |
| 10.6 | Escalate when queue exhausted | `[x]` | orchestrator escalation block |

---

## 11. Benchmarking

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 11.1 | Baseline benchmark before any changes | `[x]` | orchestrator step 2 |
| 11.2 | Small file benchmark (`1kb.html`) | `[x]` | config.yaml urls |
| 11.3 | Medium file benchmark (`100kb.html`) | `[x]` | |
| 11.4 | Large file benchmark (`1mb.html`) | `[x]` | |
| 11.5 | Benchmark after every applied fix | `[x]` | remediation agent |
| 11.6 | Final sustained benchmark (30 min wrk2 run) | `[x]` | orchestrator step 5.5, config-gated |
| 11.7 | Parse wrk2 output → `BenchmarkResult` (req/sec, p50, p99, errors) | `[x]` | adapters/nginx.py _parse_wrk2 |

---

## 12. Reporting

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 12.1 | `report/report.md` — human-readable | `[x]` | core/reporter.py; now distinguishes best-achieved vs final-state results and logs optimization decisions |
| 12.2 | `report/report.json` — machine-readable | `[x]` | includes best_results/final results, optimization outcomes, and final effective config snapshot |
| 12.3 | `report/timeline.json` — chronological decision log | `[-]` | covered by report.json |
| 12.4 | Executive summary — what was found, fixed, % improvement | `[x]` | |
| 12.5 | System profile section — RHEL version, hardware, service version | `[x]` | |
| 12.6 | Baseline benchmarks table (small/medium/large, before) | `[x]` | |
| 12.7 | Decision log — per action: data observed, reasoning, change made | `[x]` | |
| 12.8 | Applied fixes table — parameter, old value, new value, impact % | `[x]` | |
| 12.9 | Final benchmarks table (small/medium/large, after) | `[x]` | final state remains distinct from best-achieved iteration |
| 12.10 | Remaining hypotheses — what wasn't tried and why | `[x]` | |
| 12.11 | Token consumption — total input/output tokens for full session | `[x]` | |

---

## 13. Non-Functional Requirements

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 13.1 | No hardcoded values — all in `config.yaml` | `[x]` | |
| 13.2 | No black box — every decision logged with supporting data | `[x]` | reason field on all tools; offline debate eval harness (`core/eval_harness.py`) scores saved inspection + expert artifacts with deterministic findings/corrections, debate mode logs/persists observational eval results as `iterN_debate_eval`, synthesizer eval filters non-critical omissions while checking target drift/key normalization/risk calibration, the harness now tolerates legacy/raw Nginx+RHEL expert shapes (`issue/expected`, alias keys, cross-category lookups), and completed runs persist a final effective DUT config snapshot for dashboard truth |
| 13.3 | Context-stateless — SQLite is source of truth, not message history | `[x]` | fresh agent per iteration |
| 13.4 | Idempotent — safe to run multiple times, no double-applied fixes | `[x]` | populate_queue skips if exists |
| 13.5 | Single command to run: `python main.py` | `[x]` | |

---

## Summary

| Section | Total | Done | In Progress | Not Started |
|---------|-------|------|-------------|-------------|
| 1. Project Scaffold | 5 | 5 | 0 | 0 |
| 2. LLM Backend | 6 | 6 | 0 | 0 |
| 3. Memory Layer | 16 | 16 | 0 | 0 |
| 4. SSH Layer | 4 | 4 | 0 | 0 |
| 5. PydanticAI Agents | 7 | 7 | 0 | 0 |
| 6. Agent Tools | 9 | 9 | 0 | 0 |
| 7. Service Adapters | 5 | 5 | 0 | 0 |
| 8. RHEL System Checks | 10 | 10 | 0 | 0 |
| 9. Nginx Hypothesis Queue | 13 | 13 | 0 | 0 |
| 10. Decision Engine | 6 | 6 | 0 | 0 |
| 11. Benchmarking | 7 | 7 | 0 | 0 |
| 12. Reporting | 11 | 10 | 0 | 0 |
| 13. Non-Functional | 5 | 5 | 0 | 0 |
| **Total** | **104** | **103** | **0** | **0** |
