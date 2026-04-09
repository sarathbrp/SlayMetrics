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

```bash
pip install -r requirements.txt

# Create .env on the agent machine (never committed):
# GPT_OSS_BASE_URL=...
# GPT_OSS_API_KEY=...
# GPT_OSS_MODEL=...

# Edit config.yaml: target.host, target.user, target.private_key_path

python agent.py
```

---

## Session Output

Each run produces a session folder:
```
rca_reports/<session-uuid>/
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
