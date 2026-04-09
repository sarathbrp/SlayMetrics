#!/bin/bash
# live_audit.sh — Fast single-sample CSV emitter for the LiveSampler background thread.
# Each call outputs ONE row (or the header with --header flag).
# Designed to complete in < 0.5s so it can be called every 2 seconds.
#
# CSV columns:
#   ts, softnet_dropped, softnet_squeezed, rx_discards, rx_errors,
#   tcp_time_wait, tcp_established, tcp_mem_pages,
#   cpu_us, cpu_sy, cpu_wa, ctx_switches,
#   cgroup_throttled_usec, cgroup_nr_throttled

if [[ "$1" == "--header" ]]; then
    echo "ts,softnet_dropped,softnet_squeezed,rx_discards,rx_errors,tcp_time_wait,tcp_established,tcp_mem_pages,cpu_us,cpu_sy,cpu_wa,ctx_switches,cgroup_throttled_usec,cgroup_nr_throttled"
    exit 0
fi

NIC_DEV=$(ip -o -4 route show to default 2>/dev/null | awk '{print $5}' | head -1)
NGINX_PID=$(pgrep -n nginx 2>/dev/null || echo "")

# --- Softnet ---
SOFTNET_DROP=$(awk '{sum+=strtonum("0x"$2)} END {printf "%d", sum}' /proc/net/softnet_stat 2>/dev/null)
SOFTNET_SQZ=$(awk '{sum+=strtonum("0x"$3)} END {printf "%d", sum}' /proc/net/softnet_stat 2>/dev/null)

# --- NIC counters (rx_discards + rx_errors) ---
NIC_DISC=0
NIC_ERR=0
if [[ -n "$NIC_DEV" ]]; then
    NIC_DISC=$(ethtool -S "$NIC_DEV" 2>/dev/null | grep -iE 'discard|drop|miss' | awk -F: '{sum+=int($2)} END {print int(sum)}')
    NIC_ERR=$(ethtool -S "$NIC_DEV" 2>/dev/null | grep -iE 'rx_error|rx_err\b' | awk -F: '{sum+=int($2)} END {print int(sum)}')
fi
NIC_DISC=${NIC_DISC:-0}
NIC_ERR=${NIC_ERR:-0}

# --- TCP state (/proc/net/sockstat) — label-based extraction (field positions vary) ---
_SOCKSTAT=$(grep '^TCP:' /proc/net/sockstat 2>/dev/null)
TCP_TW=$(echo "$_SOCKSTAT"  | awk '{for(i=1;i<=NF;i++){if($i=="tw")  {print $(i+1)}}}')
TCP_EST=$(echo "$_SOCKSTAT" | awk '{for(i=1;i<=NF;i++){if($i=="inuse"){print $(i+1)}}}')
TCP_MEM=$(echo "$_SOCKSTAT" | awk '{for(i=1;i<=NF;i++){if($i=="mem") {print $(i+1)}}}')
TCP_TW=${TCP_TW:-0}; TCP_EST=${TCP_EST:-0}; TCP_MEM=${TCP_MEM:-0}

# --- CPU (vmstat single sample) ---
read -r CPU_US CPU_SY CPU_WA CTX <<< $(vmstat 1 1 2>/dev/null | tail -1 | awk '{print $13,$14,$16,$12}')
CPU_US=${CPU_US:-0}; CPU_SY=${CPU_SY:-0}; CPU_WA=${CPU_WA:-0}; CTX=${CTX:-0}

# --- Cgroup CPU throttle ---
CGROUP_THROTTLED_US=0
CGROUP_NR_THROTTLED=0
if [[ -n "$NGINX_PID" ]]; then
    # Try cgroup v2 first, then v1
    CG_PATH=$(cat /proc/"$NGINX_PID"/cgroup 2>/dev/null | grep -m1 '0::' | cut -d: -f3)
    CPU_STAT_V2="/sys/fs/cgroup${CG_PATH}/cpu.stat"
    CG_PATH_V1=$(cat /proc/"$NGINX_PID"/cgroup 2>/dev/null | grep 'cpu,' | cut -d: -f3)
    CPU_STAT_V1="/sys/fs/cgroup/cpu${CG_PATH_V1}/cpu.stat"

    if [[ -f "$CPU_STAT_V2" ]]; then
        CGROUP_THROTTLED_US=$(grep throttled_usec "$CPU_STAT_V2" 2>/dev/null | awk '{print $2}')
        CGROUP_NR_THROTTLED=$(grep nr_throttled "$CPU_STAT_V2" 2>/dev/null | awk '{print $2}')
    elif [[ -f "$CPU_STAT_V1" ]]; then
        CGROUP_THROTTLED_US=$(grep throttled_time "$CPU_STAT_V1" 2>/dev/null | awk '{print int($2/1000)}')
        CGROUP_NR_THROTTLED=$(grep nr_throttled "$CPU_STAT_V1" 2>/dev/null | awk '{print $2}')
    fi
fi
CGROUP_THROTTLED_US=${CGROUP_THROTTLED_US:-0}
CGROUP_NR_THROTTLED=${CGROUP_NR_THROTTLED:-0}

# --- Output CSV row ---
TS=$(date +%s)
echo "$TS,$SOFTNET_DROP,$SOFTNET_SQZ,$NIC_DISC,$NIC_ERR,$TCP_TW,$TCP_EST,$TCP_MEM,$CPU_US,$CPU_SY,$CPU_WA,$CTX,$CGROUP_THROTTLED_US,$CGROUP_NR_THROTTLED"
