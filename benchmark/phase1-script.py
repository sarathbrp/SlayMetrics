#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess

COMMANDS: list[tuple[str, str]] = [
    ("interrupts", "cat /proc/interrupts | grep -E 'CPU|eth|ens|eno|virtio' | head -n 20"),
    ("nginx_workers", "ps -eo pid,psr,comm | grep nginx"),
    (
        "tcp_limits",
        "sysctl net.core.somaxconn net.ipv4.tcp_max_syn_backlog net.ipv4.ip_local_port_range",
    ),
    ("network_link_stats", "ip -s link"),
    ("socket_summary", "ss -s"),
    ("memory", "free -m"),
    ("vmstat", "vmstat 1 2"),
]


def run(command: str) -> dict[str, object]:
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "output": (result.stdout.strip() or result.stderr.strip())[:12000],
    }


def count_interrupt_cpu_columns(text: str) -> int:
    first_line = next((line for line in text.splitlines() if "CPU" in line), "")
    return len(re.findall(r"CPU\d+", first_line))


def count_nginx_workers(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def extract_worker_cores(text: str) -> list[int]:
    cores: list[int] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "nginx":
            try:
                cores.append(int(parts[1]))
            except ValueError:
                continue
    return sorted(set(cores))


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


def main() -> None:
    sections = {name: run(command) for name, command in COMMANDS}
    summary = {
        "interrupt_cpu_lines": count_interrupt_cpu_columns(sections["interrupts"]["output"]),
        "nginx_worker_count": count_nginx_workers(sections["nginx_workers"]["output"]),
        "nginx_worker_cores": extract_worker_cores(sections["nginx_workers"]["output"]),
        "somaxconn": extract_sysctl_value(sections["tcp_limits"]["output"], "net.core.somaxconn"),
        "tcp_max_syn_backlog": extract_sysctl_value(
            sections["tcp_limits"]["output"], "net.ipv4.tcp_max_syn_backlog"
        ),
        "ip_local_port_range": extract_sysctl_value(
            sections["tcp_limits"]["output"], "net.ipv4.ip_local_port_range"
        ),
        "rx_drop_total": extract_link_drop_count(sections["network_link_stats"]["output"], "RX"),
        "tx_drop_total": extract_link_drop_count(sections["network_link_stats"]["output"], "TX"),
        "tcp_established": extract_ss_established(sections["socket_summary"]["output"]),
        "mem_used_mb": extract_mem_used(sections["memory"]["output"]),
        "vmstat_run_queue": extract_vmstat_value(sections["vmstat"]["output"], "r"),
        "vmstat_blocked": extract_vmstat_value(sections["vmstat"]["output"], "b"),
    }
    print(json.dumps({"summary": summary, "sections": sections}))


if __name__ == "__main__":
    main()
