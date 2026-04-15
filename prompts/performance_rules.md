## Performance Rules Specification (MANDATORY — all stages must follow)

These rules are derived from Red Hat's verified RHEL+NGINX tuning guidelines and must be followed by every analysis stage (investigation, network, kernel, nginx). Violating these rules produces broken configurations.

### Constraint Chains — ALWAYS raise ceilings, NEVER lower limits

When two settings have a dependency, fix the ceiling FIRST, then raise the limit:

```
fs.nr_open  >=  systemd LimitNOFILE  >=  worker_rlimit_nofile  >=  worker_connections
```

- NEVER lower LimitNOFILE to fit under fs.nr_open — raise fs.nr_open instead
- NEVER lower worker_rlimit_nofile to fit under LimitNOFILE — raise LimitNOFILE instead
- NEVER set worker_connections higher than worker_rlimit_nofile
- OS file descriptor limit (LimitNOFILE) should be at least 2× worker_connections

```
net.core.somaxconn  >=  net.ipv4.tcp_max_syn_backlog  >=  nginx listen backlog
```

- When raising somaxconn, ALWAYS raise tcp_max_syn_backlog to match
- When raising somaxconn, the nginx listen backlog directive MUST be updated to match
- Example: somaxconn=65535 requires `listen 80 backlog=65535;`

```
sendfile on  →  tcp_nopush on (requires sendfile)
sendfile  +  gzip/content filters  =  sendfile auto-disabled by nginx
```

- NEVER enable tcp_nopush without sendfile on — it has no effect
- If gzip is active, sendfile is automatically disabled by nginx — do not flag this as a problem

### Fix Priority — remove hard caps before tuning

Fixes MUST be tiered in this order:

| Tier | Category | Rationale |
|------|----------|-----------|
| 1 | Remove cgroup throttles (CPUQuota, MemoryMax) | These mask ALL other improvements |
| 1 | Set worker_processes=auto | Unlocks all CPU cores |
| 1 | Disable access_log | Proven 2-3× RPS gain for static serving |
| 2 | Fix file descriptor chain (fs.nr_open → LimitNOFILE → worker_rlimit_nofile) | Unlocks connection capacity |
| 2 | Fix connection chain (somaxconn → backlog) | Stops listen drops |
| 2 | Enable sendfile + tcp_nopush | Zero-copy I/O path |
| 3 | Tune keepalive, buffers, tcp params | Incremental gains through monitoring |
| 3 | Fix scheduling (CPUWeight, IOWeight, Nice) | Priority tuning |

### Mandatory Grouping Rules (OOM prevention)

- **MemoryMax removal MUST be in the SAME group as worker_processes=auto and TasksMax=infinity**. Spawning 112 workers (auto) without raising the memory cap (e.g., 256M) causes cgroup OOM kill. All three must be applied together or not at all.
- **All systemd sabotage settings MUST be in ONE group**: CPUQuota, MemoryMax, Nice, CPUWeight, IOWeight, OOMScoreAdjust, TasksMax + worker_processes=auto. These compound — partial removal can cause OOM, scheduling starvation, or no measurable improvement.

### Hard Rules

1. NEVER set tcp_tw_reuse=1 — always use 2 on RHEL (kernel-controlled reuse)
2. NEVER raise netdev_max_backlog unless kernel logs show buffer errors — blind raising wastes memory
3. NEVER set worker_connections to a fixed high value without also raising worker_rlimit_nofile to match
4. NEVER recommend keepalive_timeout or keepalive_requests to specific values without benchmark evidence — these require monitoring to tune correctly
5. ALWAYS remove systemd drop-in sabotage (Nice=19, OOMScoreAdjust>0, CPUWeight<100, IOWeight<100, TasksMax<nproc+10) before tuning nginx config
6. ALWAYS check effective nginx directive values — server block overrides http block (last occurrence wins)
7. When proposing LimitNOFILE raise, ALWAYS include fs.nr_open raise as a prerequisite fix at equal or higher value
8. Systemd services don't use PAM — limits from /etc/security/limits.conf have NO effect on nginx. Only LimitNOFILE in the systemd unit or drop-in files matters
9. Limit* directives (LimitNOFILE, LimitNPROC, etc.) are PER-PROCESS — child processes can fork and get independent limits. For service-wide enforcement, prefer cgroup resource controls: MemoryMax (replaces LimitRSS which is not implemented on Linux), CPUQuota, TasksMax. When diagnosing throttling, check cgroup controls FIRST (they can't be escaped), then per-process Limit* directives
10. When removing a systemd drop-in sabotage file, ALWAYS run daemon-reload before restart
11. System-wide defaults in `/etc/systemd/system.conf.d/*.conf` (e.g. DefaultLimitNOFILE) apply to ALL services unless overridden per-service. Always check `cat /etc/systemd/system.conf.d/*.conf 2>/dev/null` for hidden global throttles
12. Some services set their own resource limits at runtime (overriding systemd). nginx does NOT do this — it respects systemd Limit* directives

### RHEL Network Stack Tuning (from RHEL Performance Guide)

- `net.ipv4.tcp_fastopen=3` — enables TCP Fast Open for both client and server, reduces handshake latency
- `net.core.default_qdisc=fq_codel` — Fair Queue with Controlled Delay, reduces bufferbloat (better than default pfifo_fast)
- `net.core.netdev_budget=600` — packets processed per softirq iteration (default 300, increase for high-throughput NICs)
- `net.ipv4.tcp_mtu_probing=1` — automatic MTU discovery, prevents fragmentation
- `net.ipv4.tcp_abort_on_overflow=1` — fail fast when listen queue overflows instead of silently dropping
- `net.core.rmem_default=4194304` and `net.core.wmem_default=4194304` — 4MB default socket buffers
- `vm.dirty_background_ratio` — tune alongside `vm.dirty_ratio` (controls when background writeback starts)
- NIC ring buffers: `ethtool -G <nic> rx 4096 tx 4096` — increase from defaults to prevent packet drops at NIC level
- NIC interrupt coalescing: `ethtool -C <nic> adaptive-rx on adaptive-tx on` — auto-balances latency vs throughput

### Nginx-Specific Rules (from nginx.org verified anti-patterns)

13. worker_rlimit_nofile MUST be at least 2× worker_connections — each connection uses 1 FD for client + 1 FD per served file. When proxying: 1 FD client + 1 FD upstream + potentially 1 FD temp file = 3 FDs per connection
14. Verify total FD usage: `worker_rlimit_nofile × worker_processes` must be significantly less than `fs.file-max`. If nginx exhausts all system FDs (e.g. during DoS), the machine becomes unmanageable
15. `error_log off` does NOT disable logging — it creates a file named "off". To discard errors use `error_log /dev/null crit;`
16. Directive inheritance: child context (server/location) OVERRIDES parent (http) — values are NOT added together. When same directive appears in both http{} and server{}, only the server{} value applies
17. NEVER disable proxy_buffering unless specifically required (long polling). Disabling it breaks rate limiting, caching, and degrades performance
18. `sendfile` is automatically disabled by nginx when content-changing filters (gzip, sub_filter) are active in the same context — this is expected behavior, not a bug
19. On multi-core systems (>4 cores), `listen 80 reuseport;` enables SO_REUSEPORT socket sharding — each worker gets its own socket listener, eliminating kernel accept lock contention. This makes accept_mutex redundant (automatically disabled when reuseport is set). Combine with backlog: `listen 80 reuseport backlog=65535;`
20. The `listen` directive parameters (reuseport, backlog) are in the server{} block (conf.d/), NOT in nginx.conf main/http context
21. worker_connections=256 (default 512) is a CRITICAL bottleneck on multi-core systems — with 112 workers × 256 = only 28K total concurrent connections. Raise to at least 4096-65535 per worker. ALWAYS flag worker_connections < 1024 as a problem, even if RPS looks acceptable — it limits peak throughput under load spikes
22. worker_connections, tcp_nopush, tcp_nodelay, and keepalive_requests are common "last mile" bottlenecks — ALWAYS check and fix these even when major sabotage has been resolved
