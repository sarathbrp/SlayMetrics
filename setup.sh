#!/bin/bash
# SlayMetricsAgent — Bench Node Setup
# Run this on System 2 (benchmarking node) where the agent runs.
# DUT (System 1) with nginx is provided separately.
#
# Usage:
#   chmod +x setup.sh
#   sudo ./setup.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$INSTALL_DIR/venv"
SQLITE_DB_PATH="${SQLITE_DB_PATH:-$INSTALL_DIR/data/slaymetrics.db}"
SQLITE_DB_DIR="$(dirname "$SQLITE_DB_PATH")"

log()  { echo -e "${GREEN}[setup]${NC} $1"; }
warn() { echo -e "${YELLOW}[warn]${NC}  $1"; }
err()  { echo -e "${RED}[error]${NC} $1"; exit 1; }
ok()   { echo -e "  ${GREEN}OK${NC}    $1"; }
miss() { echo -e "  ${RED}MISSING${NC} $1"; }

# ── Check root ───────────────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    err "Please run as root: sudo ./setup.sh"
fi

# ── Detect package manager ───────────────────────────────────────────────────
if command -v dnf &>/dev/null; then
    PKG="dnf"
elif command -v yum &>/dev/null; then
    PKG="yum"
else
    err "Neither dnf nor yum found. Is this RHEL/CentOS?"
fi

# ══════════════════════════════════════════════════════════════════════════════
# SCAN — Check what's present and what's missing
# ══════════════════════════════════════════════════════════════════════════════

echo ""
echo -e "${BOLD}SlayMetricsAgent — Bench Node Setup${NC}"
echo -e "${DIM}This sets up System 2 (benchmarking node) where the agent runs.${NC}"
echo -e "${DIM}DUT (System 1 with nginx) is configured separately via .env${NC}"
echo ""

MISSING=()
PRESENT=()

# ── OS Info ──────────────────────────────────────────────────────────────────
echo -e "${CYAN}System:${NC}"
if [ -f /etc/redhat-release ]; then
    ok "$(cat /etc/redhat-release)"
else
    ok "$(uname -sr)"
fi
ok "$(nproc) CPU cores, $(free -h | awk '/Mem:/{print $2}') RAM"
echo ""

echo -e "${CYAN}Required Components:${NC}"

# ── 1. Python 3 ─────────────────────────────────────────────────────────────
if command -v python3 &>/dev/null; then
    ok "Python 3         $(python3 --version 2>&1 | awk '{print $2}')"
    PRESENT+=("python3")
else
    miss "Python 3         not found"
    MISSING+=("python3")
fi

# ── 2. pip ───────────────────────────────────────────────────────────────────
if command -v pip3 &>/dev/null; then
    ok "pip3             $(pip3 --version 2>&1 | awk '{print $2}')"
    PRESENT+=("pip3")
else
    miss "pip3             not found"
    MISSING+=("pip3")
fi

# ── 3. Python venv + deps ────────────────────────────────────────────────────
if [ -f "$VENV_DIR/bin/python3" ] && "$VENV_DIR/bin/python3" -c "import langgraph, langchain" &>/dev/null 2>&1; then
    ok "Python venv      $VENV_DIR (LangChain/LangGraph deps installed)"
    PRESENT+=("pydeps")
else
    miss "Python venv      not found or deps missing"
    MISSING+=("pydeps")
fi

# ── 4. wrk / wrk2 ──────────────────────────────────────────────────────────
if command -v wrk &>/dev/null; then
    ok "wrk              $(wrk --version 2>&1 | head -1)"
    PRESENT+=("wrk2")
elif command -v wrk2 &>/dev/null; then
    ok "wrk2             $(wrk2 --version 2>&1 | head -1)"
    PRESENT+=("wrk2")
else
    miss "wrk/wrk2         not found (will build from source)"
    MISSING+=("wrk2")
fi

# ── 5. Build tools (for wrk2) ───────────────────────────────────────────────
if command -v gcc &>/dev/null && command -v make &>/dev/null; then
    ok "Build tools      gcc + make"
    PRESENT+=("buildtools")
else
    miss "Build tools      gcc/make not found (needed for wrk2)"
    MISSING+=("buildtools")
fi

# ── 6. SQLite data path ─────────────────────────────────────────────────────
if [ -f "$SQLITE_DB_PATH" ]; then
    ok "SQLite DB        $SQLITE_DB_PATH"
    PRESENT+=("sqlite-db")
elif mkdir -p "$SQLITE_DB_DIR" 2>/dev/null; then
    ok "SQLite path      $SQLITE_DB_PATH (will be created on first run)"
    PRESENT+=("sqlite-path")
else
    miss "SQLite path      cannot create directory: $SQLITE_DB_DIR"
    MISSING+=("sqlite-dir")
fi

# ── 7. SSH key ───────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}Agent Connectivity:${NC}"
if [ -f "$HOME/.ssh/id_rsa" ]; then
    ok "SSH key          $HOME/.ssh/id_rsa exists"
    PRESENT+=("sshkey")
else
    miss "SSH key          not found (will generate)"
    MISSING+=("sshkey")
fi

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY + INSTALL
# ══════════════════════════════════════════════════════════════════════════════

echo ""
echo "────────────────────────────────────────────"
echo -e "${BOLD}Scan Summary:${NC} ${GREEN}${#PRESENT[@]} present${NC}, ${RED}${#MISSING[@]} missing${NC}"
echo "────────────────────────────────────────────"

if [ ${#MISSING[@]} -eq 0 ]; then
    echo ""
    log "Everything is installed. Nothing to do."
    echo ""
    echo "  To run the agent:"
    echo "    cd $INSTALL_DIR"
    echo "    cp .env.example .env  # set DUT_HOST (and ANTHROPIC_API_KEY only if using Claude)"
    echo "    ollama pull granite4:7b-a1b-h"
    echo "    $VENV_DIR/bin/python3 main.py -v"
    echo ""
    exit 0
fi

echo ""
echo -n -e "${BOLD}Install ${#MISSING[@]} missing components? [Y/n] ${NC}"
read -r answer
case "$answer" in
    [nN]|[nN][oO])
        echo "Aborted."
        exit 0
        ;;
esac
echo ""
log "Installing ${#MISSING[@]} missing components..."
echo ""

contains() { local item="$1"; shift; for x in "$@"; do [ "$x" = "$item" ] && return 0; done; return 1; }

# ── System packages ─────────────────────────────────────────────────────────
SYS_PKGS=()
contains "python3"    "${MISSING[@]}" && SYS_PKGS+=(python3)
contains "pip3"       "${MISSING[@]}" && SYS_PKGS+=(python3-pip)
contains "buildtools" "${MISSING[@]}" && SYS_PKGS+=(git gcc make openssl-devel zlib-devel)

if [ ${#SYS_PKGS[@]} -gt 0 ]; then
    log "Installing system packages: ${SYS_PKGS[*]}"
    $PKG install -y "${SYS_PKGS[@]}" 2>&1 | tail -5
fi

# ── wrk/wrk2 ─────────────────────────────────────────────────────────────────
if contains "wrk2" "${MISSING[@]}"; then
    # Install zlib-devel if needed for build
    $PKG install -y zlib-devel 2>&1 | tail -2
    log "Building wrk from source..."
    cd /tmp
    [ -d wrk ] && rm -rf wrk
    git clone https://github.com/wg/wrk.git 2>/dev/null
    cd wrk
    make -j$(nproc) 2>&1 | tail -3
    cp wrk /usr/local/bin/wrk
    cd /
    rm -rf /tmp/wrk
    if command -v wrk &>/dev/null; then
        log "wrk installed: $(wrk --version 2>&1 | head -1)"
    else
        warn "wrk build failed — benchmark.sh may not work"
    fi
fi

# ── Python venv + deps ────────────────────────────────────────────────────────
if contains "pydeps" "${MISSING[@]}"; then
    log "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
    log "Installing Python dependencies in venv..."
    "$VENV_DIR/bin/pip" install --upgrade pip 2>&1 | tail -1
    "$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" 2>&1 | tail -5
    log "Venv ready: $VENV_DIR"
fi

# ── SQLite path fixup ───────────────────────────────────────────────────────
if contains "sqlite-dir" "${MISSING[@]}"; then
    log "Creating SQLite directory: $SQLITE_DB_DIR"
    mkdir -p "$SQLITE_DB_DIR" || err "Failed to create SQLite directory: $SQLITE_DB_DIR"
fi

# ── SSH key ──────────────────────────────────────────────────────────────────
if contains "sshkey" "${MISSING[@]}"; then
    log "Generating SSH key..."
    ssh-keygen -t rsa -N "" -f "$HOME/.ssh/id_rsa" -q
    log "SSH key generated: $HOME/.ssh/id_rsa.pub"
    echo ""
    warn "Add this public key to the DUT (System 1):"
    echo ""
    echo "  $(cat $HOME/.ssh/id_rsa.pub)"
    echo ""
    warn "Run on DUT: echo '<key>' >> ~/.ssh/authorized_keys"
fi

# ══════════════════════════════════════════════════════════════════════════════
# VERIFY
# ══════════════════════════════════════════════════════════════════════════════

echo ""
log "============================================"
log "  SlayMetricsAgent bench node ready"
log "============================================"
echo ""
echo "  wrk2:     $(which wrk2 2>/dev/null || echo 'not found')"
if [ -f "$SQLITE_DB_PATH" ]; then
    SQLITE_STATE="present"
else
    SQLITE_STATE="not created yet (auto-created on first run)"
fi
echo "  SQLite:   $SQLITE_DB_PATH ($SQLITE_STATE)"
echo "  Python:   $($VENV_DIR/bin/python3 --version 2>&1)"
echo "  Venv:     $VENV_DIR"
echo "  Agent:    $INSTALL_DIR"
echo ""
echo "  Next steps:"
echo "    1. Copy SSH key to DUT:  ssh-copy-id root@<DUT_IP>"
echo "    2. Configure:            cp .env.example .env"
echo "       Set DUT_HOST in .env"
echo "    3. Start Granite:        ollama pull granite4:7b-a1b-h"
echo "    4. Run:                  $VENV_DIR/bin/python3 main.py -v"
echo ""
