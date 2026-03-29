# Nginx-Hackathon-Judge — Round 2/3 Score

## Degradation Applied
**Scenario:** NUMA misalignment + iptables + disk I/O contention + nginx misconfig
- Nginx: 4 workers, 1024 conns, aio threads, gzip level 9, limit_rate 50m, tcp_nodelay off
- iptables: conntrack max=8192, connlimit 200 → DROP
- Disk I/O: continuous dd background writes (direct I/O)
- Kernel: somaxconn=512, small TCP buffers, no tw_reuse, tcp_fin_timeout=90
- CPU: governor to powersave
- THP: always + defrag always
- IRQ: pinned to CPUs 0-1, irqbalance stopped

## Agent Results (Session: 3751966d)

### Benchmark Comparison (vs Healthy Baseline)

| Payload | Degraded | Post-Remediation | Healthy Baseline | Recovery % |
|---------|----------|------------------|------------------|------------|
| Small   | 4,901    | 166,904          | 368,220          | 45.3% of baseline |
| Medium  | 421      | 212              | 1,401            | 15.1% of baseline |
| Large   | 42       | 29               | 186              | 15.6% of baseline |

### What the Agent Found (Correct Diagnoses)
- worker_connections=1024, worker_rlimit_nofile=1024 ✓
- tcp_nodelay off ✓
- open_file_cache off ✓
- access_log with buffer/flush ✓
- keepalive_requests=100, keepalive_timeout=65 ✓
- listen_backlog=512 ✓
- somaxconn=512, tcp_max_syn_backlog=512 ✓
- rmem_max/wmem_max=524288 ✓
- tcp_tw_reuse=0 ✓
- CPU governor: powersave → performance ✓ (GOOD — found this!)
- irqbalance inactive ✓
- THP always ✓
- SELinux enforcing ✓
- nofile=1024 ✓
- aio threads → reverted to off (self-corrected after regression!) ✓

### What the Agent MISSED
- **worker_processes=4** — never changed to auto (112 cores, only 4 workers → 5 after affinity)
- **limit_rate 50m** — per-connection rate limit was never detected or removed
- **gzip level 9** — CPU-heavy compression was never disabled
- **gzip on** for binary content (application/octet-stream) — never addressed
- **directio 512** — directio conflicts with sendfile for files >512 bytes, never addressed
- **tcp_nopush off** — never restored
- **iptables connlimit DROP rule** — never detected (200 conn limit causes drops)
- **conntrack max=8192** — never detected (conntrack table overflow under load)
- **Background dd I/O pressure** — never detected or addressed
- **tcp_rmem/tcp_wmem** — small buffer caps never addressed
- **vm.swappiness=60** — never addressed
- **nofile still 1024** — kept trying to fix but failed (no systemd drop-in approach)

### Performance Outcome
- Small files: +3305% from degraded baseline (impressive!)
- Medium files: -49.6% (WORSE than degraded baseline)
- Large files: -29.5% (WORSE than degraded baseline)
- Agent self-harmed medium/large by introducing aio threads, then partially reverted

## Scoring (0-100)

| Category | Weight | Score | Weighted |
|----------|--------|-------|----------|
| Performance Recovery (40%) | 40% | 15/40 | **15** — small files hugely improved but medium/large got worse |
| RCA Accuracy (25%) | 25% | 14/25 | **14** — found many issues, missed iptables/conntrack/gzip/limit_rate/worker_processes |
| Explainability (15%) | 15% | 10/15 | **10** — detailed RCA with 19 findings, good structure |
| Full Autonomy (10%) | 10% | 10/10 | **10** — fully autonomous |
| Nginx/RHEL 9.7 Optimization (10%) | 10% | 5/10 | **5** — fixed CPU governor (good!) but missed limit_rate, gzip, directio |

## Round 2 Score: 54/100

### Detailed Feedback

**Strengths:**
1. Found CPU governor=powersave and fixed it — Round 1 missed this entirely
2. Massive improvement on small files (+3305%)
3. Self-corrected the aio threads regression (recognized it caused medium/large drops)
4. Comprehensive RCA with 19 root cause findings
5. Good token efficiency (41,678 tokens)

**Weaknesses:**
1. Never detected iptables/conntrack — this is a whole class of firewall-level bottlenecks the agent is blind to
2. Never detected limit_rate directive — rate limiting per connection is a critical nginx performance killer
3. Never changed worker_processes from 4 to auto — consistent miss across both rounds
4. gzip level 9 on binary content wastes CPU for zero benefit — not detected
5. directio 512 conflicts with sendfile — not detected
6. Medium/large still regressed despite 3 iterations — couldn't recover these workloads
7. nofile fix keeps failing across iterations — never tried systemd approach
