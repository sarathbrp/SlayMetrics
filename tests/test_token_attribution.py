from __future__ import annotations

from types import SimpleNamespace

from agents import TokenCounter
from agents.agent import _attribute_tool_tokens


class FakeRunResult:
    def __init__(self, messages):
        self._messages = messages

    def all_messages(self):
        return self._messages


def _msg_with_usage_and_tools(inp: int, out: int, tools: list[str]):
    parts = [SimpleNamespace(part_kind="tool-call", tool_name=t) for t in tools]
    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
        parts=parts,
    )


def _msg_with_usage_only(inp: int, out: int):
    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
        parts=[SimpleNamespace(part_kind="text")],
    )


def test_attribute_tool_tokens_distributes_call_and_post_usage():
    # One response asks for two tools; next response is post-tool synthesis.
    run_result = FakeRunResult(
        [
            _msg_with_usage_and_tools(10, 4, ["inspect", "apply_nginx"]),
            _msg_with_usage_only(12, 6),
        ]
    )

    tc = TokenCounter()
    _attribute_tool_tokens(run_result, tc)
    rows = {r["tool"]: r for r in tc.tool_token_rows()}

    assert rows["inspect"]["calls"] == 1
    assert rows["apply_nginx"]["calls"] == 1
    assert rows["inspect"]["call_input_tokens"] + rows["apply_nginx"]["call_input_tokens"] == 10
    assert rows["inspect"]["call_output_tokens"] + rows["apply_nginx"]["call_output_tokens"] == 4
    assert rows["inspect"]["post_input_tokens"] + rows["apply_nginx"]["post_input_tokens"] == 12
    assert rows["inspect"]["post_output_tokens"] + rows["apply_nginx"]["post_output_tokens"] == 6
