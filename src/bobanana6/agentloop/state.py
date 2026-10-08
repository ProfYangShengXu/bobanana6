"""Checkpoint state at turn boundaries.

Why this exists (架构设计 §7.7): "local retry" is only cheap if a retry does not
restart the whole subtask. The checkpoint is not extra robustness -- it is the
cost precondition for the ``N``-way fan-out.

Two fork disciplines are encoded structurally:

  * ``messages_digest`` is a **content fingerprint**, never an index: compressing
    or repairing a transcript replaces dicts and inserts rows, and a stale index
    then replays a prefix that tears open in the middle of a tool row.
  * ``status`` distinguishes ``failed`` from ``never_started``. Collapsing them
    makes the resume path replay inputs that were already answered.

There is exactly one write point: :func:`save_turn`. Success and failure both go
through it, because a failed turn is the form that most needs resuming.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

__all__ = [
    "CHECKPOINT_STATUSES",
    "Checkpoint",
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "Resume",
    "load_checkpoint",
    "messages_digest",
    "resume",
    "save_turn",
]

#: ``never_started`` is not a synonym for ``failed`` (实现规格 §5.9.1).
CHECKPOINT_STATUSES: tuple[str, ...] = ("ok", "failed", "never_started")


@dataclass(frozen=True)
class Checkpoint:
    task_id: str
    worker_id: str
    turn: int
    messages_digest: str
    done_actions: tuple[str, ...] = ()
    status: str = "never_started"

    def __post_init__(self) -> None:
        if not self.task_id or not self.worker_id:
            raise ValueError("Checkpoint.task_id and Checkpoint.worker_id must not be empty")
        if self.turn < 0:
            raise ValueError("Checkpoint.turn must be >= 0")
        if not self.messages_digest:
            raise ValueError("Checkpoint.messages_digest must be a content fingerprint, not empty")
        if self.status not in CHECKPOINT_STATUSES:
            raise ValueError(f"unknown checkpoint status {self.status!r}; expected one of {CHECKPOINT_STATUSES}")


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def messages_digest(messages: Iterable[Mapping[str, Any]]) -> str:
    """A stable digest of the transcript's *content*.

    Order matters (it is a transcript), so the list is not sorted; keys inside
    each message are, so a re-serialised dict hashes the same.
    """
    payload = [_canonical(message) for message in messages]
    return sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")).hexdigest()


class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...

    def load(self, task_id: str) -> Checkpoint | None: ...


class InMemoryCheckpointStore:
    """The test double, and the shape any durable store must match."""

    def __init__(self) -> None:
        self._by_task: dict[str, Checkpoint] = {}

    def save(self, checkpoint: Checkpoint) -> None:
        self._by_task[checkpoint.task_id] = checkpoint

    def load(self, task_id: str) -> Checkpoint | None:
        return self._by_task.get(task_id)


def save_turn(
    store: CheckpointStore,
    *,
    task_id: str,
    worker_id: str,
    turn: int,
    messages: Iterable[Mapping[str, Any]],
    done_actions: Iterable[str] = (),
    status: str = "ok",
) -> Checkpoint:
    """The single write point: persist at a turn boundary, success *or* failure."""
    checkpoint = Checkpoint(
        task_id=task_id,
        worker_id=worker_id,
        turn=turn,
        messages_digest=messages_digest(messages),
        done_actions=tuple(done_actions),
        status=status,
    )
    store.save(checkpoint)
    return checkpoint


def load_checkpoint(store: CheckpointStore, task_id: str) -> Checkpoint | None:
    return store.load(task_id)


@dataclass(frozen=True)
class Resume:
    """What the next attempt should do, derived from the checkpoint's status."""

    start_turn: int
    completed_actions: tuple[str, ...]
    run: bool = True  # blame: fail-open — 省略即"续跑"；不该续的跑会被续（这条判据决定要不要重放旧输入）


def resume(checkpoint: Checkpoint) -> Resume:
    """Decide where to continue from -- and what must not be replayed.

    ``failed`` and ``never_started`` produce different resumes on purpose: a
    failed turn keeps its completed actions so the retry does not redo them,
    while a never-started candidate has none and starts at turn 0.
    """
    if checkpoint.status == "never_started":
        return Resume(start_turn=0, completed_actions=(), run=True)
    if checkpoint.status == "failed":
        return Resume(start_turn=checkpoint.turn, completed_actions=checkpoint.done_actions, run=True)
    return Resume(start_turn=checkpoint.turn + 1, completed_actions=checkpoint.done_actions, run=True)
