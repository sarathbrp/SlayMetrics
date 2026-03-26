# Nginx Performance Tuning on RHEL

Source: https://access.redhat.com/solutions/7025194

## Worker Processes and Connections

- `worker_processes`: Set to `auto` to match CPU core count. Default is 1 which severely limits throughput.
- `worker_connections`: Max simultaneous connections per worker. Default is 512. Must not exceed `worker_rlimit_nofile`. The OS `fs.file-max` should be at least 2x this value.
- Diagnostic: `grep worker_processes /etc/nginx/nginx.conf` and compare with `nproc`.
- Fix: `worker_processes auto;`

## Sendfile

- `sendfile on` enables zero-copy file transfer via the kernel's `sendfile()` syscall. Data goes from disk to socket without entering userspace.
- Critical for small and medium static files — eliminates context switching and reduces CPU usage.
- When sendfile is enabled, nginx bypasses content filters like gzip. If both `sendfile` and a content filter are active in the same context, nginx auto-disables sendfile.
- Diagnostic: `grep sendfile /etc/nginx/nginx.conf`
- Fix: `sendfile on;` in the http block.

## TCP Optimization

- `tcp_nopush on`: Batches HTTP headers and file body into a single TCP segment. Reduces packet count. Only works with `sendfile on`.
- `tcp_nodelay on`: Disables Nagle's algorithm. Reduces latency for small responses. Important for small file workloads.
- Both should be enabled together for optimal small/medium file throughput.
- Fix: Add both directives in the http block.

## Keepalive Connections

- `keepalive_requests`: Number of requests per keepalive connection. Default 100. Higher values reduce socket creation overhead (fewer CPU cycles, fewer file descriptors).
- `keepalive_timeout`: How long idle connections stay open. Default 75s.
- Keepalive reduces CPU, networking, and file descriptor usage by reusing existing connections.
- For upstream servers: `keepalive <N>` in the upstream block, plus `proxy_http_version 1.1;` and `proxy_set_header Connection "";`.

## Open File Cache

- `open_file_cache max=1000 inactive=20s`: Caches file descriptors, file sizes, and modification times. Avoids repeated `stat()` and `open()` syscalls.
- `open_file_cache_valid 30s`: How often to re-validate cached entries.
- `open_file_cache_min_uses 2`: Minimum access count before caching.
- Very high impact for small file workloads where the same files are served repeatedly.
- Diagnostic: check if directive exists in nginx.conf.
- Fix: Add to http block.

## Access Logging

- Disk I/O from access logging can bottleneck throughput under heavy load.
- `access_log off;` disables logging entirely. Use only if logging is not needed.
- `access_log /path/to/log buffer=256k flush=5s;` buffers log writes to reduce I/O.

## RHEL Network Tuning for Nginx

- `net.core.somaxconn`: Max queued connections. Default 128/4096 depending on RHEL version. Set to 65535 for high-traffic servers.
- When changing somaxconn, the nginx `listen` directive backlog must match: `listen *:80 backlog=65535;`
- `net.core.netdev_max_backlog`: Packet buffer queue before CPU handoff. Increase only if kernel logs show drops.
- `net.ipv4.ip_local_port_range`: Ephemeral port range. Increase if port exhaustion observed.
- `net.ipv4.tcp_max_syn_backlog`: SYN queue size. Set to 65535 for high connection rates.
- `net.ipv4.tcp_tw_reuse`: Reuse TIME_WAIT sockets. Set to 1 for high connection churn.
- Apply with: `sysctl -w <param>=<value>` and persist in `/etc/sysctl.d/99-nginx.conf`.

## RHEL File Descriptor Limits

- Each nginx connection uses up to 2 file descriptors. Keepalive reduces this via socket reuse.
- Monitor with: `ls /proc/<nginx_pid>/fd | wc -l` and compare to `ulimit -n`.
- If nearing limit, increase in `/etc/security/limits.conf` or the nginx systemd unit: `LimitNOFILE=65536`.
- `worker_rlimit_nofile` in nginx.conf should match the OS limit.

## Connection Limits (DDoS Protection)

- `limit_conn_zone` + `limit_conn`: Limit concurrent connections per client IP.
- `limit_req_zone` + `limit_req`: Limit request rate per client IP.
- `limit_rate`: Throttle response bandwidth per connection.
- These protect against overload but should not be set too low for legitimate traffic.
