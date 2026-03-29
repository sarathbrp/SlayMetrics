# Nginx-Hackathon-Judge — Final Summary (v3 Runs)

## Overall Scores

| Round | Scenario | v1 | v2 | v3 |
|-------|----------|----|----|-----|
| 1 | Full-stack massacre (cgroup 15%, 1 worker, sendfile off, ulimits) | 31 | 50 | **86** |
| 2 | Subtle misconfig (iptables, disk I/O, gzip 9, limit_rate 50m, aio threads) | 54 | 57 | **83** |
| 3 | Stealth bottlenecks (tc shaping, rate limiting, cgroup weights, memory pressure) | N/A | 64 | **91** |

## Final Team Score: 260/300 (86.7%)

## Score Progression
- v1: 85/200 (42.5%) — 2 rounds only
- v2: 171/300 (57.0%)
- **v3: 260/300 (86.7%)** — +89 points improvement

## Best Results by Round

### Round 1 — Small: 124K req/s (+11,201%), Medium: 91% recovery, Large: 99% recovery
### Round 2 — Medium: 130% of baseline, Large: 472% of baseline, Mixed: 113% of baseline
### Round 3 — Small: 1.37M req/s (+6,398%), ALL workloads at 98-100% or exceeding baseline

## Key Improvements v2 → v3
1. **Apply success rate**: ~50% → nearly 100% (limit_rate, directio, accept_mutex, output_buffers all work now)
2. **tcp_rmem/tcp_wmem**: was failing → now applied
3. **Auto-fix network**: agent auto-applies iptables/conntrack/tc fixes when issues detected
4. **systemd_nofile**: reliable via drop-in across all rounds
5. **No more aio threads self-harm**: agent correctly sets aio=off

## Remaining Gaps (minor at this point)
1. LLM scope check still rejects some valid recommendations (limit_req, limit_conn removal via LLM path)
2. Cgroup weight removal not implemented (IOWeight, CPUWeight)
3. Background process detection (dd, stress-ng, tmpfs) not implemented
4. Homepage/small recovery in R1/R2 lower than R3 (cgroup CPU throttle effect in R1, disk I/O in R2)
