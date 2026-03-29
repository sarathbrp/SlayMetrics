#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import signal
import subprocess
import time
from pathlib import Path

STOP = False


def _handle_stop(_signum, _frame) -> None:
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, _handle_stop)
signal.signal(signal.SIGINT, _handle_stop)


def run(command: str) -> str:
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return (result.stdout.strip() or result.stderr.strip())[:12000]


def count_nginx_workers(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def extract_worker_cores(text: str) -> str:
    cores: list[int] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "nginx":
            try:
                cores.append(int(parts[1]))
            except ValueError:
                continue
    return ",".join(str(core) for core in sorted(set(cores)))


def extract_sysctl_value(text: str, key: str) -> str:
    pattern = re.compile(rf"{re.escape(key)}\s*=\s*(.+)")
    for line in text.splitlines():
        match = pattern.search(line)
        if match:
            return match.group(1).strip()
    return "unknown"


def extract_link_drop_count(text: str, direction: str) -> int:
    direction = direction.upper()
    total = 0
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().startswith(direction + ":") and idx + 1 < len(lines):
            values = lines[idx + 1].split()
            if len(values) >= 4:
                try:
                    total += int(values[3])
                except ValueError:
                    continue
    return total


def extract_ss_established(text: str) -> int:
    match = re.search(r"estab\s+(\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def extract_mem_used(text: str) -> int:
    for line in text.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    return int(parts[2])
                except ValueError:
                    return 0
    return 0


def extract_vmstat_value(text: str, column: str) -> int:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return 0
    headers = lines[-2].split()
    values = lines[-1].split()
    if column in headers:
        idx = headers.index(column)
        if idx < len(values):
            try:
                return int(values[idx])
            except ValueError:
                return 0
    return 0


def sample() -> dict[str, object]:
    nginx_workers = run("ps -eo pid,psr,comm | grep nginx")
    tcp_limits = run(
        "sysctl net.core.somaxconn net.ipv4.tcp_max_syn_backlog net.ipv4.ip_local_port_range"
    )
    network_link_stats = run("ip -s link")
    socket_summary = run("ss -s")
    memory = run("free -m")
    vmstat = run("vmstat 1 2")
    return {
        "timestamp": int(time.time()),
        "nginx_worker_count": count_nginx_workers(nginx_workers),
        "nginx_worker_cores": extract_worker_cores(nginx_workers),
        "somaxconn": extract_sysctl_value(tcp_limits, "net.core.somaxconn"),
        "tcp_max_syn_backlog": extract_sysctl_value(tcp_limits, "net.ipv4.tcp_max_syn_backlog"),
        "ip_local_port_range": extract_sysctl_value(tcp_limits, "net.ipv4.ip_local_port_range"),
        "rx_drop_total": extract_link_drop_count(network_link_stats, "RX"),
        "tx_drop_total": extract_link_drop_count(network_link_stats, "TX"),
        "tcp_established": extract_ss_established(socket_summary),
        "mem_used_mb": extract_mem_used(memory),
        "vmstat_run_queue": extract_vmstat_value(vmstat, "r"),
        "vmstat_blocked": extract_vmstat_value(vmstat, "b"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp",
        "nginx_worker_count",
        "nginx_worker_cores",
        "somaxconn",
        "tcp_max_syn_backlog",
        "ip_local_port_range",
        "rx_drop_total",
        "tx_drop_total",
        "tcp_established",
        "mem_used_mb",
        "vmstat_run_queue",
        "vmstat_blocked",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        handle.flush()
        while not STOP:
            writer.writerow(sample())
            handle.flush()
            time.sleep(max(args.interval, 0.2))


if __name__ == "__main__":
    main()
