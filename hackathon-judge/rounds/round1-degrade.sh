#!/bin/bash
# ============================================================
# Round 1 — Full-Stack Performance Degradation
# Scenario: "Post-migration regression on RHEL 9.7"
# Target: DUT (root@d21-h23-000-r650.rdu2.scalelab.redhat.com)
#
# Run ON the DUT:
#   bash round1-degrade.sh
#
# Or remotely:
#   ssh root@d21-h23-000-r650.rdu2.scalelab.redhat.com < round1-degrade.sh
# ============================================================

set -euo pipefail

echo "=========================================="
echo "ROUND 1 — FULL-STACK DEGRADATION"
echo "=========================================="

# ─── LAYER 1: KERNEL SYSCTLS ───────────────────────────────
echo ""
echo ">>> Layer 1: Kernel sysctl regression"
sysctl -w net.core.somaxconn=128
sysctl -w net.ipv4.tcp_max_syn_backlog=128
sysctl -w net.core.netdev_max_backlog=300
sysctl -w net.core.rmem_max=87380
sysctl -w net.core.wmem_max=87380
sysctl -w net.core.rmem_default=65536
sysctl -w net.core.wmem_default=65536
sysctl -w net.ipv4.tcp_rmem="4096 87380 87380"
sysctl -w net.ipv4.tcp_wmem="4096 65536 65536"
sysctl -w net.ipv4.tcp_tw_reuse=0
sysctl -w net.ipv4.tcp_fin_timeout=120
sysctl -w net.ipv4.tcp_slow_start_after_idle=1
sysctl -w net.ipv4.tcp_max_tw_buckets=2048
sysctl -w net.ipv4.ip_local_port_range="32768 60999"
sysctl -w vm.swappiness=100
sysctl -w vm.dirty_ratio=5
sysctl -w vm.dirty_background_ratio=2
sysctl -w vm.vfs_cache_pressure=200
sysctl -w kernel.sched_migration_cost_ns=5000000 2>/dev/null || true
sysctl -w kernel.sched_autogroup_enabled=1 2>/dev/null || true

# ─── LAYER 2: NGINX CONFIG ─────────────────────────────────
echo ""
echo ">>> Layer 2: Nginx config — worst possible settings"

# Backup originals
cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.pre-round1 2>/dev/null || true
cp /etc/nginx/conf.d/hackathon.conf /etc/nginx/conf.d/hackathon.conf.pre-round1 2>/dev/null || true

cat > /etc/nginx/nginx.conf << 'NGINXEOF'
user nginx;
worker_processes 1;
worker_rlimit_nofile 512;
error_log /var/log/nginx/error.log debug;
pid /run/nginx.pid;

include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 256;
    accept_mutex on;
    multi_accept off;
}

http {
    access_log /var/log/nginx/access.log;

    sendfile off;
    tcp_nopush off;
    tcp_nodelay off;
    keepalive_timeout 5;
    keepalive_requests 10;
    types_hash_max_size 1024;
    client_body_buffer_size 1k;
    client_max_body_size 1m;
    output_buffers 1 4k;
    postpone_output 1460;

    aio off;
    directio off;
    gzip off;

    open_file_cache off;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    include /etc/nginx/conf.d/*.conf;
}
NGINXEOF

cat > /etc/nginx/conf.d/hackathon.conf << 'SERVEREOF'
server {
    listen 80 default_server backlog=128;
    listen [::]:80 default_server backlog=128;
    server_name _;
    root /var/www/nginx;

    location / {
        autoindex on;
    }

    types_hash_max_size 1024;
}
SERVEREOF

nginx -t && systemctl restart nginx
echo "Nginx restarted with degraded config"

# ─── LAYER 3: IRQ AFFINITY ─────────────────────────────────
echo ""
echo ">>> Layer 3: IRQ affinity — pin all NIC interrupts to CPU 0"
systemctl stop irqbalance 2>/dev/null || true
systemctl disable irqbalance 2>/dev/null || true
for irq in $(grep -E "eth|ens|eno|enp" /proc/interrupts | awk -F: '{print $1}' | tr -d ' '); do
    echo 1 > /proc/irq/$irq/smp_affinity 2>/dev/null || true
done
echo "IRQ affinity pinned to CPU 0, irqbalance stopped"

# ─── LAYER 4: CGROUP CPU + MEMORY THROTTLE ─────────────────
echo ""
echo ">>> Layer 4: Cgroup CPU throttle on nginx"
systemctl set-property nginx.service CPUQuota=15%
systemctl set-property nginx.service MemoryMax=256M
systemctl daemon-reload
systemctl restart nginx
echo "Nginx CPU capped at 15%, memory at 256M"

# ─── LAYER 5: FILESYSTEM / PAGE CACHE ──────────────────────
echo ""
echo ">>> Layer 5: Filesystem — drop caches, minimize readahead"
echo 3 > /proc/sys/vm/drop_caches
for dev in /dev/sda /dev/nvme0n1 /dev/nvme0n1p1 /dev/nvme0n1p2 /dev/nvme0n1p3; do
    blockdev --setra 8 "$dev" 2>/dev/null || true
done
echo "Page cache dropped, readahead minimized"

# ─── LAYER 6: I/O SCHEDULER ────────────────────────────────
echo ""
echo ">>> Layer 6: I/O scheduler → mq-deadline"
for sched in /sys/block/sd*/queue/scheduler /sys/block/nvme*/queue/scheduler; do
    echo "mq-deadline" > "$sched" 2>/dev/null || true
done
echo "I/O scheduler set to mq-deadline"

# ─── LAYER 7: ULIMITS VIA SYSTEMD DROP-IN ──────────────────
echo ""
echo ">>> Layer 7: Restrict nginx ulimits"
mkdir -p /etc/systemd/system/nginx.service.d/
cat > /etc/systemd/system/nginx.service.d/limits.conf << 'LIMEOF'
[Service]
LimitNOFILE=512
LimitNPROC=64
LIMEOF
systemctl daemon-reload
systemctl restart nginx
echo "Nginx file descriptor limit=512, nproc=64"

# ─── VERIFICATION ──────────────────────────────────────────
echo ""
echo "=========================================="
echo "ALL 7 LAYERS OF DEGRADATION APPLIED"
echo "=========================================="
echo ""
echo "Verification:"
echo "--- Sysctls ---"
sysctl net.core.somaxconn net.ipv4.tcp_max_syn_backlog net.core.netdev_max_backlog \
       net.core.rmem_max net.core.wmem_max vm.swappiness
echo "--- Nginx workers ---"
grep worker_processes /etc/nginx/nginx.conf
grep worker_connections /etc/nginx/nginx.conf
echo "--- Nginx features ---"
grep -E "sendfile|tcp_nopush|tcp_nodelay|open_file_cache|access_log|error_log" /etc/nginx/nginx.conf
echo "--- Cgroup limits ---"
systemctl show nginx.service | grep -E "CPUQuota|MemoryMax"
echo "--- Ulimits ---"
cat /etc/systemd/system/nginx.service.d/limits.conf
echo "--- IRQbalance ---"
systemctl is-active irqbalance 2>/dev/null || echo "irqbalance: stopped"
echo ""
echo "DUT is now fully degraded. Ready for agent test."
