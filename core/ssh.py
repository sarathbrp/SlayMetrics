import hashlib
import logging
import stat
import time
from pathlib import Path

import paramiko

logger = logging.getLogger("slayMetrics.ssh")

_CONNECT_RETRIES = 3
_CONNECT_RETRY_WAIT = 5  # seconds between retries


class RemoteExecutor:
    """SSH/SFTP client for the DUT. Use as a context manager."""

    def __init__(self, host: str, user: str, key_path: str,
                 port: int = 22, timeout: int = 10):
        self.host = host
        self.user = user
        self.key_path = key_path
        self.port = port
        self.timeout = timeout
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        last_err: Exception | None = None
        for attempt in range(1, _CONNECT_RETRIES + 1):
            try:
                self._client = paramiko.SSHClient()
                self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self._client.connect(
                    hostname=self.host,
                    username=self.user,
                    key_filename=self.key_path,
                    port=self.port,
                    timeout=self.timeout,
                )
                return
            except Exception as e:
                last_err = e
                if attempt < _CONNECT_RETRIES:
                    logger.warning("SSH connect attempt %d/%d failed (%s) — retrying in %ds",
                                   attempt, _CONNECT_RETRIES, e, _CONNECT_RETRY_WAIT)
                    time.sleep(_CONNECT_RETRY_WAIT)
        raise last_err

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    def run(self, cmd: str, timeout: int = 120) -> tuple[str, str]:
        """Run a command, return (stdout, stderr)."""
        _, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        return (
            stdout.read().decode("utf-8", errors="replace"),
            stderr.read().decode("utf-8", errors="replace"),
        )

    def upload(self, local_path: Path, remote_path: str) -> bool:
        """Upload file only if remote md5 differs. Returns True if uploaded."""
        local_md5 = hashlib.md5(local_path.read_bytes()).hexdigest()

        out, _ = self.run(f"md5sum {remote_path} 2>/dev/null")
        remote_md5 = out.strip().split()[0] if out.strip() else ""

        if local_md5 == remote_md5:
            return False

        logger.info("Uploading %s -> %s", local_path.name, remote_path)
        sftp = self._client.open_sftp()
        try:
            sftp.put(str(local_path), remote_path)
            sftp.chmod(remote_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)
        finally:
            sftp.close()
        return True
