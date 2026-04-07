"""Apply tools — fix DUT state across 5 categories.

Each tool builds a single bash script for its category and executes
it in one SSH call. Only applies parameters listed in config.yaml targets.
"""

from __future__ import annotations

from typing import Any

from tools.ssh import LocalClient, SSHClient


def apply_kernel(ssh: LocalClient | SSHClient, changes: dict[str, str]) -> dict[str, Any]:
    """Category 2: Apply kernel params in one SSH script."""
    if not changes:
        return {"applied": {}, "failed": {}, "skipped": []}

    lines = ["#!/bin/bash", "set +e", "RESULT=''"]

    for param, value in changes.items():
        if param == "transparent_hugepage":
            lines.append(
                f"echo {value} > /sys/kernel/mm/transparent_hugepage/enabled 2>&1"
                f' && RESULT="$RESULT {param}=OK"'
                f' || RESULT="$RESULT {param}=FAIL"'
            )
        elif param == "selinux":
            mode = "0" if value.lower() in ("permissive", "0") else "1"
            lines.append(f"setenforce {mode} 2>&1")
            if value.lower() in ("permissive", "0"):
                lines.append(
                    "sed -i 's/^SELINUX=enforcing/SELINUX=permissive/'"
                    " /etc/selinux/config 2>/dev/null || true"
                )
            lines.append(f'RESULT="$RESULT {param}=OK"')
        elif param == "cpu_governor":
            lines.append(
                f"echo {value} | tee /sys/devices/system/cpu/cpu*/cpufreq/"
                f"scaling_governor >/dev/null 2>&1"
                f' && RESULT="$RESULT {param}=OK"'
                f' || RESULT="$RESULT {param}=FAIL"'
            )
        elif param == "irqbalance":
            lines.append(
                "systemctl enable --now irqbalance 2>&1"
                f' && RESULT="$RESULT {param}=OK"'
                f' || RESULT="$RESULT {param}=FAIL"'
            )
        elif param == "net.ipv4.ip_local_port_range":
            lines.append(
                f"sysctl -w 'net.ipv4.ip_local_port_range={value}' 2>&1"
                f' && RESULT="$RESULT {param}=OK"'
                f' || RESULT="$RESULT {param}=FAIL"'
            )
        elif param.startswith(("net.", "vm.", "fs.")):
            # Quote values with spaces (e.g. tcp_rmem="4096 87380 16777216")
            quoted_value = f'"{value}"' if " " in value else value
            lines.append(
                f"sysctl -w {param}={quoted_value} 2>&1"
                f' && RESULT="$RESULT {param}=OK"'
                f' || RESULT="$RESULT {param}=FAIL"'
            )

    lines.append("echo $RESULT")
    script = "\n".join(lines)

    result = ssh.execute(f"bash -c '{script}'", timeout=30)
    return _parse_script_result(result.stdout, changes)


def apply_resource_limits(
    ssh: LocalClient | SSHClient, changes: dict[str, str],
    systemd_unit: str = "nginx.service",
    binary_path: str = "/usr/sbin/nginx",
) -> dict[str, Any]:
    """Category 3: Fix cgroups, systemd limits, kill background hogs."""
    if not changes:
        return {"applied": {}, "failed": {}, "actions": []}

    actions: list[str] = []
    svc_name = systemd_unit.replace(".service", "")

    # Remove cgroup CPU cap (any value means "remove the cap" — LLM may say "max", "100%", etc.)
    if changes.get("cgroup_cpu"):
        ssh.execute(
            f"systemctl set-property {systemd_unit} CPUQuota= 2>/dev/null || true",
            timeout=10,
        )
        actions.append("removed cgroup CPU cap")

    # Remove cgroup memory cap (any value means "remove the cap" — LLM may say "4G", "max", etc.)
    if changes.get("cgroup_memory"):
        ssh.execute(
            f"systemctl set-property {systemd_unit} MemoryMax=infinity 2>/dev/null || true",
            timeout=10,
        )
        actions.append("removed cgroup memory cap")

    # Fix systemd LimitNOFILE via drop-in (the RIGHT way)
    nofile = changes.get("systemd_nofile")
    if nofile:
        ssh.execute(
            f"mkdir -p /etc/systemd/system/{systemd_unit}.d && "
            f"cat > /etc/systemd/system/{systemd_unit}.d/limits.conf << 'EOF'\n"
            "[Service]\n"
            f"LimitNOFILE={nofile}\n"
            f"LimitNPROC={changes.get('systemd_nproc', 'infinity')}\n"
            "EOF",
            timeout=10,
        )
        actions.append(f"systemd drop-in: LimitNOFILE={nofile}")

    # Also update limits.conf as belt-and-suspenders
    if nofile:
        ssh.execute(
            "sed -i '/nofile/d' /etc/security/limits.conf 2>/dev/null || true && "
            f"echo '* soft nofile {nofile}' >> /etc/security/limits.conf && "
            f"echo '* hard nofile {nofile}' >> /etc/security/limits.conf",
            timeout=10,
        )

    # Fix cgroup IO weight (reset to default 100)
    io_w = changes.get("cgroup_io_weight")
    if io_w:
        ssh.execute(
            f"systemctl set-property {systemd_unit} IOWeight={io_w} 2>/dev/null || true",
            timeout=10,
        )
        # Also remove any persistent override
        ssh.execute(
            f"rm -f /etc/systemd/system/{systemd_unit}.d/50-IOWeight.conf 2>/dev/null || true",
            timeout=5,
        )
        actions.append(f"cgroup IOWeight={io_w}")

    # Fix cgroup CPU weight (reset to default 100)
    cpu_w = changes.get("cgroup_cpu_weight")
    if cpu_w:
        ssh.execute(
            f"systemctl set-property {systemd_unit} CPUWeight={cpu_w} 2>/dev/null || true",
            timeout=10,
        )
        ssh.execute(
            f"rm -f /etc/systemd/system/{systemd_unit}.d/50-CPUWeight.conf 2>/dev/null || true",
            timeout=5,
        )
        actions.append(f"cgroup CPUWeight={cpu_w}")

    # NUMA policy
    numa_policy = changes.get("numa_policy")
    if numa_policy == "remove":
        ssh.execute(
            f"rm -f /etc/systemd/system/{systemd_unit}.d/numa.conf 2>/dev/null || true",
            timeout=5,
        )
        actions.append("removed NUMA interleave drop-in")
    elif numa_policy == "bind_nic":
        # Detect NIC's NUMA node and pin service to it
        nic_node = ssh.execute(
            "cat /sys/class/net/$(ip route get 8.8.8.8 2>/dev/null"
            " | grep -oP 'dev \\K\\S+')/device/numa_node 2>/dev/null || echo 0",
            timeout=5,
        ).stdout.strip()
        if not nic_node.isdigit():
            nic_node = "0"
        ssh.execute(
            f"mkdir -p /etc/systemd/system/{systemd_unit}.d && "
            f"cat > /etc/systemd/system/{systemd_unit}.d/numa.conf << 'EOF'\n"
            "[Service]\n"
            "ExecStart=\n"
            f"ExecStart=/usr/bin/numactl --cpunodebind={nic_node}"
            f" --membind={nic_node} {binary_path} -g 'daemon off;'\n"
            "Type=simple\n"
            "EOF",
            timeout=5,
        )
        actions.append(f"pinned {svc_name} to NUMA node {nic_node} (NIC node)")

    # Kill background hogs (stress-ng, dd, fio, etc.)
    if changes.get("kill_background_hogs") == "true":
        ssh.execute(
            "pkill -9 -f 'dd if=/dev' 2>/dev/null; "
            "pkill -9 -f 'dd of=' 2>/dev/null; "
            "pkill -9 -f 'fio' 2>/dev/null; "
            "pkill -9 -f 'stress-ng' 2>/dev/null; "
            "pkill -9 -f 'stress ' 2>/dev/null; "
            "pkill -9 -f 'sysbench' 2>/dev/null; "
            "pkill -9 -f 'iperf' 2>/dev/null; "
            "pkill -9 -f 'io_pressure' 2>/dev/null; "
            "rm -f /tmp/io_pressure* 2>/dev/null; "
            "umount /dev/shm 2>/dev/null;"
            " mount -t tmpfs tmpfs /dev/shm 2>/dev/null || true; "
            "echo done",
            timeout=10,
        )
        actions.append("killed background hog processes")

    # Reload systemd and restart service to pick up new limits
    needs_restart = any(
        changes.get(k)
        for k in (
            "systemd_nofile",
            "cgroup_cpu",
            "cgroup_memory",
            "cgroup_io_weight",
            "cgroup_cpu_weight",
            "numa_policy",
        )
    )
    if needs_restart:
        ssh.execute(
            f"systemctl daemon-reload && systemctl restart {svc_name} 2>&1 || true",
            timeout=15,
        )
        actions.append(f"restarted {svc_name} with new limits")

    return {"applied": changes, "failed": {}, "actions": actions}


def apply_network(ssh: LocalClient | SSHClient, changes: dict[str, str]) -> dict[str, Any]:
    """Category 4: Fix firewall, conntrack, tc rules."""
    if not changes:
        return {"applied": {}, "failed": {}, "actions": []}

    actions: list[str] = []

    # Flush iptables DROP/connlimit rules
    if changes.get("iptables_drop_rules") == "flush":
        # Remove only DROP and connlimit rules, keep ACCEPT
        ssh.execute(
            "iptables -L INPUT --line-numbers -n 2>/dev/null"
            " | grep -iE 'DROP|REJECT|connlimit' | awk '{print $1}'"
            " | sort -rn | while read n; do iptables -D INPUT $n 2>/dev/null; done; "
            "iptables -L FORWARD --line-numbers -n 2>/dev/null"
            " | grep -iE 'DROP|REJECT|connlimit' | awk '{print $1}'"
            " | sort -rn | while read n; do iptables -D FORWARD $n 2>/dev/null; done; "
            "iptables -L OUTPUT --line-numbers -n 2>/dev/null"
            " | grep -iE 'DROP|REJECT|connlimit' | awk '{print $1}'"
            " | sort -rn | while read n; do iptables -D OUTPUT $n 2>/dev/null; done; "
            "echo done",
            timeout=15,
        )
        actions.append("flushed iptables DROP/connlimit rules")

    # Flush nftables DROP/limit rules (R5-style nftables rate limiting)
    if changes.get("iptables_drop_rules") == "flush":
        nft_check = ssh.execute(
            "nft list ruleset 2>/dev/null | grep -iE 'drop|reject|limit'",
            timeout=5,
        ).stdout.strip()
        if nft_check:
            ssh.execute(
                "nft flush ruleset 2>/dev/null || true",
                timeout=10,
            )
            actions.append("flushed nftables ruleset")

    # Fix conntrack max
    conntrack = changes.get("conntrack_max")
    if conntrack:
        ssh.execute(
            f"sysctl -w net.netfilter.nf_conntrack_max={conntrack} 2>/dev/null || "
            f"sysctl -w net.nf_conntrack_max={conntrack} 2>/dev/null || true",
            timeout=5,
        )
        actions.append(f"conntrack_max={conntrack}")

    # Remove tc rules
    if changes.get("tc_rules") == "remove":
        ssh.execute(
            "for dev in $(ip -o link show | awk -F': ' '{print $2}'); do "
            "tc qdisc del dev $dev root 2>/dev/null; done; echo done",
            timeout=10,
        )
        actions.append("removed tc qdisc rules")

    return {"applied": changes, "failed": {}, "actions": actions}


def apply_storage(ssh: LocalClient | SSHClient, changes: dict[str, str]) -> dict[str, Any]:
    """Category 5: Fix I/O scheduler, readahead, kill I/O hogs."""
    if not changes:
        return {"applied": {}, "failed": {}, "actions": []}

    actions: list[str] = []

    # Set I/O scheduler
    scheduler = changes.get("io_scheduler")
    if scheduler:
        ssh.execute(
            f"echo {scheduler} > /sys/block/$(lsblk -ndo NAME | head -1)"
            f"/queue/scheduler 2>/dev/null || true",
            timeout=5,
        )
        actions.append(f"I/O scheduler={scheduler}")

    # Set readahead
    readahead = changes.get("readahead")
    if readahead:
        ssh.execute(
            f"blockdev --setra {readahead} /dev/$(lsblk -ndo NAME | head -1) 2>/dev/null || true",
            timeout=5,
        )
        actions.append(f"readahead={readahead}")

    # Kill I/O hog processes
    if changes.get("kill_io_hogs") == "true":
        ssh.execute(
            "pkill -f 'dd if=/dev' 2>/dev/null; pkill -f 'fio ' 2>/dev/null; echo done",
            timeout=10,
        )
        actions.append("killed I/O hog processes")

    return {"applied": changes, "failed": {}, "actions": actions}


def _parse_script_result(output: str, changes: dict[str, str]) -> dict[str, Any]:
    """Parse bash script output with PARAM=OK/FAIL tokens."""
    applied: dict[str, str] = {}
    failed: dict[str, str] = {}

    for token in output.split():
        if "=" not in token:
            continue
        key, status = token.rsplit("=", 1)
        if status == "OK":
            applied[key] = changes.get(key, "")
        elif status == "FAIL":
            failed[key] = "command failed"

    # Params not in output assumed applied
    for param, value in changes.items():
        if param not in applied and param not in failed:
            applied[param] = value

    return {"applied": applied, "failed": failed}
