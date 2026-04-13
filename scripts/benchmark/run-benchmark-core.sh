#!/bin/bash
# Shared benchmark implementation used by OS-specific wrappers.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_OS="${BENCHMARK_OS:-generic}"
BENCHMARK_PROFILE="${BENCHMARK_PROFILE:-standard}"
BENCHMARK_OUTPUT_MODE="${BENCHMARK_OUTPUT_MODE:-table}"
CONTESTANT_NAME="${1:-anonymous}"
ALL_WORKLOADS=("homepage" "small" "medium" "large" "mixed")

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_header() {
    if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
        echo -e "\n${BLUE}=== $1 ===${NC}\n"
    fi
}

print_success() {
    if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
        echo -e "${GREEN}✓ $1${NC}"
    fi
}

print_warning() {
    if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
        echo -e "${YELLOW}⚠ $1${NC}"
    fi
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

workload_settings() {
    if [ "$BENCHMARK_PROFILE" = "fast" ]; then
        case "$1" in
            homepage) echo "16 1000 6s" ;;
            small)    echo "16 1000 8s" ;;
            medium)   echo "16 300 8s" ;;
            large)    echo "16 100 8s" ;;
            mixed)    echo "16 100 8s" ;;
            *) return 1 ;;
        esac
        return 0
    fi

    case "$1" in
        homepage) echo "16 1000 30s" ;;
        small)    echo "16 1000 60s" ;;
        medium)   echo "16 300 60s" ;;
        large)    echo "16 100 60s" ;;
        mixed)    echo "16 100 60s" ;;
        *) return 1 ;;
    esac
}

iso_timestamp() {
    if command -v python3 >/dev/null 2>&1; then
        python3 - <<'PY'
from datetime import datetime
print(datetime.now().astimezone().isoformat())
PY
        return 0
    fi
    date "+%Y-%m-%dT%H:%M:%S%z"
}

detect_target_host() {
    if [ -n "${TARGET_HOST:-}" ]; then
        return
    fi

    if [ "$BENCHMARK_OS" = "linux" ] && [ -f /root/.hackathon_env ]; then
        # shellcheck disable=SC1091
        source /root/.hackathon_env
    fi

    if [ -z "${TARGET_HOST:-}" ] && [ "$BENCHMARK_OS" = "linux" ]; then
        TARGET_HOST=$(grep -A 10 '^\[test_machines\]' /etc/ansible/hosts 2>/dev/null \
            | grep -v '^\[' | grep -v '^$' | head -1 | awk '{print $2}' | cut -d= -f2)
    fi

    if [ -z "${TARGET_HOST:-}" ]; then
        TARGET_HOST=$(grep 'test-machine' /etc/hosts 2>/dev/null | awk '{print $1}' | head -1)
    fi
}

RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results}"

if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    cat << EOF
Hackathon Performance Benchmark Script

Usage:
    ./benchmark.sh [contestant-name]
    ./benchmark.sh                    # Uses 'anonymous' as name

Environment Variables:
    TARGET_HOST       - IP/hostname of test machine
    RESULTS_DIR       - Results directory (default: ${SCRIPT_DIR}/results)
    BENCHMARK_PROFILE - standard|fast (current: ${BENCHMARK_PROFILE})
    BENCHMARK_OUTPUT_MODE - table|verbose (current: ${BENCHMARK_OUTPUT_MODE})

Resolved Runtime:
    OS profile: ${BENCHMARK_OS}
    Benchmark profile: ${BENCHMARK_PROFILE}

Workload Configuration:
    homepage - 16 threads, 1000 connections
    small    - 16 threads, 1000 connections
    medium   - 16 threads, 300 connections
    large    - 16 threads, 100 connections
    mixed    - 16 threads, 100 connections

Results:
    ${RESULTS_DIR}/<contestant-name>_<workload>.json
EOF
    exit 0
fi

if [ -z "$CONTESTANT_NAME" ]; then
    print_error "Contestant name cannot be empty"
    exit 1
fi
if ! echo "$CONTESTANT_NAME" | grep -qE '^[a-zA-Z0-9_-]+$'; then
    print_error "Contestant name must contain only letters, numbers, dashes, and underscores"
    exit 1
fi

detect_target_host
if [ -z "${TARGET_HOST:-}" ]; then
    print_error "TARGET_HOST not set and could not be auto-detected"
    echo "Set explicitly: TARGET_HOST=192.168.1.10 ./benchmark.sh ${CONTESTANT_NAME}"
    exit 1
fi

print_header "Hackathon Performance Benchmark"
if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
    echo "Contestant: $CONTESTANT_NAME"
    echo "Target: $TARGET_HOST"
    echo "OS profile: $BENCHMARK_OS"
    echo "Benchmark profile: $BENCHMARK_PROFILE"
    echo ""
fi

mkdir -p "$RESULTS_DIR"

if ! command -v wrk >/dev/null 2>&1; then
    print_error "wrk is not installed."
    echo "Install wrk on this host (examples):"
    echo "  Debian/Ubuntu: apt-get update -y && apt-get install -y wrk"
    echo "  RHEL/Fedora:   dnf install -y wrk   (or yum install -y wrk)"
    exit 1
fi

print_header "Connectivity Check"
HTTP_CODE="$(curl -sS -o /dev/null -w "%{http_code}" --connect-timeout 8 "http://${TARGET_HOST}/" || true)"
# Consider 2xx/3xx as reachable; also allow 401/403 for auth-restricted endpoints.
if ! echo "$HTTP_CODE" | grep -Eq "^(2[0-9][0-9]|3[0-9][0-9]|401|403)$"; then
    print_error "Cannot reach target at http://${TARGET_HOST}/ (code=${HTTP_CODE:-none})"
    exit 1
fi
print_success "Target is reachable (HTTP ${HTTP_CODE})"

print_header "Checking Workload Scripts"
for WORKLOAD in "${ALL_WORKLOADS[@]}"; do
    if [ "$WORKLOAD" = "homepage" ]; then
        print_success "homepage - No Lua script needed"
        continue
    fi
    LUA_SCRIPT="${SCRIPT_DIR}/${WORKLOAD}.lua"
    if [ ! -f "$LUA_SCRIPT" ]; then
        print_error "Lua script not found: $LUA_SCRIPT"
        exit 1
    fi
    print_success "Found: ${WORKLOAD}.lua"
done
if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
    echo ""
fi

RPS_RESULTS=()
LATENCY_RESULTS=()
TRANSFER_RESULTS=()
TOTAL_WORKLOADS=${#ALL_WORKLOADS[@]}
CURRENT=0

for WORKLOAD in "${ALL_WORKLOADS[@]}"; do
    CURRENT=$((CURRENT + 1))
    read THREADS CONNECTIONS DURATION <<< "$(workload_settings "$WORKLOAD")"

    print_header "Running Benchmark [$CURRENT/$TOTAL_WORKLOADS]: $WORKLOAD"
    if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
        echo "Configuration: ${THREADS} threads, ${CONNECTIONS} connections, ${DURATION}"
        echo ""
    fi

    TIMESTAMP=$(date +%s)
    RAW_OUTPUT="${RESULTS_DIR}/${CONTESTANT_NAME}_${WORKLOAD}_${TIMESTAMP}_raw.txt"
    JSON_OUTPUT="${RESULTS_DIR}/${CONTESTANT_NAME}_${WORKLOAD}.json"

    if [ "$WORKLOAD" = "homepage" ]; then
        if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
            wrk -t${THREADS} -c${CONNECTIONS} -d${DURATION} --latency "http://${TARGET_HOST}/" | tee "$RAW_OUTPUT"
        else
            wrk -t${THREADS} -c${CONNECTIONS} -d${DURATION} --latency "http://${TARGET_HOST}/" > "$RAW_OUTPUT" 2>&1
        fi
    else
        LUA_SCRIPT="${SCRIPT_DIR}/${WORKLOAD}.lua"
        if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
            wrk -t${THREADS} -c${CONNECTIONS} -d${DURATION} --latency -s "$LUA_SCRIPT" "http://${TARGET_HOST}/" | tee "$RAW_OUTPUT"
        else
            wrk -t${THREADS} -c${CONNECTIONS} -d${DURATION} --latency -s "$LUA_SCRIPT" "http://${TARGET_HOST}/" > "$RAW_OUTPUT" 2>&1
        fi
    fi

    REQ_PER_SEC=$(awk '/^[[:space:]]*Requests\/sec:/{print $2; exit}' "$RAW_OUTPUT")
    LATENCY_AVG=$(awk '/^[[:space:]]*Latency[[:space:]]+[0-9]/{print $2; exit}' "$RAW_OUTPUT")
    TRANSFER_SEC=$(awk '/^[[:space:]]*Transfer\/sec:/{print $2; exit}' "$RAW_OUTPUT")
    if [ -z "$REQ_PER_SEC" ] || [ -z "$LATENCY_AVG" ] || [ -z "$TRANSFER_SEC" ]; then
        print_error "Failed to parse benchmark output for workload '${WORKLOAD}' (raw: $RAW_OUTPUT)"
        exit 1
    fi

    IDX=$((CURRENT - 1))
    RPS_RESULTS[$IDX]="$REQ_PER_SEC"
    LATENCY_RESULTS[$IDX]="$LATENCY_AVG"
    TRANSFER_RESULTS[$IDX]="$TRANSFER_SEC"

    if command -v python3 >/dev/null 2>&1 && [ -f "${SCRIPT_DIR}/parse_wrk_output.py" ]; then
        python3 "${SCRIPT_DIR}/parse_wrk_output.py" "$RAW_OUTPUT" > "${RAW_OUTPUT}.json"
        cat > "$JSON_OUTPUT" << EOF
{
  "contestant": "$CONTESTANT_NAME",
  "workload": "$WORKLOAD",
  "timestamp": "$(iso_timestamp)",
  "timestamp_epoch": $TIMESTAMP,
  "target_host": "$TARGET_HOST",
  "test_config": {
    "workload": "$WORKLOAD",
    "duration": "$DURATION",
    "threads": $THREADS,
    "connections": $CONNECTIONS
  },
  "results": $(cat "${RAW_OUTPUT}.json")
}
EOF
        rm "${RAW_OUTPUT}.json"
        print_success "Results saved to: $JSON_OUTPUT"
    else
        print_warning "Python parser unavailable, raw output only"
        print_success "Raw results saved to: $RAW_OUTPUT"
    fi

    if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
        echo ""
        echo "Workload $WORKLOAD complete:"
        echo "  Requests/sec: $REQ_PER_SEC"
        echo "  Latency (avg): $LATENCY_AVG"
        echo "  Transfer/sec: $TRANSFER_SEC"
        echo ""
    fi

    if [ "$CURRENT" -lt "$TOTAL_WORKLOADS" ]; then
        if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
            echo "Pausing 5 seconds before next workload..."
        fi
        sleep 5
        if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
            echo ""
        fi
    fi
done

if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
    print_header "All Benchmarks Complete - Summary"
fi
printf "%-10s | %12s | %15s | %15s | %15s\n" "Workload" "Config" "Requests/sec" "Latency (avg)" "Transfer/sec"
printf "%s\n" "--------------------------------------------------------------------------------------------"
for IDX in "${!ALL_WORKLOADS[@]}"; do
    WORKLOAD="${ALL_WORKLOADS[$IDX]}"
    read THREADS CONNECTIONS DURATION <<< "$(workload_settings "$WORKLOAD")"
    CONFIG="${THREADS}t,${CONNECTIONS}c,${DURATION}"
    printf "%-10s | %12s | %15s | %15s | %15s\n" \
        "$WORKLOAD" "$CONFIG" "${RPS_RESULTS[$IDX]}" "${LATENCY_RESULTS[$IDX]}" "${TRANSFER_RESULTS[$IDX]}"
done
if [ "$BENCHMARK_OUTPUT_MODE" = "verbose" ]; then
    echo ""
    echo "Results saved to:"
    for WORKLOAD in "${ALL_WORKLOADS[@]}"; do
        echo "  $WORKLOAD: ${RESULTS_DIR}/${CONTESTANT_NAME}_${WORKLOAD}.json"
    done
    echo ""

    if [ -f "${SCRIPT_DIR}/compare-results.sh" ]; then
        echo "For detailed comparison, run:"
        echo "  ${SCRIPT_DIR}/compare-results.sh $CONTESTANT_NAME"
    fi
fi
