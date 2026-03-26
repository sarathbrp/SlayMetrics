from __future__ import annotations

import os
from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...


class ClaudeEmbeddings:
    """Uses Anthropic's embedding API."""

    def __init__(self, model: str = "voyage-3"):
        import anthropic
        self._client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model
        self.dimensions = 1024

    def embed(self, text: str) -> list[float]:
        response = self._client.embeddings.create(
            model=self.model,
            input=text[:8192],
        )
        return response.embeddings[0].values


class LocalEmbeddings:
    """Fallback: simple TF-IDF-style hash embedding when no API is available.
    Not semantically rich but keeps the system functional offline."""

    def __init__(self, dimensions: int = 1536):
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        import hashlib
        import math

        words = text.lower().split()
        vec = [0.0] * self.dimensions
        for word in words:
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            idx = h % self.dimensions
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


def from_config(cfg: dict) -> EmbeddingProvider:
    profile_name = cfg["llm"]["active_profile"]
    profile = cfg["llm"]["profiles"][profile_name]
    if profile["backend"] == "claude" and os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeEmbeddings()
    return LocalEmbeddings()
