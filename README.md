# SlayMetrics — Autonomous RCA & Remediation Agent

A LangGraph + DSPy agent that autonomously diagnoses and fixes NGINX performance bottlenecks on a remote DUT (Device Under Test). It runs an audit, benchmarks with live runtime sampling, generates domain-focused LLM Root Cause Analyses with chained context, applies fixes in a benchmark-gated loop, and learns from each run.

---

## Architecture

```
run_audit → run_benchmark ──── live sampler (background SSH thread) ────┐
                 ↓                                                       ↓
          analyze_network ──(network_summary)──→ analyze_kernel ──(kernel_summary)──→ analyze_nginx
               ↓                                      ↓                                   ↓
          (net fixes)                          (sysctl fixes)                      (nginx fixes)
               └──────────────────────────────────────┴───────────────────────────────────┘
                                                       ↓
                                                 merge_fixes
                                                       ↓
                                               remediate_fix ◄────────────────────┐
                                                       ↓ more fixes?              │
                                                       └───────────────────────────┘
                                                       ↓ done
                                                      END
```

**Three focused LLM calls per run — with chained context:**
1. `analyze_network` — live metrics + Group 5 audit → network fixes + `network_summary`
2. `analyze_kernel` — Groups 1-3 audit + `network_summary` → kernel fixes + `kernel_summary`
3. `analyze_nginx` — Group 4 audit + both summaries → nginx fixes

Each node receives a compact summary of what previous nodes found and fixed — no domain overlap, no repeated recommendations.

All fix execution is plain Python + SSH — no LLM in the remediation loop.

---

## Features

- **5-group static audit** via `omega_master_audit.sh` — hardware, kernel, systemd, nginx, network chaos
- **Live runtime sampler** — background SSH thread collects 25 samples during benchmark (TCP state, NIC discards, softirq, cgroup throttle, CPU); analyzed into compact hypothesis via pandas; injected into `analyze_network`
- **3 focused domain prompts** — `network_analysis.md`, `kernel_analysis.md`, `nginx_analysis.md` (replaces monolithic `rca.md`)
- **Context chaining** — each LLM node receives summaries from all previous nodes (no re-suggesting already-fixed issues)
- **Scoped remediation tools** — LLM can only call pre-defined tools (no arbitrary shell)
- **Benchmark-gated loop** — each fix benchmarked; kept only if priority workloads (`homepage`, `small`) improve; low-RPS workloads excluded from noise-prone percentage checks
- **Network chaos tools** — auto-detected and auto-accepted without benchmarking (TC shaping, iptables connlimit, nftables rate limits)
- **Cooling period** — configurable pause after each benchmark to allow DUT to drain connections before SSH
- **SSH retry** — 3-attempt retry with backoff on connection timeout
- **Rollback** — every tool stores original state; restored on rejection or Ctrl+C
- **Semantic memory** — ChromaDB (local `all-MiniLM-L6-v2`) stores past outcomes; injected into all 3 LLM calls
- **DSPy optimization** — BootstrapFewShot compiles better prompts after 30+ examples
- **Session IDs** — every run gets a UUID; all artifacts linked by session

---

## Tool Registry

| Tool | Domain | Scope | What it fixes |
|------|--------|-------|--------------|
| `tc_shaping` | Network | configurable | Removes HTB qdisc bandwidth throttle — **auto-accepted** |
| `iptables_connlimit` | Network | configurable | Removes iptables connlimit DROP rules — **auto-accepted** |
| `nftables_ratelimit` | Network | configurable | Flushes nftables rate-limit rules — **auto-accepted** |
| `sysctl` | Kernel | always | Kernel network params (somaxconn, tcp buffers, conntrack, etc.) |
| `systemd_property` | Kernel | always | nginx.service cgroup limits (LimitNOFILE, CPUQuota) |
| `cpu_governor` | Kernel | always | CPU frequency scaling governor |
| `nginx_directive` | Nginx | always | nginx config directives (worker_connections, access_log, etc.) |
| `nginx_listen_backlog` | Nginx | always | TCP listen backlog on nginx listen lines |

Network tools support `none | read | write` scope in `config.yaml`.

---

## Evaluation Logic

- **Priority workloads** (`homepage`, `small`): average improvement must be ≥ threshold (`-0.2%` default — reject only if actively hurting)
- **Other workloads** (`medium`, `large`, `mixed`): degradation must not exceed tolerance (`-5.0%` default)
- **Low-RPS workloads** (< 10 RPS): excluded from degradation check — percentage swings are meaningless noise at tiny baselines
- **Network tools**: auto-accepted — removing a 25× bandwidth throttle is always correct
- All thresholds configurable in `config.yaml`

---

## Live Runtime Sampling

While the benchmark runs, a background SSH thread collects metrics every 2 seconds:

| Metric | Signal |
|--------|--------|
| `/proc/net/softnet_stat` | Softirq budget exhaustion, packet drops |
| `ethtool -S <iface>` | NIC rx_discards, rx_errors at ring level |
| `/proc/net/sockstat` | TCP TIME_WAIT, ESTABLISHED (label-based extraction) |
| `vmstat` | CPU us/sy/wa, context switches |
| `cgroup cpu.stat` | CPUQuota throttle ratio (v1 and v2 supported) |

Samples saved to `rca_reports/<session-id>/live_samples.csv`. Pandas analysis computes deltas, peaks, and trend slopes. A compact severity-tagged hypothesis is printed to console and passed to `analyze_network`.

---

## Configuration

```yaml
target:
  connect_timeout_seconds: 30  # SSH timeout per attempt (3 retries with 5s backoff)

orchestration:
  max_parallel_audits: 10      # --fleet: static audits fan out in parallel
  target_password: ""          # optional installer->target SSH password
  installer:
    user: root                 # Mac -> installer SSH user
    private_key_path: /path/to/key
    port: 22
    auto_install_wrk: true     # auto-install wrk on installer if missing

targets:                       # optional inventory for --fleet mode
  - name: node01
    host: 172.21.90.178
  - name: node02
    host: 172.21.90.179
    # group is optional; if omitted, inferred from name prefix before the last '-'
    # group: automationcontroller

remediation:
  improvement_threshold_pct: -0.2   # reject only if priority workloads degrade > 0.2%
  degradation_tolerance_pct: -5.0   # non-priority workloads noise floor
  max_fixes: 15
  network_tools:
    tc_shaping: write               # none | read | write
    iptables_connlimit: write
    nftables_ratelimit: write

benchmark:
  cooling_period_seconds: 30        # pause after benchmark for DUT to drain connections
  collect_live_audit: true
  live_sampling:
    enabled: true
    interval_seconds: 2
    max_samples: 25
  final_benchmark_duration_minutes: 5

memory:
  inject_into_rca_analysis: true    # pass similar cases to analyze_network
  inject_into_fix_extraction: true  # pass similar cases to analyze_kernel + analyze_nginx

optimization:
  min_new_examples: 30              # trigger DSPy BootstrapFewShot after N examples
  max_bootstrap_demos: 3
```

---

## Setup

**Requires Python 3.12+**

```bash
# 1. Install dependencies
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment variables
cp .env.example .env
# Edit .env with your values:
#   GPT_OSS_BASE_URL   — LLM endpoint (required)
#   GPT_OSS_API_KEY    — LLM API key (required)
#   GPT_OSS_MODEL      — LLM model name (required)
#   GPT_OSS_EMBED_MODEL — embedding model (optional, defaults to GPT_OSS_MODEL)
#   SLAY_DUT_HOST      — target host IP (overrides config.yaml)
#   SLAY_DUT_USER      — SSH user (overrides config.yaml)
#   SLAY_DUT_KEY       — SSH private key path (overrides config.yaml)
#   SLAY_DUT_PORT      — SSH port (overrides config.yaml)
#   SLAY_DUT_TIMEOUT   — SSH timeout (overrides config.yaml)
#   SLAY_MLFLOW_ENABLED — enable/disable MLflow (overrides config.yaml)
#   SLAY_MLFLOW_URI    — MLflow tracking URI (overrides config.yaml)
#   SLAY_MLFLOW_EXPERIMENT — MLflow experiment name (overrides config.yaml)

# 3. Configure config.yaml (defaults work if .env is set)
#   target.*       — DUT connection (overridden by SLAY_DUT_* env vars)
#   benchmark.*    — benchmark scripts, workloads, cooling period
#   remediation.*  — fix thresholds, max fixes, network tool scopes
#   mlflow.*       — experiment tracking (overridden by SLAY_MLFLOW_* env vars)
#   memory.*       — semantic memory injection toggles
#   optimization.* — DSPy BootstrapFewShot trigger thresholds

# 4. Run
python agent.py

# Audit-only mode (no fix application; RCA + recommended fixes only)
python agent.py --audit

# Fleet mode: audits in parallel, benchmark/RCA sequential per target
python agent.py --fleet

# Fleet + audit-only directives (no fix apply on any target)
python agent.py --fleet --audit

# Fleet via installer (required flags for installer-orchestrated execution)
python agent.py --fleet --audit --orchestrate --installer <installer-ip>

# Force password auth from installer -> selected targets
python agent.py --fleet --audit --orchestrate --installer <installer-ip> --target-password '<password>'

# Choose only one group from CLI
python agent.py --fleet --audit --orchestrate --installer <installer-ip> --target-group automationcontroller

# Choose specific nodes from CLI (name or IP)
python agent.py --fleet --audit --orchestrate --installer <installer-ip> --target automationhub-0,10.1.91.191

# List available groups/nodes from config
python agent.py --list-targets

# Remediation via installer requires explicit confirmation flag
python agent.py --fleet --orchestrate --installer <installer-ip> --confirm-remediation
```

---

## Session Output

Each run produces a session folder:
```
rca_reports/<session-uuid>/
  final_report.md        # comprehensive run summary (baseline, fixes, final benchmark, runtime)
  rca_report.md          # combined summaries from all 3 LLM calls
  live_samples.csv       # raw runtime samples (25+ rows per benchmark)
  prompt_network.json    # full inputs + response for network LLM call
  prompt_kernel.json     # full inputs + response for kernel LLM call
  prompt_nginx.json      # full inputs + response for nginx LLM call
  final_benchmark.txt    # extended benchmark (if fixes were accepted)

dspy_data/
  examples.jsonl         # training examples with remediation outcomes
  rca_program/           # compiled DSPy program (after 30+ examples)
  chroma/                # ChromaDB semantic memory store
```

---

## Scripts

```bash
# View / clean semantic memory
python scripts/clean_chromadb.py
python scripts/clean_chromadb.py --reset
python scripts/clean_chromadb.py --before 2026-04-09
```
