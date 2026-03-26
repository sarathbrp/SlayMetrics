from __future__ import annotations

from dataclasses import dataclass

from tools.ssh import SSHClient


@dataclass
class CheckResult:
    name: str
    value: str
    status: str          # "ok" | "warning" | "critical"
    recommendation: str  # empty string if no action needed


def run_all(ssh: SSHClient, checks: list[str]) -> list[CheckResult]:
    runners = {
        "cpu_governor":            _cpu_governor,
        "transparent_hugepages":   _transparent_hugepages,
        "selinux_mode":            _selinux_mode,
        "sysctl_net_params":       _sysctl_net_params,
        "irq_affinity":            _irq_affinity,
        "filesystem_mount_options":_filesystem_mount_options,
        "numa_topology":           _numa_topology,
        "open_file_limits":        _open_file_limits,
        "nic_offloading":          _nic_offloading,
        "kernel_version":          _kernel_version,
    }
    results = []
    for check in checks:
        fn = runners.get(check)
        if fn:
            results.append(fn(ssh))
    return results


def _cpu_governor(ssh: SSHClient) -> CheckResult:
    r = ssh.execute(
        "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u"
    )
    value = r.stdout.strip() or "unknown"
    if "performance" in value:
        return CheckResult("cpu_governor", value, "ok", "")
    return CheckResult(
        "cpu_governor", value, "critical",
        "echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
    )


def _transparent_hugepages(ssh: SSHClient) -> CheckResult:
    r = ssh.execute("cat /sys/kernel/mm/transparent_hugepage/enabled")
    value = r.stdout.strip()
    if "[never]" in value or "[madvise]" in value:
        return CheckResult("transparent_hugepages", value, "ok", "")
    return CheckResult(
        "transparent_hugepages", value, "warning",
        "echo never > /sys/kernel/mm/transparent_hugepage/enabled",
    )


def _selinux_mode(ssh: SSHClient) -> CheckResult:
    r = ssh.execute("getenforce 2>/dev/null || echo Disabled")
    value = r.stdout.strip()
    if value == "Enforcing":
        return CheckResult(
            "selinux_mode", value, "warning",
            "setenforce 0  # or tune policy with audit2allow",
        )
    return CheckResult("selinux_mode", value, "ok", "")


def _sysctl_net_params(ssh: SSHClient) -> CheckResult:
    params = [
        "net.core.somaxconn",
        "net.ipv4.tcp_max_syn_backlog",
        "net.core.netdev_max_backlog",
        "net.ipv4.tcp_tw_reuse",
    ]
    r = ssh.execute(f"sysctl {' '.join(params)} 2>/dev/null")
    value = r.stdout.strip()
    recommendations = []
    if "net.core.somaxconn = " in value:
        val = int(_sysctl_val(value, "net.core.somaxconn"))
        if val < 4096:
            recommendations.append("sysctl -w net.core.somaxconn=65535")
    if "net.ipv4.tcp_max_syn_backlog = " in value:
        val = int(_sysctl_val(value, "net.ipv4.tcp_max_syn_backlog"))
        if val < 4096:
            recommendations.append("sysctl -w net.ipv4.tcp_max_syn_backlog=65535")
    status = "warning" if recommendations else "ok"
    return CheckResult("sysctl_net_params", value, status,
                       " && ".join(recommendations))


def _irq_affinity(ssh: SSHClient) -> CheckResult:
    r = ssh.execute("cat /proc/interrupts | grep -i eth | head -5")
    value = r.stdout.strip() or "no ethernet IRQs found"
    r2 = ssh.execute("nproc")
    cores = r2.stdout.strip()
    return CheckResult(
        "irq_affinity", f"cores={cores} | {value[:200]}", "warning" if value else "ok",
        "Use irqbalance or set /proc/irq/N/smp_affinity manually",
    )


def _filesystem_mount_options(ssh: SSHClient) -> CheckResult:
    r = ssh.execute("findmnt -o TARGET,OPTIONS | grep -v 'relatime\\|noatime' | grep 'atime'")
    value = r.stdout.strip()
    if value:
        return CheckResult(
            "filesystem_mount_options", value, "warning",
            "Add 'noatime' to mount options in /etc/fstab and remount",
        )
    return CheckResult("filesystem_mount_options", "noatime or relatime in use", "ok", "")


def _numa_topology(ssh: SSHClient) -> CheckResult:
    r = ssh.execute("numactl --hardware 2>/dev/null || echo 'numactl not installed'")
    value = r.stdout.strip()
    nodes = value.count("node") if "node" in value else 0
    if nodes > 1:
        return CheckResult(
            "numa_topology", value[:300], "warning",
            f"System has {nodes} NUMA nodes — consider numactl --cpunodebind=0 --membind=0",
        )
    return CheckResult("numa_topology", value[:200], "ok", "")


def _open_file_limits(ssh: SSHClient) -> CheckResult:
    r = ssh.execute("ulimit -n && cat /proc/sys/fs/file-max")
    value = r.stdout.strip()
    try:
        soft = int(value.split("\n")[0])
        if soft < 65536:
            return CheckResult(
                "open_file_limits", value, "warning",
                "echo '* soft nofile 65536\n* hard nofile 65536' >> /etc/security/limits.conf",
            )
    except (ValueError, IndexError):
        pass
    return CheckResult("open_file_limits", value, "ok", "")


def _nic_offloading(ssh: SSHClient) -> CheckResult:
    r = ssh.execute(
        "ip link show | grep -oP '(?<=\\d: )\\w+' | grep -v lo | head -1"
    )
    iface = r.stdout.strip()
    if not iface:
        return CheckResult("nic_offloading", "no interface found", "ok", "")
    r2 = ssh.execute(f"ethtool -k {iface} 2>/dev/null | grep -E 'tx-checksum|rx-checksum|scatter'")
    value = r2.stdout.strip() or "ethtool not available"
    return CheckResult("nic_offloading", f"{iface}: {value[:200]}", "ok", "")


def _kernel_version(ssh: SSHClient) -> CheckResult:
    r = ssh.execute("uname -r")
    value = r.stdout.strip()
    return CheckResult("kernel_version", value, "ok", "")


def _sysctl_val(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(key):
            return line.split("=")[-1].strip()
    return "0"
