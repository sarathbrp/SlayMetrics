# Workflow Gaps And Implementation Plan

## Target Workflow

1. Orchestration and Data Collection
2. Analyze and Diagnose
3. Autonomous Remediation

The desired operating model is:

- Establish secure connections to the DUT
- Aggregate benchmark results from the secondary system
- Use the secondary system for additional tooling or model hosting if needed
- Perform evidence-backed RCA
- Produce transparent, human-readable recommendations
- Automatically apply relevant tunings to the DUT
- Optimize specifically for Nginx performance

## Current State Summary

The current codebase already supports:

- Separate DUT and bench connections in [main.py](./main.py)
- Baseline and final benchmark collection from the bench system in [core/orchestrator.py](./core/orchestrator.py)
- Hackathon benchmark parity in [core/orchestrator.py](./core/orchestrator.py) and [adapters/nginx.py](./adapters/nginx.py)
- Nginx and system inspection in [agents/agent.py](./agents/agent.py)
- Batch application of Nginx and system tunings in [agents/agent.py](./agents/agent.py)
- Deterministic final result construction in Python in [agents/agent.py](./agents/agent.py)
- Reporting and persistence in [core/reporter.py](./core/reporter.py) and [memory/tidb_store.py](./memory/tidb_store.py)

## Gaps Found

### 1. Telemetry Collection Is Too Thin

What exists:

- Static system checks
- Config inspection
- Benchmark results
- Limited CPU and memory parsing after benchmarks

What is missing:

- Runtime socket telemetry
- Per-core CPU behavior during benchmark
- IRQ and NIC counter telemetry tied to benchmark windows
- Disk and memory pressure snapshots tied to benchmark windows
- Nginx runtime state collection during diagnosis and remediation

Impact:

- Diagnosis is driven mostly by known-good config targets, not live runtime evidence.

## 2. RCA Is Not Explicit

What exists:

- Detection of settings that differ from proven targets
- A short model-generated summary

What is missing:

- A structured root cause record
- Clear evidence chain from symptom to cause
- Confidence scoring
- Conflict detection across hardware, kernel, and service layers

Impact:

- The workflow behaves like configuration drift correction, not true RCA.

## 3. Explainability Is Too Weak

What exists:

- Saved findings
- Markdown and JSON reports
- Basic reasoning text

What is missing:

- Human-readable recommendation objects
- Evidence-backed explanation per action
- Expected effect and validation per action
- Separation between observed symptoms, chosen fix, and measured outcome

Impact:

- Output is understandable, but not yet transparent enough for operator trust or auditability.

## 4. Remediation Is One-Shot, Not Self-Healing

What exists:

- One diagnosis pass
- One remediation pass
- One post-fix benchmark

What is missing:

- Iterative diagnose -> apply -> verify -> continue/stop loop
- Decision logic for partial success
- Follow-up remediation passes based on measured results

Impact:

- The system can improve performance, but it cannot yet adapt across multiple rounds.

## 5. No Performance-Based Rollback

What exists:

- Nginx syntax rollback on invalid config

What is missing:

- Rollback when measured performance regresses
- Rollback when latency regresses beyond thresholds
- Rollback of system tuning batches with tracked restore points

Impact:

- The system is not yet safely self-healing under adverse tuning outcomes.

## 6. No Workload-Aware Tuning Strategy

What exists:

- Static proven tuning list
- Benchmark outputs by workload

What is missing:

- Workload classification logic
- Priority adjustments based on homepage vs small vs medium vs large vs mixed behavior
- Different tuning strategies for request-rate bottlenecks vs throughput bottlenecks

Impact:

- Nginx optimization is still generic rather than workload-adaptive.

## 7. Secondary System Usage Is Limited

What exists:

- Bench system execution for benchmark runs

What is missing:

- Explicit orchestration for additional tooling on the bench system
- Explicit support for remote model hosting or remote inference routing
- Bench-side telemetry or benchmark-side diagnostics

Impact:

- The second system is used, but not yet treated as a full orchestration participant.

## 8. Persistence Model Does Not Capture RCA Well

What exists:

- Facts
- Context
- Profile

What is missing:

- RCA records
- Remediation batch records
- Rollback history
- Telemetry snapshots with timestamps and benchmark correlation

Impact:

- The database stores useful traces, but not enough structure for multi-step autonomous tuning.

## Proposed Implementation Plan

### Phase 1. Telemetry Foundation

Goal:

- Make diagnosis evidence-driven instead of config-only.

Tasks:

- Add a telemetry collector module for DUT runtime snapshots
- Collect socket, CPU, memory, disk, NIC, IRQ, and Nginx runtime metrics
- Attach telemetry collection to baseline and post-fix benchmark windows
- Save telemetry snapshots into memory context with structured summaries

Expected files:

- `telemetry/collector.py`
- `telemetry/parsers.py`
- updates to [core/orchestrator.py](./core/orchestrator.py)
- updates to [adapters/nginx.py](./adapters/nginx.py)

## Phase 2. Structured RCA

Goal:

- Produce explicit, explainable root-cause output.

Tasks:

- Introduce a canonical RCA record structure
- Distinguish symptom, evidence, root cause, confidence, recommendation, and expected effect
- Teach the diagnosis workflow to emit RCA records before remediation
- Persist RCA records in memory

Expected files:

- `agents/agent.py`
- `memory/tidb_store.py`
- `core/reporter.py`

## Phase 3. Iterative Self-Healing Loop

Goal:

- Turn one-shot remediation into autonomous remediation.

Tasks:

- Replace the single diagnosis/remediation pass with a bounded iteration loop
- Track remediation batches and outcomes
- Stop when improvement flattens, targets are met, or no safe actions remain
- Expose iteration summaries in the report

Expected files:

- `core/orchestrator.py`
- `agents/agent.py`
- `core/reporter.py`

## Phase 4. Performance-Based Rollback

Goal:

- Make autonomous remediation safe.

Tasks:

- Add benchmark-based regression detection
- Snapshot pre-batch Nginx config and selected sysctls
- Restore previous state when throughput or latency regresses
- Persist rollback actions and reasons

Expected files:

- `agents/agent.py`
- `adapters/nginx.py`
- `tools/ssh.py`
- `memory/tidb_store.py`

## Phase 5. Workload-Aware Optimization

Goal:

- Optimize specifically for the observed Nginx workload profile.

Tasks:

- Classify dominant bottleneck from baseline benchmark and telemetry
- Adjust tuning priorities based on workload shape
- Separate request-rate tuning from throughput tuning
- Reflect workload-specific reasoning in RCA and reports

Expected files:

- `agents/agent.py`
- `core/orchestrator.py`
- `core/reporter.py`

## Phase 6. Expand Bench-System Role

Goal:

- Treat the secondary system as a first-class orchestration node.

Tasks:

- Add explicit bench-side telemetry/tool execution support
- Make model routing aware of local vs remote hosting
- Support auxiliary tooling from the bench system if configured

Expected files:

- `main.py`
- `models/registry.py`
- `core/orchestrator.py`
- `tools/ssh.py`

## Recommended Implementation Order

1. Phase 1: Telemetry Foundation
2. Phase 2: Structured RCA
3. Phase 3: Iterative Self-Healing Loop
4. Phase 4: Performance-Based Rollback
5. Phase 5: Workload-Aware Optimization
6. Phase 6: Expand Bench-System Role

## Definition Of Done

The workflow should be considered aligned with the target model when it can:

- Gather benchmark-correlated telemetry from DUT and bench
- Produce explicit RCA records with evidence and confidence
- Apply Nginx-focused tunings autonomously
- Re-benchmark and decide whether to continue, stop, or roll back
- Explain each action in human-readable form
- Persist enough data to reconstruct what happened across iterations

## Immediate Next Step

Start with Phase 1 by adding benchmark-correlated telemetry collection and wiring it into the orchestrator before and after remediation.
