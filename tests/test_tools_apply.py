"""Tests for agents/tools_apply.py — 5-category apply tools."""

from __future__ import annotations

from agents.tools_apply import (
    apply_kernel,
    apply_network,
    apply_resource_limits,
    apply_storage,
)
from tools.ssh import SSHResult


class FakeSSH:
    """Records executed commands and returns configurable results."""

    def __init__(self):
        self.commands: list[str] = []

    def execute(self, command: str, timeout: int | None = None) -> SSHResult:
        self.commands.append(command)
        # Simulate sysctl script output
        if "echo $RESULT" in command:
            # Parse the script to find params and return OK for all
            tokens = []
            for line in command.split("\n"):
                if "sysctl -w" in line:
                    # Extract param name from sysctl -w param=value
                    import re

                    m = re.search(r"sysctl -w (\S+?)=", line)
                    if m:
                        tokens.append(f"{m.group(1)}=OK")
                elif "echo never" in line and "transparent_hugepage" in line:
                    tokens.append("transparent_hugepage=OK")
                elif "irqbalance" in line:
                    tokens.append("irqbalance=OK")
                elif "setenforce" in line:
                    tokens.append("selinux=OK")
                elif "scaling_governor" in line:
                    tokens.append("cpu_governor=OK")
            return SSHResult(stdout=" ".join(tokens), stderr="", exit_code=0)
        return SSHResult(stdout="ok", stderr="", exit_code=0)


# ── Fix 1: Cgroup weight removal ────────────────────────────────


def test_apply_resource_limits_removes_cgroup_cpu_cap():
    ssh = FakeSSH()
    result = apply_resource_limits(ssh, {"cgroup_cpu": "max"})
    assert any("CPUQuota=" in cmd for cmd in ssh.commands)
    assert "removed cgroup CPU cap" in result["actions"]


def test_apply_resource_limits_removes_cgroup_memory_cap():
    ssh = FakeSSH()
    result = apply_resource_limits(ssh, {"cgroup_memory": "max"})
    assert any("MemoryMax=infinity" in cmd for cmd in ssh.commands)
    assert "removed cgroup memory cap" in result["actions"]


def test_apply_resource_limits_sets_io_weight():
    ssh = FakeSSH()
    result = apply_resource_limits(ssh, {"cgroup_io_weight": "100"})
    assert any("IOWeight=100" in cmd for cmd in ssh.commands)
    # Should also remove persistent override file
    assert any("50-IOWeight.conf" in cmd for cmd in ssh.commands)
    assert "cgroup IOWeight=100" in result["actions"]


def test_apply_resource_limits_sets_cpu_weight():
    ssh = FakeSSH()
    result = apply_resource_limits(ssh, {"cgroup_cpu_weight": "100"})
    assert any("CPUWeight=100" in cmd for cmd in ssh.commands)
    assert any("50-CPUWeight.conf" in cmd for cmd in ssh.commands)
    assert "cgroup CPUWeight=100" in result["actions"]


def test_apply_resource_limits_creates_systemd_dropin():
    ssh = FakeSSH()
    result = apply_resource_limits(ssh, {"systemd_nofile": "524288"})
    assert any("limits.conf" in cmd and "LimitNOFILE=524288" in cmd for cmd in ssh.commands)
    assert any("systemd drop-in" in a for a in result["actions"])


def test_apply_resource_limits_removes_numa_dropin():
    ssh = FakeSSH()
    result = apply_resource_limits(ssh, {"numa_policy": "remove"})
    assert any("numa.conf" in cmd for cmd in ssh.commands)
    assert "removed NUMA interleave drop-in" in result["actions"]


def test_apply_resource_limits_restarts_nginx_after_changes():
    ssh = FakeSSH()
    apply_resource_limits(ssh, {"cgroup_cpu": "max", "systemd_nofile": "65536"})
    assert any("systemctl restart nginx" in cmd for cmd in ssh.commands)


# ── Fix 2: Background process killing ───────────────────────────


def test_apply_resource_limits_kills_background_hogs():
    ssh = FakeSSH()
    result = apply_resource_limits(ssh, {"kill_background_hogs": "true"})
    kill_cmd = next(c for c in ssh.commands if "pkill" in c)
    assert "dd if=/dev" in kill_cmd
    assert "fio" in kill_cmd
    assert "stress-ng" in kill_cmd
    assert "sysbench" in kill_cmd
    assert "iperf" in kill_cmd
    assert "io_pressure" in kill_cmd
    assert "-9" in kill_cmd  # Force kill
    assert "killed background hog processes" in result["actions"]


def test_apply_storage_kills_io_hogs():
    ssh = FakeSSH()
    result = apply_storage(ssh, {"kill_io_hogs": "true"})
    kill_cmd = next(c for c in ssh.commands if "pkill" in c)
    assert "dd if=/dev" in kill_cmd
    assert "fio" in kill_cmd
    assert "killed I/O hog processes" in result["actions"]


# ── Kernel apply: tcp_rmem/tcp_wmem quoting ──────────────────────


def test_apply_kernel_quotes_space_values():
    ssh = FakeSSH()
    apply_kernel(ssh, {"net.ipv4.tcp_rmem": "4096 87380 16777216"})
    script_cmd = next(c for c in ssh.commands if "bash -c" in c)
    assert '"4096 87380 16777216"' in script_cmd


def test_apply_kernel_no_quotes_for_simple_values():
    ssh = FakeSSH()
    apply_kernel(ssh, {"net.core.somaxconn": "65535"})
    script_cmd = next(c for c in ssh.commands if "bash -c" in c)
    assert "somaxconn=65535" in script_cmd
    # Should NOT have extra quotes around simple value
    assert 'somaxconn="65535"' not in script_cmd


def test_apply_kernel_returns_applied_params():
    ssh = FakeSSH()
    result = apply_kernel(
        ssh, {"net.core.somaxconn": "65535", "vm.swappiness": "10"}
    )
    assert "net.core.somaxconn" in result["applied"]
    assert "vm.swappiness" in result["applied"]


# ── Network apply ────────────────────────────────────────────────


def test_apply_network_flushes_iptables():
    ssh = FakeSSH()
    result = apply_network(ssh, {"iptables_drop_rules": "flush"})
    assert any("iptables" in cmd for cmd in ssh.commands)
    assert "flushed iptables DROP/connlimit rules" in result["actions"]


def test_apply_network_sets_conntrack():
    ssh = FakeSSH()
    apply_network(ssh, {"conntrack_max": "1048576"})
    assert any("conntrack_max=1048576" in cmd for cmd in ssh.commands)


def test_apply_network_removes_tc():
    ssh = FakeSSH()
    result = apply_network(ssh, {"tc_rules": "remove"})
    assert any("tc qdisc del" in cmd for cmd in ssh.commands)
    assert "removed tc qdisc rules" in result["actions"]


# ── Storage apply ────────────────────────────────────────────────


def test_apply_storage_sets_scheduler():
    ssh = FakeSSH()
    result = apply_storage(ssh, {"io_scheduler": "none"})
    assert any("queue/scheduler" in cmd for cmd in ssh.commands)
    assert "I/O scheduler=none" in result["actions"]


def test_apply_storage_sets_readahead():
    ssh = FakeSSH()
    result = apply_storage(ssh, {"readahead": "256"})
    assert any("setra 256" in cmd for cmd in ssh.commands)
    assert "readahead=256" in result["actions"]


# ── Empty input handling ─────────────────────────────────────────


def test_apply_kernel_empty():
    result = apply_kernel(FakeSSH(), {})
    assert result["applied"] == {}


def test_apply_resource_limits_empty():
    result = apply_resource_limits(FakeSSH(), {})
    assert result["actions"] == []


def test_apply_network_empty():
    result = apply_network(FakeSSH(), {})
    assert result["actions"] == []


def test_apply_storage_empty():
    result = apply_storage(FakeSSH(), {})
    assert result["actions"] == []
