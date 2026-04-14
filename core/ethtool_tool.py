"""Ethtool remediation tool — NIC ring buffers and interrupt coalescing."""

import logging
import shlex

from .base_tool import RemediationTool
from .ssh import RemoteExecutor

logger = logging.getLogger("slayMetrics.tools")

# Auto-detect the benchmark NIC (VLAN parent or default route device)
_DETECT_NIC = (
    "ip -o -4 route show to default | awk '{print $5}' | cut -d. -f1"
)


class EthtoolTool(RemediationTool):
    """Tunes NIC ring buffers and interrupt coalescing via ethtool."""

    name = "ethtool"
    params_schema = (
        '{"action": "<ring_buffers|coalescing>", '
        '"rx": <int>, "tx": <int>}'
    )

    @classmethod
    def _get_nic(cls, executor: RemoteExecutor) -> str:
        out, _ = executor.run(_DETECT_NIC)
        return out.strip()

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        nic = cls._get_nic(executor)
        action = params.get("action", "ring_buffers")
        if action == "ring_buffers":
            out, _ = executor.run(f"ethtool -g {shlex.quote(nic)} 2>/dev/null | tail -4")
            return out.strip() or "unknown"
        elif action == "coalescing":
            out, _ = executor.run(
                f"ethtool -c {shlex.quote(nic)} 2>/dev/null "
                "| grep -E 'rx-usecs|tx-usecs|adaptive-rx|adaptive-tx' | head -4"
            )
            return out.strip() or "unknown"
        return "unknown action"

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        # Ethtool changes are always worth applying — hard to detect no-op from text
        return False

    def apply(self, params: dict) -> None:
        action = params.get("action", "ring_buffers")
        self._nic = self._get_nic(self.executor)
        q_nic = shlex.quote(self._nic)
        self._action = action

        if action == "ring_buffers":
            rx = int(params.get("rx", 4096))
            tx = int(params.get("tx", 4096))
            if not (64 <= rx <= 8192 and 64 <= tx <= 8192):
                raise ValueError(f"Ring buffer values out of range: rx={rx} tx={tx}")
            # Save originals
            self._original = self._run(f"ethtool -g {q_nic} 2>/dev/null | tail -4")
            logger.info("ethtool ring buffers %s: rx=%d tx=%d", self._nic, rx, tx)
            self._run(f"ethtool -G {q_nic} rx {rx} tx {tx}")
            self._log_verified(f"ethtool -g {q_nic} | tail -4", f"ethtool ring {self._nic}")

        elif action == "coalescing":
            # Enable adaptive coalescing for balanced latency/throughput
            self._original = self._run(
                f"ethtool -c {q_nic} 2>/dev/null "
                "| grep -E 'rx-usecs|tx-usecs|adaptive-rx|adaptive-tx' | head -4"
            )
            logger.info("ethtool coalescing %s: enabling adaptive rx/tx", self._nic)
            self._run(f"ethtool -C {q_nic} adaptive-rx on adaptive-tx on")
            self._log_verified(
                f"ethtool -c {q_nic} | grep adaptive", f"ethtool coalescing {self._nic}",
            )

        else:
            raise ValueError(f"Unknown ethtool action: {action}")

    def rollback(self) -> None:
        if not self._original or not hasattr(self, "_nic"):
            return
        q_nic = shlex.quote(self._nic)
        if self._action == "ring_buffers":
            # Parse original rx/tx from saved output
            import re
            rx_m = re.search(r"RX:\s*(\d+)", self._original)
            tx_m = re.search(r"TX:\s*(\d+)", self._original)
            if rx_m and tx_m:
                logger.info("Rollback ethtool ring %s: rx=%s tx=%s",
                            self._nic, rx_m.group(1), tx_m.group(1))
                self._run(f"ethtool -G {q_nic} rx {rx_m.group(1)} tx {tx_m.group(1)}")
        elif self._action == "coalescing":
            logger.info("Rollback ethtool coalescing %s: disabling adaptive", self._nic)
            self._run(f"ethtool -C {q_nic} adaptive-rx off adaptive-tx off")
