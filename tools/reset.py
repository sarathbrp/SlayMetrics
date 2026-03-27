#!/usr/bin/env python3
"""Reset target system to clean state before a fresh agent run.

Reverts nginx config to default, clears sysctl changes, and optionally
clears TiDB session data.

Usage:
    python3 tools/reset.py                      # reset system only
    python3 tools/reset.py --clear-db           # also clear TiDB sessions
    python3 tools/reset.py --host 192.168.1.100 # remote target
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
}


def reset_system(client) -> None:
    print("Resetting system to clean state...\n")

    # 1. Restore default nginx.conf
    print("  [nginx] Restoring default nginx.conf...")
    client.execute(f"cat > /etc/nginx/nginx.conf << 'NGINX_EOF'\n{DEFAULT_NGINX_CONF}NGINX_EOF")
    # Find nginx binary (may not be in PATH over SSH)
    nginx_bin = client.execute("which nginx 2>/dev/null || echo /usr/sbin/nginx").stdout.strip()
    r = client.execute(f"{nginx_bin} -t 2>&1")
    if "syntax is ok" in r.stdout or "test is successful" in r.stdout:
        client.execute("systemctl restart nginx")
        print("  [nginx] Config restored and service restarted")
    else:
        print(f"  [nginx] WARNING: config test failed: {r.stdout[:100]}")

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
    client.execute("tuned-adm profile throughput-performance 2>/dev/null || "
                   "tuned-adm profile virtual-guest 2>/dev/null || true")

    # 6. Verify
    print("\n  Verifying...")
    r = client.execute("curl -s -o /dev/null -w '%{http_code}' http://localhost/")
    print(f"  [nginx] HTTP status: {r.stdout.strip()}")
    nginx_bin = client.execute("which nginx 2>/dev/null || echo /usr/sbin/nginx").stdout.strip()
    r = client.execute(
        f"{nginx_bin} -T 2>&1 | "
        "grep -E 'worker_processes|sendfile|tcp_nopush|access_log|worker_connections|open_file_cache|gzip|aio'"
    )
    for line in r.stdout.strip().splitlines():
        print(f"  [nginx] {line.strip()}")

    print("\n  System reset complete.")


def clear_db(cfg: dict) -> None:
    import pymysql

    m = cfg["memory"]
    conn = pymysql.connect(
        host=m["host"],
        port=int(m.get("port", 4000)),
        user=m["user"],
        password=os.environ.get(m.get("password_env", ""), "") or "",
        database=m["database"],
        autocommit=True,
    )
    with conn.cursor() as cur:
        cur.execute("DELETE FROM context")
        cur.execute("DELETE FROM facts WHERE type != 'knowledge'")
        cur.execute("DELETE FROM hypothesis_queue")
        cur.execute("DELETE FROM profile")
    conn.close()
    print("  [tidb] Cleared all sessions (knowledge base preserved)")


def reset_all_db(cfg: dict) -> None:
    import pymysql

    m = cfg["memory"]
    conn = pymysql.connect(
        host=m["host"],
        port=int(m.get("port", 4000)),
        user=m["user"],
        password=os.environ.get(m.get("password_env", ""), "") or "",
        database=m["database"],
        autocommit=True,
    )
    with conn.cursor() as cur:
        cur.execute("DELETE FROM context")
        cur.execute("DELETE FROM facts")
        cur.execute("DELETE FROM hypothesis_queue")
        cur.execute("DELETE FROM profile")
    conn.close()

    # Remove knowledge hash so facts/ get reloaded on next run
    hash_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "facts", ".loaded_hash"
    )
    if os.path.exists(hash_file):
        os.remove(hash_file)

    print("  [tidb] Cleared EVERYTHING — sessions, fixes, AND knowledge base")


def main():
    parser = argparse.ArgumentParser(description="Reset system to clean state")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--clear-db", action="store_true", help="Clear TiDB session data (preserves knowledge base)"
    )
    parser.add_argument(
        "--reset-all", action="store_true", help="Clear EVERYTHING in TiDB including knowledge base"
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
        elif args.clear_db:
            clear_db(cfg)
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
