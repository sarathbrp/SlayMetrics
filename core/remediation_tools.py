"""
Remediation tools derived directly from omega_master_audit.sh.
Each tool maps to a parameter group the audit measures.
The LLM can ONLY call tools in TOOL_REGISTRY — no arbitrary shell commands.

Network-level tools (TC, iptables, nftables) live in network_tools.py and are
added to TOOL_REGISTRY only when config.remediation.network_tools.enabled = true.
"""

import logging
import re
import shlex

from .base_tool import RemediationTool
from .ssh import RemoteExecutor

logger = logging.getLogger("slayMetrics.tools")

# ---------------------------------------------------------------------------
# Allowlists — derived from omega_master_audit.sh SYS_KNOBS / SD_KNOBS / NG_KNOBS
# ---------------------------------------------------------------------------

ALLOWED_SYSCTL = {
    "net.core.somaxconn", "net.ipv4.tcp_max_syn_backlog",
    "net.core.netdev_max_backlog", "net.core.rmem_max", "net.core.wmem_max",
    "net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem", "net.ipv4.tcp_tw_reuse",
    "net.ipv4.tcp_fin_timeout", "net.ipv4.tcp_slow_start_after_idle",
    "net.ipv4.ip_local_port_range", "vm.swappiness", "vm.dirty_ratio",
    "vm.vfs_cache_pressure", "net.netfilter.nf_conntrack_max",
    "net.ipv4.tcp_syncookies",
    "fs.nr_open", "fs.file-max",
}

ALLOWED_SYSTEMD = {
    "LimitNOFILE", "LimitNPROC", "CPUQuota", "CPUWeight", "MemoryMax", "MemoryHigh",
    "IOWeight", "Nice", "OOMScoreAdjust", "TasksMax",
}

ALLOWED_NGINX = {
    "worker_processes", "worker_connections", "worker_rlimit_nofile",
    "worker_cpu_affinity", "accept_mutex", "multi_accept", "access_log",
    "sendfile", "tcp_nopush", "tcp_nodelay", "keepalive_timeout",
    "keepalive_requests", "gzip", "open_file_cache", "limit_rate",
    "client_body_buffer_size", "aio", "directio",
}

ALLOWED_CPU_GOVERNORS = {"performance", "powersave", "ondemand", "conservative"}

# ---------------------------------------------------------------------------
# Value validation — reject shell metacharacters in LLM-supplied values
# ---------------------------------------------------------------------------

_SAFE_VALUE_RE = re.compile(r"^[a-zA-Z0-9_./:, -]+$")


def _validate_value(value: str, label: str) -> str:
    """Reject values containing shell metacharacters."""
    if not _SAFE_VALUE_RE.match(value):
        raise ValueError(
            f"Unsafe characters in {label} value: {value!r}. "
            "Only alphanumerics, dots, underscores, slashes, colons, commas, spaces, and hyphens allowed."
        )
    return value


# ---------------------------------------------------------------------------
# Group 2 — Kernel sysctl
# ---------------------------------------------------------------------------

class SysctlTool(RemediationTool):
    name = "sysctl"
    params_schema = '{"param": "<sysctl_name>", "value": "<new_value>"}'

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        param = params.get("param", "")
        if param not in ALLOWED_SYSCTL:
            return f"unknown param: {param}"
        out, _ = executor.run(f"sysctl -n {shlex.quote(param)}")
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        return current_value.strip() == str(params.get("value", "")).strip()

    def apply(self, params: dict) -> None:
        param = params["param"]
        value = _validate_value(str(params["value"]), "sysctl value")
        if param not in ALLOWED_SYSCTL:
            raise ValueError(f"sysctl param '{param}' not in allowlist")
        self._original_param = param
        q_param = shlex.quote(param)
        q_value = shlex.quote(value)
        self._original = self._run(f"sysctl -n {q_param}")
        self._no_op_check(self._original, value, f"sysctl {param}")
        logger.info("sysctl %s: %s → %s", param, self._original, value)
        self._run(f"sysctl -w {q_param}={q_value}")
        self._log_verified(f"sysctl -n {q_param}", f"sysctl {param}")

    def rollback(self) -> None:
        if self._original:
            logger.info("Rollback sysctl %s → %s", self._original_param, self._original)
            self._run(f"sysctl -w {shlex.quote(self._original_param)}={shlex.quote(self._original)}")


# ---------------------------------------------------------------------------
# Group 3 — Systemd service properties
# ---------------------------------------------------------------------------

# Properties that need drop-in files + daemon-reload + restart
_DROPIN_PROPS = {"LimitNOFILE", "LimitNPROC"}
_DROPIN_DIR = "/etc/systemd/system/nginx.service.d"

# Properties that use systemctl set-property (runtime)
_SETPROP_PROPS = {"CPUQuota", "CPUWeight", "MemoryMax", "MemoryHigh", "IOWeight",
                  "Nice", "OOMScoreAdjust", "TasksMax"}


class SystemdPropertyTool(RemediationTool):
    name = "systemd_property"
    params_schema = '{"property": "<LimitNOFILE|CPUQuota|...>", "value": "<new_value>"}'

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        prop = params.get("property", "")
        if prop not in ALLOWED_SYSTEMD:
            return f"unknown property: {prop}"
        q_prop = shlex.quote(prop)
        # CPUQuota: read CPUQuotaPerSecUSec for accurate detection
        if prop == "CPUQuota":
            out, _ = executor.run(
                "systemctl show nginx.service -p CPUQuotaPerSecUSec | awk -F= '{print $2}'"
            )
            raw = out.strip()
            if raw == "infinity":
                return "infinity"
            # Convert µs to percentage: e.g. 500000 → 50%
            try:
                usec = int(raw.replace("us", ""))
                return f"{usec / 10000:.0f}%"
            except (ValueError, AttributeError):
                return raw
        out, _ = executor.run(
            f"systemctl show nginx.service -p {q_prop} | awk -F= '{{print $2}}'"
        )
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        prop = params.get("property", "")
        value = str(params.get("value", "")).strip()
        current = current_value.strip()
        # CPUQuota: "infinity" means already removed
        if prop == "CPUQuota" and current == "infinity":
            return True
        return current == value

    def apply(self, params: dict) -> None:
        prop  = params["property"]
        value = str(params["value"])
        if prop not in ALLOWED_SYSTEMD:
            raise ValueError(f"systemd property '{prop}' not in allowlist")
        # CPUQuota allows empty value; others must validate
        if prop != "CPUQuota":
            value = _validate_value(value, "systemd value")
        self._prop = prop
        self._original = self.read_current(self.executor, params)
        self._no_op_check(self._original, value, f"systemd {prop}")
        logger.info("systemd nginx.service %s: %s → %s", prop, self._original, value)

        # Cross-validate: LimitNOFILE must not exceed fs.nr_open
        if prop == "LimitNOFILE" and value.isdigit():
            nr_open = self._run("sysctl -n fs.nr_open").strip()
            if nr_open.isdigit() and int(value) > int(nr_open):
                new_nr_open = max(int(value), 1048576)
                logger.info(
                    "LimitNOFILE(%s) > fs.nr_open(%s) — raising kernel limits first",
                    value, nr_open,
                )
                self._run(f"sysctl -w fs.nr_open={new_nr_open}")
                self._run(f"sysctl -w fs.file-max={new_nr_open}")

        if prop == "CPUQuota":
            # CPUQuota removal: empty value removes the limit entirely
            self._run("systemctl set-property nginx.service CPUQuota=")
            self._run("systemctl daemon-reload")
        elif prop in _DROPIN_PROPS:
            # LimitNOFILE/LimitNPROC need drop-in files
            q_value = shlex.quote(value)
            dropin_name = f"zz_hosttune_{prop.lower()}.conf"
            dropin_path = f"{_DROPIN_DIR}/{dropin_name}"
            self._dropin_path = dropin_path
            self._run(f"mkdir -p {_DROPIN_DIR}")
            self._run(
                f"printf '[Service]\\n{prop}={q_value}\\n' > {shlex.quote(dropin_path)}"
            )
            self._run("systemctl daemon-reload && systemctl restart nginx")
        else:
            # CPUWeight, MemoryMax, IOWeight — use set-property
            q_prop = shlex.quote(prop)
            q_value = shlex.quote(value)
            self._run(f"systemctl set-property nginx.service {q_prop}={q_value}")
            self._run("systemctl daemon-reload")

        self._log_verified(
            f"systemctl show nginx.service -p {shlex.quote(prop)} | awk -F= '{{print $2}}'",
            f"systemd {prop}",
        )

    def rollback(self) -> None:
        if not self._original:
            return
        logger.info("Rollback systemd %s → %s", self._prop, self._original)
        if self._prop == "CPUQuota":
            # Restore original quota
            q_orig = shlex.quote(self._original)
            self._run(f"systemctl set-property nginx.service CPUQuota={q_orig}")
            self._run("systemctl daemon-reload")
        elif self._prop in _DROPIN_PROPS:
            # Remove drop-in file and restart
            if hasattr(self, "_dropin_path"):
                self._run(f"rm -f {shlex.quote(self._dropin_path)}")
            self._run("systemctl daemon-reload && systemctl restart nginx")
        else:
            q_prop = shlex.quote(self._prop)
            q_orig = shlex.quote(self._original)
            self._run(f"systemctl set-property nginx.service {q_prop}={q_orig}")
            self._run("systemctl daemon-reload")


# ---------------------------------------------------------------------------
# Group 4 — Nginx directives (edit config + reload)
# ---------------------------------------------------------------------------

class NginxDirectiveTool(RemediationTool):
    name = "nginx_directive"
    params_schema = '{"directive": "<nginx_directive_name>", "value": "<new_value>"}'
    _CONF = "/etc/nginx/nginx.conf"

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        directive = params.get("directive", "")
        if directive not in ALLOWED_NGINX:
            return f"unknown directive: {directive}"
        q_dir = shlex.quote(directive)
        # Collect ALL occurrences — server block overrides http block
        out, _ = executor.run(
            f"nginx -T 2>/dev/null | grep -E '^\\s*'{q_dir}'\\s+'"
        )
        lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
        if not lines:
            return "not set"
        # Return last occurrence (server block wins over http block)
        return lines[-1]

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        target = str(params.get("value", "")).strip()
        return target in current_value and current_value.strip() != "not set"

    def apply(self, params: dict) -> None:
        directive = params["directive"]
        value     = _validate_value(str(params["value"]), "nginx value")
        if directive not in ALLOWED_NGINX:
            raise ValueError(f"nginx directive '{directive}' not in allowlist")
        self._directive = directive
        q_conf = shlex.quote(self._CONF)
        q_dir = shlex.quote(directive)
        q_value = shlex.quote(value)
        self._run(f"cp {q_conf} {q_conf}.bak")
        # Read all occurrences; last one is the effective value (server > http)
        all_matches = self._run(
            f"nginx -T 2>/dev/null | grep -E '^\\s*'{q_dir}'\\s+'"
        )
        lines = [ln.strip() for ln in all_matches.strip().splitlines() if ln.strip()]
        self._original = lines[-1] if lines else ""
        logger.info("nginx %s: [%s] → %s", directive, self._original.strip(), value)
        # Use awk for safer in-place editing instead of sed with unescaped regex
        self._run(
            f"awk -v dir={q_dir} -v val={q_value} "
            "'$1 == dir {$0 = \"    \" dir \" \" val \";\"} 1' "
            f"{q_conf} > {q_conf}.tmp && mv {q_conf}.tmp {q_conf}"
        )
        self._run("nginx -t && nginx -s reload")
        self._log_verified(
            f"nginx -T 2>/dev/null | grep -E '^\\s*'{q_dir}'\\s+' | head -n1",
            f"nginx {directive}",
        )

    def rollback(self) -> None:
        logger.info("Rollback nginx config from .bak")
        q_conf = shlex.quote(self._CONF)
        self._run(f"cp {q_conf}.bak {q_conf}")
        self._run("nginx -t && nginx -s reload")


class NginxListenBacklogTool(RemediationTool):
    name = "nginx_listen_backlog"
    params_schema = '{"value": <integer_backlog_size>}'
    _CONF = "/etc/nginx/nginx.conf"

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        out, _ = executor.run(
            "grep -E 'listen.*(backlog)' /etc/nginx/nginx.conf | head -n1"
        )
        return out.strip() if out.strip() else "no backlog set"

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        target = str(params.get("value", "")).strip()
        return target in current_value and current_value != "no backlog set"

    def apply(self, params: dict) -> None:
        value = int(params["value"])  # int() rejects non-numeric input
        if not (1 <= value <= 65535):
            raise ValueError(f"backlog value {value} out of range (1-65535)")
        q_conf = shlex.quote(self._CONF)
        self._run(f"cp {q_conf} {q_conf}.bak")
        current = self._run(
            "grep -E 'listen.*(backlog)' /etc/nginx/nginx.conf | head -n1"
        ) or "no backlog set"
        self._no_op_check(current, str(value), "nginx listen backlog")
        logger.info("nginx listen backlog: [%s] → %d", current, value)
        self._run(
            f"sed -i 's/listen\\s\\+\\(80\\|443\\)\\b/listen \\1 backlog={value}/' {q_conf}"
        )
        self._run("nginx -t && nginx -s reload")
        self._log_verified(
            "grep -E 'listen.*(backlog)' /etc/nginx/nginx.conf | head -n1",
            "nginx listen backlog",
        )

    def rollback(self) -> None:
        logger.info("Rollback nginx listen backlog from .bak")
        q_conf = shlex.quote(self._CONF)
        self._run(f"cp {q_conf}.bak {q_conf}")
        self._run("nginx -t && nginx -s reload")


# ---------------------------------------------------------------------------
# Group 1 — Hardware
# ---------------------------------------------------------------------------

class CpuGovernorTool(RemediationTool):
    name = "cpu_governor"
    params_schema = '{"governor": "<performance|powersave|ondemand|conservative>"}'

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        out, _ = executor.run(
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        )
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        return current_value.strip() == params.get("governor", "").strip()

    def apply(self, params: dict) -> None:
        governor = params["governor"]
        if governor not in ALLOWED_CPU_GOVERNORS:
            raise ValueError(f"CPU governor '{governor}' not in allowlist")
        # governor is from the allowlist, but quote anyway for defense-in-depth
        q_gov = shlex.quote(governor)
        self._original = self._run(
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        )
        logger.info("CPU governor: %s → %s", self._original, governor)
        self._run(
            f"for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; "
            f"do echo {q_gov} > $f; done"
        )

    def rollback(self) -> None:
        if self._original:
            logger.info("Rollback CPU governor → %s", self._original)
            q_orig = shlex.quote(self._original)
            self._run(
                f"for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; "
                f"do echo {q_orig} > $f; done"
            )


# ---------------------------------------------------------------------------
# Group 1 — IRQ Balance
# ---------------------------------------------------------------------------

class IrqbalanceTool(RemediationTool):
    """Enables and restarts irqbalance — fixes both inactive irqbalance
    and pinned NIC IRQ affinity in one shot."""

    name = "irqbalance"
    params_schema = "{}"

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        out, _ = executor.run("systemctl is-active irqbalance 2>/dev/null || echo inactive")
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        return current_value.strip() == "active"

    def apply(self, params: dict) -> None:
        self._original = self._run(
            "systemctl is-active irqbalance 2>/dev/null || echo inactive"
        )
        if self._original == "active":
            raise ValueError("No-op: irqbalance is already active — skipping")
        logger.info("irqbalance: %s → active", self._original)
        self._run("systemctl enable irqbalance 2>/dev/null || true")
        self._run("systemctl start irqbalance")
        self._run("systemctl restart irqbalance")  # force re-balance all IRQ affinities
        self._log_verified(
            "systemctl is-active irqbalance", "irqbalance",
        )

    def rollback(self) -> None:
        logger.info("Rollback irqbalance → %s", self._original)
        self._run("systemctl stop irqbalance")
        self._run("systemctl disable irqbalance")


# ---------------------------------------------------------------------------
# Group 1 — Readahead
# ---------------------------------------------------------------------------

class ReadaheadTool(RemediationTool):
    """Sets block device readahead sectors."""

    name = "readahead"
    params_schema = '{"value": <integer_sectors>}'

    _DETECT_DEV = (
        "lsblk -dno NAME,TYPE | awk '$2==\"disk\" {print $1}' | grep -m1 nvme || "
        "lsblk -dno NAME,TYPE | awk '$2==\"disk\" {print $1}' | head -1"
    )

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        dev = cls._get_device(executor)
        out, _ = executor.run(f"blockdev --getra /dev/{shlex.quote(dev)}")
        return out.strip()

    @classmethod
    def _get_device(cls, executor: RemoteExecutor) -> str:
        out, _ = executor.run(cls._DETECT_DEV)
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        return current_value.strip() == str(params.get("value", "")).strip()

    def apply(self, params: dict) -> None:
        value = int(params["value"])
        if not (1 <= value <= 65536):
            raise ValueError(f"readahead value {value} out of range (1-65536)")
        self._dev = self._get_device(self.executor)
        q_dev = shlex.quote(f"/dev/{self._dev}")
        self._original = self._run(f"blockdev --getra {q_dev}")
        self._no_op_check(self._original, str(value), "readahead")
        logger.info("readahead %s: %s → %d", self._dev, self._original, value)
        self._run(f"blockdev --setra {value} {q_dev}")
        # Also fix partition devices
        self._run(
            f"for part in /dev/{shlex.quote(self._dev)}p*; do "
            f"blockdev --setra {value} \"$part\" 2>/dev/null || true; done"
        )
        self._log_verified(f"blockdev --getra {q_dev}", f"readahead {self._dev}")

    def rollback(self) -> None:
        if self._original:
            q_dev = shlex.quote(f"/dev/{self._dev}")
            logger.info("Rollback readahead %s → %s", self._dev, self._original)
            self._run(f"blockdev --setra {self._original} {q_dev}")


# ---------------------------------------------------------------------------
# Group 1 — I/O Scheduler
# ---------------------------------------------------------------------------

ALLOWED_IO_SCHEDULERS = {"none", "mq-deadline", "kyber", "bfq"}


class IoSchedulerTool(RemediationTool):
    """Sets the block device I/O scheduler."""

    name = "io_scheduler"
    params_schema = '{"value": "<none|mq-deadline|kyber|bfq>"}'

    _DETECT_DEV = ReadaheadTool._DETECT_DEV

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        dev = cls._get_device(executor)
        out, _ = executor.run(f"cat /sys/block/{shlex.quote(dev)}/queue/scheduler")
        return out.strip()

    @classmethod
    def _get_device(cls, executor: RemoteExecutor) -> str:
        out, _ = executor.run(cls._DETECT_DEV)
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        target = params.get("value", "").strip()
        return f"[{target}]" in current_value

    def apply(self, params: dict) -> None:
        value = params["value"].strip()
        if value not in ALLOWED_IO_SCHEDULERS:
            raise ValueError(f"I/O scheduler '{value}' not in allowlist")
        self._dev = self._get_device(self.executor)
        # Guard: only set 'none' on NVMe — mq-deadline is correct for HDD
        if value == "none" and "nvme" not in self._dev:
            raise ValueError(
                f"Refusing to set scheduler=none on non-NVMe device '{self._dev}'. "
                "mq-deadline is correct for rotational disks."
            )
        q_dev = shlex.quote(self._dev)
        sched_path = f"/sys/block/{q_dev}/queue/scheduler"
        self._original_raw = self._run(f"cat {sched_path}")
        # Extract current active scheduler from "[mq-deadline] none kyber"
        import re
        m = re.search(r"\[(\w[\w-]*)\]", self._original_raw)
        self._original_sched = m.group(1) if m else "mq-deadline"
        if f"[{value}]" in self._original_raw:
            raise ValueError(f"No-op: I/O scheduler is already [{value}] — skipping")
        logger.info("I/O scheduler %s: [%s] → %s", self._dev, self._original_sched, value)
        self._run(f"echo {shlex.quote(value)} > {sched_path}")
        self._log_verified(f"cat {sched_path}", f"io_scheduler {self._dev}")

    def rollback(self) -> None:
        if hasattr(self, "_original_sched"):
            q_dev = shlex.quote(self._dev)
            logger.info("Rollback I/O scheduler %s → %s", self._dev, self._original_sched)
            self._run(
                f"echo {shlex.quote(self._original_sched)} > /sys/block/{q_dev}/queue/scheduler"
            )


# ---------------------------------------------------------------------------
# Registry — core tools always available; network tools added if enabled
# ---------------------------------------------------------------------------

from .network_tools import NETWORK_TOOL_CLASSES  # noqa: E402

_CORE_TOOLS = [
    SysctlTool,
    SystemdPropertyTool,
    NginxDirectiveTool,
    NginxListenBacklogTool,
    CpuGovernorTool,
    IrqbalanceTool,
    ReadaheadTool,
    IoSchedulerTool,
]

# Full registry (network tools included — filtered at agent level based on config)
TOOL_REGISTRY: dict[str, type[RemediationTool]] = {
    t.name: t for t in _CORE_TOOLS + NETWORK_TOOL_CLASSES
}

# Names of network-level tools — agent checks config before allowing these
NETWORK_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in NETWORK_TOOL_CLASSES)


# Expected parameter keys per tool — derived from params_schema definitions above
_REQUIRED_PARAMS: dict[str, set[str]] = {
    "sysctl":             {"param", "value"},
    "systemd_property":   {"property", "value"},
    "nginx_directive":    {"directive", "value"},
    "nginx_listen_backlog": {"value"},
    "cpu_governor":       {"governor"},
    "irqbalance":         set(),
    "readahead":          {"value"},
    "io_scheduler":       {"value"},
    "tc_shaping":         set(),
    "iptables_connlimit": set(),
    "nftables_ratelimit": set(),
}


def dispatch(tool_name: str, params: dict, executor: RemoteExecutor) -> RemediationTool:
    """Instantiate and apply a tool by name. Raises ValueError for unknown tools or bad params."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("tool_name must be a non-empty string")
    if not isinstance(params, dict):
        raise ValueError(f"params must be a dict, got {type(params).__name__}")
    if tool_name not in TOOL_REGISTRY:
        raise ValueError(
            f"Unknown tool '{tool_name}'. Allowed: {sorted(TOOL_REGISTRY)}"
        )
    # Validate required params are present
    required = _REQUIRED_PARAMS.get(tool_name, set())
    missing = required - set(params.keys())
    if missing:
        raise ValueError(
            f"Tool '{tool_name}' missing required params: {sorted(missing)}. "
            f"Expected: {sorted(required)}"
        )
    tool = TOOL_REGISTRY[tool_name](executor)
    tool.apply(params)
    return tool  # caller holds reference for rollback
