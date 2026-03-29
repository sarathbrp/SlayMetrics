from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

import paramiko


@dataclass
class SSHResult:
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def __str__(self) -> str:
        out = self.stdout.strip()
        err = self.stderr.strip()
        if err and not out:
            return err
        if err:
            return f"{out}\n[stderr]: {err}"
        return out


class LocalClient:
    """Direct subprocess execution — no SSH overhead for localhost."""

    def __init__(self):
        pass

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def execute(self, command: str, timeout: int | None = None) -> SSHResult:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout or 120,
            )
            return SSHResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            return SSHResult(stdout="", stderr="Command timed out", exit_code=124)

    def execute_as(self, command: str, user: str = "root") -> SSHResult:
        return self.execute(f"sudo -u {user} {command}")

    def __enter__(self) -> LocalClient:
        return self

    def __exit__(self, *_) -> None:
        pass


class SSHClient:
    """Paramiko SSH client for remote targets."""

    def __init__(self, host: str, user: str, key_path: str, timeout: int = 30):
        self.host = host
        self.user = user
        self.key_path = os.path.expanduser(key_path)
        self.timeout = timeout
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            username=self.user,
            key_filename=self.key_path,
            timeout=self.timeout,
        )
        self._client = client

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def execute(self, command: str, timeout: int | None = None) -> SSHResult:
        if not self._client:
            self.connect()
        assert self._client is not None
        try:
            _, stdout, stderr = self._client.exec_command(command, timeout=timeout or self.timeout)
        except Exception:
            # Reconnect on channel/transport errors and retry once
            self.disconnect()
            self.connect()
            assert self._client is not None
            _, stdout, stderr = self._client.exec_command(command, timeout=timeout or self.timeout)
        channel = stdout.channel
        exit_code = channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if hasattr(channel, "close"):
            channel.close()
        return SSHResult(stdout=out, stderr=err, exit_code=exit_code)

    def execute_as(self, command: str, user: str = "root") -> SSHResult:
        return self.execute(f"sudo -u {user} {command}")

    def __enter__(self) -> SSHClient:
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()


def from_config(cfg: dict, section: str = "target") -> LocalClient | SSHClient:
    t = cfg[section]
    # Resolve host from env var if host_env is set
    host = t.get("host", "localhost")
    host_env = t.get("host_env")
    if host_env:
        host = os.environ.get(host_env, host)
    if host in ("localhost", "127.0.0.1", "::1"):
        return LocalClient()
    return SSHClient(
        host=host,
        user=t["ssh_user"],
        key_path=t["ssh_key"],
        timeout=t.get("ssh_timeout", 30),
    )
