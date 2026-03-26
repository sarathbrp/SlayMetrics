from __future__ import annotations

from rhel import system_checks
from tools.ssh import SSHResult


class FakeSSH:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def execute(self, command: str, timeout=None):
        del timeout
        return SSHResult(self.mapping.get(command, ""), "", 0)


def test_run_all_and_check_variants():
    ssh = FakeSSH(
        {
            "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u": "powersave\n",
            "cat /sys/kernel/mm/transparent_hugepage/enabled": "always [always] madvise never",
            "getenforce 2>/dev/null || echo Disabled": "Enforcing",
            "sysctl -n net.core.somaxconn 2>/dev/null": "128",
            "sysctl -n net.ipv4.tcp_max_syn_backlog 2>/dev/null": "256",
            "sysctl -n net.core.netdev_max_backlog 2>/dev/null": "1000",
            "sysctl -n net.ipv4.tcp_tw_reuse 2>/dev/null": "0",
            "cat /proc/interrupts | grep -i eth | head -5": "",
            "nproc": "8",
            "findmnt -o TARGET,OPTIONS | grep -v 'relatime\\|noatime' | grep 'atime'": "/ rw,atime",
            "numactl --hardware 2>/dev/null || echo 'numactl not installed'": "available: 2 nodes\nnode 0 cpus: 0 1\nnode 1 cpus: 2 3\n",
            "ulimit -n": "1024",
            "ulimit -Hn": "2048",
            "cat /proc/sys/fs/file-max": "999999",
            "ip link show | grep -oP '(?<=\\d: )\\w+' | grep -v lo | head -1": "ens3",
            "ethtool -k ens3 2>/dev/null | grep -E 'rx-checksum|tx-checksum|tcp-seg|generic-receive'": "rx-checksum: on [fixed]\ntx-checksum-ipv4: off [fixed]",
            "uname -r": "6.1.0",
        }
    )
    results = system_checks.run_all(
        ssh,
        [
            "cpu_governor",
            "transparent_hugepages",
            "selinux_mode",
            "sysctl_net_params",
            "irq_affinity",
            "filesystem_mount_options",
            "numa_topology",
            "open_file_limits",
            "nic_offloading",
            "kernel_version",
        ],
    )
    assert len(results) == 10
    by_name = {r.name: r for r in results}
    assert by_name["cpu_governor"].status == "critical"
    assert by_name["transparent_hugepages"].status == "warning"
    assert by_name["selinux_mode"].status == "warning"
    assert by_name["sysctl_net_params"].status == "warning"
    assert by_name["filesystem_mount_options"].status == "warning"
    assert by_name["numa_topology"].status == "warning"
    assert by_name["open_file_limits"].status == "warning"
    assert by_name["nic_offloading"].value.startswith("ens3:")
    assert by_name["kernel_version"].value == "6.1.0"
    assert system_checks._sysctl_val("a=1\nb=2", "b") == "2"
    assert system_checks._sysctl_val("a=1", "c") == "0"


def test_cpu_governor_and_thp_ok_paths():
    ssh = FakeSSH(
        {
            "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u": "performance\n",
            "cat /sys/kernel/mm/transparent_hugepage/enabled": "always madvise [never]",
        }
    )
    assert system_checks._cpu_governor(ssh).status == "ok"
    assert system_checks._transparent_hugepages(ssh).status == "ok"


def test_more_system_check_ok_and_fallback_paths():
    ssh = FakeSSH(
        {
            "getenforce 2>/dev/null || echo Disabled": "Permissive",
            "findmnt -o TARGET,OPTIONS | grep -v 'relatime\\|noatime' | grep 'atime'": "",
            "numactl --hardware 2>/dev/null || echo 'numactl not installed'": "available: 1 nodes\nnode 0 cpus: 0 1\n",
            "ulimit -n": "bad",
            "ulimit -Hn": "bad",
            "cat /proc/sys/fs/file-max": "999",
            "ip link show | grep -oP '(?<=\\d: )\\w+' | grep -v lo | head -1": "",
        }
    )
    assert system_checks._selinux_mode(ssh).status == "ok"
    assert system_checks._filesystem_mount_options(ssh).status == "ok"
    assert system_checks._numa_topology(ssh).status == "ok"
    assert system_checks._open_file_limits(ssh).status == "warning"
    assert system_checks._nic_offloading(ssh).value == "no interface found"
