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
