# TiDB Concerns — Addressed

This document addresses the architectural concerns raised about using TiDB as the sole persistence layer for SlayMetricsAgent's three-tier memory (Profile, Facts, Context). Each concern is acknowledged and mapped to the mitigation implemented in the production-grade schema.

---

## Tier 1 — Profile (rarely changes)

### Concern: Overengineered for static data

> You're using a distributed SQL database as a config store — that's overengineered for data that barely changes.

**Acknowledged.** The old schema had a `profile` table that was essentially a config mirror — one row per session, rarely updated.

**Mitigation:** The new schema splits this into `systems` (persistent, one row per host+service) and `sessions` (one row per agent run). The system row is read once at session start and cached in `_system_id_cache` in `TiDBStore` — no repeated cold reads. TiDB's overhead is amortized because `systems` also serves as the cross-session identity layer, which YAML can't do.

### Concern: Cold read latency on reconnect

> Cold read latency on reconnect for a 24/7 agent adds up if your agent reloads the profile frequently.

**Mitigation:** System data is cached in-memory after first read. The `_system_id_cache` dict in `TiDBStore` maps `session_id → system_id`, eliminating repeated lookups. For a 24/7 agent, the connection stays warm; for bounded runs (hackathon), it's a single read.

### Concern: No change history/audit

> No change history/audit out of the box — you'd need to build versioning yourself if the profile does change.

**Mitigation:** The `sessions` table IS the history. Every agent run against a system produces a timestamped session with `rps_start`, `rps_end`, `rps_delta_pct`, `total_tokens`, and `fixes_applied`. You can trace how a system evolved over time:

```sql
SELECT id, rps_start, rps_end, rps_delta_pct, started_at
FROM sessions
WHERE system_id = ?
ORDER BY started_at ASC;
```

---

## Tier 2 — Facts (written on successful fixes)

### Concern: LLM-distilled knowledge doesn't map to relational rows

> Facts are LLM-distilled knowledge — "worker_processes 8 works better above 400 req/s" — this doesn't map cleanly to relational rows or even vector embeddings.

**Mitigation:** The `knowledge` table is designed specifically for this. Each entry has structured fields (`parameter`, `before_value`, `after_value`, `impact_pct`) for the quantitative data, plus `reasoning` (TEXT) and `condition` (TEXT) for the qualitative/conditional knowledge. The `condition` field captures applicability predicates like "cpu_cores >= 16 AND rhel >= 9" — so the knowledge is both queryable by SQL and searchable by vector similarity.

### Concern: No merge/conflict logic

> No built-in merge/conflict logic — if a new fact contradicts an old fact, you handle that yourself.

**Mitigation:** We don't merge — we validate through independent confirmation. Two contradicting facts both exist, but the confidence scoring resolves which one the agent trusts:

- Every `save_validation()` call adjusts confidence: `confirmed` → +0.1, `contradicted` → -0.15, `partial` → +0.03
- The asymmetry is deliberate — trust is harder to build than to lose
- Over time, unreliable facts sink to near-zero confidence and stop appearing in prioritized queries
- The `superseded_by` column and `supersede_knowledge()` method handle explicit fact evolution

```python
# When agent discovers worker_processes=8 beats worker_processes=4:
store.supersede_knowledge(old_id=wp4_id, new_id=wp8_id)
# Old entry: status='superseded', excluded from future queries
```

### Concern: Vector search requires managing embeddings and thresholds

> Vector search retrieval here requires embedding every fact and managing similarity thresholds — that's ByteRover's entire value prop, you'd be rebuilding it.

**Acknowledged.** We ARE building a simpler version of this, intentionally. The trade-off: we own the logic (50 lines of Python + SQL), have no vendor dependency, and it runs entirely self-hosted on the customer's box — which aligns with the RHEL/Apache 2.0 deployment story.

The `semantic_search()` method in `TiDBStore` uses `VEC_COSINE_DISTANCE()` over the `knowledge` table's `embedding VECTOR(1536)` column. The results are ranked by cosine distance and filtered to `status = 'active'` entries only.

What we don't have (and ByteRover does): a dedicated LLM curation pass that decides what's worth keeping. This is a post-hackathon enhancement — the schema supports it, and it would be an optional `CurateAgent` node that runs asynchronously after validation.

### Concern: No deduplication strategy

> Write on validated fix is fine, but what's your deduplication strategy? TiDB won't know "this fact supersedes that one."

**Mitigation:** Three mechanisms:

1. **Supersession:** `superseded_by VARCHAR(64)` column + `supersede_knowledge()` method. When a better fix for the same parameter is found, the old entry is explicitly marked superseded.

2. **Confidence decay:** Contradicted facts lose 0.15 confidence per contradiction. After a few contradictions, they effectively disappear from prioritized queries.

3. **Scope promotion:** `run_knowledge_promotion()` identifies system-scoped knowledge validated on 3+ different systems and promotes it to `service_type` scope. This consolidation naturally surfaces the most reliable version of duplicate knowledge.

---

## Tier 3 — Context (everything else)

### Concern: TiDB is not designed for agent working memory

> TiDB is not designed for agent working memory — it's a persistence layer, not a scratchpad.

**Acknowledged.** This is why the new schema fundamentally changes how context is stored:

- **Removed embeddings from context.** The old schema had `VECTOR(1536)` on every context row — expensive re-embedding on rapidly changing data. The new context table is a plain append log.
- **Added `iteration_num INT`** for cheap recency filtering. `WHERE iteration_num >= current - 3` replaces vector search for working memory — faster, zero embedding cost, predictable.
- **Added `cleanup_context()`** method that keeps the last N rows per session and drops the rest.

### Concern: Constant re-embedding is expensive and slow

> Vector search on rapidly changing context data means constant re-embedding — expensive and slow.

**Mitigation:** Embeddings are now ONLY on the `knowledge` table (learned facts that change slowly). Context has no embeddings at all. This is the right split: semantic search makes sense on accumulated knowledge ("find me past fixes for high iowait"), but not on ephemeral command outputs.

### Concern: No TTL/expiry — stale context accumulates

> No TTL/expiry built in — stale context accumulates, you'll need a cleanup job.

**Mitigation:** `cleanup_context(session_id, keep_last_n=50)` is the agent-driven equivalent of TTL:

```python
def cleanup_context(self, session_id: str, keep_last_n: int = 50) -> int:
    """Remove old context entries, keeping the most recent N per session."""
    # Deletes everything except the newest keep_last_n rows
```

This runs at the orchestrator's discretion — after each iteration, at session end, or on a schedule. It's more intelligent than infrastructure TTL because the agent can decide WHEN to clean up based on session state.

### Concern: LangGraph checkpointer doesn't sync to TiDB

> LangGraph's state management doesn't natively sync to TiDB — you'd need custom checkpointing logic.

**Not applicable.** We use PydanticAI, not LangGraph. Each agent is born fresh, reads what it needs from TiDB, does its work, writes results back, and exits. No framework state to checkpoint. The "stateless agent, stateful store" pattern eliminates this concern entirely.

---

## The Core Issue — "Zero opinions on what to keep"

### Concern

> TiDB gives you storage and retrieval but zero opinions on:
> - What's worth keeping vs discarding
> - When a fact is stale or superseded
> - How to merge conflicting context
> - What the agent "knows" vs "has seen"

### How the new schema addresses each

| Concern | Solution | Implementation |
|---------|----------|----------------|
| What's worth keeping | Confidence scoring | Facts below threshold stop appearing in queries |
| When a fact is stale | `superseded_by` + validation timestamps | `last_validated` tracks recency; `status='deprecated'` on contradiction |
| Merging conflicting context | Validation, not merging | Both facts exist; confidence determines which the agent trusts |
| "Knows" vs "has seen" | `knowledge` table vs `context` table | Clean separation by design — knowledge persists, context is ephemeral |

---

## What's still honestly missing

Two capabilities that would complete the picture for a true 24/7 production system:

1. **LLM-driven curation agent.** A periodic `CurateAgent` that reviews the knowledge table and consolidates related entries. If the agent discovers 5 slightly different facts about `worker_connections` tuning, they're currently 5 separate rows. A curation pass could distill them into one high-quality entry. The schema supports this — it's an implementation task, not an architectural change.

2. **Automated drift detection.** Querying `get_performance_trend()` on a schedule and triggering an alert session when RPS drops below a threshold. The schema supports it (the `scheduled` phase enum exists in `benchmarks`), but the triggering logic isn't wired up yet.

Both are post-hackathon features that build on the existing schema without requiring structural changes.

---

## Summary: Why TiDB (still) works

The concerns are real for a naive "dump everything in one database" approach. The mitigation is not "TiDB handles it" — it's "the store layer handles it, backed by TiDB." The opinions about what to keep, when facts are stale, and how to handle conflicts live in `TiDBStore`'s methods, not in the database engine.

The result: one `tiup playground` command to start the entire persistence layer. One connection string. One thing to back up, monitor, and explain to customers. And a schema that's visibly designed for multi-system, long-running operation — even when the hackathon only exercises the single-system path.
