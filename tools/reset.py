#!/usr/bin/env python3
"""Reset target system to clean state before a fresh agent run.

Reverts nginx config to default, clears sysctl/cgroup/iptables/tc changes,
and optionally clears SQLite session data.

Usage:
    python3 tools/reset.py                      # reset system only
    python3 tools/reset.py --clear-db           # clear sessions (keep knowledge)
    python3 tools/reset.py --clear-leaderboard  # clear leaderboard + knowledge + sessions
    python3 tools/reset.py --reset-all          # clear EVERYTHING
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from tools.ssh import from_config

DEFAULT_NGINX_CONF = """\
user nginx;
worker_processes auto;
worker_rlimit_nofile 1024;
error_log /var/log/nginx/error.log;
pid /run/nginx.pid;

# Load dynamic modules. See /usr/share/doc/nginx/README.dynamic.
include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 1024;
}

http {
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';

        access_log /var/log/nginx/access.log main;

    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    keepalive_requests 100;
    types_hash_max_size 4096;
    client_body_buffer_size 8k;
    client_max_body_size 1m;

    aio off;

        gzip off;

        open_file_cache off;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    # Load modular configuration files from the /etc/nginx/conf.d directory.
    # See http://nginx.org/en/docs/ngx_core_module.html#include
    # for more information.
    include /etc/nginx/conf.d/*.conf;
}
"""

SYSCTL_DEFAULTS = {
    "net.core.somaxconn": "4096",
    "net.ipv4.tcp_max_syn_backlog": "1024",
    "net.core.netdev_max_backlog": "1000",
    "net.ipv4.tcp_tw_reuse": "2",
    "net.ipv4.tcp_autocorking": "1",
    "net.core.rmem_max": "212992",
    "net.core.wmem_max": "212992",
    "net.core.rmem_default": "212992",
    "net.core.wmem_default": "212992",
    "net.ipv4.tcp_fin_timeout": "60",
    "net.ipv4.tcp_slow_start_after_idle": "1",
    "net.ipv4.ip_local_port_range": "32768 60999",
    "vm.swappiness": "30",
    "vm.dirty_ratio": "20",
    "vm.dirty_background_ratio": "10",
    "vm.vfs_cache_pressure": "100",
}


def reset_system(client, cfg: dict | None = None) -> None:
    print("Resetting system to clean state...\n")

    # Load service profile for defaults
    svc_cfg = (cfg or {}).get("service") or {}
    svc_name = svc_cfg.get("name", "nginx")
    systemd_unit = svc_cfg.get("systemd_unit", "nginx.service")
    config_path = svc_cfg.get("config_path", "/etc/nginx/nginx.conf")

    try:
        from services import load_profile
        profile = load_profile(svc_name)
        default_config = profile.default_config
    except Exception:
        default_config = DEFAULT_NGINX_CONF

    # 1. Restore default service config
    print(f"  [{svc_name}] Restoring default config...")
    client.execute(f"cat > {config_path} << 'SVC_EOF'\n{default_config}SVC_EOF")
    if svc_name == "nginx":
        # nginx-specific: validate config before restart
        nginx_bin = client.execute("which nginx 2>/dev/null || echo /usr/sbin/nginx").stdout.strip()
        r = client.execute(f"{nginx_bin} -t 2>&1")
        if "syntax is ok" in r.stdout or "test is successful" in r.stdout:
            client.execute(f"systemctl restart {svc_name}")
            print(f"  [{svc_name}] Config restored and service restarted")
        else:
            print(f"  [{svc_name}] WARNING: config test failed: {r.stdout[:100]}")
    else:
        client.execute(f"systemctl restart {svc_name}")
        print(f"  [{svc_name}] Config restored and service restarted")

    # 2. Reset sysctl values
    print("  [sysctl] Restoring defaults...")
    for param, value in SYSCTL_DEFAULTS.items():
        r = client.execute(f"sysctl -w {param}={value}")
        print(f"    {param} = {value}")

    # 3. Reset transparent hugepages
    print("  [thp] Resetting to system default...")
    client.execute("echo always > /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null")

    # 4. Reset SELinux
    print("  [selinux] Setting to enforcing...")
    client.execute("setenforce 1 2>/dev/null")

    # 5. Reset tuned profile
    print("  [tuned] Resetting to default profile...")
    client.execute(
        "tuned-adm profile throughput-performance 2>/dev/null || "
        "tuned-adm profile virtual-guest 2>/dev/null || true"
    )

    # 6. Remove systemd drop-ins and cgroup limits
    print("  [cgroup] Removing systemd drop-ins and cgroup limits...")
    client.execute(f"rm -rf /etc/systemd/system/{systemd_unit}.d/")
    client.execute(
        f"systemctl set-property {systemd_unit} "
        "CPUQuota= CPUWeight= IOWeight= MemoryMax= 2>/dev/null || true"
    )
    client.execute("systemctl daemon-reload")

    # 7. Flush iptables and nftables
    print("  [firewall] Flushing iptables and nftables...")
    client.execute("iptables -F 2>/dev/null || true")
    client.execute("nft flush ruleset 2>/dev/null || true")

    # 8. Remove tc traffic shaping
    print("  [tc] Removing traffic shaping rules...")
    nic = client.execute(
        "ip route get 8.8.8.8 2>/dev/null | grep -oP 'dev \\K\\S+' || echo eth0"
    ).stdout.strip()
    client.execute(f"tc qdisc del dev {nic} root 2>/dev/null || true")

    # 9. Enable irqbalance
    print("  [irq] Enabling irqbalance...")
    client.execute("systemctl enable irqbalance 2>/dev/null || true")
    client.execute("systemctl start irqbalance 2>/dev/null || true")

    # 10. Kill background hog processes
    print("  [hogs] Killing background hog processes...")
    client.execute("pkill -f 'dd if=/dev/zero' 2>/dev/null || true")
    client.execute("killall stress-ng fio sysbench iperf 2>/dev/null || true")
    client.execute("rm -f /dev/shm/pressure /tmp/io_pressure 2>/dev/null || true")

    # 6. Verify
    print("\n  Verifying...")
    r = client.execute(f"systemctl is-active {svc_name} 2>/dev/null || echo inactive")
    print(f"  [{svc_name}] Status: {r.stdout.strip()}")

    print("\n  System reset complete.")


def clear_db(cfg: dict) -> None:
    import sqlite3

    m = cfg.get("memory") or {}
    db_path = m.get("path", "data/slaymetrics.db")
    if not os.path.exists(db_path):
        print(f"  [sqlite] Database not found at {db_path} — nothing to clear")
        return
    conn = sqlite3.connect(db_path)
    tables = [
        "validations",
        "benchmarks",
        "context",
        "hypothesis_queue",
        "sessions",
    ]
    cur = conn.cursor()
    for table in tables:
        try:
            cur.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass  # table doesn't exist yet — skip
    try:
        cur.execute("DELETE FROM knowledge WHERE type != 'knowledge'")
    except sqlite3.OperationalError:
        pass
    # Keep systems — they persist across sessions
    conn.commit()
    conn.close()
    print("  [sqlite] Cleared all sessions (knowledge base and systems preserved)")


def clear_leaderboard(cfg: dict) -> None:
    """Clear benchmarks table (leaderboard data) and associated knowledge.

    Use this for a fresh start of the lessons-learned system while
    keeping the system identity intact.
    """
    import sqlite3

    m = cfg.get("memory") or {}
    db_path = m.get("path", "data/slaymetrics.db")
    if not os.path.exists(db_path):
        print(f"  [sqlite] Database not found at {db_path} — nothing to clear")
        return
    conn = sqlite3.connect(db_path)
    tables_to_clear = [
        "apply_failures",
        "validations",
        "benchmarks",
        "context",
        "hypothesis_queue",
        "knowledge",
        "sessions",
    ]
    cur = conn.cursor()
    for table in tables_to_clear:
        try:
            cur.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    print("  [sqlite] Cleared leaderboard, knowledge, and all session data (systems preserved)")


def reset_all_db(cfg: dict) -> None:
    import sqlite3

    m = cfg.get("memory") or {}
    db_path = m.get("path", "data/slaymetrics.db")
    if not os.path.exists(db_path):
        print(f"  [sqlite] Database not found at {db_path} — nothing to clear")
        return
    conn = sqlite3.connect(db_path)
    tables = [
        "apply_failures",
        "validations",
        "benchmarks",
        "context",
        "hypothesis_queue",
        "knowledge",
        "sessions",
        "systems",
    ]
    cur = conn.cursor()
    for table in tables:
        try:
            cur.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass  # table doesn't exist yet — skip
    conn.commit()
    conn.close()

    # Remove knowledge hash so facts/ get reloaded on next run
    hash_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "facts", ".loaded_hash"
    )
    if os.path.exists(hash_file):
        os.remove(hash_file)

    print("  [sqlite] Cleared EVERYTHING — sessions, fixes, AND knowledge base")


def main():
    parser = argparse.ArgumentParser(description="Reset system to clean state")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--clear-db", action="store_true", help="Clear session data (preserves knowledge base)"
    )
    parser.add_argument(
        "--reset-all", action="store_true", help="Clear EVERYTHING including knowledge base"
    )
    parser.add_argument(
        "--clear-leaderboard",
        action="store_true",
        help="Clear leaderboard + knowledge + sessions (fresh lessons-learned start)",
    )
    args = parser.parse_args()

    # Load .env so DUT_HOST resolves in config
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    # Resolve ${VAR:-default} in config
    import re

    raw = open(args.config).read()
    raw = re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: os.environ.get(m.group(1), m.group(2)), raw)
    raw = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), raw)
    cfg = yaml.safe_load(raw)

    # Resolve host_env for target
    t = cfg["target"]
    host_env = t.get("host_env")
    if host_env:
        t["host"] = os.environ.get(host_env, t.get("host", "127.0.0.1"))

    print(f"  Target: {t['host']}")
    client = from_config(cfg)
    client.connect()

    try:
        reset_system(client)
        if args.reset_all:
            answer = input(
                "\n  WARNING: This will delete ALL data including knowledge base. Continue? [y/N] "
            )
            if answer.lower() != "y":
                print("  Aborted.")
                return
            reset_all_db(cfg)
        elif args.clear_leaderboard:
            clear_leaderboard(cfg)
        elif args.clear_db:
            clear_db(cfg)
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
