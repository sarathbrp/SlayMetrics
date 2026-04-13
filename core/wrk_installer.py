"""Ensure wrk is available on the installer host, installing it if needed."""

from __future__ import annotations

import logging
import shlex

from .ssh import RemoteExecutor

logger = logging.getLogger("slayMetrics.wrk_installer")

_WRK_INSTALL_SCRIPT = """
set +e
if command -v wrk >/dev/null 2>&1; then echo "__WRK_STATUS__:ok"; exit 0; fi

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >/dev/null 2>&1; apt-get install -y wrk >/dev/null 2>&1
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y wrk >/dev/null 2>&1
  if ! command -v wrk >/dev/null 2>&1; then
    if command -v subscription-manager >/dev/null 2>&1; then
      RHEL_VER="$(rpm -E %rhel 2>/dev/null || echo 9)"; ARCH="$(arch 2>/dev/null || echo x86_64)"
      subscription-manager repos --enable "codeready-builder-for-rhel-${RHEL_VER}-${ARCH}-rpms" >/dev/null 2>&1 || true
    fi
    RHEL_VER="$(rpm -E %rhel 2>/dev/null || echo 9)"
    dnf install -y "https://dl.fedoraproject.org/pub/epel/epel-release-latest-${RHEL_VER}.noarch.rpm" >/dev/null 2>&1 || true
    dnf install -y epel-release >/dev/null 2>&1 || true; dnf makecache >/dev/null 2>&1 || true
    dnf install -y wrk >/dev/null 2>&1
  fi
elif command -v yum >/dev/null 2>&1; then
  yum install -y wrk >/dev/null 2>&1
  if ! command -v wrk >/dev/null 2>&1; then
    if command -v subscription-manager >/dev/null 2>&1; then
      RHEL_VER="$(rpm -E %rhel 2>/dev/null || echo 8)"; ARCH="$(arch 2>/dev/null || echo x86_64)"
      subscription-manager repos --enable "codeready-builder-for-rhel-${RHEL_VER}-${ARCH}-rpms" >/dev/null 2>&1 || true
    fi
    RHEL_VER="$(rpm -E %rhel 2>/dev/null || echo 8)"
    yum install -y "https://dl.fedoraproject.org/pub/epel/epel-release-latest-${RHEL_VER}.noarch.rpm" >/dev/null 2>&1 || true
    yum install -y epel-release >/dev/null 2>&1 || true; yum makecache >/dev/null 2>&1 || true
    yum install -y wrk >/dev/null 2>&1
  fi
elif command -v zypper >/dev/null 2>&1; then
  zypper --non-interactive install wrk >/dev/null 2>&1
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache wrk >/dev/null 2>&1
fi

if ! command -v wrk >/dev/null 2>&1; then
  TMP_WRK_DIR="/tmp/slay_wrk_build_$$"; mkdir -p "$TMP_WRK_DIR" >/dev/null 2>&1 || true
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y git make gcc openssl-devel >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git make gcc openssl-devel >/dev/null 2>&1 || true
  elif command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y >/dev/null 2>&1 || true; apt-get install -y git make gcc libssl-dev >/dev/null 2>&1 || true
  fi
  if command -v git >/dev/null 2>&1 && command -v make >/dev/null 2>&1 && command -v gcc >/dev/null 2>&1; then
    git clone --depth 1 https://github.com/wg/wrk.git "$TMP_WRK_DIR/wrk" >/dev/null 2>&1 || true
    if [ -d "$TMP_WRK_DIR/wrk" ]; then
      (cd "$TMP_WRK_DIR/wrk" && make >/dev/null 2>&1) || true
      if [ -x "$TMP_WRK_DIR/wrk/wrk" ]; then
        cp "$TMP_WRK_DIR/wrk/wrk" /usr/local/bin/wrk >/dev/null 2>&1 || true
        chmod 755 /usr/local/bin/wrk >/dev/null 2>&1 || true
      fi
    fi
  fi
  rm -rf "$TMP_WRK_DIR" >/dev/null 2>&1 || true
fi

if command -v wrk >/dev/null 2>&1; then echo "__WRK_STATUS__:ok"; else echo "__WRK_STATUS__:missing"; fi
""".strip()


def _has_command(executor: RemoteExecutor, command: str) -> bool:
    out, _ = executor.run(
        f"bash -lc {shlex.quote(f'command -v {command} >/dev/null 2>&1 && echo ok || echo missing')}",
        timeout=30,
    )
    return out.strip() == "ok"


def ensure_installer_wrk(
    executor: RemoteExecutor,
    auto_install: bool,
    installer_user: str,
    installer_host: str,
) -> None:
    """Ensure wrk is present on executor host; auto-install if permitted."""
    if _has_command(executor, "wrk"):
        return
    if not auto_install:
        raise RuntimeError(
            "Installer is missing 'wrk'. Set orchestration.installer.auto_install_wrk=true "
            "or install wrk manually on installer host."
        )
    logger.info("Installer missing wrk; attempting auto-install via package manager.")
    out, err = executor.run(f"bash -lc {shlex.quote(_WRK_INSTALL_SCRIPT)}", timeout=900)
    if "__WRK_STATUS__:ok" in out:
        logger.info("Installer wrk auto-install succeeded.")
        return
    raise RuntimeError(
        "Installer is missing 'wrk' and auto-install failed. "
        "Install manually (RHEL usually needs EPEL + CRB first, then dnf install -y wrk) "
        f"on {installer_user}@{installer_host}. stderr: {err.strip()}"
    )
