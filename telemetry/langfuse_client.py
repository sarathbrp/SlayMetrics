from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator

from core import log as logger


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


class LangfuseClient:
    def __init__(self, client: Any | None, metadata: dict[str, Any] | None = None):
        self._client = client
        self._metadata = metadata or {}
        self._last_trace_url: str | None = None
        self._generation_api_supported = bool(
            client
            and hasattr(client, "start_as_current_generation")
            and hasattr(client, "update_current_generation")
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def last_trace_url(self) -> str | None:
        return self._last_trace_url

    @classmethod
    def from_env(
        cls,
        metadata: dict[str, Any] | None = None,
        *,
        enabled: bool = True,
    ) -> "LangfuseClient":
        if not enabled:
            return cls(None, metadata=metadata)
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
        base_url = (
            os.environ.get("LANGFUSE_BASE_URL", "").strip()
            or os.environ.get("LANGFUSE_HOST", "").strip()
        )
        if not public_key or not secret_key:
            return cls(None, metadata=metadata)

        try:
            from langfuse import Langfuse
        except ModuleNotFoundError:
            logger.log(
                "langfuse",
                "LANGFUSE_* env set but langfuse package is not installed; tracing disabled",
                "warn",
            )
            return cls(None, metadata=metadata)

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url or None,
        )
        return cls(client, metadata=metadata)

    def auth_check(self) -> bool:
        if not self._client:
            return False
        try:
            result = self._client.auth_check()
        except Exception as exc:
            logger.log("langfuse", f"Auth check failed: {exc}", "warn")
            return False
        return bool(result)

    @contextmanager
    def trace(self, name: str, *, input: Any = None, metadata: dict[str, Any] | None = None):
        if not self._client:
            yield None
            return

        merged = {**self._metadata, **(metadata or {})}
        with self._client.start_as_current_observation(
            name=name,
            input=_jsonable(input),
            metadata=_jsonable(merged),
        ) as observation:
            self._last_trace_url = self._safe_trace_url()
            try:
                yield observation
            finally:
                self._last_trace_url = self._safe_trace_url() or self._last_trace_url

    @contextmanager
    def span(self, name: str, *, input: Any = None, metadata: dict[str, Any] | None = None):
        with self.trace(name, input=input, metadata=metadata) as observation:
            yield observation

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model_parameters: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        if not self._client:
            yield None
            return

        merged = {**self._metadata, **(metadata or {})}
        generation_context = (
            self._client.start_as_current_generation(
                name=name,
                model=model,
                input=_jsonable(input),
                metadata=_jsonable(merged),
                model_parameters=_jsonable(model_parameters or {}),
            )
            if self._generation_api_supported
            else self._client.start_as_current_observation(
                name=name,
                input=_jsonable(input),
                metadata=_jsonable(
                    {
                        **merged,
                        "model": model,
                        "model_parameters": _jsonable(model_parameters or {}),
                        "compat_mode": "observation_generation_fallback",
                    }
                ),
            )
        )
        with generation_context as generation:
            self._last_trace_url = self._safe_trace_url()
            try:
                yield generation
            finally:
                self._last_trace_url = self._safe_trace_url() or self._last_trace_url

    def update_generation(
        self,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        usage_details: dict[str, int] | None = None,
    ) -> None:
        if not self._client:
            return
        if self._generation_api_supported:
            self._client.update_current_generation(
                output=_jsonable(output),
                metadata=_jsonable({**self._metadata, **(metadata or {})}),
                usage_details=usage_details or None,
            )
            return
        merged = {**self._metadata, **(metadata or {})}
        if usage_details:
            merged["usage_details"] = usage_details
        self._client.update_current_span(
            output=_jsonable(output),
            metadata=_jsonable(merged),
        )

    def update_span(self, *, output: Any = None, metadata: dict[str, Any] | None = None) -> None:
        if not self._client:
            return
        self._client.update_current_span(
            output=_jsonable(output),
            metadata=_jsonable({**self._metadata, **(metadata or {})}),
        )

    def event(
        self,
        name: str,
        *,
        input: Any = None,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
    ) -> None:
        if not self._client:
            return
        self._client.create_event(
            name=name,
            input=_jsonable(input),
            output=_jsonable(output),
            metadata=_jsonable({**self._metadata, **(metadata or {})}),
            level=level,
        )

    def flush(self) -> None:
        if not self._client:
            return
        self._client.flush()

    def shutdown(self) -> None:
        if not self._client:
            return
        self._client.shutdown()

    def _safe_trace_url(self) -> str | None:
        if not self._client:
            return None
        try:
            return self._client.get_trace_url()
        except Exception:
            return None


def summarize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for message in messages[-8:]:
        role = getattr(message, "type", None) or message.__class__.__name__
        content = getattr(message, "content", "")
        if isinstance(content, list):
            content = json.dumps(_jsonable(content), ensure_ascii=True)
        summary.append(
            {
                "role": str(role),
                "content": str(content)[:1500],
                "tool_calls": _jsonable(getattr(message, "tool_calls", None)),
            }
        )
    return summary
