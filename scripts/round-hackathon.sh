#!/bin/bash
# ============================================================
# SlayMetrics Test Degradation Script
# Applies multi-layer performance sabotage for agent testing.
#
# Run ON the DUT:
#   bash round-hackathon.sh
#
# Or remotely:
#   ssh root@<DUT> < round-hackathon.sh
#
# Reset to clean state:
#   bash round-hackathon.sh --reset
# ============================================================

set -euo pipefail

CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

BACKUP_DIR="/root/.hackathon-backup"

# ─── RESET MODE ───────────────────────────────────────────────
if [[ "${1:-}" == "--reset" ]]; then
    echo -e "${GREEN}=========================================="
    echo "RESETTING TO CLEAN STATE"
    echo -e "==========================================${NC}"

    # Restore nginx configs
    if [[ -f "$BACKUP_DIR/nginx.conf" ]]; then
        cp "$BACKUP_DIR/nginx.conf" /etc/nginx/nginx.conf
        echo "Restored nginx.conf"
    fi
    if [[ -f "$BACKUP_DIR/hackathon.conf" ]]; then
        cp "$BACKUP_DIR/hackathon.conf" /etc/nginx/conf.d/hackathon.conf
        echo "Restored hackathon.conf"
    fi

    # Remove all systemd drop-ins we created
    rm -f /etc/systemd/system/nginx.service.d/hackathon_degrade.conf
    rm -f /etc/systemd/system/nginx.service.d/zz_hosttune_limitnofile.conf
    rm -f /etc/systemd/system/nginx.service.d/zz_hosttune_limitnproc.conf

    # Reset systemd runtime properties
    systemctl set-property nginx.service CPUQuota= 2>/dev/null || true
    systemctl set-property nginx.service MemoryMax=infinity 2>/dev/null || true

    # Restore kernel params
    if [[ -f "$BACKUP_DIR/sysctl.conf" ]]; then
        sysctl -p "$BACKUP_DIR/sysctl.conf" 2>/dev/null
        echo "Restored sysctl values"
    fi

    # Restore fs limits
    sysctl -w fs.nr_open=1048576 2>/dev/null
    sysctl -w fs.file-max=1048576 2>/dev/null

    # Restore irqbalance
    systemctl enable irqbalance 2>/dev/null || true
    systemctl start irqbalance 2>/dev/null || true

    # Restore I/O scheduler for NVMe
    for sched in /sys/block/nvme*/queue/scheduler; do
        echo "none" > "$sched" 2>/dev/null || true
    done

    # Restore readahead
    for dev in /dev/nvme0n1 /dev/nvme0n1p1 /dev/nvme0n1p2 /dev/nvme0n1p3; do
        blockdev --setra 256 "$dev" 2>/dev/null || true
    done

    systemctl daemon-reload
    systemctl restart nginx
    echo -e "${GREEN}Reset complete. nginx restarted.${NC}"
    exit 0
fi

# ─── BACKUP CURRENT STATE ─────────────────────────────────────
echo -e "${CYAN}=========================================="
echo "SLAYMETRICS TEST DEGRADATION"
echo -e "==========================================${NC}"
echo ""
echo "Backing up current state..."
mkdir -p "$BACKUP_DIR"

# Remove any zz_ overrides from previous agent runs
rm -f /etc/systemd/system/nginx.service.d/zz_hosttune_*.conf 2>/dev/null
echo "Cleared previous agent overrides (zz_hosttune_*.conf)"
cp /etc/nginx/nginx.conf "$BACKUP_DIR/nginx.conf" 2>/dev/null || true
cp /etc/nginx/conf.d/hackathon.conf "$BACKUP_DIR/hackathon.conf" 2>/dev/null || true

# Save current sysctl values for reset
cat > "$BACKUP_DIR/sysctl.conf" << 'SYSEOF'
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.core.netdev_max_backlog = 20000
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.rmem_default = 212992
net.core.wmem_default = 212992
net.ipv4.tcp_rmem = 4096 131072 6291456
net.ipv4.tcp_wmem = 4096 16384 4194304
net.ipv4.tcp_tw_reuse = 2
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_max_tw_buckets = 262144
net.ipv4.ip_local_port_range = 1024 65535
vm.swappiness = 10
vm.dirty_ratio = 20
vm.dirty_background_ratio = 10
vm.vfs_cache_pressure = 50
SYSEOF

# ─── LAYER 1: KERNEL SYSCTLS ──────────────────────────────────
echo ""
echo -e "${YELLOW}>>> Layer 1: Kernel sysctl degradation${NC}"
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
echo "  Applied 17 degraded sysctl values"

# ─── LAYER 2: NGINX CONFIG ────────────────────────────────────
echo ""
echo -e "${YELLOW}>>> Layer 2: Nginx config — worst possible settings${NC}"

cat > /etc/nginx/nginx.conf << 'NGINXEOF'
user nginx;
worker_processes 1;
worker_rlimit_nofile 512;
error_log /var/log/nginx/error.log warn;
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
echo "  Nginx restarted with degraded config"

# ─── LAYER 3: IRQ AFFINITY ────────────────────────────────────
echo ""
echo -e "${YELLOW}>>> Layer 3: IRQ affinity — pin all NIC IRQs to CPU 0${NC}"
systemctl stop irqbalance 2>/dev/null || true
systemctl disable irqbalance 2>/dev/null || true
for irq in $(grep -E "eth|ens|eno|enp" /proc/interrupts | awk -F: '{print $1}' | tr -d ' '); do
    echo 1 > /proc/irq/$irq/smp_affinity 2>/dev/null || true
done
echo "  IRQ affinity pinned to CPU 0, irqbalance stopped"

# ─── LAYER 4: CGROUP CPU + MEMORY THROTTLE ────────────────────
echo ""
echo -e "${YELLOW}>>> Layer 4: Cgroup CPU/memory throttle on nginx${NC}"
systemctl set-property nginx.service CPUQuota=15%
systemctl set-property nginx.service MemoryMax=256M
systemctl daemon-reload
systemctl restart nginx
echo "  Nginx CPU capped at 15%, memory at 256M"

# ─── LAYER 5: FILESYSTEM / PAGE CACHE ─────────────────────────
echo ""
echo -e "${YELLOW}>>> Layer 5: Filesystem — drop caches, minimize readahead${NC}"
echo 3 > /proc/sys/vm/drop_caches
for dev in /dev/sda /dev/nvme0n1 /dev/nvme0n1p1 /dev/nvme0n1p2 /dev/nvme0n1p3; do
    blockdev --setra 8 "$dev" 2>/dev/null || true
done
echo "  Page cache dropped, readahead = 8 sectors"

# ─── LAYER 6: I/O SCHEDULER ───────────────────────────────────
echo ""
echo -e "${YELLOW}>>> Layer 6: I/O scheduler → mq-deadline (suboptimal for NVMe)${NC}"
for sched in /sys/block/sd*/queue/scheduler /sys/block/nvme*/queue/scheduler; do
    echo "mq-deadline" > "$sched" 2>/dev/null || true
done
echo "  I/O scheduler forced to mq-deadline"

# ─── LAYER 7: SYSTEMD DROP-IN SABOTAGE ────────────────────────
echo ""
echo -e "${YELLOW}>>> Layer 7: Systemd drop-in — crippling resource limits${NC}"
mkdir -p /etc/systemd/system/nginx.service.d/

cat > /etc/systemd/system/nginx.service.d/hackathon_degrade.conf << 'LIMEOF'
[Service]
LimitNOFILE=512
LimitNPROC=64
Nice=19
CPUWeight=10
IOWeight=10
TasksMax=100
OOMScoreAdjust=500
LIMEOF

systemctl daemon-reload
systemctl restart nginx
echo "  Nginx LimitNOFILE=512, LimitNPROC=64, Nice=19, CPUWeight=10"

# ─── LAYER 8: fs.nr_open TRAP ─────────────────────────────────
echo ""
echo -e "${YELLOW}>>> Layer 8: fs.nr_open trap — kernel ceiling for file descriptors${NC}"
sysctl -w fs.nr_open=65536
sysctl -w fs.file-max=65536
echo "  fs.nr_open=65536, fs.file-max=65536 (will block LimitNOFILE > 65536)"

# ─── VERIFICATION ─────────────────────────────────────────────
echo ""
echo -e "${RED}=========================================="
echo "ALL 8 LAYERS OF DEGRADATION APPLIED"
echo -e "==========================================${NC}"
echo ""
echo "Verification:"
echo "--- Sysctls ---"
sysctl net.core.somaxconn net.ipv4.tcp_max_syn_backlog net.core.netdev_max_backlog \
       net.core.rmem_max net.core.wmem_max vm.swappiness fs.nr_open fs.file-max
echo "--- Nginx workers ---"
grep worker_processes /etc/nginx/nginx.conf
grep worker_connections /etc/nginx/nginx.conf
grep worker_rlimit_nofile /etc/nginx/nginx.conf
echo "--- Nginx features ---"
grep -E "sendfile|tcp_nopush|tcp_nodelay|open_file_cache|access_log" /etc/nginx/nginx.conf
echo "--- Listen backlog ---"
grep backlog /etc/nginx/conf.d/hackathon.conf
echo "--- Cgroup limits ---"
systemctl show nginx.service -p CPUQuotaPerSecUSec -p MemoryMax
echo "--- Systemd drop-in ---"
cat /etc/systemd/system/nginx.service.d/hackathon_degrade.conf
echo "--- IRQbalance ---"
systemctl is-active irqbalance 2>/dev/null || echo "irqbalance: stopped"
echo "--- Nginx status ---"
systemctl is-active nginx 2>/dev/null || echo "nginx: NOT RUNNING"
echo ""
echo -e "${RED}DUT is now fully degraded. Ready for agent test.${NC}"
echo -e "To reset: ${GREEN}bash $0 --reset${NC}"
