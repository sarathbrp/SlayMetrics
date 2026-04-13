#!/bin/bash
# Compare contestant results to baseline across all workloads
# Usage: ./compare-results.sh <contestant-name> [baseline-name]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results}"
COMPARE_SCRIPT="${SCRIPT_DIR}/compare_results.py"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

if [ -z "$1" ]; then
    echo "Usage: $0 <contestant-name> [baseline-name]"
    echo ""
    echo "Examples:"
    echo "  $0 alice                    # Compare alice vs baseline"
    echo "  $0 alice bob                # Compare alice vs bob"
    echo ""
    echo "Available results:"
    ls -1 "$RESULTS_DIR"/*.json 2>/dev/null | sed 's|.*/||' | sed 's|_.*||' | sort -u || echo "  (none yet)"
    exit 1
fi

CONTESTANT_NAME="$1"
BASELINE_NAME="${2:-baseline}"

# All workload types
WORKLOADS=("homepage" "small" "medium" "large" "mixed")

echo -e "${BLUE}=== Performance Comparison ===${NC}"
echo "Contestant: $CONTESTANT_NAME"
echo "Baseline: $BASELINE_NAME"
echo ""

# Check if any files exist
FOUND_ANY=false
for WORKLOAD in "${WORKLOADS[@]}"; do
    BASELINE_FILE="${RESULTS_DIR}/${BASELINE_NAME}_${WORKLOAD}.json"
    CONTESTANT_FILE="${RESULTS_DIR}/${CONTESTANT_NAME}_${WORKLOAD}.json"

    if [ -f "$BASELINE_FILE" ] || [ -f "$CONTESTANT_FILE" ]; then
        FOUND_ANY=true
        break
    fi
done

if [ "$FOUND_ANY" = false ]; then
    echo -e "${RED}Error: No result files found for '$BASELINE_NAME' or '$CONTESTANT_NAME'${NC}"
    echo ""
    echo "Looking for files like:"
    for WORKLOAD in "${WORKLOADS[@]}"; do
        echo "  ${BASELINE_NAME}_${WORKLOAD}.json"
        echo "  ${CONTESTANT_NAME}_${WORKLOAD}.json"
    done
    echo ""
    echo "Available results:"
    ls -1 "$RESULTS_DIR"/*.json 2>/dev/null | sed 's|.*/||' | sed 's|_.*||' | sort -u || echo "  (none yet)"
    exit 1
fi

# Display comparison table header
printf "%-10s | %15s | %15s | %15s | %10s\n" "Workload" "Baseline (rps)" "Current (rps)" "Change" "Status"
echo "--------------------------------------------------------------------------------"

# Compare each workload
for WORKLOAD in "${WORKLOADS[@]}"; do
    BASELINE_FILE="${RESULTS_DIR}/${BASELINE_NAME}_${WORKLOAD}.json"
    CONTESTANT_FILE="${RESULTS_DIR}/${CONTESTANT_NAME}_${WORKLOAD}.json"

    if [ ! -f "$BASELINE_FILE" ]; then
        printf "%-10s | %15s | %15s | %15s | %10s\n" "$WORKLOAD" "N/A" "N/A" "N/A" "MISSING"
        continue
    fi

    if [ ! -f "$CONTESTANT_FILE" ]; then
        BASELINE_RPS=$(grep -o '"per_sec"[[:space:]]*:[[:space:]]*[0-9.]*' "$BASELINE_FILE" | head -1 | awk -F: '{print $2}' | tr -d ' ')
        printf "%-10s | %15s | %15s | %15s | %10s\n" "$WORKLOAD" "$BASELINE_RPS" "N/A" "N/A" "MISSING"
        continue
    fi

    # Extract metrics
    BASELINE_RPS=$(grep -o '"per_sec"[[:space:]]*:[[:space:]]*[0-9.]*' "$BASELINE_FILE" | head -1 | awk -F: '{print $2}' | tr -d ' ')
    CONTESTANT_RPS=$(grep -o '"per_sec"[[:space:]]*:[[:space:]]*[0-9.]*' "$CONTESTANT_FILE" | head -1 | awk -F: '{print $2}' | tr -d ' ')

    if [ -n "$BASELINE_RPS" ] && [ -n "$CONTESTANT_RPS" ]; then
        CHANGE_PCT=$(python3 -c "print(f'{((float($CONTESTANT_RPS) - float($BASELINE_RPS)) / float($BASELINE_RPS) * 100):.1f}')" 2>/dev/null || echo "N/A")

        # Determine status
        if [ "$CHANGE_PCT" != "N/A" ]; then
            if python3 -c "exit(0 if float($CHANGE_PCT) > 10 else 1)" 2>/dev/null; then
                STATUS="${GREEN}IMPROVED${NC}"
            elif python3 -c "exit(0 if float($CHANGE_PCT) < -10 else 1)" 2>/dev/null; then
                STATUS="${RED}DEGRADED${NC}"
            else
                STATUS="${YELLOW}STABLE${NC}"
            fi
            CHANGE_STR="${CHANGE_PCT}%"
        else
            STATUS="UNKNOWN"
            CHANGE_STR="N/A"
        fi

        printf "%-10s | %15.0f | %15.0f | %15s | " "$WORKLOAD" "$BASELINE_RPS" "$CONTESTANT_RPS" "$CHANGE_STR"
        echo -e "$STATUS"
    else
        printf "%-10s | %15s | %15s | %15s | %10s\n" "$WORKLOAD" "${BASELINE_RPS:-N/A}" "${CONTESTANT_RPS:-N/A}" "N/A" "ERROR"
    fi
done

echo ""
echo "Legend:"
echo -e "  ${GREEN}IMPROVED${NC}  - More than 10% improvement"
echo -e "  ${YELLOW}STABLE${NC}   - Within ±10%"
echo -e "  ${RED}DEGRADED${NC} - More than 10% degradation"
echo ""

# Optional: detailed comparison using Python script if available
if [ -f "$COMPARE_SCRIPT" ]; then
    echo "For detailed comparison of a specific workload, run:"
    echo "  python3 $COMPARE_SCRIPT <baseline-file> <contestant-file>"
    echo ""
fi
