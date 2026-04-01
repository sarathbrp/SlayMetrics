# SlayMetricsAgent

An autonomous AI agent that diagnoses and remediates performance issues on RHEL systems running Nginx. Point it at a degraded system, let it run — get a full diagnostic report with before-and-after proof.

**Score: 353/400 (88.3%)** across 4 rounds of adversarial testing with multi-layer system degradation.

## What It Does

1. **Pre-flight validation** — verifies DUT serves all workloads correctly (catches SELinux labels, file permissions, firewall blocks)
2. **System fingerprint** — collects CPU, RAM, NIC speed, disk throughput
3. **Baseline benchmarks** — all 5 workloads (homepage, small, medium, large, mixed)
4. **5-category inspection** — webserver, kernel, resource limits, network, storage
5. **Planner pipeline** — deterministic, hybrid, or debate planning from the same inspection data
6. **Batch apply** — grouped by category, one SSH call per category
7. **Post-apply verification** — re-reads DUT state to confirm changes took effect
8. **Iteration loop** — benchmarks all workloads, detects per-workload regressions, self-corrects (max 3 iterations)
9. **Report generation** — decision log, per-payload results, token usage, hypothesis trace

Every decision is logged with the data and reasoning that drove it. No black box.

## Architecture

```
Pre-flight → Baseline benchmarks → Iteration loop:
  ┌─────────────────────────────────────────────────────────┐
  │ inspect_all()  ← 1 call, 5 categories                  │
  │   webserver: 21+ nginx directives                       │
  │   kernel: 30+ sysctl + THP + SELinux + governor + IRQ   │
  │   resource_limits: cgroup, systemd limits, hog processes │
  │   network: iptables, conntrack, tc                      │
  │   storage: I/O scheduler, readahead, I/O hogs           │
  ├─────────────────────────────────────────────────────────┤
  │ deterministic/hybrid: rules_engine → validator(optional)│
  │ debate: nginx_expert + rhel_expert → synth → planner    │
  ├─────────────────────────────────────────────────────────┤
  │ Batch apply per category → Verify on DUT → Benchmark    │
  │ Check: all workloads within 1% of baseline? → stop/loop │
  └─────────────────────────────────────────────────────────┘
```

- **Config-driven allowlist** — agent can only touch parameters listed in `config.yaml`
- **TiDB** knowledge-scoped memory with vector search
- **Cross-session learning** — knowledge persists, session state is fresh
- **Auto-fix** — resource limits, network, and storage issues fixed automatically when detected

## Quick Start

### Prerequisites

- RHEL 9.x system with Nginx
- Root SSH access to the target (DUT)
- TiDB v8.4+ (for memory/knowledge)
- An LLM backend (GPT-OSS, Granite, or Claude)

### Setup

```bash
git clone https://github.com/sarathbrp/SlayMetrics.git /opt/SlayMetrics
cd /opt/SlayMetrics
chmod +x setup.sh
sudo ./setup.sh
```

### Configure

```bash
cp .env.example .env
vi .env          # set DUT_HOST, LLM keys
vi config.yaml   # adjust targets if needed
```

```yaml
llm:
  active_profile: gpt-oss-api      # or: granite-api, claude-api

target:
  host_env: DUT_HOST               # reads from .env
  ssh_user: root
  ssh_key: ~/.ssh/id_rsa
```

### Run

```bash
python3 main.py                 # use planner_mode from config.yaml
python3 main.py -v              # verbose (show all tool calls)
python3 main.py --planner-mode hybrid
python3 main.py --max-phase 3   # stop after planning (no apply)
python3 main.py --session <id>  # resume previous session
```

### Reset Between Runs

```bash
python3 tools/reset.py              # reset DUT (nginx, sysctl, THP, SELinux)
python3 tools/reset.py --clear-db   # + clear sessions (keeps knowledge base)
python3 tools/reset.py --reset-all  # + clear everything including knowledge
```

## 5 Tuning Categories

All parameters the agent can inspect and apply are defined in `config.yaml`. Adding a new parameter = one config line, no code change.

| Category | Examples | Apply Method |
|----------|---------|-------------|
| **webserver** | worker_processes, sendfile, tcp_nopush, limit_rate, gzip, directio, accept_mutex, client timeouts | nginx config rewrite + reload |
| **kernel** | somaxconn, tcp_rmem/wmem, swappiness, dirty_ratio, THP, SELinux, CPU governor, IRQ | single bash sysctl script |
| **resource_limits** | cgroup CPU/memory/IO weight, systemd LimitNOFILE (drop-in), NUMA policy, background hog killing | systemctl + systemd drop-in |
| **network** | iptables DROP/connlimit flush, conntrack_max, tc qdisc removal | iptables/tc commands |
| **storage** | I/O scheduler, readahead, I/O hog process killing | sysfs + pkill |

## LLM Profiles

Change one line in `config.yaml`:

```yaml
llm:
  active_profile: gpt-oss-api       # GPT-OSS 120B via OpenAI-compatible endpoint
  # active_profile: granite-api     # Granite 4 7B via Ollama
  # active_profile: claude-api      # Claude Opus via Anthropic API
```

## Output

```
report/
├── report_20260328_183232_99b3fc48.md     ← human-readable report
├── report_20260328_183232_99b3fc48.json   ← structured data
├── log_20260328_183232_99b3fc48.md        ← detailed execution log

hypothesis/99b3fc48/
├── 00_preflight.md                        ← DUT validation results
├── iter1_00_summary.md                    ← benchmark table + decision
├── iter1_01_nginx_expert.md               ← nginx expert analysis
├── iter1_02_rhel_expert.md                ← RHEL expert analysis
├── iter1_03_synthesizer.md                ← merged recommendations
├── iter1_04_apply_planner.md              ← grouped changes for execution
├── iter2_00_summary.md                    ← iteration 2 (if needed)
└── ...
```

## Hypothesis Dashboard

Visual dashboard for reviewing agent sessions and aggregating parameter evidence across runs:

```bash
cd hypothesis-dashboard
npm run dev                  # starts backend + frontend for local dev
# Open http://localhost:5176

podman-compose up --build    # or docker-compose
# Open http://localhost:8080 for the containerized build
```

If the root dev command fails, run the frontend and backend separately to isolate the issue:

```bash
cd hypothesis-dashboard
npm run dev:backend
# in another shell
npm run dev:frontend
```

- **Session View** — benchmark charts, iteration timeline, agent reasoning
- **Parameters View** — hot/cold parameters, rejection patterns, and cross-session evidence matrix

## Knowledge Base

Drop `.md` files into `facts/` with performance tuning documentation. Automatically chunked, embedded, and loaded into TiDB on startup.

## Tests

```bash
pip install pytest pytest-cov
pytest --ignore=tests/test_token_attribution.py -q
# 326 tests passing
```
