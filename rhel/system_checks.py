from __future__ import annotations

from dataclasses import dataclass

from tools.ssh import LocalClient, SSHClient

SSHLike = LocalClient | SSHClient


@dataclass
class CheckResult:
    name: str
    value: str
    status: str  # "ok" | "warning" | "critical"
    recommendation: str  # empty string if no action needed


def run_all(ssh: SSHLike, checks: list[str]) -> list[CheckResult]:
    runners = {
        "cpu_governor": _cpu_governor,
        "transparent_hugepages": _transparent_hugepages,
        "selinux_mode": _selinux_mode,
        "sysctl_net_params": _sysctl_net_params,
        "irq_affinity": _irq_affinity,
        "filesystem_mount_options": _filesystem_mount_options,
        "numa_topology": _numa_topology,
        "open_file_limits": _open_file_limits,
        "nic_offloading": _nic_offloading,
        "kernel_version": _kernel_version,
    }
    results = []
    for check in checks:
        fn = runners.get(check)
        if fn:
            results.append(fn(ssh))
    return results


def _cpu_governor(ssh: SSHLike) -> CheckResult:
    r = ssh.execute(
        "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u"
    )
    value = r.stdout.strip()
    if not value:
        return CheckResult("cpu_governor", "not available (VM)", "ok", "")
    if "performance" in value:
        return CheckResult("cpu_governor", value, "ok", "")
    return CheckResult(
        "cpu_governor",
        value,
        "critical",
        "echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
    )


def _transparent_hugepages(ssh: SSHLike) -> CheckResult:
    r = ssh.execute("cat /sys/kernel/mm/transparent_hugepage/enabled")
    raw = r.stdout.strip()
    # Extract the active value from [brackets]
    import re as _re

    m = _re.search(r"\[(\w+)\]", raw)
    active = m.group(1) if m else raw
    if active in ("never", "madvise"):
        return CheckResult("transparent_hugepages", active, "ok", "")
    return CheckResult(
        "transparent_hugepages",
        active,
        "warning",
        "echo never > /sys/kernel/mm/transparent_hugepage/enabled",
    )


def _selinux_mode(ssh: SSHLike) -> CheckResult:
    r = ssh.execute("getenforce 2>/dev/null || echo Disabled")
    value = r.stdout.strip()
    if value == "Enforcing":
        return CheckResult(
            "selinux_mode",
            value,
            "warning",
            "setenforce 0  # or tune policy with audit2allow",
        )
    return CheckResult("selinux_mode", value, "ok", "")


def _sysctl_net_params(ssh: SSHLike) -> CheckResult:
    params = {
        "net.core.somaxconn": 4096,
        "net.ipv4.tcp_max_syn_backlog": 4096,
        "net.core.netdev_max_backlog": 1000,
        "net.ipv4.tcp_tw_reuse": 0,
    }
    values = {}
    for p in params:
        r = ssh.execute(f"sysctl -n {p} 2>/dev/null")
        values[p] = r.stdout.strip()

    # Build compact summary
    summary = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in values.items())

    recommendations = []
    try:
        if int(values.get("net.core.somaxconn", "0")) < 4096:
            recommendations.append("sysctl -w net.core.somaxconn=65535")
        if int(values.get("net.ipv4.tcp_max_syn_backlog", "0")) < 4096:
            recommendations.append("sysctl -w net.ipv4.tcp_max_syn_backlog=65535")
    except ValueError:
        pass

    status = "warning" if recommendations else "ok"
    return CheckResult("sysctl_net_params", summary, status, " && ".join(recommendations))


def _irq_affinity(ssh: SSHLike) -> CheckResult:
    r = ssh.execute("cat /proc/interrupts | grep -i eth | head -5")
    value = r.stdout.strip() or "no ethernet IRQs found"
    r2 = ssh.execute("nproc")
    cores = r2.stdout.strip()
    return CheckResult(
        "irq_affinity",
        f"cores={cores} | {value[:200]}",
        "warning" if value else "ok",
        "Use irqbalance or set /proc/irq/N/smp_affinity manually",
    )


def _filesystem_mount_options(ssh: SSHLike) -> CheckResult:
    r = ssh.execute("findmnt -o TARGET,OPTIONS | grep -v 'relatime\\|noatime' | grep 'atime'")
    value = r.stdout.strip()
    if value:
        return CheckResult(
            "filesystem_mount_options",
            value,
            "warning",
            "Add 'noatime' to mount options in /etc/fstab and remount",
        )
    return CheckResult("filesystem_mount_options", "noatime or relatime in use", "ok", "")


def _numa_topology(ssh: SSHLike) -> CheckResult:
    r = ssh.execute("numactl --hardware 2>/dev/null || echo 'numactl not installed'")
    raw = r.stdout.strip()
    # Count nodes and extract CPU list
    node_count = len(
        [line for line in raw.splitlines() if line.startswith("node") and "cpus:" in line]
    )
    cpus_line = next((line for line in raw.splitlines() if "cpus:" in line), "")
    summary = f"{node_count} node(s)"
    if cpus_line:
        summary += f", {cpus_line.strip()}"
    if node_count > 1:
        return CheckResult(
            "numa_topology",
            summary,
            "warning",
            f"System has {node_count} NUMA nodes — consider numactl --cpunodebind=0 --membind=0",
        )
    return CheckResult("numa_topology", summary, "ok", "")


def _open_file_limits(ssh: SSHLike) -> CheckResult:
    soft_r = ssh.execute("ulimit -n")
    hard_r = ssh.execute("ulimit -Hn")
    sysmax_r = ssh.execute("cat /proc/sys/fs/file-max")
    try:
        soft = int(soft_r.stdout.strip())
        hard = int(hard_r.stdout.strip())
    except ValueError:
        soft, hard = 0, 0
    sysmax = sysmax_r.stdout.strip()
    value = f"soft={soft}, hard={hard}, system-max={sysmax}"
    if soft < 65536:
        return CheckResult(
            "open_file_limits",
            value,
            "warning",
            "echo '* soft nofile 65536\\n* hard nofile 65536' >> /etc/security/limits.conf",
        )
    return CheckResult("open_file_limits", value, "ok", "")


def _nic_offloading(ssh: SSHLike) -> CheckResult:
    r = ssh.execute("ip link show | grep -oP '(?<=\\d: )\\w+' | grep -v lo | head -1")
    iface = r.stdout.strip()
    if not iface:
        return CheckResult("nic_offloading", "no interface found", "ok", "")
    r2 = ssh.execute(
        f"ethtool -k {iface} 2>/dev/null | "
        "grep -E 'rx-checksum|tx-checksum|tcp-seg|generic-receive'"
    )
    # Compact: extract on/off status
    features = {}
    for line in r2.stdout.strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            features[k.strip()] = v.strip().split()[0]  # "on [fixed]" → "on"
    summary = f"{iface}: " + ", ".join(f"{k}={v}" for k, v in features.items())
    return CheckResult("nic_offloading", summary[:120], "ok", "")


def _kernel_version(ssh: SSHLike) -> CheckResult:
    r = ssh.execute("uname -r")
    value = r.stdout.strip()
    return CheckResult("kernel_version", value, "ok", "")


def _sysctl_val(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(key):
            return line.split("=")[-1].strip()
    return "0"
