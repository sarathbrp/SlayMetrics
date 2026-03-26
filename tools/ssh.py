from __future__ import annotations

import os
from dataclasses import dataclass, field

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


class SSHClient:
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
        _, stdout, stderr = self._client.exec_command(
            command, timeout=timeout or self.timeout
        )
        exit_code = stdout.channel.recv_exit_status()
        return SSHResult(
            stdout=stdout.read().decode("utf-8", errors="replace"),
            stderr=stderr.read().decode("utf-8", errors="replace"),
            exit_code=exit_code,
        )

    def execute_as(self, command: str, user: str = "root") -> SSHResult:
        return self.execute(f"sudo -u {user} {command}")

    def __enter__(self) -> SSHClient:
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()


def from_config(cfg: dict) -> SSHClient:
    t = cfg["target"]
    return SSHClient(
        host=t["host"],
        user=t["ssh_user"],
        key_path=t["ssh_key"],
        timeout=t.get("ssh_timeout", 30),
    )
