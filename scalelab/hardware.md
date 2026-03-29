# ScaleLab Hardware — Cloud77

## DUT (d21-h23) — Nginx Server

- **Hostname:** d21-h23-000-r650.rdu2.scalelab.redhat.com
- **IP:** 10.1.90.178 (management), 172.21.90.178 (private)
- **OS:** Red Hat Enterprise Linux 9.7 (Plow)
- **Kernel:** 5.14.0-611.42.1.el9_7.x86_64
- **CPU:** 2x Intel Xeon Gold 6330 @ 2.00GHz (28 cores/socket, HT on)
- **Total vCPUs:** 112
- **RAM:** 502 GB
- **NUMA:** 2 nodes (node0: even CPUs, node1: odd CPUs)
- **Disks:** 1x 447GB SSD, 2x 1.7TB SSD, 1x 2.9TB NVMe
- **NICs:** 25Gbps (eno12399np0, eno12409np1), plus 4x additional
- **Nginx:** 1.20.1 (RHEL 9 stock)
- **Hardware:** Dell r650

## Bench Node (d21-h24) — wrk + Agent

- **Hostname:** d21-h24-000-r650.rdu2.scalelab.redhat.com
- **IP:** 10.1.89.124 (management), 172.21.89.124 (private)
- **OS:** Red Hat Enterprise Linux 9.7 (Plow)
- **Kernel:** 5.14.0-611.42.1.el9_7.x86_64
- **CPU:** 2x Intel Xeon Gold 6330 @ 2.00GHz (112 vCPUs)
- **RAM:** 502 GB
- **NUMA:** 2 nodes
- **wrk:** 4.2.0 (pre-installed)
- **Hardware:** Dell r650

## DUT Nginx Config (stock — before tuning)

```nginx
user nginx;
worker_processes auto;
worker_rlimit_nofile 1024;
error_log /var/log/nginx/error.log;
pid /run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    access_log /var/log/nginx/access.log main;
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    keepalive_requests 100;
    client_body_buffer_size 8k;
    client_max_body_size 1m;
    aio off;
    gzip off;
    open_file_cache off;
}
```

## Intentional Detunings Visible

- `worker_rlimit_nofile 1024` — way too low for 112 cores
- `worker_connections 1024` — too low
- `keepalive_requests 100` — default, should be higher
- `gzip off` — explicitly disabled
- `open_file_cache off` — explicitly disabled
- `aio off` — explicitly disabled
- `access_log on` — disk I/O overhead
- CPU scaling at 36% — likely powersave governor

## Benchmark Baselines (degraded system)

| Workload | Files | Size | RPS | Config |
|----------|-------|------|-----|--------|
| homepage | 1 page | - | 374,706 | 16t, 1000c, 30s |
| small | 2.5M | 64B | 368,220 | 16t, 1000c, 60s |
| medium | 250K | 2MB | 1,401 | 16t, 300c, 60s |
| large | 250 | 15MB | 186 | 16t, 100c, 60s |
| mixed | 70/25/5% | mixed | 2,269 | 16t, 100c, 60s |

## Benchmark Tool

- Uses `wrk` (not wrk2) — no rate limiting
- Lua scripts for workload generation
- Files at `/stress_test_data/{small,medium,large}/...`
- Run: `./benchmark.sh slaymetrics` from bench node
