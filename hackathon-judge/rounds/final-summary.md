# Nginx-Hackathon-Judge — Final Summary (v2 Runs)

## Overall Scores

| Round | Scenario | Score | vs v1 |
|-------|----------|-------|-------|
| 1 | Full-stack massacre (cgroup, sysctl, nginx, IRQ, ulimits) | **50/100** | +19 (was 31) |
| 2 | Subtle misconfig (iptables, disk I/O, gzip, limit_rate, aio threads) | **57/100** | +3 (was 54) |
| 3 | Stealth bottlenecks (tc shaping, rate limiting, cgroup weights, memory pressure) | **64/100** | N/A (new) |

## Final Team Score: 171/300 (57.0%)

## Improvement Areas (v1 → v2)

### Fixed in v2
| Gap | v1 | v2 |
|-----|----|----|
| worker_processes | Never changed | ✓ auto |
| sendfile/tcp_nopush | Missed | ✓ Applied |
| systemd LimitNOFILE | Config-only (overridden) | ✓ systemd drop-in |
| CPU governor | Missed R1, found R2 | ✓ All rounds |
| iptables/conntrack | Completely blind | ✓ Detects and removes |
| tc traffic shaping | Not checked | ✓ Detects and removes |
| gzip | Not checked | ✓ Disables |
| conntrack_max | Not checked | ✓ Raises |
| readahead | Not checked | ✓ Adjusts |
| 5-category inspection | 2 categories | ✓ 5 categories |

### Still Broken (Critical Gaps)
| Gap | Impact | All 3 Rounds |
|-----|--------|-------------|
| **limit_rate apply** | Cannot remove rate limiting | FAILED every time |
| **limit_req/limit_conn apply** | Cannot remove request/connection limits | FAILED every time |
| **accept_mutex apply** | Cannot disable | FAILED every time |
| **directio apply** | Cannot disable | FAILED every time |
| **output_buffers/postpone_output** | Cannot tune | FAILED every time |
| **error_log_level** | Cannot change log level | FAILED every time |
| **client_body/header/send_timeout** | Cannot tune timeouts | FAILED every time |
| **cgroup CPU/IO/Memory removal** | Identified but never removed | All rounds |
| **Background processes (dd, stress-ng)** | Never detected | R2, R3 |
| **Memory pressure (tmpfs)** | Never detected | R3 |
| **Benchmark result caching** | iter3 returns stale data | R2, R3 |

### Key Observations
1. **Detection vs Apply gap**: The agent correctly IDENTIFIES many issues but cannot APPLY fixes for ~11 nginx directive types. This is the #1 blocker.
2. **Cgroup awareness**: Agent detects cgroup limits (CPU quota, IO weight) but has no mechanism to remove them via systemd.
3. **Process-level awareness**: Agent never checks for rogue background processes consuming I/O or memory.
4. **Benchmark staleness**: The benchmark script appears to cache or reuse prior results when exit code is non-zero.
5. **Self-harm via aio threads**: Agent no longer introduces aio threads regression (fixed from v1).
