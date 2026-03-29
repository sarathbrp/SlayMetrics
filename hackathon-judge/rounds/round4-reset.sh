#!/bin/bash
# ============================================================
# Round 4 — Reset DUT to Clean State
# ============================================================

set -euo pipefail

echo "=========================================="
echo "RESETTING DUT (ROUND 4) TO CLEAN STATE"
echo "=========================================="

# ─── REMOVE SYSTEMD DROP-INS ───────────────────────────────
echo ">>> Removing systemd drop-ins and cgroup limits"
rm -rf /etc/systemd/system/nginx.service.d/
systemctl set-property nginx.service CPUQuota= CPUWeight= IOWeight= MemoryMax= 2>/dev/null || true
systemctl daemon-reload

# ─── RESTORE KERNEL SYSCTLS ────────────────────────────────
echo ">>> Restoring kernel sysctls"
sysctl -w net.core.somaxconn=65536
sysctl -w net.ipv4.tcp_max_syn_backlog=65535
sysctl -w net.core.netdev_max_backlog=65535
sysctl -w net.core.rmem_max=16777216
sysctl -w net.core.wmem_max=16777216
sysctl -w net.core.rmem_default=212992
sysctl -w net.core.wmem_default=212992
sysctl -w net.ipv4.tcp_rmem="4096 131072 16777216"
sysctl -w net.ipv4.tcp_wmem="4096 16384 16777216"
sysctl -w net.ipv4.tcp_tw_reuse=1
sysctl -w net.ipv4.tcp_fin_timeout=15
sysctl -w net.ipv4.tcp_slow_start_after_idle=0
sysctl -w net.ipv4.tcp_max_tw_buckets=262144
sysctl -w net.ipv4.ip_local_port_range="1024 65535"
sysctl -w vm.swappiness=30
sysctl -w vm.dirty_ratio=20
sysctl -w vm.dirty_background_ratio=10
sysctl -w vm.vfs_cache_pressure=100
sysctl -w vm.dirty_expire_centisecs=3000
sysctl -w vm.dirty_writeback_centisecs=500

# ─── RESTORE SELinux BOOLEANS ───────────────────────────────
echo ">>> Restoring SELinux booleans"
setsebool -P httpd_can_network_connect on 2>/dev/null || true
setsebool -P httpd_setrlimit on 2>/dev/null || true
setenforce 1

# ─── RESTORE NGINX CONFIG ──────────────────────────────────
echo ">>> Restoring nginx config"
cat > /etc/nginx/nginx.conf << 'NGINXEOF'
user nginx;
worker_processes auto;
worker_rlimit_nofile 200000;
error_log /var/log/nginx/error.log;
pid /run/nginx.pid;
worker_cpu_affinity auto;

include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 65536;
}

http {
    reset_timedout_connection on;
    open_file_cache_min_uses 2;
    open_file_cache_valid 30s;
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';

    access_log off;

    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 30;
    keepalive_requests 10000;
    types_hash_max_size 4096;
    client_body_buffer_size 8k;
    client_max_body_size 1m;

    aio off;
    gzip off;

    open_file_cache max=200000 inactive=60s;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    include /etc/nginx/conf.d/*.conf;
}
NGINXEOF

cat > /etc/nginx/conf.d/hackathon.conf << 'SERVEREOF'
server {
    listen 80 default_server backlog=65535;
    listen [::]:80 default_server backlog=65535;
    server_name _;
    root /var/www/nginx;

    location / {
        autoindex on;
    }

    types_hash_max_size 2048;
}
SERVEREOF

# ─── RESTORE THP ───────────────────────────────────────────
echo ">>> Restoring THP"
echo "madvise" > /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || true
echo "defer+madvise" > /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null || true

# ─── RESTORE IRQ BALANCE ───────────────────────────────────
echo ">>> Restoring irqbalance"
systemctl enable irqbalance 2>/dev/null || true
systemctl start irqbalance 2>/dev/null || true

# ─── RESTART NGINX ──────────────────────────────────────────
echo ">>> Validating and restarting nginx"
nginx -t && systemctl restart nginx

echo ""
echo "=========================================="
echo "DUT RESET TO CLEAN STATE — READY"
echo "=========================================="
