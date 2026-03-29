# SlayMetricsAgent — Agent Runtime Guide

## Project Overview

This project diagnoses and remediates performance issues on RHEL systems, with the hackathon target
being an unknown degraded RHEL 9.7 + Nginx environment. The customer evaluates the solution by
running it on their own system, so the code must be robust against real-world drift, partial
misconfiguration, and model variability.

Use `REQUIREMENTS.md` and `REQUIREMENT-MATRIX.md` as the primary product/design references.

---

## Requirement Tracking

After implementation work:

1. Find the matching row(s) in `REQUIREMENT-MATRIX.md`
2. Move status from `[ ]` or `[~]` to `[x]` when complete
3. Add a short note if the implementation has caveats or specific file references
4. Update the summary counts at the bottom

Status values:
- `[ ]` Not started
- `[~]` In progress
- `[x]` Done
- `[-]` Skipped / out of scope

---

## Current Runtime

The active runtime is:

| Component | Technology |
|-----------|-----------|
| Agent runtime | LangChain + LangGraph |
| Default local model | `granite4:7b-a1b-h` via Ollama |
| Alternate local model | `gpt-oss-120b` via Ollama |
| Remote fallback | Claude Opus via Anthropic |
| Memory DB | TiDB |
| SSH / local exec | Paramiko + subprocess |
| Benchmarking | Hackathon `benchmark.sh` by default |
| Language | Python 3.12 |
| Config | YAML |

Important runtime rule:
- The model is used for decisioning and tool selection.
- Final structured run state is built in Python, not authored by the model.

This is intentional. It reduces failures from model-specific JSON drift.

---

## LLM Profiles

Profiles in `config.yaml` are intentionally limited to three:

```yaml
llm:
  active_profile: granite-local   # or: gpt-oss-local, claude-remote
```

Backends:
- `granite-local` -> local Ollama Granite
- `gpt-oss-local` -> local Ollama GPT-OSS
- `claude-remote` -> Anthropic remote model

Do not add extra profile permutations unless there is a strong operational need.

---

## Architecture

High-level flow:

1. Direct system inspection
2. Baseline benchmark
3. Config and prior-fix collection
4. LangGraph diagnosis loop
5. Final benchmark
6. Deterministic report generation

Design principles:
- Keep adapters, memory, and reporting deterministic
- Keep benchmark paths consistent between baseline and post-fix validation
- Avoid asking the model to restate values the code already knows
- Treat model outputs as unreliable at schema edges; normalize before persistence

---

## Core Rules

- No hardcoded host-specific tuning outside `config.yaml` unless it is part of a proven default set
- No black box behavior: tool calls and reasoning context must be observable in logs
- Prefer deterministic Python logic over model synthesis for final state
- TiDB is the system of record for facts, context, and run history
- Keep tool interfaces simple and tolerant for local models
- Never silently turn command failures into zero-value metrics
- Benchmark baselines and validation must use the same measurement path when configured

---

## File Map

Key files:

- `main.py` — entry point and dependency wiring
- `models/registry.py` — model selection
- `agents/agent.py` — diagnosis workflow and LangGraph tool loop
- `core/orchestrator.py` — top-level run orchestration
- `adapters/` — service-specific config and benchmark logic
- `memory/tidb_store.py` — TiDB persistence
- `core/reporter.py` — deterministic report generation

Support files:

- `rhel/system_checks.py`
- `tools/ssh.py`
- `facts/`
- `schema.sql`

---

## Operational Notes

For local Ollama workflows:

```bash
ollama pull granite4:7b-a1b-h
ollama serve
python3 main.py -v
```

For TiDB:

```bash
tiup playground v8.4.0
mysql -h 127.0.0.1 -P 4000 -u root < schema.sql
```

For full local setup:

```bash
./setup.sh
```

---

## What To Optimize

Prioritize:
- Correctness on unknown degraded systems
- Stable tool calling with local models
- Accurate before/after benchmarking
- Clear logs and reproducible reports
- Minimal unnecessary model turns

Do not prioritize:
- Fancy agent decomposition
- Additional sub-agents unless there is a concrete need
- Overly strict output schemas that local models routinely violate

---

## Preferred Engineering Direction

When in doubt:
- move complexity from the model into Python
- keep model prompts short and operational
- keep tool payloads simple
- compute final state deterministically
- preserve hackathon benchmark parity
