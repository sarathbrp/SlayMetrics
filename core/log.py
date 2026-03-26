from __future__ import annotations

import os
import re
import textwrap
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

_console = Console()
_log_file = None
_verbose = False
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _clean(message: str) -> str:
    text = _ANSI_RE.sub("", str(message))
    text = text.replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())


def init(session_id: str, verbose: bool = False, log_dir: str = "report") -> str:
    global _log_file, _verbose
    _verbose = verbose
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(log_dir, f"log_{ts}_{session_id}.md")
    _log_file = open(path, "w")
    _log_file.write(f"# SlayMetricsAgent Log — {session_id}\n")
    _log_file.write(f"Started: {datetime.now().isoformat()}\n\n")
    _log_file.flush()
    _console.print(f"[dim]Log file:[/dim] {path}")
    _console.print(f"[dim]Report will be saved to:[/dim] {log_dir}/report_*_{session_id}.md")
    return path


def log(agent: str, message: str, level: str = "info") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    tag = f"[{agent}]"
    msg = _clean(message)

    if _log_file:
        _log_file.write(f"{ts} {tag:16s} {msg}\n")
        _log_file.flush()

    color = {"info": "dim", "action": "yellow", "result": "green",
             "error": "red", "warn": "yellow", "skip": "dim"}.get(level, "dim")

    if level in ("error", "warn", "result", "action"):
        _console.print(f"       [{color}]{tag}[/{color}] {msg}")
    elif _verbose:
        _console.print(f"       [{color}]{tag}[/{color}] {msg}")


def llm_call(agent: str, message: str) -> None:
    """Log an LLM API call — always visible."""
    ts = datetime.now().strftime("%H:%M:%S")
    msg = _clean(message)
    if _log_file:
        _log_file.write(f"{ts} [LLM]            {msg}\n")
        _log_file.flush()
    _console.print(f"       [bold magenta][LLM][/bold magenta] {msg}")


def tool_call(tool_name: str, message: str) -> None:
    """Log a tool execution — always visible."""
    ts = datetime.now().strftime("%H:%M:%S")
    msg = _clean(message)
    if _log_file:
        _log_file.write(f"{ts} [TOOL:{tool_name:12s}] {msg}\n")
        _log_file.flush()
    _console.print(f"       [cyan][TOOL:{tool_name}][/cyan] {msg}")


def tool_result(tool_name: str, message: str) -> None:
    """Log a tool result — verbose only on console, always in file."""
    ts = datetime.now().strftime("%H:%M:%S")
    msg = _clean(message)
    if _log_file:
        _log_file.write(f"{ts} [TOOL:{tool_name:12s}] -> {msg}\n")
        _log_file.flush()
    if _verbose:
        _console.print(f"       [dim][TOOL:{tool_name}][/dim] -> {msg}")


def step(message: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    msg = _clean(message)
    if _log_file:
        _log_file.write(f"\n## {ts} {msg}\n\n")
        _log_file.flush()
    _console.print()
    _console.print(Rule(f"[bold]{msg}[/bold]", style="bright_black"))


def status(agent: str, message: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    msg = _clean(message)
    if _log_file:
        _log_file.write(f"{ts} [{agent}]         {msg}\n")
        _log_file.flush()
    _console.print(f"  [dim]{agent:>10}[/dim]  {msg}")


def check(name: str, value: str, check_status: str, recommendation: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    color = {"ok": "green", "warning": "yellow", "critical": "red"}.get(check_status, "white")
    safe_value = _clean(value)
    wrapped = textwrap.fill(
        safe_value,
        width=max(40, _console.width - 22),
        subsequent_indent=" " * 16,
        break_long_words=False,
    )
    _console.print(f"  [{color}]●[/{color}] {name}: {wrapped}")
    if _log_file:
        rec = f" -> {_clean(recommendation)}" if recommendation else ""
        _log_file.write(f"{ts} [check]          {name}: {safe_value} [{check_status}]{rec}\n")
        _log_file.flush()


def benchmark(label: str, rps: float, p99: float, cpu: float = 0, mem: float = 0) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    msg = f"{label}: {rps:.1f} req/sec  p99={p99:.1f}ms  CPU={cpu:.1f}%  MEM={mem:.0f}MB"
    _console.print(
        f"  [cyan]{label:<16}[/cyan] "
        f"[bold]{rps:>9.1f}[/bold] req/s   "
        f"p99 [yellow]{p99:>8.1f} ms[/yellow]   "
        f"CPU [dim]{cpu:>5.1f}%[/dim]   "
        f"MEM [dim]{mem:>6.0f} MB[/dim]"
    )
    if _log_file:
        _log_file.write(f"{ts} [benchmark]      {msg}\n")
        _log_file.flush()


def panel(title: str, body: str) -> None:
    cleaned_body = "\n".join(_clean(line) for line in str(body).splitlines())
    _console.print(Panel(cleaned_body, title=title, border_style="bright_black"))
    if _log_file:
        ts = datetime.now().strftime("%H:%M:%S")
        _log_file.write(f"\n### {ts} {title}\n{cleaned_body}\n\n")
        _log_file.flush()


def tokens(agent: str, inp: int, out: int, cumulative: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    msg = f"Tokens: in={inp:,} out={out:,} | {_clean(cumulative)}"
    if _log_file:
        _log_file.write(f"{ts} [TOKENS]         {msg}\n")
        _log_file.flush()
    _console.print(f"       [bold blue][TOKENS][/bold blue] {msg}")


def close() -> None:
    if _log_file:
        _log_file.write(f"\nEnded: {datetime.now().isoformat()}\n")
        _log_file.close()
