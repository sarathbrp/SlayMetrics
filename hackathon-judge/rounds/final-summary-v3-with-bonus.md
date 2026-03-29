# Nginx-Hackathon-Judge — Final Summary (v3 + Bonus Rounds)

## Overall Scores

| Round | Scenario | Score |
|-------|----------|-------|
| 1 | Full-stack massacre (cgroup 15%, 1 worker, sendfile off, ulimits) | **86/100** |
| 2 | Subtle misconfig (iptables, disk I/O, gzip 9, limit_rate 50m, aio threads) | **83/100** |
| 3 | Stealth bottlenecks (tc shaping, rate limiting, cgroup weights, memory pressure) | **91/100** |
| 4 | Production incident (limit_rate 20m, SELinux booleans, cgroup weights, THP) | **93/100** |

## Final Team Score (3 rounds): 260/300 (86.7%)
## Final Team Score (4 rounds): 353/400 (88.3%)

## Score Progression Across Versions
| Version | Score | Improvement |
|---------|-------|-------------|
| v1 (2 rounds) | 85/200 (42.5%) | — |
| v2 (3 rounds) | 171/300 (57.0%) | +14.5% |
| **v3 (4 rounds)** | **353/400 (88.3%)** | **+31.3%** |

## Best Results Summary
- **Round 1**: Small 124K req/s (+11,201%), Medium 91%, Large 99%, Mixed 98%
- **Round 2**: Medium 130%, Large 472%, Mixed 113% of baseline
- **Round 3**: Small 1.37M req/s (+6,398%), ALL workloads 98-100%+
- **Round 4**: Small 864K req/s (+3,449%), ALL workloads 98-290%+

## Agent Characteristics (v3)
- **1 iteration in most rounds** — efficient, no regressions
- **25K-60K tokens per run** — highly token-efficient
- **5-category inspection** covering webserver, kernel, resource_limits, network, storage
- **Auto-fix for network issues** (iptables, conntrack, tc)
- **systemd drop-in for nofile** — reliable across all rounds
- **0 apply failures** in Rounds 2-4

## Remaining Minor Gaps
1. Cgroup weight/quota removal not attempted (IOWeight, CPUWeight detected but not removed)
2. Background process detection not implemented
3. worker_processes not always set to 'auto' (sometimes stays at original value)
4. LLM scope check occasionally rejects valid recommendations
