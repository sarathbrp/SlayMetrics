#!/usr/bin/env python3
"""Intentionally degrade a RHEL 9 + Nginx system for agent self-testing.

This maps 1:1 to the agent's hypothesis queue — every degradation here
is something the agent should detect and fix.

Usage:
    python tools/degrade.py --host 192.168.1.100 --user root --key ~/.ssh/id_rsa
    python tools/degrade.py --host 192.168.1.100 --restore
    python tools/degrade.py --host 192.168.1.100 --list
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.ssh import SSHClient

DEGRADATIONS = [
    {
        "name": "cpu_governor_powersave",
        "hypothesis": "cpu_governor_performance",
        "degrade": "echo powersave | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
        "restore": "echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
        "verify": "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
    },
    {
        "name": "transparent_hugepages_always",
        "hypothesis": "transparent_hugepages_disabled",
        "degrade": "echo always > /sys/kernel/mm/transparent_hugepage/enabled",
        "restore": "echo never > /sys/kernel/mm/transparent_hugepage/enabled",
        "verify": "cat /sys/kernel/mm/transparent_hugepage/enabled",
    },
    {
        "name": "selinux_enforcing",
        "hypothesis": "selinux_tuned",
        "degrade": "setenforce 1 2>/dev/null || true",
        "restore": "setenforce 0 2>/dev/null || true",
        "verify": "getenforce 2>/dev/null || echo Disabled",
    },
    {
        "name": "low_somaxconn",
        "hypothesis": "net_somaxconn_backlog",
        "degrade": "sysctl -w net.core.somaxconn=128",
        "restore": "sysctl -w net.core.somaxconn=65535",
        "verify": "sysctl net.core.somaxconn",
    },
    {
        "name": "low_tcp_backlog",
        "hypothesis": "net_somaxconn_backlog",
        "degrade": (
            "sysctl -w net.ipv4.tcp_max_syn_backlog=128 && "
            "sysctl -w net.core.netdev_max_backlog=300"
        ),
        "restore": (
            "sysctl -w net.ipv4.tcp_max_syn_backlog=65535 && "
            "sysctl -w net.core.netdev_max_backlog=65535"
        ),
        "verify": "sysctl net.ipv4.tcp_max_syn_backlog net.core.netdev_max_backlog",
    },
    {
        "name": "nginx_worker_processes_1",
        "hypothesis": "worker_processes_match_cores",
        "degrade": (
            "sed -i 's/^worker_processes.*/worker_processes 1;/' "
            "/etc/nginx/nginx.conf && systemctl reload nginx"
        ),
        "restore": (
            "sed -i 's/^worker_processes.*/worker_processes auto;/' "
            "/etc/nginx/nginx.conf && systemctl reload nginx"
        ),
        "verify": "grep worker_processes /etc/nginx/nginx.conf",
    },
    {
        "name": "nginx_sendfile_off",
        "hypothesis": "sendfile_enabled",
        "degrade": (
            "sed -i 's/sendfile\\s\\+on;/sendfile off;/' "
            "/etc/nginx/nginx.conf && systemctl reload nginx"
        ),
        "restore": (
            "sed -i 's/sendfile\\s\\+off;/sendfile on;/' "
            "/etc/nginx/nginx.conf && systemctl reload nginx"
        ),
        "verify": "grep sendfile /etc/nginx/nginx.conf",
    },
    {
        "name": "nginx_tcp_nopush_off",
        "hypothesis": "tcp_nopush_nodelay",
        "degrade": (
            "sed -i 's/tcp_nopush\\s\\+on;/tcp_nopush off;/' /etc/nginx/nginx.conf && "
            "sed -i 's/tcp_nodelay\\s\\+on;/tcp_nodelay off;/' /etc/nginx/nginx.conf && "
            "systemctl reload nginx"
        ),
        "restore": (
            "sed -i 's/tcp_nopush\\s\\+off;/tcp_nopush on;/' /etc/nginx/nginx.conf && "
            "sed -i 's/tcp_nodelay\\s\\+off;/tcp_nodelay on;/' /etc/nginx/nginx.conf && "
            "systemctl reload nginx"
        ),
        "verify": "grep -E 'tcp_nopush|tcp_nodelay' /etc/nginx/nginx.conf",
    },
]


def degrade(ssh: SSHClient) -> None:
    print("Applying degradations...\n")
    for d in DEGRADATIONS:
        print(f"  [{d['name']}] degrading...")
        ssh.execute(d["degrade"])
        verify = ssh.execute(d["verify"])
        print(f"    -> {verify.stdout.strip()}")
    print(f"\nApplied {len(DEGRADATIONS)} degradations. System is now detuned.")


def restore(ssh: SSHClient) -> None:
    print("Restoring system...\n")
    for d in DEGRADATIONS:
        if d["restore"]:
            print(f"  [{d['name']}] restoring...")
            ssh.execute(d["restore"])
            verify = ssh.execute(d["verify"])
            print(f"    -> {verify.stdout.strip()}")
    print(f"\nRestored {len(DEGRADATIONS)} settings.")


def list_degradations() -> None:
    print("Available degradations:\n")
    print(f"  {'Name':<35} {'Agent Hypothesis':<35}")
    print(f"  {'-' * 35} {'-' * 35}")
    for d in DEGRADATIONS:
        print(f"  {d['name']:<35} {d['hypothesis']:<35}")


def main():
    parser = argparse.ArgumentParser(
        description="Degrade/restore RHEL+Nginx for agent self-testing"
    )
    parser.add_argument("--host", help="Target host IP")
    parser.add_argument("--user", default="root", help="SSH user (default: root)")
    parser.add_argument("--key", default="~/.ssh/id_rsa", help="SSH key path")
    parser.add_argument("--restore", action="store_true", help="Restore system to good state")
    parser.add_argument("--list", action="store_true", help="List available degradations")
    args = parser.parse_args()

    if args.list:
        list_degradations()
        return

    if not args.host:
        parser.error("--host is required (or use --list)")

    ssh = SSHClient(host=args.host, user=args.user, key_path=args.key)
    ssh.connect()

    try:
        if args.restore:
            restore(ssh)
        else:
            degrade(ssh)
    finally:
        ssh.disconnect()


if __name__ == "__main__":
    main()
