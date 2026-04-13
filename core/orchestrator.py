"""Fleet orchestration: FleetTarget, InstallerOrchestrator, fleet helper functions."""

from __future__ import annotations

import logging
import os
import shlex
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NotRequired, TypedDict
from uuid import uuid4

from .config import Config
from .ssh import RemoteExecutor
from .audit import AuditRunner
from .display import Display
from .constants import SCRIPTS_DIR, REMOTE_TMP, AUDIT_SCRIPT
from .wrk_installer import ensure_installer_wrk
from .installer_benchmark import installer_audit_target, run_installer_benchmark

logger = logging.getLogger("slayMetrics.orchestrator")


class FleetTarget(TypedDict):
    name: str
    group: str
    host: str
    user: str
    private_key_path: str
    port: int
    connect_timeout_seconds: int
    password: NotRequired[str]  # only present when --target-password is used


@contextmanager
def _target_env(target: FleetTarget) -> Iterator[None]:
    """Temporarily override SLAY_DUT_* env vars for a fleet target."""
    mapping = {
        "SLAY_DUT_HOST":    target["host"],
        "SLAY_DUT_USER":    target["user"],
        "SLAY_DUT_KEY":     target["private_key_path"],
        "SLAY_DUT_PORT":    str(target["port"]),
        "SLAY_DUT_TIMEOUT": str(target["connect_timeout_seconds"]),
    }
    prev = {k: os.environ.get(k) for k in mapping}
    try:
        os.environ.update(mapping)
        yield
    finally:
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _target_tag(target: FleetTarget) -> str:
    raw = f"{target['name']}_{target['host']}"
    return "".join(ch if ch.isalnum() else "_" for ch in raw)


class InstallerOrchestrator:
    """Runs remote execution from installer while control plane stays local."""

    def __init__(self, config: Config, installer_host: str):
        self.config = config
        self.installer_host = installer_host
        self.installer_user = config.orchestration_installer_user
        self.installer_key = config.orchestration_installer_key
        self.installer_port = config.orchestration_installer_port
        self.installer_timeout = config.orchestration_installer_timeout
        base = config.orchestration_installer_remote_tmp.rstrip("/") or "/tmp/slaymetrics_orchestrate"
        self.remote_workspace = f"{base}_{uuid4().hex[:10]}"
        self._remote_target_keys: dict[str, str] = {}

    def _executor(self) -> RemoteExecutor:
        return RemoteExecutor(
            host=self.installer_host, user=self.installer_user,
            key_path=self.installer_key, port=self.installer_port,
            timeout=self.installer_timeout,
        )

    def _upload_tree(self, executor: RemoteExecutor, local_root: Path, remote_root: str) -> None:
        for path in local_root.rglob("*"):
            rel = path.relative_to(local_root)
            if any(part in {"results", "__pycache__"} for part in rel.parts):
                continue
            remote_path = f"{remote_root}/{str(rel)}"
            if path.is_dir():
                executor.run(f"mkdir -p {shlex.quote(remote_path)}", timeout=30)
                continue
            executor.run(f"mkdir -p {shlex.quote(str(Path(remote_path).parent))}", timeout=30)
            executor.upload(path, remote_path)

    def setup(self) -> None:
        with self._executor() as ex:
            ensure_installer_wrk(
                ex, self.config.orchestration_installer_auto_install_wrk,
                self.installer_user, self.installer_host,
            )
            ex.run(f"mkdir -p {shlex.quote(self.remote_workspace)}", timeout=30)
            ex.upload(SCRIPTS_DIR / AUDIT_SCRIPT, f"{self.remote_workspace}/{AUDIT_SCRIPT}")
            ex.upload(SCRIPTS_DIR / "live_audit.sh", f"{self.remote_workspace}/live_audit.sh")
            self._upload_tree(ex, SCRIPTS_DIR / "benchmark", f"{self.remote_workspace}/benchmark")
        logger.info("Installer workspace ready: %s@%s:%s",
                    self.installer_user, self.installer_host, self.remote_workspace)

    def prepare_target_auth(self, targets: list[FleetTarget]) -> None:
        """Prepare installer->target auth (password and/or key-based)."""
        password_targets = [t for t in targets if str(t.get("password", "")).strip()]
        key_paths = sorted({
            str(t["private_key_path"]).strip()
            for t in targets
            if not str(t.get("password", "")).strip()
        })
        with self._executor() as ex:
            if password_targets:
                out, _ = ex.run(
                    "bash -lc 'command -v sshpass >/dev/null 2>&1 && echo ok || echo missing'",
                    timeout=30,
                )
                if out.strip() != "ok":
                    raise RuntimeError(
                        "Password auth requested but sshpass is missing on installer. "
                        "Install sshpass or configure key-based target auth."
                    )
            if key_paths:
                ex.run(f"mkdir -p {shlex.quote(self.remote_workspace)}/keys", timeout=30)
                for idx, key_path in enumerate(key_paths, 1):
                    local = Path(key_path)
                    if not local.exists():
                        raise FileNotFoundError(f"Target key not found on orchestrator host: {local}")
                    remote = f"{self.remote_workspace}/keys/target_key_{idx}"
                    ex.upload(local, remote)
                    ex.run(f"chmod 600 {shlex.quote(remote)}", timeout=30)
                    self._remote_target_keys[key_path] = remote
        logger.info(
            "Prepared target auth on installer (password targets=%d, uploaded keys=%d).",
            len(password_targets), len(key_paths),
        )

    def cleanup(self) -> None:
        try:
            with self._executor() as ex:
                ex.run(f"rm -rf {shlex.quote(self.remote_workspace)}", timeout=60)
            logger.info("Installer workspace cleaned: %s", self.remote_workspace)
        except Exception as e:
            logger.warning("Installer workspace cleanup failed (%s): %s", self.remote_workspace, e)

    def audit_target(self, target: FleetTarget) -> tuple[str, str, str]:
        return installer_audit_target(
            self._executor, self.remote_workspace, target, self._remote_target_keys,
        )

    def run_benchmark_with_live(self, target: FleetTarget) -> tuple[str, str]:
        return run_installer_benchmark(
            self._executor, self.config, self.remote_workspace, target, self._remote_target_keys,
        )


# ---------------------------------------------------------------------------
# Fleet helper functions (direct SSH, no installer)
# ---------------------------------------------------------------------------

def _audit_target(target: FleetTarget) -> tuple[str, str, str]:
    """Direct SSH audit (no installer). Returns (target_name, audit_output, error)."""
    name = target["name"]
    try:
        with RemoteExecutor(
            host=target["host"], user=target["user"],
            key_path=target["private_key_path"],
            port=target["port"], timeout=target["connect_timeout_seconds"],
        ) as executor:
            output = AuditRunner(executor, SCRIPTS_DIR, REMOTE_TMP, AUDIT_SCRIPT).deploy_and_run()
        Display.audit_summary(target["host"], AUDIT_SCRIPT, output)
        return name, output, ""
    except Exception as e:
        return name, "", str(e)


def _collect_parallel_audits(
    config: Config,
    targets: list[FleetTarget],
    audit_fn: Callable[[FleetTarget], tuple[str, str, str]] = _audit_target,
) -> dict[str, str]:
    if not targets:
        return {}
    workers = max(1, min(config.orchestration_max_parallel_audits, len(targets)))
    logger.info(
        "Fleet pre-audit: %d targets, parallelism=%d (benchmarks remain sequential).",
        len(targets), workers,
    )
    audits: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(audit_fn, t): t for t in targets}
        for fut in as_completed(futures):
            target = futures[fut]
            name = target["name"]
            try:
                t_name, output, err = fut.result()
                if err:
                    logger.error("Audit failed [%s %s]: %s", t_name, target["host"], err)
                    continue
                audits[t_name] = output
                logger.info("Audit complete [%s %s] (%d bytes)", t_name, target["host"], len(output))
            except Exception as e:
                logger.error("Audit worker crashed [%s %s]: %s", name, target["host"], e)
    return audits


def _normalize_filter_values(values: list[str] | None) -> set[str]:
    out: set[str] = set()
    for value in values or []:
        for part in value.split(","):
            token = part.strip()
            if token:
                out.add(token)
    return out


def _select_targets(
    targets: list[FleetTarget],
    group_filter: set[str],
    target_filter: set[str],
) -> list[FleetTarget]:
    if not group_filter and not target_filter:
        return targets
    selected: list[FleetTarget] = []
    for t in targets:
        if group_filter and t["group"] not in group_filter:
            continue
        if target_filter and t["name"] not in target_filter and t["host"] not in target_filter:
            continue
        selected.append(t)
    return selected
