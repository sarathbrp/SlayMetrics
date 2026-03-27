from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from adapters.base import ServiceAdapter
from memory.tidb_store import TiDBStore
from tools.ssh import LocalClient, SSHClient


@dataclass
class TokenCounter:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    tool_tokens: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, usage) -> tuple[int, int]:
        inp = 0
        out = 0
        if hasattr(usage, "input_tokens"):
            inp = usage.input_tokens or 0
            out = usage.output_tokens or 0
        elif hasattr(usage, "request_tokens"):
            inp = usage.request_tokens or 0
            out = usage.response_tokens or 0
        self.input_tokens += inp
        self.output_tokens += out
        self.tool_calls += 1
        return inp, out

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def summary(self) -> str:
        return (
            f"in={self.input_tokens:,} out={self.output_tokens:,} "
            f"total={self.total:,} calls={self.tool_calls}"
        )

    def add_tool_tokens(
        self,
        tool_name: str,
        *,
        call_input: int = 0,
        call_output: int = 0,
        post_input: int = 0,
        post_output: int = 0,
        calls: int = 0,
    ) -> None:
        if tool_name not in self.tool_tokens:
            self.tool_tokens[tool_name] = {
                "calls": 0,
                "call_input_tokens": 0,
                "call_output_tokens": 0,
                "post_input_tokens": 0,
                "post_output_tokens": 0,
            }
        row = self.tool_tokens[tool_name]
        row["calls"] += calls
        row["call_input_tokens"] += max(0, int(call_input))
        row["call_output_tokens"] += max(0, int(call_output))
        row["post_input_tokens"] += max(0, int(post_input))
        row["post_output_tokens"] += max(0, int(post_output))

    def tool_token_rows(self) -> list[dict[str, Any]]:
        rows = []
        for tool_name, row in sorted(self.tool_tokens.items()):
            total_in = row["call_input_tokens"] + row["post_input_tokens"]
            total_out = row["call_output_tokens"] + row["post_output_tokens"]
            rows.append(
                {
                    "tool": tool_name,
                    "calls": row["calls"],
                    "call_input_tokens": row["call_input_tokens"],
                    "call_output_tokens": row["call_output_tokens"],
                    "post_input_tokens": row["post_input_tokens"],
                    "post_output_tokens": row["post_output_tokens"],
                    "input_tokens": total_in,
                    "output_tokens": total_out,
                    "total_tokens": total_in + total_out,
                }
            )
        return rows


@dataclass
class AgentDeps:
    adapter: ServiceAdapter
    memory: TiDBStore
    ssh: LocalClient | SSHClient
    session_id: str
    config: dict
    bench: LocalClient | SSHClient | None = None
    token_counter: TokenCounter = field(default_factory=TokenCounter)
    langfuse: Any | None = None

    def __post_init__(self) -> None:
        if self.bench is None:
            self.bench = self.ssh
