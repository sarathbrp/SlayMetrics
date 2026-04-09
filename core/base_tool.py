"""Abstract base class for all DUT remediation tools."""

import logging
from abc import ABC, abstractmethod

from .ssh import RemoteExecutor

logger = logging.getLogger("slayMetrics.tools")


class RemediationTool(ABC):
    """Base class for all DUT remediation tools."""

    name: str = ""
    params_schema: str = ""  # human-readable param description for the LLM

    def __init__(self, executor: RemoteExecutor):
        self.executor = executor
        self._original: str = ""

    @abstractmethod
    def apply(self, params: dict) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        """Return the current value on the DUT for display in the fix plan."""
        return ""

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        """Return True if the fix would change nothing (already at target)."""
        return False

    def _run(self, cmd: str) -> str:
        out, err = self.executor.run(cmd)
        if err.strip():
            logger.debug("  stderr: %s", err.strip())
        return out.strip()

    def _no_op_check(self, current: str, target: str, label: str) -> None:
        if current.strip() == target.strip():
            raise ValueError(f"No-op: {label} is already {target!r} — skipping")

    def _log_verified(self, cmd: str, label: str) -> None:
        actual = self._run(cmd)
        logger.info("  verified: %s = %s", label, actual)
