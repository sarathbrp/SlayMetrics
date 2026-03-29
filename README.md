# SlayMetricsAgent

An autonomous AI agent that diagnoses and remediates performance issues on RHEL systems. Point it at a degraded system, let it run — get a full diagnostic report with before-and-after proof.

## What It Does

1. Collects system state (RHEL checks, fingerprint, nginx config)
2. Benchmarks the service across small, medium, and large payloads
3. Queries the knowledge base (RAG) and cross-session memory for proven fixes
4. AI agent inspects what needs fixing, applies all fixes in batched operations
5. Benchmarks again, captures bottleneck analysis
6. Generates a report with decision log, per-payload results, and token usage

Every decision is logged with the data and reasoning that drove it. No black box.

## Architecture

```
Steps 1-4: Direct (subprocess + vector search) → zero LLM tokens
Step 5:    LangGraph diagnosis loop with model-selected tools
Steps 6-8: Direct (wrk2 + template) → zero LLM tokens
```

- **LangGraph runtime** with a single diagnosis workflow and 7 consolidated tools
- **TiDB** pyramid memory (profile → facts → context) with vector search
- **Cross-session learning** — agent remembers past fixes, never repeats
- **Knowledge base** — Red Hat performance docs loaded via RAG from `facts/` folder
- **LangChain model registry** — Granite, GPT-OSS, and Claude profiles

## Quick Start

### Prerequisites

- RHEL 9.x / CentOS Stream 9+ system
- Root access
- An LLM backend (local Granite via Ollama or vLLM; Claude remains optional)

### Automated Setup

```bash
git clone https://github.com/sarathbrp/SlayMetrics.git /opt/slaymetrics
cd /opt/slaymetrics
chmod +x setup.sh
sudo ./setup.sh
```

The setup script scans the system, shows what's present/missing, asks for confirmation, then installs only what's needed.

### Configure

```bash
# Edit target and LLM profile
vi config.yaml
```

```yaml
llm:
  active_profile: gpt-oss-api      # or: granite-api, claude-api

target:
  host: 127.0.0.1                  # localhost = subprocess, remote = SSH
  ssh_user: root
  ssh_key: /root/.ssh/id_rsa
```

### Run

```bash
ollama pull granite4:7b-a1b-h
ollama serve
python3 main.py           # normal
python3 main.py -v        # verbose (show all tool calls)
python3 main.py --session <id>   # resume previous session
```

### Reset Between Runs

```bash
python3 tools/reset.py              # reset system only (nginx, sysctl, THP, SELinux)
python3 tools/reset.py --clear-db   # + clear sessions (keeps knowledge base)
python3 tools/reset.py --reset-all  # + clear everything including knowledge
```

### Self-Testing

```bash
python3 tools/degrade.py            # intentionally degrade the system
python3 main.py -v                  # let the agent fix it
python3 tools/degrade.py --restore  # undo degradations
```

## LLM Profiles

Change one line in `config.yaml`:

```yaml
llm:
  active_profile: gpt-oss-api       # GPT-OSS 120B via deployed OpenAI-compatible endpoint — default
  active_profile: granite-api       # Granite 4 7B A1B via Ollama API
  active_profile: claude-api        # Claude Opus via Anthropic API

`gpt-oss-api` reads its deployment settings from the environment:

```bash
export GPT_OSS_BASE_URL=http://your-gpt-oss-host:8002/v1
export GPT_OSS_API_KEY=your-token
```

To stop after planning and inspect telemetry, RCA, and recommendations without applying remediations:

```bash
python3 main.py -v --max-phase 3
```
```

## Knowledge Base

Drop `.md` files into `facts/` with Red Hat performance tuning docs. They're automatically chunked, embedded, and loaded into TiDB on startup (hash-based — skips if unchanged).

```
facts/
├── nginx_performance_tuning.md
├── rhel_linux_performance_tuning_guide.md
└── tuning_access_log_in_nginx.md
```

## Adding a New Service

1. Create `adapters/<service>.py` implementing `ServiceAdapter`
2. Update `config.yaml` with service-specific settings
3. Run `python3 main.py`

No core code changes required.

## Output

```
report/
├── report_20260326_215258_04d5fecc.md    ← timestamped, never overwritten
├── report_20260326_215258_04d5fecc.json
├── log_20260326_215258_04d5fecc.md       ← detailed execution log
```

Report includes:
- Executive summary with improvement %
- Per-payload benchmark tables (small/medium/large, before vs after)
- Resource usage (CPU%, memory)
- Bottleneck analysis (NIC speed, disk I/O, throughput per payload)
- Applied fixes with reasoning
- Token consumption + cross-session history
