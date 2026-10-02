"""Structured event stream: the program-readable trace attribution runs on.

Why not logs (实现规格 §5.9.2 / 架构设计 §7.7): a log is prose for humans, while
attribution needs fields it can branch on. The discipline "attribute to a
specific role / specific defect, never stop at the symptom" is unreachable
without structured events -- you would be reading strings and guessing.

The log is append-only and single-writer: :class:`EventLog.record` is the one
place ``seq`` is assigned, which is what keeps ordering meaningful when several
workers interleave.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["EVENT_KINDS", "Event", "EventLog", "EventSink"]

#: The event kinds the runtime may emit (实现规格 §5.9.2).
EVENT_KINDS: tuple[str, ...] = (
    "call",
    "retry",
    "truncate",
    "tool_result",
    "gate_check",
    "checkpoint",
    "attribute",
)


@dataclass(frozen=True)
class Event:
    """One structured fact. ``detail`` is the machine-readable payload."""

    seq: int
    task_id: str
    worker_id: str
    kind: str
    at: float
    duration_ms: int
    ok: bool
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {self.kind!r}; expected one of {EVENT_KINDS}")
        if self.seq < 1:
            raise ValueError("Event.seq must be >= 1")
        if self.at < 0:
            raise ValueError("Event.at must be >= 0")
        if self.duration_ms < 0:
            raise ValueError("Event.duration_ms must be >= 0")
        if not self.task_id or not self.worker_id:
            raise ValueError("Event.task_id and Event.worker_id must not be empty")


@runtime_checkable
class EventSink(Protocol):
    """Where events go. The runtime depends on this, never on a concrete log."""

    def emit(self, ev: Event) -> None: ...


class EventLog:
    """Append-only in-memory log; the single writer of ``seq``.

    ``emit`` is kept for the :class:`EventSink` protocol, but it refuses an event
    whose ``seq`` is not the next one -- so a caller cannot smuggle in its own
    numbering and make two writers disagree about order.
    """

    def __init__(self) -> None:
        self._events: list[Event] = []

    def record(
        self,
        *,
        kind: str,
        task_id: str,
        worker_id: str,
        at: float,
        duration_ms: int = 0,
        ok: bool = True,
        detail: Mapping[str, Any] | None = None,
    ) -> Event:
        event = Event(
            seq=len(self._events) + 1,
            task_id=task_id,
            worker_id=worker_id,
            kind=kind,
            at=at,
            duration_ms=duration_ms,
            ok=ok,
            detail=dict(detail or {}),
        )
        self._events.append(event)
        return event

    def emit(self, ev: Event) -> None:
        if ev.seq != len(self._events) + 1:
            raise ValueError(f"EventLog assigns seq itself; got {ev.seq}, expected {len(self._events) + 1}")
        self._events.append(ev)

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def of_kind(self, kind: str) -> tuple[Event, ...]:
        return tuple(event for event in self._events if event.kind == kind)

    def for_worker(self, worker_id: str) -> tuple[Event, ...]:
        return tuple(event for event in self._events if event.worker_id == worker_id)

    def __len__(self) -> int:
        return len(self._events)
