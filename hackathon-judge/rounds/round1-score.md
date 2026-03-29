# Nginx-Hackathon-Judge — Round 1/3 Score

## Degradation Applied
**Scenario:** Full-Stack Performance Massacre (7 layers)
- Kernel sysctls: somaxconn=128, tiny TCP buffers, no tw_reuse, swappiness=100
- Nginx: 1 worker, 256 conns, sendfile off, no cache, debug logging
- IRQ: all NIC interrupts pinned to CPU 0, irqbalance disabled
- Cgroup: CPU 15%, memory 256M cap on nginx
- Filesystem: page cache dropped, readahead=8 sectors
- I/O scheduler: mq-deadline
- Ulimits: nofile=512, nproc=64

## Agent Results (Session: c075824c)

### Benchmark Comparison

| Payload | Baseline (degraded) | Post-Remediation | Change |
|---------|---------------------|------------------|--------|
| Small   | 1,105 req/s         | 844 req/s        | **-23.7%** |
| Medium  | 20 req/s            | 0 req/s          | **-100%** |
| Large   | 3 req/s             | 0 req/s          | **-100%** |
| Homepage| (not tracked)       | 1,137 req/s      | — |

### What the Agent Found (Correct Diagnoses)
- somaxconn=128, tcp_max_syn_backlog=128 (listen queue overflow)
- netdev_max_backlog=300 (packet drops)
- rmem_max/wmem_max=87380 (small TCP buffers)
- worker_connections=256, worker_rlimit_nofile=512
- sendfile off, tcp_nodelay off, open_file_cache off
- access_log enabled (I/O overhead)
- keepalive_requests=10, keepalive_timeout=5
- irqbalance inactive
- SELinux enforcing
- THP always
- nofile=1024

### What the Agent MISSED
- **worker_processes=1** — agent never changed this to auto (CRITICAL miss, stayed at 1 worker on 112 cores)
- **Cgroup CPU throttle (15%)** — never detected or removed
- **Cgroup memory limit (256M)** — never detected or removed
- **Systemd drop-in LimitNOFILE=512** — agent changed nginx config but systemd override still capped it
- **sendfile off** — identified but never actually fixed it
- **tcp_nopush off** — never addressed
- **error_log debug** — never changed from debug to normal level
- **readahead=8** — never addressed
- **vm.swappiness=100** — never addressed
- **vm.dirty_ratio/dirty_background_ratio** — never addressed
- **tcp_rmem/tcp_wmem** — never addressed
- **tcp_fin_timeout=120** — never addressed

### Performance Outcome
The agent made things WORSE — medium/large went to 0 req/s, small dropped 24%.
The cgroup CPU throttle at 15% was the dominant bottleneck and was never detected.

## Scoring (0-100)

| Category | Weight | Score | Weighted |
|----------|--------|-------|----------|
| Performance Recovery (40%) | 40% | 0/40 | **0** — performance degraded further |
| RCA Accuracy (25%) | 25% | 10/25 | **10** — found nginx/sysctl issues but missed cgroup, worker_processes, sendfile |
| Explainability (15%) | 15% | 8/15 | **8** — report is structured, lists findings, but all impacts show 0% |
| Full Autonomy (10%) | 10% | 10/10 | **10** — ran fully autonomously, no human intervention |
| Nginx/RHEL 9.7 Optimization (10%) | 10% | 3/10 | **3** — applied some sysctls but missed critical layers |

## Round 1 Score: 31/100

### Detailed Feedback

**Strengths:**
- Fully autonomous execution, no intervention needed
- Identified many sysctl and nginx config issues correctly
- Good hypothesis file structure with preflight, RCA, recommendations
- Reasonable token efficiency (43,696 tokens across 27 calls)

**Critical Weaknesses:**
1. **Never fixed worker_processes=1** — this is the single most impactful nginx setting on a 112-core system. The agent set worker_cpu_affinity and worker_rlimit_nofile but left worker_processes at 1.
2. **Blind to cgroup limits** — the 15% CPU quota was the dominant bottleneck. The agent has no system check for cgroup/systemd resource limits.
3. **Systemd drop-in override** — the agent set nofile in nginx config but the systemd LimitNOFILE=512 override capped it anyway.
4. **Made medium/large worse** — went from degraded (20/3 req/s) to completely broken (0/0 req/s). The aio threads change caused a regression.
5. **sendfile never restored** — identified as off but never applied sendfile=on.
6. **No self-correction loop** — when medium/large hit 0 req/s, the agent didn't effectively diagnose and recover.
