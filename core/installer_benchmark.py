"""Installer-side SSH audit and benchmark execution functions."""

from __future__ import annotations

import logging
import shlex
from typing import TYPE_CHECKING, Callable

from .display import Display
from .constants import AUDIT_SCRIPT

if TYPE_CHECKING:
    from .orchestrator import FleetTarget
    from .config import Config
    from .ssh import RemoteExecutor

logger = logging.getLogger("slayMetrics.installer_benchmark")


def installer_ssh_opts(
    target: FleetTarget,
    remote_key: str = "",
    password_mode: bool = False,
) -> str:
    """Build SSH option string for installer->target connections."""
    opts = (
        f"-o BatchMode={'no' if password_mode else 'yes'} "
        "-o StrictHostKeyChecking=no "   # fleet hosts are ephemeral/dynamic; no known_hosts file
        "-o UserKnownHostsFile=/dev/null "
        "-o LogLevel=ERROR "
        f"-o ConnectTimeout={int(target['connect_timeout_seconds'])} "
        f"-p {int(target['port'])}"
    )
    if password_mode:
        opts += " -o PreferredAuthentications=password -o PubkeyAuthentication=no"
    if remote_key:
        opts += f" -i {shlex.quote(remote_key)}"
    return opts


def _resolve_auth(
    target: FleetTarget,
    target_keys: dict[str, str],
) -> tuple[str, str]:
    """Return (ssh_opts, auth_setup_shell_fragment) or raise RuntimeError if no auth."""
    target_password = str(target.get("password", "")).strip()
    remote_key = target_keys.get(str(target["private_key_path"]).strip(), "")
    if target_password:
        ssh_opts = installer_ssh_opts(target, password_mode=True)
        # SSHPASS is read by sshpass via -e (env var), never passed as a CLI arg.
        # The export travels over the encrypted SSH channel; assume installer root shell history
        # is disabled on managed infrastructure.
        auth_setup = f"export SSHPASS={shlex.quote(target_password)}\nSSH_BIN='sshpass -e ssh'"
    elif remote_key:
        ssh_opts = installer_ssh_opts(target, remote_key=remote_key)
        auth_setup = "SSH_BIN='ssh'"
    else:
        raise RuntimeError(
            "No target auth available. Configure target.password/SLAY_TARGET_PASSWORD "
            "or a valid target private_key_path."
        )
    return ssh_opts, auth_setup


def installer_audit_target(
    executor_factory: Callable,
    workspace: str,
    target: FleetTarget,
    target_keys: dict[str, str],
    audit_script: str = AUDIT_SCRIPT,
) -> tuple[str, str, str]:
    """Run audit on target via installer SSH tunnel. Returns (name, output, error)."""
    name = target["name"]
    try:
        ssh_opts, auth_setup = _resolve_auth(target, target_keys)
    except RuntimeError as e:
        return name, "", str(e)

    script = f"""
set -e
WS={shlex.quote(workspace)}
TARGET_HOST={shlex.quote(target["host"])}
TARGET_USER={shlex.quote(target["user"])}
TARGET="${{TARGET_USER}}@${{TARGET_HOST}}"
SSH_OPTS={shlex.quote(ssh_opts)}
{auth_setup}
cat "$WS/{audit_script}" | $SSH_BIN $SSH_OPTS "$TARGET" 'cat > /tmp/{audit_script} && chmod +x /tmp/{audit_script}'
$SSH_BIN $SSH_OPTS "$TARGET" 'bash /tmp/{audit_script}'
""".strip()
    try:
        with executor_factory() as ex:
            out, err = ex.run(f"bash -lc {shlex.quote(script)}", timeout=240)
        if not out.strip():
            return name, "", f"Empty audit output. stderr: {err.strip()}"
        Display.audit_summary(target["host"], audit_script, out)
        return name, out, ""
    except Exception as e:
        return name, "", str(e)


def run_installer_benchmark(
    executor_factory: Callable,
    config: Config,
    workspace: str,
    target: FleetTarget,
    target_keys: dict[str, str],
) -> tuple[str, str]:
    """Run benchmark + live sampler on target via installer. Returns (benchmark_output, csv_text)."""
    tag = "".join(ch if ch.isalnum() else "_" for ch in f"{target['name']}_{target['host']}")
    interval = max(1, int(config.live_sampling_interval))
    ssh_opts, auth_setup = _resolve_auth(target, target_keys)

    script = f"""
set -e
WS={shlex.quote(workspace)}
TARGET_HOST={shlex.quote(target["host"])}
TARGET_USER={shlex.quote(target["user"])}
TARGET="${{TARGET_USER}}@${{TARGET_HOST}}"
SSH_OPTS={shlex.quote(ssh_opts)}
{auth_setup}
CONTESTANT={shlex.quote(config.benchmark_contestant)}
RESULTS_DIR="$WS/results"
LIVE_CSV="$WS/live_{tag}.csv"
STOP_FILE="$WS/live_{tag}.stop"
BENCH_OUT="$WS/bench_{tag}.out"
BENCH_ERR="$WS/bench_{tag}.err"
mkdir -p "$RESULTS_DIR"
rm -f "$STOP_FILE" "$LIVE_CSV" "$BENCH_OUT" "$BENCH_ERR"

cat "$WS/live_audit.sh" | $SSH_BIN $SSH_OPTS "$TARGET" 'cat > /tmp/live_audit.sh && chmod +x /tmp/live_audit.sh'
HEADER=$($SSH_BIN $SSH_OPTS "$TARGET" 'bash /tmp/live_audit.sh --header' 2>/dev/null || true)
if [ -n "$HEADER" ]; then echo "$HEADER" > "$LIVE_CSV"; fi

(
  while [ ! -f "$STOP_FILE" ]; do
    ROW=$($SSH_BIN $SSH_OPTS "$TARGET" 'bash /tmp/live_audit.sh' 2>/dev/null || true)
    if [ -n "$ROW" ]; then echo "$ROW" >> "$LIVE_CSV"; fi
    sleep {interval}
  done
) &
SAMPLER_PID=$!

set +e
TARGET_HOST="$TARGET_HOST" RESULTS_DIR="$RESULTS_DIR" bash "$WS/benchmark/benchmark.sh" "$CONTESTANT" > "$BENCH_OUT" 2> "$BENCH_ERR"
BENCH_RC=$?
set -e

touch "$STOP_FILE"
wait "$SAMPLER_PID" || true
cat "$BENCH_OUT"
echo "__BENCH_RC__:$BENCH_RC"
if [ "$BENCH_RC" -ne 0 ]; then
  echo "__BENCH_STDERR_BEGIN__" >&2; cat "$BENCH_ERR" >&2 || true; echo "__BENCH_STDERR_END__" >&2
fi
""".strip()

    logger.info(
        "Installer benchmark started for %s (%s). "
        "Standard profile runs ~290s plus startup/teardown; output is emitted when complete.",
        target["name"], target["host"],
    )
    with executor_factory() as ex:
        out, err = ex.run(f"bash -lc {shlex.quote(script)}", timeout=1200)
        remote_csv = f"{workspace}/live_{tag}.csv"
        cleanup_paths = [
            remote_csv,
            f"{workspace}/live_{tag}.stop",
            f"{workspace}/bench_{tag}.out",
            f"{workspace}/bench_{tag}.err",
        ]
        csv_text, _ = ex.run(
            f"bash -lc {shlex.quote(f'cat {shlex.quote(remote_csv)} 2>/dev/null || true')}",
            timeout=30,
        )
        ex.run(
            f"bash -lc {shlex.quote('rm -f ' + ' '.join(shlex.quote(p) for p in cleanup_paths))}",
            timeout=30,
        )

    bench_rc = 0
    bench_lines: list[str] = []
    for line in out.splitlines():
        if line.startswith("__BENCH_RC__:"):
            try:
                bench_rc = int(line.split(":", 1)[1].strip())
            except ValueError:
                bench_rc = 1
            continue
        bench_lines.append(line)
    benchmark_output = "\n".join(bench_lines).strip()

    if bench_rc != 0:
        stderr = err.strip()
        logger.error("Installer benchmark exited %d for %s. stderr: %s",
                     bench_rc, target["host"], stderr)
        detail = benchmark_output or stderr or "no output"
        raise RuntimeError(
            f"Installer benchmark failed for {target['host']} (exit code {bench_rc}): {detail}"
        )
    if not benchmark_output:
        raise RuntimeError(
            f"Installer benchmark produced no output for {target['host']}. stderr: {err.strip()}"
        )
    logger.info("Installer benchmark completed for %s (%s).", target["name"], target["host"])
    return benchmark_output, csv_text
