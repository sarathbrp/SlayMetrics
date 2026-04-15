# SlayMetrics — Autonomous SRE Performance Agent

An autonomous LLM-driven agent that diagnoses and remediates NGINX performance bottlenecks on RHEL 9.x systems. It investigates like a senior SRE — forming hypotheses, validating via SSH, and applying fixes in dependency-aware groups with benchmark-gated acceptance.

---

## Architecture

```
                         ┌──────────────────────┐
                         │   bootstrap_audit.sh  │
                         │   (5s, system ID)     │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │   preflight_check     │
                         │   nginx up? HTTP 200? │
                         │   fix fs.nr_open trap │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │   run_benchmark       │
                         │   5 workloads (~5min)  │
                         │   + live sampler       │
                         └──────────┬───────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │         SRE Investigation Agent            │
              │                                           │
              │  PLANNING (1 LLM call, no SSH)            │
              │  → Analyze bootstrap + benchmark          │
              │    + live sampler findings                 │
              │  → Produce ranked hypothesis table        │
              │                                           │
              │  EXECUTION (1 LLM call per hypothesis)    │
              │  → [P1] worker_processes=1  ──► SSH       │
              │  → [P2] LimitNOFILE=512     ──► SSH       │
              │  → [P3] access_log on       ──► SSH       │
              │  → [PN] ...                 ──► SSH       │
              │                                           │
              │  FORCED SUMMARY (if needed)               │
              │  → Structured report for fix generator    │
              └─────────────────┬─────────────────────────┘
                                │
              ┌─────────────────▼─────────────────────┐
              │       Fix Generator (1 LLM call)       │
              │  Input: investigation report + rules   │
              │  Output: fix_groups (not flat list)    │
              │  ┌──────────────────────────────────┐  │
              │  │ Grp1: systemd sabotage (6 fixes) │  │
              │  │ Grp2: CPU capacity (2 fixes)     │  │
              │  │ Grp3: fd chain (4 fixes)         │  │
              │  │ Grp4: backlog chain (3 fixes)    │  │
              │  │ Grp5: I/O path (2 fixes)         │  │
              │  └──────────────────────────────────┘  │
              └─────────────────┬──────────────────────┘
                                │
              ┌─────────────────▼──────────────────────┐
              │          merge_fixes                     │
              │  validate tools, filter no-ops           │
              └─────────────────┬──────────────────────┘
                                │
     ┌──────────────────────────▼──────────────────────────┐
     │           Group-Based Remediation Loop               │
     │                                                      │
     │  ┌─────────────────────────────────────────────┐     │
     │  │ Grp1: Apply ALL fixes ──► benchmark ──►     │     │
     │  │       ACCEPT or REJECT (rollback entire grp)│     │
     │  ├─────────────────────────────────────────────┤     │
     │  │ Grp2 ... GrpN: same pattern                │     │
     │  └─────────────────────────────────────────────┘     │
     │                        │                             │
     │              Any rejected groups?                    │
     │                ┌───────┴───────┐                     │
     │              YES               NO                    │
     │                ▼                ▼                     │
     │  ┌──────────────────┐   ┌──────────┐                │
     │  │ RETRY PASS (1x)  │   │   DONE   │                │
     │  │ Re-test rejected │   └────┬─────┘                │
     │  │ on improved sys  │        │                       │
     │  └────────┬─────────┘        │                       │
     │           └──────────────────┘                       │
     └──────────────────────┬──────────────────────────────┘
                            │
              ┌─────────────▼──────────────────┐
              │     Final Benchmark (5 min)     │
              └─────────────┬──────────────────┘
                            │
              ┌─────────────▼──────────────────┐
              │     Comparisons                 │
              │     vs detuned baseline         │
              │     vs vanilla (healthy)        │
              └─────────────┬──────────────────┘
                            │
              ┌─────────────▼──────────────────┐
              │     Final Report (markdown)     │
              └────────────────────────────────┘
```

---

## Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Agent Framework** | [LangGraph](https://github.com/langchain-ai/langgraph) | State machine orchestration with conditional routing |
| **Prompt Optimization** | [DSPy](https://dspy-docs.vercel.app/) | Typed Signatures, BootstrapFewShot optimization after 30+ runs |
| **LLM Provider** | GPT-OSS (OpenAI-compatible) | Via `dspy.LM()` with LiteLLM backend |
| **Experiment Tracking** | [MLflow](https://mlflow.org/) | Run metrics, token usage, fix outcomes |
| **Semantic Memory** | [ChromaDB](https://www.trychroma.com/) | Local `all-MiniLM-L6-v2` embeddings for past case retrieval |
| **SSH Execution** | [Paramiko](https://www.paramiko.org/) | Remote command execution, file upload, 3-retry with backoff |
| **Benchmarking** | [wrk](https://github.com/wg/wrk) | HTTP load testing with Lua scripts per workload |
| **Live Metrics** | pandas + numpy | Runtime CSV analysis with delta/peak/trend detection |
| **Language** | Python 3.12+ | Type-annotated, PEP 8, max 300 lines per file |

---

## How DSPy Works Here

DSPy provides **typed LLM interfaces** via Signatures — each LLM call has defined InputFields and OutputFields with descriptions. This gives us:

1. **Structured I/O** — the LLM receives named fields (not a blob of text) and returns typed outputs
2. **Prompt optimization** — after collecting 30+ run examples, `BootstrapFewShot` compiles optimized few-shot prompts that improve fix accuracy over time
3. **Provider-agnostic** — swap LLM by changing one line in `.env` (`GPT_OSS_MODEL`)

```python
class Sig(dspy.Signature):
    investigation_notes: str = dspy.InputField(desc="SRE investigation findings")
    performance_rules: str = dspy.InputField(desc="Constraint chains and fix ordering rules")
    result_json: str = dspy.OutputField(desc='JSON: {"fix_groups": [...]}')

Sig.__doc__ = (prompts_dir / "fix_generator.md").read_text()
module = dspy.Predict(Sig)
result = module(investigation_notes=notes, performance_rules=rules)
```

**DSPy calls in the pipeline:**

| Call | Module | Input | Output |
|------|--------|-------|--------|
| Investigation planning | `dspy.Predict` | bootstrap + benchmark + live sampler | Hypothesis table |
| Investigation execution | `dspy.Predict` × N | Previous findings + planned hypothesis | Commands + findings |
| Fix generation | `dspy.Predict` | Investigation report + tool docs + rules | Fix groups |

---

## Observability

### MLflow Integration

Every run is tracked as an MLflow experiment with:

- **Run metadata**: session ID, DUT host, LLM model, timestamps
- **Metrics**: total tokens (input/output), fix count, acceptance rate
- **Per-fix tracking**: description, tool, accepted/rejected, improvement %
- **Artifacts**: final report, investigation iterations, prompt I/O

```bash
# Enable in config.yaml or .env
SLAY_MLFLOW_ENABLED=true
SLAY_MLFLOW_URI=http://localhost:5000
```

### Session Artifacts

Each run produces a full audit trail:

```
rca_reports/<session-uuid>/
  investigation_iter_0.json    # planning phase (hypothesis table)
  investigation_iter_1.json    # execution: hypothesis, evidence, plan, commands + outputs
  investigation_iter_2.json    # ...
  live_samples.csv             # 25+ runtime metric samples during benchmark
  prompt_fix_generator.json    # fix generator LLM I/O (what it received, what it produced)
  final_report.md              # comprehensive run summary
  final_benchmark.txt          # post-fix benchmark results
  rca_report.md                # RCA summary

logs/
  audit_rca_YYYYMMDD_HHMMSS.log  # full run log with hypothesis/evidence/plan per iteration
```

### DSPy Learning Pipeline

```
Run 1-5:   Collect examples → dspy_data/examples.jsonl
Run 6+:    BootstrapFewShot triggers → compiles optimized prompts
Run N+:    Optimized prompts improve fix accuracy over time
```

---

## Key Features

### Autonomous SRE Investigation
- **Plan-driven**: LLM analyzes 3 inputs (bootstrap + benchmark + live sampler), produces ranked hypothesis table
- **Hypothesis → evidence → plan → commands**: each iteration is logged with full reasoning
- **Adaptive**: number of iterations varies (3-10) based on how quickly hypotheses are confirmed
- **Performance rules spec**: 20 rules from Red Hat KB + nginx.org, injected into every LLM call

### Intelligent Fix Generation
- **Single LLM call** replaces 3 domain analyzers — sees full investigation report + all tool docs
- **Group-based output**: dependency chains grouped (fd chain, backlog chain, sabotage removal)
- **Every confirmed bottleneck produces a fix** — no silent drops

### Group-Based Remediation
- **Dependency chains applied together**: fs.nr_open → LimitNOFILE → worker_rlimit_nofile → worker_connections tested as ONE unit
- **Benchmark per group** (not per fix): prevents false rejections of chain dependencies
- **Retry pass**: rejected groups retried once on the improved system

### Preflight Health Check
- Detects crashed nginx (LimitNOFILE > fs.nr_open trap)
- Removes sabotage systemd drop-ins
- Verifies HTTP reachability before benchmarking

### zz_ Override Pattern
- Sabotage drop-in files (e.g., `hackathon_degrade.conf`) are never modified or deleted
- Agent creates `zz_hosttune_<prop>.conf` overrides that sort after sabotage files
- Values persist across nginx restarts; clean rollback by removing the override

---

## Tool Registry

| Tool | Domain | What it fixes |
|------|--------|--------------|
| `sysctl` | Kernel | somaxconn, tcp buffers, fs.nr_open, tcp_fastopen, default_qdisc, etc. |
| `systemd_property` | Systemd | LimitNOFILE, CPUQuota, MemoryMax, Nice, CPUWeight, IOWeight, TasksMax, OOMScoreAdjust |
| `nginx_directive` | Nginx | worker_processes, worker_connections, sendfile, access_log, etc. |
| `nginx_listen_backlog` | Nginx | TCP listen backlog on listen directives |
| `cpu_governor` | Hardware | CPU frequency scaling governor |
| `irqbalance` | Hardware | IRQ distribution across cores |
| `readahead` | I/O | Block device readahead sectors |
| `io_scheduler` | I/O | Block device I/O scheduler |
| `ethtool` | NIC | Ring buffers (rx/tx) and adaptive interrupt coalescing |
| `tc_shaping` | Network | Removes TC bandwidth throttle (auto-accepted) |
| `iptables_connlimit` | Network | Removes iptables connection limits (auto-accepted) |
| `nftables_ratelimit` | Network | Removes nftables rate limits (auto-accepted) |

---

## Performance Rules Spec

A shared "constitution" injected into every LLM call (`prompts/performance_rules.md`):

- **Constraint chains**: `fs.nr_open >= LimitNOFILE >= worker_rlimit_nofile >= worker_connections`
- **Fix ordering**: remove cgroup throttles → unlock workers → fix fd chain → optimize I/O
- **Hard rules**: never lower limits (raise ceilings), never set tcp_tw_reuse=1
- **RHEL-specific**: systemd drop-in behavior, PAM limits don't affect services, per-process vs cgroup limits
- **nginx-specific**: worker_rlimit_nofile >= 2x worker_connections, directive inheritance, reuseport

Based on Red Hat verified KB articles + nginx.org anti-patterns guide.

---

## Setup

```bash
# 1. Install
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env   # Edit with LLM + DUT credentials

# 3. Run
python agent.py                    # Full remediation
python agent.py --audit            # Recommendations only (no fixes applied)
python agent.py --fleet            # Multi-target mode
python agent.py --fleet --audit    # Multi-target audit only
python agent.py --list-targets     # Show configured targets
```

---

## Results

Tested against an 8-layer degradation on a 112-core RHEL 9.7 system:

| Metric | Before | After |
|--------|--------|-------|
| Homepage RPS | 2,840 | **935,140** (+32,822%) |
| Small file RPS | 61 | **964,989** (+1,583,927%) |
| Medium file RPS | 345 | 1,408 (+308%) |
| Large file RPS | 55 | 187 (+240%) |
| Fixes applied | — | 12 accepted / 3 rejected |
| Total tokens | — | 48,140 |
| LLM calls | — | 2 (investigation + fix generator) |
| Runtime | — | ~25 minutes |

Homepage and small file RPS **exceeded the healthy vanilla baseline by 2.5x**.
