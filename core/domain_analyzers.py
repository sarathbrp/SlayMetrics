"""
Domain-specific analyzers for the multi-node RCA graph.

Each analyzer handles one domain (network / kernel / nginx), uses a focused
prompt, and returns structured fixes + a context summary for the next node.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import dspy

from .config import Config

# MLflow trace decorator — falls back to no-op if mlflow unavailable
try:
    import mlflow
    _trace = mlflow.trace
except (ImportError, AttributeError):
    def _trace(fn=None, **kwargs):  # type: ignore
        return fn if fn else (lambda f: f)


def _save_prompt(save_dir: Path, name: str, inputs: dict,
                 fixes: list, summary: str, in_tok: int, out_tok: int) -> None:
    """Save LLM prompt + response to session folder for debugging."""
    try:
        history = dspy.settings.lm.history
        raw_messages = history[-1] if history else {}
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"prompt_{name}.json"
        payload = {
            "timestamp":    datetime.now().isoformat(),
            "domain":       name,
            "inputs":       {k: v[:2000] + "…" if isinstance(v, str) and len(v) > 2000 else v
                             for k, v in inputs.items()},
            "fixes":        fixes,
            "summary":      summary,
            "tokens":       {"input": in_tok, "output": out_tok},
            "raw_messages": raw_messages,
        }
        path.write_text(json.dumps(payload, indent=2, default=str))
        logger.debug("Prompt saved to %s", path)
    except Exception as e:
        logger.warning("Failed to save prompt for %s: %s", name, e)

logger = logging.getLogger("slayMetrics.analyzer")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]")


def extract_audit_groups(audit_output: str, groups: list[int]) -> str:
    """Return only the requested audit groups from omega_master_audit.sh output."""
    clean  = _ANSI_RE.sub("", audit_output)
    lines  = clean.splitlines()
    result: list[str] = []
    include = False
    for line in lines:
        for g in range(1, 6):
            if f"[{g}/5]" in line:
                include = g in groups
                break
        if include:
            result.append(line)
    return "\n".join(result)


def _extract_tokens() -> tuple[int, int]:
    history = dspy.settings.lm.history
    if not history:
        return 0, 0
    usage = history[-1].get("usage", {})
    return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def _parse_fixes_json(raw: str) -> tuple[list[dict], str]:
    """Parse the LLM JSON output → (fixes, summary)."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
    data = json.loads(raw)
    fixes   = data.get("fixes", []) if isinstance(data, dict) else data
    summary = data.get("summary", "") if isinstance(data, dict) else ""
    return fixes, summary


# ---------------------------------------------------------------------------
# Network Analyzer
# ---------------------------------------------------------------------------

_NET_TOOL_DOCS = (
    '  "tc_shaping": params={}\n'
    '  "iptables_connlimit": params={}\n'
    '  "nftables_ratelimit": params={}\n'
    '  "sysctl": params={"param": "net.netfilter.nf_conntrack_max", "value": "262144"}'
)


class NetworkAnalyzer:
    """Identifies TC shaping, iptables/nftables blocks, conntrack exhaustion."""

    def __init__(self, config: Config, prompts_dir: Path):
        self.config      = config
        self.prompts_dir = prompts_dir
        self._module: dspy.Module | None = None

    def _build(self) -> dspy.Module:
        instructions = (self.prompts_dir / "network_analysis.md").read_text()

        class Sig(dspy.Signature):
            network_audit_section: str = dspy.InputField(
                desc="Group 5 (Traffic Control & Error Telemetry) from omega_master_audit.sh"
            )
            live_audit_output: str = dspy.InputField(
                desc="Dynamic runtime metrics (NIC discards, softirq, TCP state) from live sampler"
            )
            similar_cases: str = dspy.InputField(
                desc="Similar past cases from semantic memory. Empty if none."
            )
            result_json: str = dspy.OutputField(
                desc=(
                    f'JSON: {{"fixes": [...], "summary": "2-sentence paragraph"}}. '
                    f"Allowed tools:\n{_NET_TOOL_DOCS}"
                )
            )

        Sig.__doc__ = instructions
        return dspy.Predict(Sig)

    @_trace
    def analyze(self, network_section: str, live_audit: str,
                similar_cases: str,
                save_dir: Path | None = None) -> tuple[list[dict], str, int, int, float]:
        """Returns (fixes, summary, input_tokens, output_tokens)."""
        if self._module is None:
            self._module = self._build()
        logger.info("Network analysis — calling LLM...")
        t0 = datetime.now()
        pred = self._module(
            network_audit_section=network_section,
            live_audit_output=live_audit,
            similar_cases=similar_cases,
        )
        elapsed = (datetime.now() - t0).total_seconds()
        fixes, summary = _parse_fixes_json(pred.result_json)
        in_tok, out_tok = _extract_tokens()
        logger.info("Network analysis done in %.1fs — %d fixes found", elapsed, len(fixes))
        if summary:
            logger.info("Network summary: %s", summary)
        for f in fixes:
            logger.info("  [Net fix] %s → tool=%s params=%s", f.get("description", ""), f.get("tool", ""), f.get("params", {}))
        if save_dir:
            _save_prompt(save_dir, "network",
                         {"network_audit_section": network_section,
                          "live_audit_output": live_audit, "similar_cases": similar_cases},
                         fixes, summary, in_tok, out_tok)
        return fixes, summary, in_tok, out_tok, elapsed


# ---------------------------------------------------------------------------
# Kernel Analyzer
# ---------------------------------------------------------------------------

_KERNEL_TOOL_DOCS = (
    '  "sysctl": params={"param": "<sysctl_name>", "value": "<new_value>"}\n'
    '  "systemd_property": params={"property": "<LimitNOFILE|CPUQuota|...>", "value": "<value>"}\n'
    '  "cpu_governor": params={"governor": "<performance|powersave|ondemand|conservative>"}'
)


class KernelAnalyzer:
    """Identifies sysctl, cgroup, and hardware bottlenecks."""

    def __init__(self, config: Config, prompts_dir: Path):
        self.config      = config
        self.prompts_dir = prompts_dir
        self._module: dspy.Module | None = None

    def _build(self) -> dspy.Module:
        instructions = (self.prompts_dir / "kernel_analysis.md").read_text()

        class Sig(dspy.Signature):
            kernel_audit_section: str = dspy.InputField(
                desc="Groups 1-3 (Hardware, Kernel network stack, Systemd envelope) from audit"
            )
            benchmark_results: str = dspy.InputField(
                desc="Plain-text benchmark results showing RPS per workload"
            )
            network_summary: str = dspy.InputField(
                desc="Summary from network analysis node — do not re-fix what is listed here"
            )
            similar_cases: str = dspy.InputField(
                desc="Similar past cases from semantic memory. Empty if none."
            )
            result_json: str = dspy.OutputField(
                desc=(
                    f'JSON: {{"fixes": [...], "summary": "2-sentence paragraph"}}. '
                    f"Allowed tools:\n{_KERNEL_TOOL_DOCS}"
                )
            )

        Sig.__doc__ = instructions
        return dspy.Predict(Sig)

    @_trace
    def analyze(self, kernel_section: str, benchmark_results: str,
                network_summary: str, similar_cases: str,
                save_dir: Path | None = None) -> tuple[list[dict], str, int, int, float]:
        """Returns (fixes, summary, input_tokens, output_tokens)."""
        if self._module is None:
            self._module = self._build()
        logger.info("Kernel analysis — calling LLM...")
        t0 = datetime.now()
        pred = self._module(
            kernel_audit_section=kernel_section,
            benchmark_results=benchmark_results,
            network_summary=network_summary,
            similar_cases=similar_cases,
        )
        elapsed = (datetime.now() - t0).total_seconds()
        fixes, summary = _parse_fixes_json(pred.result_json)
        in_tok, out_tok = _extract_tokens()
        logger.info("Kernel analysis done in %.1fs — %d fixes found", elapsed, len(fixes))
        if summary:
            logger.info("Kernel summary: %s", summary)
        for f in fixes:
            logger.info("  [Kernel fix] %s → tool=%s params=%s", f.get("description", ""), f.get("tool", ""), f.get("params", {}))
        if save_dir:
            _save_prompt(save_dir, "kernel",
                         {"kernel_audit_section": kernel_section,
                          "benchmark_results": benchmark_results,
                          "network_summary": network_summary, "similar_cases": similar_cases},
                         fixes, summary, in_tok, out_tok)
        return fixes, summary, in_tok, out_tok, elapsed


# ---------------------------------------------------------------------------
# Nginx Analyzer
# ---------------------------------------------------------------------------

_NGINX_TOOL_DOCS = (
    '  "nginx_directive": params={"directive": "<name>", "value": "<new_value>"}\n'
    '  "nginx_listen_backlog": params={"value": <integer>}'
)


class NginxAnalyzer:
    """Identifies nginx config bottlenecks given network+kernel context."""

    def __init__(self, config: Config, prompts_dir: Path):
        self.config      = config
        self.prompts_dir = prompts_dir
        self._module: dspy.Module | None = None

    def _build(self) -> dspy.Module:
        instructions = (self.prompts_dir / "nginx_analysis.md").read_text()

        class Sig(dspy.Signature):
            nginx_audit_section: str = dspy.InputField(
                desc="Group 4 (NGINX Internal Directives) from omega_master_audit.sh"
            )
            benchmark_results: str = dspy.InputField(
                desc="Plain-text benchmark results showing RPS per workload"
            )
            network_summary: str = dspy.InputField(
                desc="Summary from network analysis — do not repeat fixes listed here"
            )
            kernel_summary: str = dspy.InputField(
                desc="Summary from kernel analysis — includes LimitNOFILE and somaxconn context"
            )
            similar_cases: str = dspy.InputField(
                desc="Similar past cases from semantic memory. Empty if none."
            )
            result_json: str = dspy.OutputField(
                desc=(
                    f'JSON: {{"fixes": [...]}}. '
                    f"Allowed tools:\n{_NGINX_TOOL_DOCS}"
                )
            )

        Sig.__doc__ = instructions
        return dspy.Predict(Sig)

    @_trace
    def analyze(self, nginx_section: str, benchmark_results: str, network_summary: str,
                kernel_summary: str, similar_cases: str,
                save_dir: Path | None = None) -> tuple[list[dict], int, int, float]:
        """Returns (fixes, input_tokens, output_tokens)."""
        if self._module is None:
            self._module = self._build()
        logger.info("Nginx analysis — calling LLM...")
        t0 = datetime.now()
        pred = self._module(
            nginx_audit_section=nginx_section,
            benchmark_results=benchmark_results,
            network_summary=network_summary,
            kernel_summary=kernel_summary,
            similar_cases=similar_cases,
        )
        elapsed = (datetime.now() - t0).total_seconds()
        fixes, _ = _parse_fixes_json(pred.result_json)
        in_tok, out_tok = _extract_tokens()
        logger.info("Nginx analysis done in %.1fs — %d fixes found", elapsed, len(fixes))
        for f in fixes:
            logger.info("  [Nginx fix] %s → tool=%s params=%s", f.get("description", ""), f.get("tool", ""), f.get("params", {}))
        if save_dir:
            _save_prompt(save_dir, "nginx",
                         {"nginx_audit_section": nginx_section,
                          "benchmark_results": benchmark_results,
                          "network_summary": network_summary,
                          "kernel_summary": kernel_summary, "similar_cases": similar_cases},
                         fixes, "", in_tok, out_tok)
        return fixes, in_tok, out_tok, elapsed
