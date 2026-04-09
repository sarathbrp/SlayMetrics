import logging

from .ssh import RemoteExecutor
from .remediation_tools import dispatch, RemediationTool

logger = logging.getLogger("slayMetrics.fix_applier")


class FixApplier:
    """Dispatches fixes to scoped remediation tools. Holds references for rollback."""

    def __init__(self, executor: RemoteExecutor):
        self.executor = executor
        self._applied_tool: RemediationTool | None = None

    def apply(self, fix: dict) -> None:
        tool_name = fix.get("tool", "")
        params    = fix.get("params", {})
        desc      = fix.get("description", "")

        logger.info("Applying fix [Tier %s] via tool '%s': %s",
                    fix.get("tier", "?"), tool_name, desc)

        try:
            self._applied_tool = dispatch(tool_name, params, self.executor)
        except Exception:
            self._applied_tool = None
            raise

    def rollback(self) -> bool:
        if not self._applied_tool:
            logger.warning("No applied tool to rollback")
            return False
        logger.info("Rolling back tool '%s'", self._applied_tool.name)
        try:
            self._applied_tool.rollback()
        except Exception as e:
            logger.error("Rollback of '%s' failed: %s", self._applied_tool.name, e)
            return False
        finally:
            self._applied_tool = None
        return True
