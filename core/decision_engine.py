from __future__ import annotations


def populate(session_id: str, memory, hypotheses: list[dict]) -> None:
    """Seed the hypothesis queue for this session (skips if already seeded)."""
    memory.populate_queue(session_id, hypotheses)


def next_hypothesis(session_id: str, memory) -> dict | None:
    """Return the next pending hypothesis, or None if queue is exhausted."""
    return memory.next_hypothesis(session_id)


def mark_done(session_id: str, memory, name: str, outcome: str) -> None:
    memory.mark_hypothesis(session_id, name, "done", outcome)


def mark_skipped(session_id: str, memory, name: str, reason: str) -> None:
    memory.mark_hypothesis(session_id, name, "skipped", reason)


def is_exhausted(session_id: str, memory) -> bool:
    return memory.pending_count(session_id) == 0
