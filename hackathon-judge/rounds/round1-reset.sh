#!/bin/bash
# ============================================================
# Round 1 — Reset DUT to Clean State
# Target: DUT (root@d21-h23-000-r650.rdu2.scalelab.redhat.com)
#
# Run ON the DUT:
#   bash round1-reset.sh
#
# Or remotely:
#   ssh root@d21-h23-000-r650.rdu2.scalelab.redhat.com < round1-reset.sh
# ============================================================

set -euo pipefail

echo "=========================================="
echo "RESETTING DUT TO CLEAN STATE"
echo "=========================================="

# ─── REMOVE SYSTEMD DROP-INS ───────────────────────────────
echo ">>> Removing systemd drop-ins"
rm -rf /etc/systemd/system/nginx.service.d/
systemctl set-property nginx.service CPUQuota= 2>/dev/null || true
systemctl set-property nginx.service MemoryMax= 2>/dev/null || true
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
sysctl -w kernel.sched_migration_cost_ns=500000 2>/dev/null || true
sysctl -w kernel.sched_autogroup_enabled=0 2>/dev/null || true

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

# ─── RESTORE IRQ BALANCE ───────────────────────────────────
echo ">>> Restoring irqbalance"
systemctl enable irqbalance 2>/dev/null || true
systemctl start irqbalance 2>/dev/null || true

# ─── RESTORE READAHEAD ─────────────────────────────────────
echo ">>> Restoring readahead"
for dev in /dev/sda /dev/nvme0n1 /dev/nvme0n1p1 /dev/nvme0n1p2 /dev/nvme0n1p3; do
    blockdev --setra 256 "$dev" 2>/dev/null || true
done

# ─── RESTORE I/O SCHEDULER ─────────────────────────────────
echo ">>> Restoring I/O scheduler to none (passthrough)"
for sched in /sys/block/sd*/queue/scheduler /sys/block/nvme*/queue/scheduler; do
    echo "none" > "$sched" 2>/dev/null || true
done

# ─── RESTART NGINX ──────────────────────────────────────────
echo ">>> Validating and restarting nginx"
nginx -t && systemctl restart nginx

echo ""
echo "=========================================="
echo "DUT RESET TO CLEAN STATE — READY"
echo "=========================================="
