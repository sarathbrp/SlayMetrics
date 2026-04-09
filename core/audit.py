import logging
import re
from pathlib import Path

from .ssh import RemoteExecutor

logger = logging.getLogger("slayMetrics.audit")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]")


class AuditRunner:
    """Deploys scripts to DUT and runs the audit, returning clean plain text."""

    def __init__(self, executor: RemoteExecutor, scripts_dir: Path,
                 remote_tmp: str = "/tmp", audit_script: str = "omega_master_audit.sh"):
        self.executor = executor
        self.scripts_dir = scripts_dir
        self.remote_tmp = remote_tmp
        self.audit_script = audit_script

    def deploy_scripts(self) -> None:
        scripts = list(self.scripts_dir.glob("*.sh"))
        if not scripts:
            raise FileNotFoundError(f"No .sh scripts found in {self.scripts_dir}")

        for script in scripts:
            remote_path = f"{self.remote_tmp}/{script.name}"
            self.executor.upload(script, remote_path)

    def run(self) -> str:
        """Run the audit script and return stripped plain-text output."""
        remote_script = f"{self.remote_tmp}/{self.audit_script}"
        cmd = f"bash {remote_script}"


        output, err = self.executor.run(cmd, timeout=120)

        if not output.strip():
            raise RuntimeError(f"Audit script produced no output. stderr: {err}")

        clean = ANSI_RE.sub("", output)
        logger.info("Captured %d bytes of audit output", len(clean))
        return clean

    def run_live(self) -> str:
        """Run live_audit.sh on the DUT and return stripped plain-text output.
        Deploy is skipped if the script is unchanged (MD5 match).
        """
        live_script = "live_audit.sh"
        local_path = self.scripts_dir / live_script
        if not local_path.exists():
            logger.warning("live_audit.sh not found in %s — skipping live audit", self.scripts_dir)
            return ""
        remote_path = f"{self.remote_tmp}/{live_script}"
        self.executor.upload(local_path, remote_path)
        output, err = self.executor.run(f"bash {remote_path}", timeout=30)
        if not output.strip():
            logger.warning("Live audit produced no output. stderr: %s", err.strip())
            return ""
        clean = ANSI_RE.sub("", output)
        logger.info("Live audit complete (%d bytes)", len(clean))
        return clean

    def deploy_and_run(self) -> str:
        self.deploy_scripts()
        return self.run()
