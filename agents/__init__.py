from __future__ import annotations

from dataclasses import dataclass, field

from adapters.base import ServiceAdapter
from memory.tidb_store import TiDBStore
from tools.ssh import SSHClient


@dataclass
class TokenCounter:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0

    def add(self, usage) -> None:
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
        return f"in={self.input_tokens:,} out={self.output_tokens:,} total={self.total:,} calls={self.tool_calls}"


@dataclass
class AgentDeps:
    adapter: ServiceAdapter
    memory: TiDBStore
    ssh: SSHClient
    session_id: str
    config: dict
    token_counter: TokenCounter = field(default_factory=TokenCounter)
