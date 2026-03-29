#!/bin/bash
# ============================================================
# Round 4 — Bonus Round
# Scenario: "Production incident — kernel module + nginx proxy
#            misconfiguration + SELinux boolean + corrupt tuned profile"
# Focus: Completely new root causes
# ============================================================

set -euo pipefail

echo "=========================================="
echo "ROUND 4 — PRODUCTION INCIDENT SIMULATION"
echo "=========================================="

# ─── LAYER 1: NGINX — PROXY MISCONFIGURATION ───────────────
echo ""
echo ">>> Layer 1: Nginx proxy/buffering misconfiguration"

cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.pre-round4 2>/dev/null || true

cat > /etc/nginx/nginx.conf << 'NGINXEOF'
user nginx;
worker_processes 16;
worker_rlimit_nofile 8192;
error_log /var/log/nginx/error.log notice;
pid /run/nginx.pid;

include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 4096;
    accept_mutex on;
}

http {
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';

    access_log /var/log/nginx/access.log main;

    sendfile on;
    tcp_nopush off;
    tcp_nodelay off;
    keepalive_timeout 10;
    keepalive_requests 200;
    types_hash_max_size 2048;

    # Severely undersized buffers
    client_body_buffer_size 1k;
    client_max_body_size 1m;
    output_buffers 1 4k;

    aio off;
    gzip off;

    open_file_cache max=5000 inactive=20s;
    open_file_cache_valid 10s;
    open_file_cache_min_uses 3;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    include /etc/nginx/conf.d/*.conf;
}
NGINXEOF

cat > /etc/nginx/conf.d/hackathon.conf << 'SERVEREOF'
server {
    listen 80 default_server backlog=2048;
    listen [::]:80 default_server backlog=2048;
    server_name _;
    root /var/www/nginx;

    location / {
        autoindex on;
        limit_rate_after 512k;
        limit_rate 20m;
    }

    types_hash_max_size 1024;
}
SERVEREOF

nginx -t && systemctl restart nginx
echo "Nginx restarted with Round 4 config"

# ─── LAYER 2: KERNEL — MODERATE MISTUNING ──────────────────
echo ""
echo ">>> Layer 2: Kernel network tuning — moderate misalignment"
sysctl -w net.core.somaxconn=2048
sysctl -w net.ipv4.tcp_max_syn_backlog=2048
sysctl -w net.core.netdev_max_backlog=5000
sysctl -w net.core.rmem_max=2097152
sysctl -w net.core.wmem_max=2097152
sysctl -w net.ipv4.tcp_rmem="4096 87380 2097152"
sysctl -w net.ipv4.tcp_wmem="4096 65536 2097152"
sysctl -w net.ipv4.tcp_tw_reuse=0
sysctl -w net.ipv4.tcp_fin_timeout=60
sysctl -w net.ipv4.tcp_slow_start_after_idle=1
sysctl -w net.ipv4.ip_local_port_range="15000 55000"
sysctl -w vm.swappiness=60
sysctl -w vm.dirty_ratio=10
sysctl -w vm.dirty_background_ratio=5
sysctl -w vm.vfs_cache_pressure=150

# ─── LAYER 3: SELinux BOOLEANS ──────────────────────────────
echo ""
echo ">>> Layer 3: SELinux enforcing + restrictive booleans"
setenforce 1
# These booleans restrict network operations
setsebool -P httpd_can_network_connect off 2>/dev/null || true
setsebool -P httpd_setrlimit off 2>/dev/null || true
echo "SELinux enforcing with restrictive booleans"

# ─── LAYER 4: IRQ MISALIGNMENT ─────────────────────────────
echo ""
echo ">>> Layer 4: IRQ — stop irqbalance, pin to CPUs 0-7"
systemctl stop irqbalance 2>/dev/null || true
for irq in $(grep -E "eth|ens|eno|enp" /proc/interrupts | awk -F: '{print $1}' | tr -d ' '); do
    echo ff > /proc/irq/$irq/smp_affinity 2>/dev/null || true
done
echo "NIC IRQs pinned to CPUs 0-7, irqbalance stopped"

# ─── LAYER 5: TRANSPARENT HUGEPAGES ────────────────────────
echo ""
echo ">>> Layer 5: THP always + compaction"
echo "always" > /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || true
echo "always" > /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null || true
echo "THP always with compaction always"

# ─── LAYER 6: CGROUP — MODERATE WEIGHT ─────────────────────
echo ""
echo ">>> Layer 6: Cgroup CPUWeight=30, IOWeight=20"
systemctl set-property nginx.service CPUWeight=30
systemctl set-property nginx.service IOWeight=20
systemctl daemon-reload
systemctl restart nginx
echo "Nginx CPUWeight=30, IOWeight=20"

# ─── VERIFICATION ──────────────────────────────────────────
echo ""
echo "=========================================="
echo "ROUND 4 — ALL DEGRADATION APPLIED"
echo "=========================================="
echo ""
echo "--- Nginx ---"
grep -E "worker_processes|worker_connections|limit_rate|tcp_nodelay|keepalive" /etc/nginx/nginx.conf /etc/nginx/conf.d/hackathon.conf 2>/dev/null
echo "--- Sysctls ---"
sysctl net.core.somaxconn net.core.rmem_max vm.swappiness vm.vfs_cache_pressure
echo "--- SELinux ---"
getenforce
getsebool httpd_can_network_connect httpd_setrlimit 2>/dev/null || echo "N/A"
echo "--- Cgroup ---"
systemctl show nginx.service | grep -E "CPUWeight|IOWeight"
echo "--- THP ---"
cat /sys/kernel/mm/transparent_hugepage/enabled
echo ""
echo "DUT is now degraded for Round 4."
