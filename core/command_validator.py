"""Safety validator for SSH diagnostic commands.

Ensures commands sent to the DUT during investigation are read-only.
Uses a blocklist approach: anything not explicitly dangerous is allowed.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("slayMetrics.validator")

_MAX_CMD_LENGTH = 500


@dataclass
class ValidationResult:
    blocked: bool
    reason: str


# Destructive command patterns — compiled once for performance
_BLOCKED_COMMANDS: list[re.Pattern] = [re.compile(p, re.IGNORECASE) for p in [
    r"\brm\s+-",
    r"\brm\s+/",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bpoweroff\b",
    r"\binit\s+[06]",
    r"\bsysctl\s+-w\b",
    r"\bsystemctl\s+(set-property|stop|disable|mask|restart|start|enable)\b",
    r"\biptables\s+-[ADIFXZ]\b",
    r"\bnft\s+(add|delete|flush|insert)\b",
    r"\btc\s+(qdisc|class|filter)\s+(add|del|change|replace)\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bmkdir\b",
    r"\bkill\s",
    r"\bkillall\b",
    r"\bpkill\b",
    r"\bcurl\b.*\s-[dXP]",
    r"\bwget\b",
    r"\byum\s+(install|remove|erase)\b",
    r"\bdnf\s+(install|remove|erase)\b",
    r"\brpm\s+-[ieU]",
    r"\bpip\s+install\b",
    r"\btee\s",
    r"\bmv\s",
    r"\bcp\s.*\s/",
    r"\btruncate\b",
    r"\bfallocate\b",
    r"\becho\b.*>\s*/",
    r"\bprintf\b.*>\s*/",
    r"\bnginx\s+-s\b",
]]

# Structural patterns — redirects that write to files
_BLOCKED_REDIRECTS: list[re.Pattern] = [re.compile(p) for p in [
    r">\s*/",
    r">>\s*/",
    r"\|.*>\s*/",
]]

# Safe redirect patterns to exclude from redirect blocking
_SAFE_REDIRECT = re.compile(r"2>\s*/dev/null|2>&1|1>&2")


class CommandValidator:
    """Validates that commands are read-only and safe to run on the DUT."""

    @classmethod
    def validate(cls, command: str) -> ValidationResult:
        """Check if a command is safe to execute on the DUT."""
        cmd = command.strip()
        if not cmd:
            return ValidationResult(blocked=True, reason="empty command")
        if len(cmd) > _MAX_CMD_LENGTH:
            return ValidationResult(
                blocked=True,
                reason=f"command too long ({len(cmd)} > {_MAX_CMD_LENGTH})",
            )

        for pattern in _BLOCKED_COMMANDS:
            if pattern.search(cmd):
                return ValidationResult(
                    blocked=True,
                    reason=f"blocked pattern: {pattern.pattern}",
                )

        # Check redirect patterns, but allow safe ones like 2>/dev/null
        cmd_without_safe = _SAFE_REDIRECT.sub("", cmd)
        for pattern in _BLOCKED_REDIRECTS:
            if pattern.search(cmd_without_safe):
                return ValidationResult(
                    blocked=True,
                    reason=f"blocked redirect: {pattern.pattern}",
                )

        return ValidationResult(blocked=False, reason="")
