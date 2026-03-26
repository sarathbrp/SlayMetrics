#!/bin/bash
# SlayMetricsAgent — Setup Script
# Run this on the target RHEL 9.x / CentOS Stream system before running the agent.
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
TIDB_VERSION="v8.4.0"
TIDB_TAG="perfagent"



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
# PHASE 1: SCAN — Check what's present and what's missing
# ══════════════════════════════════════════════════════════════════════════════

echo ""
echo -e "${BOLD}SlayMetricsAgent — System Scan${NC}"
echo -e "${DIM}Checking what's installed on this system...${NC}"
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

# ── 1. Python 3 ─────────────────────────────────────────────────────────────
echo -e "${CYAN}Required Components:${NC}"

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

# ── 3. Python deps ──────────────────────────────────────────────────────────
if python3 -c "import pydantic_ai" &>/dev/null 2>&1; then
    ok "Python deps      pydantic-ai installed"
    PRESENT+=("pydeps")
else
    miss "Python deps      pydantic-ai not found"
    MISSING+=("pydeps")
fi

# ── 4. Nginx ────────────────────────────────────────────────────────────────
if command -v nginx &>/dev/null; then
    ok "Nginx            $(nginx -v 2>&1 | awk -F/ '{print $2}')"
    PRESENT+=("nginx")
    if systemctl is-active nginx &>/dev/null; then
        ok "  service        running"
    else
        miss "  service        not running (will start)"
        MISSING+=("nginx-start")
    fi
else
    miss "Nginx            not installed"
    MISSING+=("nginx")
fi

# ── 5. Test files ────────────────────────────────────────────────────────────
WEBROOT="/usr/share/nginx/html"
if [ -f "$WEBROOT/1kb.html" ] && [ -f "$WEBROOT/100kb.html" ] && [ -f "$WEBROOT/1mb.html" ]; then
    ok "Test files        1kb, 100kb, 1mb present"
    PRESENT+=("testfiles")
else
    miss "Test files        missing in $WEBROOT"
    MISSING+=("testfiles")
fi

# ── 6. wrk2 ─────────────────────────────────────────────────────────────────
if command -v wrk2 &>/dev/null; then
    ok "wrk2             $(wrk2 --version 2>&1 | head -1)"
    PRESENT+=("wrk2")
else
    miss "wrk2             not found (will build from source)"
    MISSING+=("wrk2")
fi

# ── 7. Build tools (for wrk2) ───────────────────────────────────────────────
if command -v gcc &>/dev/null && command -v make &>/dev/null; then
    ok "Build tools      gcc + make"
    PRESENT+=("buildtools")
else
    miss "Build tools      gcc/make not found (needed for wrk2)"
    MISSING+=("buildtools")
fi

# ── 8. TiDB ─────────────────────────────────────────────────────────────────
if command -v tiup &>/dev/null || [ -f "$HOME/.tiup/bin/tiup" ]; then
    ok "TiDB (tiup)      installed"
    PRESENT+=("tiup")
    if mysql -h 127.0.0.1 -P 4000 -u root -e "SELECT 1;" &>/dev/null 2>&1; then
        ok "  server         running on :4000"
        PRESENT+=("tidb-running")
    else
        miss "  server         not running (will start)"
        MISSING+=("tidb-start")
    fi
else
    miss "TiDB (tiup)      not installed"
    MISSING+=("tiup")
fi

# ── 9. MySQL client ─────────────────────────────────────────────────────────
if command -v mysql &>/dev/null; then
    ok "MySQL client     $(mysql --version 2>&1 | head -1 | awk '{print $3,$4,$5}')"
    PRESENT+=("mysql")
else
    miss "MySQL client     not found (needed for TiDB)"
    MISSING+=("mysql")
fi

# ── 10. TiDB schema ─────────────────────────────────────────────────────────
if mysql -h 127.0.0.1 -P 4000 -u root -e "USE perfagent; SELECT 1;" &>/dev/null 2>&1; then
    ok "TiDB schema      perfagent database exists"
    PRESENT+=("schema")
else
    miss "TiDB schema      perfagent database not found"
    MISSING+=("schema")
fi

# ── 11. System diagnostic tools ──────────────────────────────────────────────
echo ""
echo -e "${CYAN}Diagnostic Tools:${NC}"
for tool in sar numactl ethtool; do
    if command -v $tool &>/dev/null; then
        ok "$tool"
        PRESENT+=("$tool")
    else
        miss "$tool            not found"
        MISSING+=("$tool")
    fi
done

# ── 12. Localhost SSH ────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}Agent Connectivity:${NC}"
if ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=3 root@127.0.0.1 "echo OK" &>/dev/null 2>&1; then
    ok "Localhost SSH    working"
    PRESENT+=("ssh")
else
    miss "Localhost SSH    not configured"
    MISSING+=("ssh")
fi

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY + INSTALL — Show what's missing and install it
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
    echo "    python3 main.py"
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
contains "mysql"      "${MISSING[@]}" && SYS_PKGS+=(mariadb)
contains "sar"        "${MISSING[@]}" && SYS_PKGS+=(sysstat)
contains "numactl"    "${MISSING[@]}" && SYS_PKGS+=(numactl)
contains "ethtool"    "${MISSING[@]}" && SYS_PKGS+=(ethtool)

if [ ${#SYS_PKGS[@]} -gt 0 ]; then
    log "Installing system packages: ${SYS_PKGS[*]}"
    $PKG install -y "${SYS_PKGS[@]}" 2>&1 | tail -5
fi

# ── Nginx ────────────────────────────────────────────────────────────────────
if contains "nginx" "${MISSING[@]}"; then
    log "Installing nginx..."
    $PKG install -y nginx 2>&1 | tail -3
fi

if contains "nginx" "${MISSING[@]}" || contains "nginx-start" "${MISSING[@]}"; then
    log "Starting nginx..."
    systemctl enable --now nginx
    log "Nginx $(nginx -v 2>&1 | awk -F/ '{print $2}') running"
fi

# ── Test files ───────────────────────────────────────────────────────────────
if contains "testfiles" "${MISSING[@]}"; then
    log "Creating test files..."
    mkdir -p "$WEBROOT"
    dd if=/dev/urandom of="$WEBROOT/1kb.html" bs=1024 count=1 2>/dev/null
    dd if=/dev/urandom of="$WEBROOT/100kb.html" bs=1024 count=100 2>/dev/null
    dd if=/dev/urandom of="$WEBROOT/1mb.html" bs=1024 count=1024 2>/dev/null
    log "Test files created in $WEBROOT"
fi

# ── wrk2 ─────────────────────────────────────────────────────────────────────
if contains "wrk2" "${MISSING[@]}"; then
    log "Building wrk2 from source..."
    cd /tmp
    [ -d wrk2 ] && rm -rf wrk2
    git clone https://github.com/giltene/wrk2.git 2>/dev/null
    cd wrk2
    # Fix stdint.h — check both possible locations (src/ or deps/)
    for hdr in src/hdr_histogram.c deps/hdr_histogram/hdr_histogram.c; do
        if [ -f "$hdr" ]; then
            grep -q "stdint.h" "$hdr" || sed -i '1i #include <stdint.h>' "$hdr"
        fi
    done
    make -j$(nproc) 2>&1 | tail -3
    cp wrk /usr/local/bin/wrk2
    cd /
    rm -rf /tmp/wrk2
    log "wrk2 installed: $(wrk2 --version 2>&1 | head -1)"
fi

# ── Python deps ──────────────────────────────────────────────────────────────
if contains "pydeps" "${MISSING[@]}"; then
    log "Installing Python dependencies..."
    cd "$INSTALL_DIR"
    pip3 install -r requirements.txt 2>&1 | tail -5
fi

# ── TiDB ─────────────────────────────────────────────────────────────────────
if contains "tiup" "${MISSING[@]}"; then
    log "Installing TiDB (tiup)..."
    curl --proto '=https' --tlsv1.2 -sSf https://tiup-mirrors.pingcap.com/install.sh | sh 2>&1 | tail -3
fi
export PATH="$PATH:$HOME/.tiup/bin"
grep -q '.tiup/bin' "$HOME/.bashrc" 2>/dev/null || \
    echo 'export PATH=$PATH:$HOME/.tiup/bin' >> "$HOME/.bashrc"

if contains "tiup" "${MISSING[@]}" || contains "tidb-start" "${MISSING[@]}"; then
    if ! mysql -h 127.0.0.1 -P 4000 -u root -e "SELECT 1;" &>/dev/null 2>&1; then
        log "Starting TiDB ${TIDB_VERSION} (this may take a few minutes on first run)..."
        nohup tiup playground "$TIDB_VERSION" --tag "$TIDB_TAG" > /tmp/tidb.log 2>&1 &

        log "Waiting for TiDB to start..."
        for i in $(seq 1 60); do
            if mysql -h 127.0.0.1 -P 4000 -u root -e "SELECT 1;" &>/dev/null 2>&1; then
                break
            fi
            sleep 5
        done

        if ! mysql -h 127.0.0.1 -P 4000 -u root -e "SELECT 1;" &>/dev/null 2>&1; then
            err "TiDB failed to start. Check /tmp/tidb.log"
        fi
        log "TiDB $(mysql -h 127.0.0.1 -P 4000 -u root -e 'SELECT VERSION();' -sN) running"
    fi
fi

# ── Schema ───────────────────────────────────────────────────────────────────
if contains "schema" "${MISSING[@]}"; then
    log "Bootstrapping TiDB schema..."
    mysql -h 127.0.0.1 -P 4000 -u root < "$INSTALL_DIR/schema.sql"
    log "Tables: $(mysql -h 127.0.0.1 -P 4000 -u root -e 'USE perfagent; SHOW TABLES;' -sN | tr '\n' ', ')"
fi

# ── Localhost SSH ────────────────────────────────────────────────────────────
if contains "ssh" "${MISSING[@]}"; then
    log "Setting up localhost SSH access..."
    [ ! -f "$HOME/.ssh/id_rsa" ] && ssh-keygen -t rsa -N "" -f "$HOME/.ssh/id_rsa" -q
    grep -q "$(cat $HOME/.ssh/id_rsa.pub)" "$HOME/.ssh/authorized_keys" 2>/dev/null || \
        cat "$HOME/.ssh/id_rsa.pub" >> "$HOME/.ssh/authorized_keys"
    chmod 600 "$HOME/.ssh/authorized_keys"
    ssh-keyscan 127.0.0.1 >> "$HOME/.ssh/known_hosts" 2>/dev/null
    ssh -o StrictHostKeyChecking=no -o BatchMode=yes root@127.0.0.1 "echo OK" &>/dev/null && \
        log "Localhost SSH OK" || warn "Localhost SSH failed — agent may not work"
fi

# ── Update config ────────────────────────────────────────────────────────────
log "Updating config.yaml for this host..."
cd "$INSTALL_DIR"
sed -i "s|host: .*|host: 127.0.0.1|" config.yaml
sed -i "s|ssh_key: .*|ssh_key: $HOME/.ssh/id_rsa|" config.yaml

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5: VERIFY
# ══════════════════════════════════════════════════════════════════════════════

echo ""
log "============================================"
log "  SlayMetricsAgent setup complete"
log "============================================"
echo ""
echo "  Nginx:    $(curl -s -o /dev/null -w '%{http_code}' http://localhost/1kb.html) on :80"
echo "  wrk2:     $(which wrk2 2>/dev/null || echo 'not found')"
echo "  TiDB:     :4000"
echo "  Python:   $(python3 --version 2>&1)"
echo "  Agent:    $INSTALL_DIR"
echo ""
echo "  To run the agent:"
echo "    cd $INSTALL_DIR"
echo "    export ANTHROPIC_API_KEY=<your-key>  # if using claude-remote profile"
echo "    python3 main.py"
echo ""
echo "  To degrade the system for testing:"
echo "    python3 tools/degrade.py --host 127.0.0.1"
echo ""
echo "  To load knowledge base:"
echo "    python3 tools/load_facts.py"
echo ""
