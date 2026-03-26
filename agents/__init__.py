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
        if hasattr(usage, "input_tokens"):
            self.input_tokens += usage.input_tokens or 0
            self.output_tokens += usage.output_tokens or 0
        self.tool_calls += 1

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class AgentDeps:
    adapter: ServiceAdapter
    memory: TiDBStore
    ssh: SSHClient
    session_id: str
    config: dict
    token_counter: TokenCounter = field(default_factory=TokenCounter)
