from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agents import TokenCounter
from agents.agent import build


class FakeMemory:
    def __init__(self):
        self.calls: list[str] = []

    def semantic_search(self, symptom: str, session_id: str, top_k: int = 5) -> list[dict]:
        del session_id
        del top_k
        self.calls.append(symptom)
        return [{"symptom": symptom}]


def test_query_memory_soft_cap_after_three_calls():
    memory = FakeMemory()
    deps = SimpleNamespace(
        memory=memory,
        session_id="s1",
        token_counter=TokenCounter(),
    )
    ctx = SimpleNamespace(deps=deps)

    agent = build("model")
    query_memory = agent._function_toolset.tools["query_memory"].function

    r1 = asyncio.run(query_memory(ctx, "cpu saturation"))
    r2 = asyncio.run(query_memory(ctx, "queue buildup"))
    r3 = asyncio.run(query_memory(ctx, "socket backlog"))
    r4 = asyncio.run(query_memory(ctx, "cache misses"))

    assert r1 == [{"symptom": "cpu saturation"}]
    assert r2 == [{"symptom": "queue buildup"}]
    assert r3 == [{"symptom": "socket backlog"}]
    assert r4 == []
    assert memory.calls == ["cpu saturation", "queue buildup", "socket backlog"]
    assert deps.token_counter.tool_calls == 4
