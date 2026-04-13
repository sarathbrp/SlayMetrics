#!/bin/bash
# bootstrap_audit.sh — Lightweight system identity for SRE investigation agent
# Collects just enough context for the investigation agent to plan its strategy.
# Full investigation is done via SSH by the LLM-driven SRE agent.

CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

fmt_line() { printf "  %-34s | %-40s\n" "$1" "$2"; }

echo -e "${CYAN}==================================================================${NC}"
echo -e "${CYAN}      BOOTSTRAP AUDIT: System Identity Snapshot                    ${NC}"
echo -e "${CYAN}==================================================================${NC}\n"

# --- [GROUP 1: HARDWARE & TOPOLOGY] ---
echo -e "${YELLOW}[1/5] Hardware & Power Topology${NC}"
fmt_line "Hostname" "$(hostname)"
fmt_line "OS" "$(cat /etc/redhat-release 2>/dev/null || uname -s)"
fmt_line "Kernel" "$(uname -r)"
fmt_line "CPU_Count" "$(nproc)"
fmt_line "CPU_Model" "$(lscpu | grep 'Model name' | sed 's/.*:\s*//')"
fmt_line "Memory_Total" "$(free -h | awk '/Mem:/ {print $2}')"
fmt_line "NUMA_Nodes" "$(lscpu | grep 'NUMA node(s)' | awk '{print $NF}')"
BLOCK_DEV=$(lsblk -dno NAME,TYPE | awk '$2=="disk" {print $1}' | grep -m1 nvme || \
            lsblk -dno NAME,TYPE | awk '$2=="disk" {print $1}' | head -1)
fmt_line "Block_Device" "$BLOCK_DEV"
NIC_DEV=$(ip -o -4 route show to default | awk '{print $5}')
fmt_line "Primary_NIC" "$NIC_DEV"
fmt_line "NIC_Speed" "$(ethtool $NIC_DEV 2>/dev/null | grep -i 'Speed:' | awk '{print $2}' || echo 'N/A')"

# --- [GROUP 2: KERNEL NETWORK STACK] ---
echo -e "\n${YELLOW}[2/5] Kernel Network Stack (The Pipe)${NC}"
fmt_line "fs.nr_open" "$(sysctl -n fs.nr_open 2>/dev/null)"
fmt_line "fs.file-max" "$(sysctl -n fs.file-max 2>/dev/null)"
fmt_line "net.core.somaxconn" "$(sysctl -n net.core.somaxconn 2>/dev/null)"

# --- [GROUP 3: SYSTEMD SERVICE ENVELOPE] ---
echo -e "\n${YELLOW}[3/5] Systemd Service Envelope${NC}"
fmt_line "nginx_service_active" "$(systemctl is-active nginx.service 2>/dev/null || echo unknown)"
fmt_line "nginx_service_result" "$(systemctl show nginx.service -p Result 2>/dev/null | awk -F= '{print $2}')"
DROPIN_DIR="/etc/systemd/system/nginx.service.d"
DROPIN_FILES=$(ls -1 "$DROPIN_DIR"/*.conf 2>/dev/null | paste -sd ',')
fmt_line "systemd_dropin_files" "${DROPIN_FILES:-none}"
fmt_line "systemd_LimitNOFILE" "$(systemctl show nginx.service -p LimitNOFILE 2>/dev/null | awk -F= '{print $2}')"

# --- [GROUP 4: NGINX APPLICATION] ---
echo -e "\n${YELLOW}[4/5] NGINX Internal Directives${NC}"
if command -v nginx >/dev/null 2>&1; then
    fmt_line "nginx_version" "$(nginx -v 2>&1 | awk -F/ '{print $2}')"
    fmt_line "nginx_binary" "$(which nginx)"
    fmt_line "nginx_runtime" "host"
    CONF_DUMP=$(nginx -T 2>/dev/null)
    WP=$(echo "$CONF_DUMP" | grep -E "^\s*worker_processes\s+" | tail -n1 | awk '{print $2}' | tr -d ';')
    fmt_line "nginx_worker_processes" "${WP:-unknown}"
else
    fmt_line "nginx_binary" "not found"
fi

# --- [GROUP 5: NETWORK PATH] ---
echo -e "\n${YELLOW}[5/5] Traffic Control & Error Telemetry${NC}"
BENCH_NIC=$(echo "$NIC_DEV" | cut -d'.' -f1)
fmt_line "Benchmark_NIC" "$BENCH_NIC"
fmt_line "TC_Qdisc_State" "$(tc qdisc show dev $BENCH_NIC 2>/dev/null | head -n1)"

echo -e "\n${CYAN}================ Bootstrap Complete ====================${NC}"
