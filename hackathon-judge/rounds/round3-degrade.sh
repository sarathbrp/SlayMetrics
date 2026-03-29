#!/bin/bash
# ============================================================
# Round 3 — Degradation Scenario
# Scenario: "Stealth bottlenecks — SELinux context + tc traffic
#            shaping + memory pressure + nginx security hardening
#            gone wrong"
# Focus: Entirely different root causes from Rounds 1 & 2
# Target: DUT (root@d21-h23-000-r650.rdu2.scalelab.redhat.com)
# ============================================================

set -euo pipefail

echo "=========================================="
echo "ROUND 3 — STEALTH DEGRADATION"
echo "=========================================="

# ─── LAYER 1: NGINX — SECURITY HARDENING GONE WRONG ────────
echo ""
echo ">>> Layer 1: Nginx security hardening that kills performance"

cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.pre-round3 2>/dev/null || true
cp /etc/nginx/conf.d/hackathon.conf /etc/nginx/conf.d/hackathon.conf.pre-round3 2>/dev/null || true

cat > /etc/nginx/nginx.conf << 'NGINXEOF'
user nginx;
worker_processes 8;
worker_rlimit_nofile 4096;
error_log /var/log/nginx/error.log info;
pid /run/nginx.pid;

include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 2048;
    accept_mutex on;
    multi_accept off;
}

http {
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';

    access_log /var/log/nginx/access.log main;

    sendfile on;
    tcp_nopush on;
    tcp_nodelay off;
    keepalive_timeout 5;
    keepalive_requests 50;
    types_hash_max_size 2048;
    client_body_buffer_size 1k;
    client_max_body_size 1m;
    client_body_timeout 5;
    client_header_timeout 5;
    send_timeout 5;

    # "Security" buffer limits that kill large file throughput
    output_buffers 1 4k;
    postpone_output 1460;

    aio off;
    gzip off;

    open_file_cache max=1000 inactive=10s;
    open_file_cache_valid 5s;
    open_file_cache_min_uses 5;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    include /etc/nginx/conf.d/*.conf;
}
NGINXEOF

cat > /etc/nginx/conf.d/hackathon.conf << 'SERVEREOF'
limit_req_zone $binary_remote_addr zone=perip:1m rate=500r/s;
limit_conn_zone $binary_remote_addr zone=connperip:1m;

server {
    listen 80 default_server backlog=1024;
    listen [::]:80 default_server backlog=1024;
    server_name _;
    root /var/www/nginx;

    location / {
        autoindex on;
        limit_req zone=perip burst=100 nodelay;
        limit_conn connperip 50;
        limit_rate_after 1m;
        limit_rate 10m;
    }

    types_hash_max_size 1024;
}
SERVEREOF

nginx -t && systemctl restart nginx
echo "Nginx restarted with security-hardened config"

# ─── LAYER 2: TC TRAFFIC SHAPING — BANDWIDTH CAP ───────────
echo ""
echo ">>> Layer 2: tc traffic shaping — cap bandwidth on NIC"
# Find the primary NIC
PRIMARY_NIC=$(ip route get 8.8.8.8 2>/dev/null | grep -oP 'dev \K\S+' || echo "eno8303")
# Delete existing qdisc
tc qdisc del dev "$PRIMARY_NIC" root 2>/dev/null || true
# Add hierarchical token bucket with 5Gbps cap (on a 25Gbps NIC)
tc qdisc add dev "$PRIMARY_NIC" root handle 1: htb default 10
tc class add dev "$PRIMARY_NIC" parent 1: classid 1:10 htb rate 5gbit ceil 5gbit
# Add latency via netem on a child qdisc
tc qdisc add dev "$PRIMARY_NIC" parent 1:10 handle 10: netem delay 500us 200us
echo "Traffic shaped: 5Gbps cap + 500us latency on $PRIMARY_NIC"

# ─── LAYER 3: MEMORY PRESSURE — CONSUME 80% RAM ────────────
echo ""
echo ">>> Layer 3: Memory pressure — consuming bulk of available RAM"
# Moderate memory pressure — enough to stress but not kill SSH
if command -v stress-ng &>/dev/null; then
    nohup stress-ng --vm 2 --vm-bytes 30G --vm-keep --vm-hang 0 --timeout 0 &>/dev/null &
    echo $! > /tmp/memory_pressure_pid
    echo "stress-ng consuming ~60G RAM"
elif command -v tail &>/dev/null; then
    mount -t tmpfs -o size=100G tmpfs /dev/shm 2>/dev/null || true
    nohup dd if=/dev/zero of=/dev/shm/pressure bs=1G count=80 2>/dev/null &
    echo $! > /tmp/memory_pressure_pid
    echo "tmpfs pressure: 80G allocation"
fi

# ─── LAYER 4: KERNEL — SUBTLE NETWORK MISTUNING ────────────
echo ""
echo ">>> Layer 4: Kernel network — subtle but impactful mistuning"
sysctl -w net.core.somaxconn=1024
sysctl -w net.ipv4.tcp_max_syn_backlog=1024
sysctl -w net.core.netdev_max_backlog=2000
sysctl -w net.ipv4.tcp_tw_reuse=0
sysctl -w net.ipv4.tcp_fin_timeout=60
sysctl -w net.ipv4.tcp_slow_start_after_idle=1
sysctl -w net.core.rmem_max=1048576
sysctl -w net.core.wmem_max=1048576
sysctl -w net.ipv4.tcp_rmem="4096 87380 1048576"
sysctl -w net.ipv4.tcp_wmem="4096 65536 1048576"
sysctl -w net.ipv4.ip_local_port_range="10000 60000"
# Enable SYN cookies (adds CPU overhead under load)
sysctl -w net.ipv4.tcp_syncookies=1
# Reduce orphan retries
sysctl -w net.ipv4.tcp_orphan_retries=1
sysctl -w vm.swappiness=80

# ─── LAYER 5: CGROUP — SUBTLE CPU + I/O LIMIT ──────────────
echo ""
echo ">>> Layer 5: Cgroup I/O weight limit on nginx"
systemctl set-property nginx.service IOWeight=10
systemctl set-property nginx.service CPUWeight=50
systemctl daemon-reload
systemctl restart nginx
echo "Nginx IOWeight=10 (lowest), CPUWeight=50 (below default 100)"

# ─── LAYER 6: FILESYSTEM — NOATIME REMOVED + SYNC MOUNT ────
echo ""
echo ">>> Layer 6: Filesystem — drop caches + aggressive writeback"
echo 3 > /proc/sys/vm/drop_caches
sysctl -w vm.dirty_ratio=3
sysctl -w vm.dirty_background_ratio=1
sysctl -w vm.dirty_expire_centisecs=50
sysctl -w vm.dirty_writeback_centisecs=10
sysctl -w vm.vfs_cache_pressure=500
echo "Page cache dropped, aggressive dirty writeback, high vfs_cache_pressure"

# ─── LAYER 7: IRQ — STOP IRQBALANCE + PIN TO 4 CPUS ────────
echo ""
echo ">>> Layer 7: IRQ — stop irqbalance, pin to CPUs 0-3"
systemctl stop irqbalance 2>/dev/null || true
for irq in $(grep -E "eth|ens|eno|enp" /proc/interrupts | awk -F: '{print $1}' | tr -d ' '); do
    echo f > /proc/irq/$irq/smp_affinity 2>/dev/null || true
done
echo "NIC IRQs pinned to CPUs 0-3, irqbalance stopped"

# ─── VERIFICATION ──────────────────────────────────────────
echo ""
echo "=========================================="
echo "ROUND 3 — ALL DEGRADATION APPLIED"
echo "=========================================="
echo ""
echo "Verification:"
echo "--- Nginx ---"
grep -E "worker_processes|worker_connections|limit_rate|limit_req|limit_conn|tcp_nodelay|keepalive" /etc/nginx/nginx.conf /etc/nginx/conf.d/hackathon.conf 2>/dev/null
echo "--- TC ---"
tc qdisc show dev "$PRIMARY_NIC" 2>/dev/null
echo "--- Sysctls ---"
sysctl net.core.somaxconn net.core.rmem_max vm.swappiness vm.vfs_cache_pressure
echo "--- Cgroup ---"
systemctl show nginx.service | grep -E "IOWeight|CPUWeight"
echo "--- Memory ---"
free -h | head -2
echo "--- IRQbalance ---"
systemctl is-active irqbalance 2>/dev/null || echo "irqbalance: stopped"
echo ""
echo "DUT is now degraded for Round 3. Ready for agent test."
