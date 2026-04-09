"""
Remediation tools derived directly from omega_master_audit.sh.
Each tool maps to a parameter group the audit measures.
The LLM can ONLY call tools in TOOL_REGISTRY — no arbitrary shell commands.

Network-level tools (TC, iptables, nftables) live in network_tools.py and are
added to TOOL_REGISTRY only when config.remediation.network_tools.enabled = true.
"""

import logging

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
}

ALLOWED_SYSTEMD = {
    "LimitNOFILE", "LimitNPROC", "CPUQuota", "CPUWeight", "MemoryMax", "IOWeight",
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
# Group 2 — Kernel sysctl
# ---------------------------------------------------------------------------

class SysctlTool(RemediationTool):
    name = "sysctl"
    params_schema = '{"param": "<sysctl_name>", "value": "<new_value>"}'

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        out, _ = executor.run(f"sysctl -n {params.get('param', '')}")
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        return current_value.strip() == str(params.get("value", "")).strip()

    def apply(self, params: dict) -> None:
        param = params["param"]
        value = str(params["value"])
        if param not in ALLOWED_SYSCTL:
            raise ValueError(f"sysctl param '{param}' not in allowlist")
        self._original_param = param
        self._original = self._run(f"sysctl -n {param}")
        self._no_op_check(self._original, value, f"sysctl {param}")
        logger.info("sysctl %s: %s → %s", param, self._original, value)
        self._run(f"sysctl -w {param}={value}")
        self._log_verified(f"sysctl -n {param}", f"sysctl {param}")

    def rollback(self) -> None:
        if self._original:
            logger.info("Rollback sysctl %s → %s", self._original_param, self._original)
            self._run(f"sysctl -w {self._original_param}={self._original}")


# ---------------------------------------------------------------------------
# Group 3 — Systemd service properties
# ---------------------------------------------------------------------------

class SystemdPropertyTool(RemediationTool):
    name = "systemd_property"
    params_schema = '{"property": "<LimitNOFILE|CPUQuota|...>", "value": "<new_value>"}'

    @classmethod
    def read_current(cls, executor: RemoteExecutor, params: dict) -> str:
        prop = params.get("property", "")
        out, _ = executor.run(
            f"systemctl show nginx.service -p {prop} | awk -F= '{{print $2}}'"
        )
        return out.strip()

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        return current_value.strip() == str(params.get("value", "")).strip()

    def apply(self, params: dict) -> None:
        prop  = params["property"]
        value = str(params["value"])
        if prop not in ALLOWED_SYSTEMD:
            raise ValueError(f"systemd property '{prop}' not in allowlist")
        self._prop = prop
        self._original = self._run(
            f"systemctl show nginx.service -p {prop} | awk -F= '{{print $2}}'"
        )
        self._no_op_check(self._original, value, f"systemd {prop}")
        logger.info("systemd nginx.service %s: %s → %s", prop, self._original, value)
        self._run(f"systemctl set-property nginx.service {prop}={value}")
        self._log_verified(
            f"systemctl show nginx.service -p {prop} | awk -F= '{{print $2}}'",
            f"systemd {prop}",
        )

    def rollback(self) -> None:
        if self._original:
            logger.info("Rollback systemd %s → %s", self._prop, self._original)
            self._run(f"systemctl set-property nginx.service {self._prop}={self._original}")


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
        out, _ = executor.run(
            f"nginx -T 2>/dev/null | grep -E '^\\s*{directive}\\s+' | head -n1"
        )
        return out.strip() or "not set"

    @classmethod
    def is_no_op(cls, current_value: str, params: dict) -> bool:
        target = str(params.get("value", "")).strip()
        return target in current_value and current_value.strip() != "not set"

    def apply(self, params: dict) -> None:
        directive = params["directive"]
        value     = str(params["value"])
        if directive not in ALLOWED_NGINX:
            raise ValueError(f"nginx directive '{directive}' not in allowlist")
        self._directive = directive
        self._run(f"cp {self._CONF} {self._CONF}.bak")
        self._original = self._run(
            f"nginx -T 2>/dev/null | grep -E '^\\s*{directive}\\s+' | head -n1"
        )
        logger.info("nginx %s: [%s] → %s", directive, self._original.strip(), value)
        self._run(
            f"sed -i 's/^\\(\\s*{directive}\\s\\+\\).*$/\\1{value};/' {self._CONF}"
        )
        self._run("nginx -t && nginx -s reload")
        self._log_verified(
            f"nginx -T 2>/dev/null | grep -E '^\\s*{directive}\\s+' | head -n1",
            f"nginx {directive}",
        )

    def rollback(self) -> None:
        logger.info("Rollback nginx config from .bak")
        self._run(f"cp {self._CONF}.bak {self._CONF}")
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
        value = int(params["value"])
        self._run(f"cp {self._CONF} {self._CONF}.bak")
        current = self._run(
            "grep -E 'listen.*(backlog)' /etc/nginx/nginx.conf | head -n1"
        ) or "no backlog set"
        self._no_op_check(current, str(value), "nginx listen backlog")
        logger.info("nginx listen backlog: [%s] → %d", current, value)
        self._run(
            f"sed -i 's/listen\\s\\+\\(80\\|443\\)\\b/listen \\1 backlog={value}/' {self._CONF}"
        )
        self._run("nginx -t && nginx -s reload")
        self._log_verified(
            "grep -E 'listen.*(backlog)' /etc/nginx/nginx.conf | head -n1",
            "nginx listen backlog",
        )

    def rollback(self) -> None:
        logger.info("Rollback nginx listen backlog from .bak")
        self._run(f"cp {self._CONF}.bak {self._CONF}")
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
        self._original = self._run(
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        )
        logger.info("CPU governor: %s → %s", self._original, governor)
        self._run(
            f"for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; "
            f"do echo {governor} > $f; done"
        )

    def rollback(self) -> None:
        if self._original:
            logger.info("Rollback CPU governor → %s", self._original)
            self._run(
                f"for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; "
                f"do echo {self._original} > $f; done"
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
]

# Full registry (network tools included — filtered at agent level based on config)
TOOL_REGISTRY: dict[str, type[RemediationTool]] = {
    t.name: t for t in _CORE_TOOLS + NETWORK_TOOL_CLASSES
}

# Names of network-level tools — agent checks config before allowing these
NETWORK_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in NETWORK_TOOL_CLASSES)


def dispatch(tool_name: str, params: dict, executor: RemoteExecutor) -> RemediationTool:
    """Instantiate and apply a tool by name. Raises ValueError for unknown tools."""
    if tool_name not in TOOL_REGISTRY:
        raise ValueError(
            f"Unknown tool '{tool_name}'. Allowed: {sorted(TOOL_REGISTRY)}"
        )
    tool = TOOL_REGISTRY[tool_name](executor)
    tool.apply(params)
    return tool  # caller holds reference for rollback
