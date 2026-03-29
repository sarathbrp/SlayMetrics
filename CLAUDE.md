# SlayMetricsAgent — Claude Code Instructions

## Project Overview

This is a fully autonomous agentic solution that diagnoses and remediates performance issues
on RHEL systems. See `REQUIREMENTS.md` for full design and `REQUIREMENT-MATRIX.md` for
implementation status.

---

## Requirement Tracking (MANDATORY)

**After completing any implementation task, you MUST update `REQUIREMENT-MATRIX.md`:**

1. Find the matching requirement row(s) for what was just implemented
2. Change the status cell from `[ ]` or `[~]` to `[x]`
3. Add a brief note in the Notes column if relevant (e.g. filename, caveat)
4. Update the **Summary table** at the bottom — increment the Done count, decrement Not Started

**Status values:**
- `[ ]` Not started
- `[~]` In progress — set this when you begin a task
- `[x]` Done — set this immediately when implementation is complete
- `[-]` Skipped / out of scope

**When starting a task**, mark it `[~]` first so progress is visible.
**Never batch updates** — update the matrix as each item completes, not at the end.

---

## Project Structure

```
perfagent/
├── CLAUDE.md                      ← this file
├── REQUIREMENTS.md                ← full design + requirements
├── REQUIREMENT-MATRIX.md          ← implementation tracking (keep updated)
├── architecture.html              ← visual architecture diagram
├── config.yaml                    ← user edits this only (no hardcoding)
├── requirements.txt
├── schema.sql                     ← TiDB bootstrap (run once)
├── main.py                        ← entry point
│
├── core/
│   ├── orchestrator.py
│   ├── decision_engine.py
│   └── reporter.py
│
├── memory/
│   ├── tidb_store.py
│   └── embeddings.py
│
├── agents/
│   ├── collector.py
│   ├── analyzer.py
│   ├── remediation.py
│   └── benchmark.py
│
├── adapters/
│   ├── base.py
│   ├── nginx.py
│   ├── postgres.py
│   └── redis.py
│
├── rhel/
│   └── system_checks.py
│
└── tools/
    └── ssh.py
```

---

## Hackathon Evaluation (KEEP IN MIND AT ALL TIMES)

This solution will be judged by a customer running it against a **degraded RHEL 9.7 system
with Nginx** that we have never seen. The customer evaluates our solution, not us.

### What the Customer Does

1. Customer runs our solution against their degraded system
2. A volunteer reveals how our solution performed in the final demo
3. If something breaks, volunteer may reach out for a minor fix

### Objective Criteria (Data-Driven) — THIS IS HOW WE WIN

- **Impact:** Improvement in max req/sec for **small, medium, and large payloads**
- **Resource usage:** Reduction in CPU/memory for the workload
- **Performance curve stability:** Consistency over a sustained 30-minute test (low variance = higher score)
- **Token efficiency:** How effectively we use tokens compared to a base model (fewer = better)

### Subjective Criteria (Qualitative)

- **Technical complexity:** How well the AI handles an **unknown detuned system**
- **Innovation:** Fresh approach to performance analysis
- **Presentation:** Clarity of demo, logic flow, documentation

### Required Report Output (MANDATORY in every run)

The solution MUST produce a report containing:
1. Benchmarking metrics **before** starting remediation
2. Decision-making log — what data/reasoning drove each change
3. Benchmarking metrics **after** all remediation
4. Total tokens consumed in the complete session

### Submission

- GitLab repo shared with Jaison Raju
- README with instructions for the customer to run our solution
- Sample output report demonstrating the above

---

## Problem-Solving Workflow

When the user reports a problem (error, debug output, unexpected behavior):

1. **Explain the problem first** — what failed, root cause, which component
2. **Propose the fix** — what will change and why, get approval before coding
3. **Implement and verify** — fix the code, run tests, confirm it works
4. **Summarize all fixes** — after fixing, provide a clear table or list of every problem found and what was done about it

Never silently fix things. Always explain before and summarize after.

---

## Core Rules

- **No hardcoding** — all values come from `config.yaml`
- **No black box** — every agent decision must include a `reason` field
- **One change at a time** — always benchmark before and after each fix
- **TiDB is source of truth** — never rely on LLM message history for state
- **Update matrix** — mark requirements complete as you implement them
- **Customer runs this, not us** — solution must work on an unknown degraded system
- **All 3 payload sizes** — always benchmark small, medium, AND large files
- **Token efficiency matters** — minimize LLM calls; don't dump raw output into context

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Agent framework | PydanticAI |
| Default LLM | Granite 3.1 8B LAB v2.1 (local vLLM) |
| Fallback LLM | Claude Opus 4.6 |
| Memory DB | TiDB v8.4+ (self-hosted, Apache 2.0) |
| SSH | Paramiko |
| Benchmarking | wrk2 |
| Language | Python 3.11+ |
| Config | YAML |

---

## LLM Profile Switching

Edit one line in `config.yaml` to switch LLM for all agents:

```yaml
llm:
  active_profile: granite-local   # or: claude-remote, ollama-local
```

Start Granite locally before running:
```bash
podman run -p 8000:8000 registry.redhat.io/rhaiis/vllm-rhel9 \
  --model registry.redhat.io/rhelai1/granite-3.1-8b-lab-v2.1
```

---

## TiDB Setup

```bash
tiup playground v8.4.0
mysql -h 127.0.0.1 -P 4000 -u root < schema.sql
```

---

## Running

```bash
python main.py                        # uses config.yaml
python main.py --config custom.yaml  # override config
```
