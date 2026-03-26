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
# For more information on configuration, see:
#   * Official English Documentation: http://nginx.org/en/docs/
#   * Official Russian Documentation: http://nginx.org/ru/docs/

user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log notice;
pid /run/nginx.pid;

# Load dynamic modules. See /usr/share/doc/nginx/README.dynamic.
include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 1024;
}

http {
    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for"';

    access_log  /var/log/nginx/access.log  main;

    sendfile            on;
    tcp_nopush          on;
    keepalive_timeout   65;
    types_hash_max_size 4096;

    include             /etc/nginx/mime.types;
    default_type        application/octet-stream;

    # Load modular configuration files from the /etc/nginx/conf.d directory.
    # See http://nginx.org/en/docs/beginners_guide.html
    include /etc/nginx/conf.d/*.conf;

    server {
        listen       80;
        listen       [::]:80;
        server_name  _;
        root         /usr/share/nginx/html;

        # Load configuration files for the default server block.
        include /etc/nginx/default.d/*.conf;

        error_page 404 /404.html;
        location = /404.html {
        }

        error_page 500 502 503 504 /50x.html;
        location = /50x.html {
        }
    }
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
    # Write default config
    client.execute(f"cat > /etc/nginx/nginx.conf << 'NGINX_EOF'\n{DEFAULT_NGINX_CONF}NGINX_EOF")
    r = client.execute("nginx -t 2>&1")
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
    client.execute("tuned-adm profile virtual-guest 2>/dev/null || true")

    # 6. Verify
    print("\n  Verifying...")
    r = client.execute("curl -s -o /dev/null -w '%{http_code}' http://localhost/1kb.html")
    print(f"  [nginx] HTTP status: {r.stdout.strip()}")
    r = client.execute(
        "nginx -T 2>&1 | "
        "grep -E 'worker_processes|sendfile|tcp_nopush|access_log|worker_connections'"
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

    cfg = yaml.safe_load(open(args.config))
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
