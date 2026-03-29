#!/bin/bash
# ============================================================
# Round 2 — Degradation Scenario
# Scenario: "NUMA misalignment + iptables rate limiting +
#            nginx misconfiguration + disk I/O contention"
# Focus: Different root causes than Round 1
# Target: DUT (root@d21-h23-000-r650.rdu2.scalelab.redhat.com)
# ============================================================

set -euo pipefail

echo "=========================================="
echo "ROUND 2 — DEGRADATION"
echo "=========================================="

# ─── LAYER 1: NGINX — SUBTLE MISCONFIGURATIONS ─────────────
echo ""
echo ">>> Layer 1: Nginx subtle misconfigurations"

cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.pre-round2 2>/dev/null || true
cp /etc/nginx/conf.d/hackathon.conf /etc/nginx/conf.d/hackathon.conf.pre-round2 2>/dev/null || true

cat > /etc/nginx/nginx.conf << 'NGINXEOF'
user nginx;
worker_processes 4;
worker_rlimit_nofile 1024;
error_log /var/log/nginx/error.log warn;
pid /run/nginx.pid;

include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 1024;
    accept_mutex on;
    multi_accept off;
}

http {
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';

    access_log /var/log/nginx/access.log main buffer=4k flush=1s;

    sendfile on;
    tcp_nopush off;
    tcp_nodelay off;
    keepalive_timeout 65;
    keepalive_requests 100;
    types_hash_max_size 2048;
    client_body_buffer_size 8k;
    client_max_body_size 1m;

    aio threads;
    directio 512;

    gzip on;
    gzip_min_length 0;
    gzip_comp_level 9;
    gzip_types text/plain text/html text/css application/octet-stream;

    open_file_cache off;

    proxy_buffering on;
    proxy_buffer_size 4k;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    include /etc/nginx/conf.d/*.conf;
}
NGINXEOF

cat > /etc/nginx/conf.d/hackathon.conf << 'SERVEREOF'
server {
    listen 80 default_server backlog=512 deferred;
    listen [::]:80 default_server backlog=512;
    server_name _;
    root /var/www/nginx;

    location / {
        autoindex on;
        limit_rate 50m;
        output_buffers 1 4k;
    }

    types_hash_max_size 1024;
}
SERVEREOF

nginx -t && systemctl restart nginx
echo "Nginx restarted with Round 2 config"

# ─── LAYER 2: IPTABLES CONNTRACK + RATE LIMITING ───────────
echo ""
echo ">>> Layer 2: iptables conntrack overhead + connection limiting"
# Load conntrack module (adds per-packet overhead)
modprobe nf_conntrack 2>/dev/null || true
# Set very low conntrack table size (causes drops under load)
sysctl -w net.netfilter.nf_conntrack_max=8192 2>/dev/null || true
sysctl -w net.netfilter.nf_conntrack_buckets=2048 2>/dev/null || true
# Add iptables rules that force conntrack on all traffic
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -p tcp --dport 80 -m connlimit --connlimit-above 200 --connlimit-mask 0 -j DROP
iptables -A INPUT -p tcp --dport 80 -j ACCEPT
echo "iptables conntrack + connlimit rules applied"

# ─── LAYER 3: DISK I/O CONTENTION ──────────────────────────
echo ""
echo ">>> Layer 3: Disk I/O contention via continuous background writes"
# Start a background dd process that creates constant I/O pressure
nohup bash -c 'while true; do dd if=/dev/zero of=/tmp/io_pressure bs=1M count=512 oflag=direct conv=fdatasync 2>/dev/null; rm -f /tmp/io_pressure; done' &>/dev/null &
echo $! > /tmp/io_pressure_pid
echo "Background I/O pressure started (PID: $(cat /tmp/io_pressure_pid))"

# ─── LAYER 4: KERNEL — NETWORK STACK THROTTLING ────────────
echo ""
echo ">>> Layer 4: Kernel network stack throttling"
sysctl -w net.core.somaxconn=512
sysctl -w net.ipv4.tcp_max_syn_backlog=512
sysctl -w net.core.netdev_max_backlog=1000
sysctl -w net.ipv4.tcp_tw_reuse=0
sysctl -w net.ipv4.tcp_fin_timeout=90
sysctl -w net.ipv4.tcp_keepalive_time=7200
sysctl -w net.ipv4.tcp_keepalive_intvl=75
sysctl -w net.ipv4.tcp_keepalive_probes=9
sysctl -w net.ipv4.tcp_slow_start_after_idle=1
sysctl -w net.ipv4.tcp_max_orphans=2048
sysctl -w net.core.rmem_max=524288
sysctl -w net.core.wmem_max=524288
sysctl -w net.ipv4.tcp_rmem="4096 87380 524288"
sysctl -w net.ipv4.tcp_wmem="4096 65536 524288"
sysctl -w vm.swappiness=60
sysctl -w vm.dirty_expire_centisecs=100
sysctl -w vm.dirty_writeback_centisecs=50

# ─── LAYER 5: CPU FREQUENCY SCALING — POWERSAVE ────────────
echo ""
echo ">>> Layer 5: CPU governor to powersave (if available)"
for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo "powersave" > "$gov" 2>/dev/null || true
done
echo "CPU governor set to powersave (where possible)"

# ─── LAYER 6: TRANSPARENT HUGEPAGES + NUMA INTERLEAVE ──────
echo ""
echo ">>> Layer 6: THP always + NUMA interleave memory policy"
echo "always" > /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || true
echo "always" > /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null || true
# Force NUMA interleave on nginx (restart with numactl if available)
if command -v numactl &>/dev/null; then
    systemctl stop nginx
    # Override nginx start with interleave policy (worst for locality)
    mkdir -p /etc/systemd/system/nginx.service.d/
    cat > /etc/systemd/system/nginx.service.d/numa.conf << 'NUMAEOF'
[Service]
ExecStart=
ExecStart=/usr/bin/numactl --interleave=all /usr/sbin/nginx -g 'daemon off;'
Type=simple
NUMAEOF
    systemctl daemon-reload
    systemctl start nginx
    echo "Nginx restarted with NUMA interleave policy"
else
    echo "numactl not available, skipping NUMA degradation"
fi

# ─── LAYER 7: IRQ — PIN TO SINGLE SOCKET ───────────────────
echo ""
echo ">>> Layer 7: IRQ affinity — pin to first 2 CPUs only"
systemctl stop irqbalance 2>/dev/null || true
for irq in $(grep -E "eth|ens|eno|enp" /proc/interrupts | awk -F: '{print $1}' | tr -d ' '); do
    echo 3 > /proc/irq/$irq/smp_affinity 2>/dev/null || true
done
echo "NIC IRQs pinned to CPUs 0-1, irqbalance stopped"

# ─── VERIFICATION ──────────────────────────────────────────
echo ""
echo "=========================================="
echo "ROUND 2 — ALL DEGRADATION APPLIED"
echo "=========================================="
echo ""
echo "Verification:"
echo "--- Nginx ---"
grep -E "worker_processes|worker_connections|aio|gzip|limit_rate|sendfile|tcp_nopush|tcp_nodelay" /etc/nginx/nginx.conf /etc/nginx/conf.d/hackathon.conf 2>/dev/null
echo "--- Sysctls ---"
sysctl net.core.somaxconn net.ipv4.tcp_max_syn_backlog net.core.rmem_max net.core.wmem_max vm.swappiness
echo "--- Conntrack ---"
sysctl net.netfilter.nf_conntrack_max 2>/dev/null || echo "conntrack: not loaded"
echo "--- iptables ---"
iptables -L INPUT -n --line-numbers 2>/dev/null | head -10
echo "--- I/O pressure ---"
cat /tmp/io_pressure_pid 2>/dev/null && echo " (running)" || echo "not running"
echo "--- CPU governor ---"
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "N/A"
echo "--- THP ---"
cat /sys/kernel/mm/transparent_hugepage/enabled
echo ""
echo "DUT is now degraded for Round 2. Ready for agent test."
