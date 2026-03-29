"""Inspect tools — read DUT state across 5 categories.

Each tool runs a single SSH script that collects all data for its category
and returns structured results. The agent can compound multiple categories
into one call to reduce token usage.
"""

from __future__ import annotations

import re
from typing import Any

from tools.ssh import LocalClient, SSHClient


def inspect_webserver(ssh: LocalClient | SSHClient, targets: dict[str, str]) -> dict[str, Any]:
    """Category 1: Inspect nginx/webserver configuration."""
    raw = ssh.execute("nginx -T 2>/dev/null", timeout=10).stdout

    current: dict[str, str] = {}
    needs_fixing: dict[str, dict[str, str]] = {}
    already_ok: list[str] = []

    for param, target in targets.items():
        if param == "listen_backlog":
            match = re.search(r"listen\s+.*backlog=(\d+)", raw)
            current[param] = match.group(1) if match else "not set"
        elif param == "error_log_level":
            match = re.search(r"error_log\s+\S+\s+(\w+)\s*;", raw)
            current[param] = match.group(1) if match else "warn"
        elif param == "worker_processes":
            match = re.search(r"worker_processes\s+(\S+)\s*;", raw)
            current[param] = match.group(1).rstrip(";") if match else "auto"
        elif param == "limit_rate":
            match = re.search(r"limit_rate\s+(\S+)\s*;", raw)
            current[param] = match.group(1).rstrip(";") if match else "0"
        elif param == "directio":
            match = re.search(r"directio\s+(\S+)\s*;", raw)
            current[param] = match.group(1).rstrip(";") if match else "off"
        elif param == "gzip_comp_level":
            match = re.search(r"gzip_comp_level\s+(\S+)\s*;", raw)
            current[param] = match.group(1).rstrip(";") if match else "1"
        else:
            match = re.search(rf"^\s*{re.escape(param)}\s+(.+?);", raw, re.MULTILINE)
            current[param] = match.group(1).strip() if match else "not set"

        cur = current.get(param, "not set")
        if cur != target and cur != "not set":
            needs_fixing[param] = {"current": cur, "target": target}
        elif cur == "not set":
            needs_fixing[param] = {"current": "not set", "target": target}
        else:
            already_ok.append(param)

    return {
        "category": "webserver",
        "needs_fixing": needs_fixing,
        "already_ok": already_ok,
        "current": current,
    }


def inspect_kernel(ssh: LocalClient | SSHClient, targets: dict[str, str]) -> dict[str, Any]:
    """Category 2: Inspect kernel/sysctl, THP, SELinux, CPU governor, IRQ."""
    current: dict[str, str] = {}

    # Sysctl params
    sysctl_keys = [k for k in targets if k.startswith(("net.", "vm.", "fs."))]
    if sysctl_keys:
        cmd = " && ".join(f"echo {k}=$(sysctl -n {k} 2>/dev/null)" for k in sysctl_keys)
        result = ssh.execute(cmd, timeout=10).stdout
        for line in result.splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                current[key.strip()] = val.strip()

    # THP
    if "transparent_hugepage" in targets:
        thp = ssh.execute(
            "cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null", timeout=5
        ).stdout
        match = re.search(r"\[(\w+)\]", thp)
        current["transparent_hugepage"] = match.group(1) if match else "unknown"

    # SELinux
    if "selinux" in targets:
        current["selinux"] = (
            ssh.execute("getenforce 2>/dev/null || echo Disabled", timeout=5).stdout.strip().lower()
        )

    # CPU governor
    if "cpu_governor" in targets:
        current["cpu_governor"] = (
            ssh.execute(
                "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null",
                timeout=5,
            ).stdout.strip()
            or "unknown"
        )

    # IRQ
    if "irqbalance" in targets:
        current["irqbalance"] = ssh.execute(
            "systemctl is-active irqbalance 2>/dev/null || echo inactive", timeout=5
        ).stdout.strip()

    needs_fixing: dict[str, dict[str, str]] = {}
    already_ok: list[str] = []
    for param, target in targets.items():
        cur = current.get(param, "unknown")
        if cur != target:
            needs_fixing[param] = {"current": cur, "target": target}
        else:
            already_ok.append(param)

    return {
        "category": "kernel",
        "needs_fixing": needs_fixing,
        "already_ok": already_ok,
        "current": current,
    }


def inspect_resource_limits(
    ssh: LocalClient | SSHClient, targets: dict[str, str]
) -> dict[str, Any]:
    """Category 3: Inspect cgroups, systemd limits, ulimits, background hogs."""
    findings: dict[str, Any] = {}

    # Systemd service limits
    svc_limits = ssh.execute(
        "systemctl show nginx.service 2>/dev/null"
        " | grep -E 'LimitNOFILE|LimitNPROC|CPUQuota|MemoryMax|MemoryLimit"
        "|IOWeight|CPUWeight'",
        timeout=5,
    ).stdout.strip()
    findings["systemd_limits"] = svc_limits

    # Parse LimitNOFILE
    nofile_match = re.search(r"LimitNOFILE=(\d+)", svc_limits)
    findings["systemd_nofile"] = nofile_match.group(1) if nofile_match else "unknown"

    nproc_match = re.search(r"LimitNPROC=(\d+|infinity)", svc_limits)
    findings["systemd_nproc"] = nproc_match.group(1) if nproc_match else "unknown"

    # Cgroup CPU
    cgroup_cpu = ssh.execute(
        "cat /sys/fs/cgroup/system.slice/nginx.service/cpu.max 2>/dev/null || echo 'max 100000'",
        timeout=5,
    ).stdout.strip()
    findings["cgroup_cpu"] = cgroup_cpu
    # Parse: "150000 1000000" means 15% CPU cap
    cpu_parts = cgroup_cpu.split()
    if cpu_parts and cpu_parts[0] != "max" and len(cpu_parts) >= 2:
        try:
            pct = int(cpu_parts[0]) / int(cpu_parts[1]) * 100
            findings["cgroup_cpu_pct"] = f"{pct:.0f}%"
        except (ValueError, ZeroDivisionError):
            pass

    # Cgroup memory
    cgroup_mem = ssh.execute(
        "cat /sys/fs/cgroup/system.slice/nginx.service/memory.max 2>/dev/null || echo 'max'",
        timeout=5,
    ).stdout.strip()
    findings["cgroup_memory"] = cgroup_mem

    # IO and CPU weight
    io_weight_match = re.search(r"IOWeight=(\d+)", svc_limits)
    findings["cgroup_io_weight"] = io_weight_match.group(1) if io_weight_match else "100"
    cpu_weight_match = re.search(r"CPUWeight=(\d+)", svc_limits)
    findings["cgroup_cpu_weight"] = cpu_weight_match.group(1) if cpu_weight_match else "100"

    # NUMA policy check
    numa_dropin = ssh.execute(
        "cat /etc/systemd/system/nginx.service.d/numa.conf 2>/dev/null || echo 'none'",
        timeout=5,
    ).stdout.strip()
    findings["numa_policy"] = "interleave" if "interleave" in numa_dropin else "default"

    # Background hogs
    top_procs = ssh.execute("ps aux --sort=-%cpu 2>/dev/null | head -8", timeout=5).stdout.strip()
    findings["top_cpu_procs"] = top_procs

    # Detect specific hogs (dd, fio, stress-ng)
    hog_procs = ssh.execute(
        "pgrep -la 'dd |fio|stress' 2>/dev/null | head -5", timeout=5
    ).stdout.strip()
    findings["hog_processes"] = hog_procs

    # Determine what needs fixing
    problems: list[str] = []
    if findings.get("cgroup_cpu_pct"):
        problems.append(f"cgroup CPU capped at {findings['cgroup_cpu_pct']}")
    if cgroup_mem != "max":
        try:
            mem_mb = int(cgroup_mem) // (1024 * 1024)
            problems.append(f"cgroup memory capped at {mem_mb}MB")
        except ValueError:
            pass
    nofile_val = findings.get("systemd_nofile", "unknown")
    if nofile_val not in ("unknown", "infinity"):
        try:
            if int(nofile_val) < int(targets.get("systemd_nofile", "524288")):
                problems.append(f"systemd LimitNOFILE={nofile_val} (too low)")
        except ValueError:
            pass
    io_w = findings.get("cgroup_io_weight", "100")
    if io_w != "100" and io_w != targets.get("cgroup_io_weight", "100"):
        problems.append(f"cgroup IOWeight={io_w} (below default 100)")
    cpu_w = findings.get("cgroup_cpu_weight", "100")
    if cpu_w != "100" and cpu_w != targets.get("cgroup_cpu_weight", "100"):
        problems.append(f"cgroup CPUWeight={cpu_w} (below default 100)")
    if findings.get("numa_policy") == "interleave":
        problems.append("NUMA interleave policy active (worst for locality)")
    if hog_procs:
        problems.append(f"background hogs detected: {hog_procs[:100]}")

    return {
        "category": "resource_limits",
        "findings": findings,
        "problems": problems,
    }


def inspect_network(ssh: LocalClient | SSHClient, targets: dict[str, str]) -> dict[str, Any]:
    """Category 4: Inspect firewall, conntrack, traffic control."""
    findings: dict[str, Any] = {}

    # iptables DROP/REJECT/connlimit rules
    iptables = ssh.execute(
        "timeout 5 iptables -L -n 2>/dev/null | grep -iE 'DROP|REJECT|connlimit|limit' | head -10",
        timeout=10,
    ).stdout.strip()
    findings["iptables_drop_rules"] = iptables

    # conntrack max
    conntrack = ssh.execute(
        "sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null"
        " || sysctl -n net.nf_conntrack_max 2>/dev/null"
        " || echo unknown",
        timeout=5,
    ).stdout.strip()
    findings["conntrack_max"] = conntrack

    # tc rules
    tc = ssh.execute(
        "tc qdisc show 2>/dev/null | grep -v 'noqueue\\|pfifo_fast\\|fq_codel' | head -5",
        timeout=5,
    ).stdout.strip()
    findings["tc_rules"] = tc

    # nftables
    nft = ssh.execute(
        "timeout 5 nft list ruleset 2>/dev/null | grep -iE 'drop|reject|limit' | head -5",
        timeout=10,
    ).stdout.strip()
    findings["nftables_drop_rules"] = nft

    problems: list[str] = []
    if iptables:
        problems.append(f"iptables DROP/limit rules found: {iptables[:100]}")
    if conntrack and conntrack != "unknown":
        target_ct = targets.get("conntrack_max", "1048576")
        try:
            if int(conntrack) < int(target_ct):
                problems.append(f"conntrack_max={conntrack} (target {target_ct})")
        except ValueError:
            pass
    if tc:
        problems.append(f"tc qdisc rules found: {tc[:100]}")

    return {
        "category": "network",
        "findings": findings,
        "problems": problems,
    }


def inspect_storage(ssh: LocalClient | SSHClient, targets: dict[str, str]) -> dict[str, Any]:
    """Category 5: Inspect I/O scheduler, readahead, disk I/O pressure."""
    findings: dict[str, Any] = {}

    # I/O scheduler
    scheduler = ssh.execute(
        "cat /sys/block/$(lsblk -ndo NAME | head -1)/queue/scheduler 2>/dev/null",
        timeout=5,
    ).stdout.strip()
    match = re.search(r"\[(\w+)\]", scheduler)
    findings["io_scheduler"] = (
        match.group(1) if match else scheduler.split()[0] if scheduler else "unknown"
    )

    # Readahead
    readahead = ssh.execute(
        "blockdev --getra /dev/$(lsblk -ndo NAME | head -1) 2>/dev/null || echo unknown",
        timeout=5,
    ).stdout.strip()
    findings["readahead"] = readahead

    # Background I/O hogs
    io_hogs = ssh.execute(
        "pgrep -la 'dd |fio|stress-ng' 2>/dev/null | head -5", timeout=5
    ).stdout.strip()
    findings["io_hog_processes"] = io_hogs

    # Disk utilization
    iostat = ssh.execute(
        "iostat -x 1 1 2>/dev/null | tail -5 || echo 'iostat not available'",
        timeout=10,
    ).stdout.strip()
    findings["iostat"] = iostat

    problems: list[str] = []
    target_sched = targets.get("io_scheduler", "none")
    if findings["io_scheduler"] not in (target_sched, "unknown"):
        problems.append(f"I/O scheduler={findings['io_scheduler']} (target {target_sched})")
    target_ra = targets.get("readahead", "256")
    if readahead != "unknown":
        try:
            if int(readahead) < int(target_ra):
                problems.append(f"readahead={readahead} (target {target_ra})")
        except ValueError:
            pass
    if io_hogs:
        problems.append(f"I/O hog processes: {io_hogs[:100]}")

    return {
        "category": "storage",
        "findings": findings,
        "problems": problems,
    }


def inspect_all(ssh: LocalClient | SSHClient, config: dict[str, Any]) -> dict[str, Any]:
    """Compound inspect: run all 5 categories and return unified result."""
    tuning = config.get("tuning") or {}

    results = {
        "webserver": inspect_webserver(ssh, tuning.get("webserver_targets") or {}),
        "kernel": inspect_kernel(ssh, tuning.get("kernel_targets") or {}),
        "resource_limits": inspect_resource_limits(
            ssh, tuning.get("resource_limits_targets") or {}
        ),
        "network": inspect_network(ssh, tuning.get("network_targets") or {}),
        "storage": inspect_storage(ssh, tuning.get("storage_targets") or {}),
    }

    # Summary
    total_issues = sum(len(r.get("needs_fixing", r.get("problems", {}))) for r in results.values())
    results["summary"] = {
        "total_issues": total_issues,
        "by_category": {
            k: len(v.get("needs_fixing", v.get("problems", {})))
            for k, v in results.items()
            if k != "summary"
        },
    }

    return results
