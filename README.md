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
Step 5:    ONE agent call with full context
Steps 6-8: Direct (wrk2 + template) → zero LLM tokens
```

- **Single agent** with 7 consolidated tools (inspect, apply, benchmark, save)
- **TiDB** pyramid memory (profile → facts → context) with vector search
- **Cross-session learning** — agent remembers past fixes, never repeats
- **Knowledge base** — Red Hat performance docs loaded via RAG from `facts/` folder
- **LocalClient** — uses subprocess for localhost targets, SSH for remote

## Quick Start

### Prerequisites

- RHEL 9.x / CentOS Stream 9+ system
- Root access
- An LLM backend (Claude API key, or local Granite via vLLM, or Ollama)

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
# Set API key (if using Claude)
echo 'ANTHROPIC_API_KEY=sk-ant-your-key' > .env  # pragma: allowlist secret

# Edit target and LLM profile
vi config.yaml
```

```yaml
llm:
  active_profile: claude-haiku     # or: claude-remote, granite-local, ollama-local

target:
  host: 127.0.0.1                  # localhost = subprocess, remote = SSH
  ssh_user: root
  ssh_key: /root/.ssh/id_rsa
```

### Run

```bash
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
  active_profile: claude-haiku      # fast + cheap, good for testing
  active_profile: claude-remote     # Claude Opus — best reasoning
  active_profile: granite-local     # Granite 3.1 8B via vLLM — air-gapped
  active_profile: ollama-local      # any Ollama model — zero infra
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
