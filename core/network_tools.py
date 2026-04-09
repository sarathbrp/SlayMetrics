"""
Network chaos remediation tools (TC, iptables, nftables).

These tools operate at the network layer and are DISABLED by default in config.yaml.
Set remediation.network_tools.enabled: true to allow them.
"""

import logging

from .base_tool import RemediationTool
from .ssh import RemoteExecutor

logger = logging.getLogger("slayMetrics.tools")

# Benchmark NIC — matches omega_master_audit.sh BENCH_NIC logic:
# 1. Find VLAN interface carrying 172.21.x.x traffic
# 2. Strip VLAN suffix to get parent physical NIC (TC shaping targets physical NIC)
_NIC_CMD = (
    "VLAN=$(ip route get 172.21.89.124 2>/dev/null | grep -oP 'dev \\K\\S+' || "
    "ip -o -4 addr show | grep '172\\.21\\.' | awk '{print $2}' | head -1 || "
    "ip -o -4 route show to default | awk '{print $5}') && "
    "echo \"${VLAN%%.*}\""
)


# ---------------------------------------------------------------------------
# TC Traffic Shaping
# ---------------------------------------------------------------------------

class TcShapingTool(RemediationTool):
    """Removes HTB qdisc that throttles NIC bandwidth."""

    name = "tc_shaping"
    params_schema = "{}"

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        out, _ = executor.run(
            f"NIC=$({_NIC_CMD}); tc qdisc show dev $NIC | grep -E 'htb|netem|tbf' || echo 'no htb'"
        )
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        return "no htb" in current_value or not current_value.strip()

    def apply(self, params: dict) -> None:
        self._nic = self._run(_NIC_CMD)
        htb = self._run(f"tc qdisc show dev {self._nic} | grep htb || true")
        if not htb.strip():
            raise ValueError(f"No HTB qdisc on {self._nic} — nothing to remove")
        # Parse rate and ceil for rollback
        class_out = self._run(f"tc class show dev {self._nic}")
        self._rate = "1gbit"
        self._ceil = "1gbit"
        parts = class_out.split()
        for i, p in enumerate(parts):
            if p == "rate" and i + 1 < len(parts):
                self._rate = parts[i + 1]
            if p == "ceil" and i + 1 < len(parts):
                self._ceil = parts[i + 1]
        logger.info("TC: removing HTB on %s (rate=%s ceil=%s)", self._nic, self._rate, self._ceil)
        self._run(f"tc qdisc del dev {self._nic} root 2>/dev/null || true")
        self._log_verified(f"tc qdisc show dev {self._nic} | head -1", f"TC qdisc {self._nic}")

    def rollback(self) -> None:
        if not hasattr(self, "_nic"):
            return
        logger.info("Rollback TC: re-adding HTB on %s (rate=%s)", self._nic, self._rate)
        self._run(f"tc qdisc add dev {self._nic} root handle 1: htb default 10")
        self._run(
            f"tc class add dev {self._nic} parent 1: classid 1:10 htb "
            f"rate {self._rate} ceil {self._ceil}"
        )


# ---------------------------------------------------------------------------
# IPTables ConnLimit
# ---------------------------------------------------------------------------

class IptablesConnLimitTool(RemediationTool):
    """Removes iptables connlimit DROP rules on port 80."""

    name = "iptables_connlimit"
    params_schema = "{}"

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        out, _ = executor.run(
            "iptables -S INPUT 2>/dev/null | grep -- '--dport 80' | grep connlimit || echo 'none'"
        )
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        return current_value.strip() in ("none", "") or not current_value.strip()

    def apply(self, params: dict) -> None:
        rules = self._run(
            "iptables -S INPUT 2>/dev/null | grep -- '--dport 80' | grep connlimit || true"
        )
        if not rules.strip():
            raise ValueError("No iptables connlimit rules on port 80 — nothing to remove")
        self._saved_rules = rules
        logger.info("iptables: removing %d connlimit rule(s) on port 80",
                    len(rules.strip().splitlines()))
        for rule in rules.strip().splitlines():
            delete_rule = rule.replace("-A ", "-D ", 1)
            self._run(f"iptables {delete_rule} 2>/dev/null || true")
        self._log_verified(
            "iptables -S INPUT 2>/dev/null | grep -- '--dport 80' | grep connlimit || echo 'none'",
            "iptables connlimit",
        )

    def rollback(self) -> None:
        if not hasattr(self, "_saved_rules") or not self._saved_rules:
            return
        logger.info("Rollback iptables: re-adding connlimit rules")
        for rule in self._saved_rules.strip().splitlines():
            self._run(f"iptables {rule} 2>/dev/null || true")


# ---------------------------------------------------------------------------
# NFTables Rate Limit
# ---------------------------------------------------------------------------

class NftablesRateLimitTool(RemediationTool):
    """Flushes nftables rate-limit rules on port 80."""

    name = "nftables_ratelimit"
    params_schema = "{}"

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        # Check if nft is available
        check, err = executor.run("nft list ruleset 2>&1 | head -1")
        if "Error" in check or "error" in check or "Permission" in err:
            return f"nft error: {check.strip() or err.strip()}"
        # Only match actual rate-limit rules on port 80 (consistent with apply())
        out, _ = executor.run(
            "nft list ruleset 2>/dev/null | grep -A5 'tcp dport 80' | grep 'limit rate' || echo 'none'"
        )
        return out.strip() or "none"

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        return current_value.strip() in ("none", "") or not current_value.strip()

    def apply(self, params: dict) -> None:
        rate_rules = self._run(
            "nft list ruleset 2>/dev/null | grep -A3 'tcp dport 80' | grep 'limit rate' || true"
        )
        if not rate_rules.strip():
            raise ValueError("No nftables rate-limit rules on port 80 — nothing to flush")
        self._saved_ruleset = self._run("nft list ruleset 2>/dev/null || true")
        logger.info("nftables: flushing ruleset (rate-limit detected)")
        self._run("nft flush ruleset")
        self._log_verified("nft list ruleset 2>/dev/null || echo empty", "nftables ruleset")

    def rollback(self) -> None:
        if not hasattr(self, "_saved_ruleset") or not self._saved_ruleset.strip():
            return
        logger.info("Rollback nftables: restoring saved ruleset")
        escaped = self._saved_ruleset.replace("'", "'\\''")
        self._run(f"echo '{escaped}' > /tmp/nft_rollback.txt && nft -f /tmp/nft_rollback.txt 2>/dev/null || true")


# All network tools — added to TOOL_REGISTRY conditionally based on config
NETWORK_TOOL_CLASSES = [TcShapingTool, IptablesConnLimitTool, NftablesRateLimitTool]
