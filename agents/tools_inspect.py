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
        "ok_count": len(already_ok),
        "current": current,  # kept for before_value in findings, not sent to LLM
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
        "ok_count": len(already_ok),
        "current": current,  # kept for before_value in findings, not sent to LLM
    }


def inspect_resource_limits(
    ssh: LocalClient | SSHClient, targets: dict[str, str]
) -> dict[str, Any]:
    """Category 3: Inspect cgroups, systemd limits, ulimits, background hogs."""
    findings: dict[str, Any] = {}

    # Systemd service limits (parse only, don't store raw output)
    svc_limits = ssh.execute(
        "systemctl show nginx.service 2>/dev/null"
        " | grep -E 'LimitNOFILE|LimitNPROC|CPUQuota|MemoryMax|MemoryLimit"
        "|IOWeight|CPUWeight'",
        timeout=5,
    ).stdout.strip()

    # Parse LimitNOFILE
    nofile_match = re.search(r"LimitNOFILE=(\d+)", svc_limits)
    findings["systemd_nofile"] = nofile_match.group(1) if nofile_match else "unknown"

    nproc_match = re.search(r"LimitNPROC=(\d+|infinity)", svc_limits)
    findings["systemd_nproc"] = nproc_match.group(1) if nproc_match else "unknown"

    # Cgroup CPU (parse percentage only)
    cgroup_cpu = ssh.execute(
        "cat /sys/fs/cgroup/system.slice/nginx.service/cpu.max 2>/dev/null || echo 'max 100000'",
        timeout=5,
    ).stdout.strip()
    cpu_parts = cgroup_cpu.split()
    if cpu_parts and cpu_parts[0] != "max" and len(cpu_parts) >= 2:
        try:
            pct = int(cpu_parts[0]) / int(cpu_parts[1]) * 100
            findings["cgroup_cpu_pct"] = f"{pct:.0f}%"
        except (ValueError, ZeroDivisionError):
            pass

    # Cgroup memory (parse to MB)
    cgroup_mem = ssh.execute(
        "cat /sys/fs/cgroup/system.slice/nginx.service/memory.max 2>/dev/null || echo 'max'",
        timeout=5,
    ).stdout.strip()
    if cgroup_mem != "max":
        try:
            findings["cgroup_memory_mb"] = int(cgroup_mem) // (1024 * 1024)
        except ValueError:
            pass

    # IO and CPU weight
    io_weight_match = re.search(r"IOWeight=(\d+)", svc_limits)
    findings["cgroup_io_weight"] = io_weight_match.group(1) if io_weight_match else "100"
    cpu_weight_match = re.search(r"CPUWeight=(\d+)", svc_limits)
    findings["cgroup_cpu_weight"] = cpu_weight_match.group(1) if cpu_weight_match else "100"

    # NUMA policy check — detect NIC node and whether nginx is bound to it
    numa_dropin = ssh.execute(
        "cat /etc/systemd/system/nginx.service.d/numa.conf 2>/dev/null || echo 'none'",
        timeout=5,
    ).stdout.strip()
    if "interleave" in numa_dropin:
        findings["numa_policy"] = "interleave"
    elif "cpunodebind" in numa_dropin:
        findings["numa_policy"] = "bind_nic"
    else:
        findings["numa_policy"] = "default"

    # Detect which NUMA node the primary NIC is on
    nic_numa = ssh.execute(
        "cat /sys/class/net/$(ip route get 8.8.8.8 2>/dev/null"
        " | grep -oP 'dev \\K\\S+')/device/numa_node 2>/dev/null || echo -1",
        timeout=5,
    ).stdout.strip()
    findings["nic_numa_node"] = nic_numa

    # Check if nginx workers have memory on the wrong NUMA node
    numa_maps = ssh.execute(
        "cat /proc/$(pgrep -n nginx)/numa_maps 2>/dev/null"
        " | awk '{for(i=1;i<=NF;i++) if($i~/^N[0-9]/) print $i}'"
        " | sort | uniq -c | sort -rn | head -4",
        timeout=5,
    ).stdout.strip()
    findings["numa_maps_summary"] = numa_maps[:120] if numa_maps else ""

    # Detect specific hogs (dd, fio, stress-ng) — bool + summary only
    hog_procs = ssh.execute(
        "pgrep -la 'dd |fio|stress|sysbench|iperf|io_pressure' 2>/dev/null | head -5",
        timeout=5,
    ).stdout.strip()
    findings["hog_detected"] = bool(hog_procs)
    if hog_procs:
        findings["hog_summary"] = hog_procs[:80]

    # Determine what needs fixing
    problems: list[str] = []
    if findings.get("cgroup_cpu_pct"):
        problems.append(f"cgroup CPU capped at {findings['cgroup_cpu_pct']}")
    if findings.get("cgroup_memory_mb"):
        problems.append(f"cgroup memory capped at {findings['cgroup_memory_mb']}MB")
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
    numa_pol = findings.get("numa_policy", "default")
    if numa_pol == "interleave":
        problems.append("NUMA interleave policy active (worst for locality)")
    elif numa_pol == "default" and targets.get("numa_policy") == "bind_nic":
        problems.append(f"nginx not pinned to NIC NUMA node {findings.get('nic_numa_node', '?')}")
    # Check numa_maps for cross-node memory
    numa_maps = findings.get("numa_maps_summary", "")
    if numa_maps and findings.get("nic_numa_node", "-1") != "-1":
        nic_node = findings["nic_numa_node"]
        wrong_node = f"N{1 - int(nic_node)}" if nic_node.isdigit() else ""
        if wrong_node and wrong_node in numa_maps:
            problems.append(
                f"nginx has memory on wrong NUMA node ({wrong_node}), NIC on N{nic_node}"
            )
    if findings.get("hog_detected"):
        problems.append(f"background hogs detected: {findings.get('hog_summary', '')}")

    return {
        "category": "resource_limits",
        "findings": findings,
        "problems": problems,
    }


def inspect_network(ssh: LocalClient | SSHClient, targets: dict[str, str]) -> dict[str, Any]:
    """Category 4: Inspect firewall, conntrack, traffic control."""
    findings: dict[str, Any] = {}

    # iptables DROP/REJECT/connlimit rules (bool + summary)
    iptables = ssh.execute(
        "timeout 5 iptables -L -n 2>/dev/null | grep -iE 'DROP|REJECT|connlimit|limit' | head -10",
        timeout=10,
    ).stdout.strip()
    findings["iptables_has_drops"] = bool(iptables)
    if iptables:
        findings["iptables_summary"] = iptables[:80]

    # conntrack max
    conntrack = ssh.execute(
        "sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null"
        " || sysctl -n net.nf_conntrack_max 2>/dev/null"
        " || echo unknown",
        timeout=5,
    ).stdout.strip()
    findings["conntrack_max"] = conntrack

    # tc rules (bool + summary)
    tc = ssh.execute(
        "tc qdisc show 2>/dev/null | grep -v 'noqueue\\|pfifo_fast\\|fq_codel' | head -5",
        timeout=5,
    ).stdout.strip()
    findings["tc_has_shaping"] = bool(tc)
    if tc:
        findings["tc_summary"] = tc[:80]

    # nftables (bool only)
    nft = ssh.execute(
        "timeout 5 nft list ruleset 2>/dev/null | grep -iE 'drop|reject|limit' | head -5",
        timeout=10,
    ).stdout.strip()
    findings["nftables_has_drops"] = bool(nft)

    problems: list[str] = []
    if iptables:
        problems.append(f"iptables DROP/limit rules found: {iptables[:80]}")
    if conntrack and conntrack != "unknown":
        target_ct = targets.get("conntrack_max", "1048576")
        try:
            if int(conntrack) < int(target_ct):
                problems.append(f"conntrack_max={conntrack} (target {target_ct})")
        except ValueError:
            pass
    if tc:
        problems.append(f"tc qdisc rules found: {tc[:80]}")

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

    # Background I/O hogs (bool only)
    io_hogs = ssh.execute(
        "pgrep -la 'dd |fio|stress|sysbench|iperf|io_pressure' 2>/dev/null | head -5",
        timeout=5,
    ).stdout.strip()
    findings["io_hog_detected"] = bool(io_hogs)

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
        problems.append(f"I/O hog processes: {io_hogs[:80]}")

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
