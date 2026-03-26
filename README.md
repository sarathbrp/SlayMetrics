# SlayMetricsAgent

Autonomous AI agent that diagnoses and remediates performance issues on RHEL systems. Point it at a degraded system, let it run — get a full diagnostic report with before-and-after proof.

## What It Does

1. Runs RHEL system-level checks (CPU governor, SELinux, sysctl, hugepages, etc.)
2. Benchmarks the service across small, medium, and large payloads
3. Iterates through a prioritized hypothesis queue — diagnose, fix, re-benchmark
4. Runs a sustained stability test to verify consistency
5. Generates a report with decision log, per-payload results, and token usage

Every decision is logged with the data and reasoning that drove it. No black box.

## Quick Start

### Prerequisites

- RHEL 9.x / CentOS Stream 9+ system
- Root access
- An LLM backend (Claude API key, or local Granite via vLLM, or Ollama)

### Automated Setup

The setup script installs all dependencies (nginx, wrk2, TiDB, Python packages, system tools) and configures localhost SSH:

```bash
git clone https://github.com/sarathbrp/SlayMetrics.git /opt/slaymetrics
cd /opt/slaymetrics
chmod +x setup.sh
sudo ./setup.sh
```

### Manual Setup

If you prefer to install components individually:

```bash
pip install -r requirements.txt

# Start TiDB (local single-node)
tiup playground v8.4.0

# Bootstrap database
mysql -h 127.0.0.1 -P 4000 -u root < schema.sql

# Start local LLM (default profile)
podman run -p 8000:8000 registry.redhat.io/rhaiis/vllm-rhel9 \
  --model registry.redhat.io/rhelai1/granite-3.1-8b-lab-v2.1
```

### Configure

Edit `config.yaml`:

```yaml
target:
  host: <target-ip>
  ssh_user: root
  ssh_key: ~/.ssh/id_rsa

service:
  name: nginx
  benchmark:
    small_file_url: "http://localhost/1kb.html"
    medium_file_url: "http://localhost/100kb.html"
    large_file_url: "http://localhost/1mb.html"
```

### Run

```bash
python main.py
```

The report is written to `report/report.md` and `report/report.json`.

### Resume a Previous Session

```bash
python main.py --session <session-id>
```

## Switching LLM

Change one line in `config.yaml`:

```yaml
llm:
  active_profile: granite-local    # local, air-gapped (default)
  active_profile: claude-remote    # best reasoning, needs API key
  active_profile: ollama-local     # lightweight alternative
```

## Adding a New Service

1. Create `adapters/<service>.py` implementing `ServiceAdapter`
2. Update `config.yaml` with service-specific settings
3. Run `python main.py`

No core code changes required.

## Output

The agent produces:

- `report/report.md` — human-readable diagnostic report
- `report/report.json` — machine-readable results

Report includes: system profile, per-payload benchmark tables (before/after), resource usage (CPU/memory), applied fixes with reasoning, stability test results, and token consumption.
