#!/bin/bash
# omega_master_audit.sh - 112-Core RHEL 9.7 Performance Source of Truth
# Target: 1.5M RPS RCA Data Collection

CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${CYAN}==================================================================${NC}"
echo -e "${CYAN}      OMEGA MASTER AUDIT: 112-CORE RHEL 9.7 STACK DATA            ${NC}"
echo -e "${CYAN}==================================================================${NC}\n"

# 1. System Context
NGINX_PID=$(pgrep -n nginx)
# Management NIC (default route) — used for general context
NIC_DEV=$(ip -o -4 route show to default | awk '{print $5}')
# Benchmark NIC — VLAN interface carrying test traffic (172.21.x.x subnet)
BENCH_VLAN=$(ip route get 172.21.89.124 2>/dev/null | grep -oP 'dev \K\S+' || \
             ip -o -4 addr show | grep '172\.21\.' | awk '{print $2}' | head -1 || \
             echo "$NIC_DEV")
# TC shaping must be on the parent physical NIC (VLAN interfaces don't support tc)
BENCH_NIC=$(echo "$BENCH_VLAN" | cut -d'.' -f1)
CONF_DUMP=$(nginx -T 2>/dev/null)

# Auto-detect primary block device (prefer NVMe over SATA)
BLOCK_DEV=$(lsblk -dno NAME,TYPE | awk '$2=="disk" {print $1}' | grep -m1 nvme || \
            lsblk -dno NAME,TYPE | awk '$2=="disk" {print $1}' | head -1)

# Helper: Consistent Formatting
fmt_line() { printf "  %-34s | %-40s\n" "$1" "$2"; }

# --- [GROUP 1: HARDWARE, TOPOLOGY & POWER] ---
echo -e "${YELLOW}[1/5] Hardware & Power Topology${NC}"
fmt_line "CPU_Governor" "$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo 'N/A')"
fmt_line "THP_Status" "$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)"
fmt_line "IRQ_Balance_Active" "$(systemctl is-active irqbalance)"
NIC_IRQ=$(grep "$NIC_DEV" /proc/interrupts | head -n1 | awk '{print $1}' | tr -d ':')
fmt_line "NIC_IRQ_Affinity" "$(cat /proc/irq/${NIC_IRQ}/smp_affinity_list 2>/dev/null || echo 'N/A')"
fmt_line "Block_Device" "${BLOCK_DEV}"
fmt_line "Readahead_sectors" "$(blockdev --getra /dev/${BLOCK_DEV} 2>/dev/null || echo 'N/A')"
command -v numastat &>/dev/null && fmt_line "NUMA_Local_Node_Hit" \
    "$(numastat -p $NGINX_PID 2>/dev/null | grep 'local_node' | awk '{print $2}')"

# --- [GROUP 2: KERNEL SYSCTL GATES (THE PIPE)] ---
echo -e "\n${YELLOW}[2/5] Kernel Network Stack (The Pipe)${NC}"
SYS_KNOBS=(
    "net.core.somaxconn" "net.ipv4.tcp_max_syn_backlog" "net.core.netdev_max_backlog"
    "net.core.rmem_max" "net.core.wmem_max" "net.ipv4.tcp_rmem" "net.ipv4.tcp_wmem"
    "net.ipv4.tcp_tw_reuse" "net.ipv4.tcp_fin_timeout" "net.ipv4.tcp_slow_start_after_idle"
    "net.ipv4.tcp_keepalive_time" "net.ipv4.tcp_keepalive_intvl" "net.ipv4.tcp_keepalive_probes"
    "net.ipv4.ip_local_port_range" "net.ipv4.tcp_max_tw_buckets"
    "vm.swappiness" "vm.dirty_ratio" "vm.dirty_background_ratio" "vm.vfs_cache_pressure"
    "net.netfilter.nf_conntrack_max" "net.ipv4.tcp_syncookies"
    "kernel.sched_migration_cost_ns" "kernel.sched_autogroup_enabled"
)
for k in "${SYS_KNOBS[@]}"; do fmt_line "$k" "$(sysctl -n $k 2>/dev/null || echo 'N/A')"; done

# --- [GROUP 3: SYSTEMD & OS LIMITS (THE ENVELOPE)] ---
echo -e "\n${YELLOW}[3/5] Systemd Service Envelope${NC}"
# CPUQuota: use CPUQuotaPerSecUSec (CPUQuota property returns empty on RHEL 9.7)
CPU_QUOTA_US=$(systemctl show nginx.service -p CPUQuotaPerSecUSec | awk -F= '{print $2}')
if [[ "$CPU_QUOTA_US" == "infinity" || -z "$CPU_QUOTA_US" ]]; then
    fmt_line "systemd_CPUQuota" "none (unlimited)"
else
    # Convert µs to % : value may be "150ms" or raw µs
    fmt_line "systemd_CPUQuota" "${CPU_QUOTA_US} ($(systemctl show nginx.service -p CPUQuotaPerSecUSec | awk -F= '{print $2}'))"
fi
for s in "LimitNOFILE" "LimitNPROC" "CPUWeight" "MemoryMax" "IOWeight"; do
    VAL=$(systemctl show nginx.service -p $s | awk -F= '{print $2}')
    fmt_line "systemd_$s" "${VAL:-[not set]}"
done
fmt_line "SELinux_State" "$(getenforce)"
fmt_line "SELinux_httpd_network" "$(getsebool httpd_can_network_connect 2>/dev/null | awk '{print $3}')"
fmt_line "IO_Scheduler" "$(cat /sys/block/${BLOCK_DEV}/queue/scheduler 2>/dev/null || echo 'N/A')"

# --- [GROUP 4: NGINX APPLICATION (THE ENGINE)] ---
echo -e "\n${YELLOW}[4/5] NGINX Internal Directives${NC}"
if [[ -n "$CONF_DUMP" ]]; then
    NG_KNOBS=(
        "worker_processes" "worker_connections" "worker_rlimit_nofile" "worker_cpu_affinity"
        "accept_mutex" "multi_accept" "access_log" "sendfile" "tcp_nopush" "tcp_nodelay"
        "keepalive_timeout" "keepalive_requests" "gzip" "gzip_comp_level" "gzip_min_length"
        "open_file_cache" "limit_rate" "limit_rate_after" "client_body_buffer_size"
        "client_body_timeout" "client_header_timeout" "send_timeout"
        "output_buffers" "aio" "directio"
    )
    for n in "${NG_KNOBS[@]}"; do
        VAL=$(echo "$CONF_DUMP" | grep -E "^\s*$n\s+" | head -n1 | awk '{$1=""; print $0}' | \
              sed 's/^ //;s/;$//')
        fmt_line "nginx_$n" "${VAL:-default}"
    done
    # limit_req and limit_conn may appear in server/location blocks
    LIMIT_REQ=$(echo "$CONF_DUMP" | grep -E "^\s*limit_req\s+" | head -n1 | sed 's/^ *//;s/;$//')
    LIMIT_CONN=$(echo "$CONF_DUMP" | grep -E "^\s*limit_conn\s+" | head -n1 | sed 's/^ *//;s/;$//')
    fmt_line "nginx_limit_req" "${LIMIT_REQ:-default}"
    fmt_line "nginx_limit_conn" "${LIMIT_CONN:-default}"
    ERR_LEVEL=$(echo "$CONF_DUMP" | grep -E "^\s*error_log\s+" | head -n1 | awk '{print $3}' | tr -d ';')
    fmt_line "nginx_error_log_level" "${ERR_LEVEL:-error (default)}"
    fmt_line "nginx_listen_backlog" "$(echo "$CONF_DUMP" | grep -oP "backlog=\K\d+" | head -n1)"
fi

# --- [GROUP 5: NETWORK CHAOS DETECTION] ---
echo -e "\n${YELLOW}[5/5] Traffic Control & Error Telemetry${NC}"

# TC traffic shaping — check BENCHMARK NIC (not management NIC)
fmt_line "TC_Qdisc_State" "$(tc qdisc show dev $BENCH_NIC | head -n1)"
fmt_line "Benchmark_NIC" "$BENCH_NIC"
# Summarise ALL active shaping types and their key params in one field
tc qdisc show dev $BENCH_NIC | while read -r line; do
    case "$line" in
        *htb*)
            RATE=$(tc class show dev $BENCH_NIC 2>/dev/null | grep -oP 'rate \K\S+' | head -1)
            CEIL=$(tc class show dev $BENCH_NIC 2>/dev/null | grep -oP 'ceil \K\S+' | head -1)
            echo "htb rate=${RATE:-?} ceil=${CEIL:-?}" ;;
        *netem*)
            DELAY=$(echo "$line" | grep -oP 'delay \K\S+(\s+\S+)?')
            LOSS=$(echo "$line" | grep -oP 'loss \K\S+')
            echo "netem delay=${DELAY:-0} loss=${LOSS:-0}" ;;
        *tbf*)
            RATE=$(echo "$line" | grep -oP 'rate \K\S+')
            echo "tbf rate=${RATE:-?}" ;;
    esac
done | paste -sd ',' | { read v; fmt_line "TC_Active_Shaping" "${v:-none}"; }
# NIC speed — show benchmark NIC speed for TC comparison
fmt_line "NIC_Speed" "$(ethtool $BENCH_NIC 2>/dev/null | grep -i 'Speed:' | awk '{print $2}' || echo 'N/A')"

# Softnet — use printf to avoid awk integer overflow on large hex values
fmt_line "Softnet_Time_Squeeze" "$(awk '{sum+=strtonum("0x"$3)} END {printf "%d\n", sum}' /proc/net/softnet_stat)"
_TCPEXT=$(grep '^TcpExt:' /proc/net/netstat)
fmt_line "TCP_Listen_Drops" "$(echo "$_TCPEXT" | awk 'NR==1{for(i=1;i<=NF;i++) if($i=="ListenDrops") col=i} NR==2{print $col}')"
fmt_line "TCP_Backlog_Drops" "$(echo "$_TCPEXT" | awk 'NR==1{for(i=1;i<=NF;i++) if($i=="TCPBacklogDrop") col=i} NR==2{print $col}')"

# Conntrack — show current/max and saturation %
CT_CURRENT=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0)
CT_MAX=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 1)
CT_PCT=$(awk "BEGIN {printf \"%.1f\", ($CT_CURRENT/$CT_MAX)*100}")
fmt_line "Conntrack_Current" "$CT_CURRENT"
fmt_line "Conntrack_Max" "$CT_MAX"
fmt_line "Conntrack_Utilization" "${CT_PCT}%"

# iptables — generic: show ALL blocking/throttling actions on port 80
IPT_SAVE=$(iptables -S INPUT 2>/dev/null | grep -- '--dport 80')
fmt_line "IPTables_Port80_Rules" "$(echo "$IPT_SAVE" | grep -c .; true)"
# Summarise each rule's action + key module in one readable line
IPT_ACTIONS=$(echo "$IPT_SAVE" | awk '{
    action=$NF
    mod=""
    if(/connlimit-above/) { match($0,/connlimit-above ([0-9]+)/,a); mod="connlimit>"a[1] }
    else if(/--limit /)   { match($0,/--limit ([^ ]+)/,a); mod="ratelimit="a[1] }
    else if(/--state/)    { mod="state" }
    if(mod!="") print action"("mod")"
    else print action
}' | paste -sd ',' )
fmt_line "IPTables_Port80_Actions" "${IPT_ACTIONS:-none}"

# nftables — generic: show ALL actions on port 80 (drop, limit, reject, accept on meter)
NFT_DUMP=$(nft list ruleset 2>/dev/null)
NFT_PORT80=$(echo "$NFT_DUMP" | grep -A5 'tcp dport 80' | grep -v '^--$')
fmt_line "NFTables_Port80_Rules" "$(echo "$NFT_PORT80" | grep -c .; true)"
# Extract action summary: drop, accept, limit rate X, meter, etc.
NFT_ACTIONS=$(echo "$NFT_PORT80" | grep -oP '(drop|reject|accept|limit rate [^\n;]+|meter [^\n;]+)' | paste -sd ',')
fmt_line "NFTables_Port80_Actions" "${NFT_ACTIONS:-none}"

# Background hog processes (R2: dd, R3: stress-ng)
fmt_line "Stress_Procs" "$(pgrep -c -f 'stress-ng|dd if=/dev/zero' 2>/dev/null; true)"

echo -e "\n${CYAN}================ Audit Complete ====================${NC}"
