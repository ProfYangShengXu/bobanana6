"""Structured event stream: kinds, seq ownership, filtering."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.events import EVENT_KINDS, Event, EventLog, EventSink


def test_the_event_kinds() -> None:
    """★ `#65` 补了 `produce_failure`；`#49` 补 `text_tool_call`；本块补 `compaction_failed`。"""
    assert EVENT_KINDS == (
        "call",
        "retry",
        "truncate",
        "tool_result",
        "gate_check",
        "checkpoint",
        "attribute",
        "produce_failure",
        "text_tool_call",
        "compaction_failed",
    )


def test_event_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown event kind"):
        Event(seq=1, task_id="t", worker_id="w", kind="chatter", at=0.0, duration_ms=0, ok=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seq": 0},
        {"at": -1.0},
        {"duration_ms": -1},
        {"task_id": ""},
        {"worker_id": ""},
    ],
)
def test_event_rejects_impossible_fields(kwargs: dict) -> None:
    base = {"seq": 1, "task_id": "t", "worker_id": "w", "kind": "call", "at": 0.0, "duration_ms": 0, "ok": True}
    with pytest.raises(ValueError):
        Event(**{**base, **kwargs})


def test_log_assigns_monotonic_seq() -> None:
    log = EventLog()
    first = log.record(kind="call", task_id="t", worker_id="w", at=0.0)
    second = log.record(kind="retry", task_id="t", worker_id="w", at=1.0)
    assert (first.seq, second.seq) == (1, 2)
    assert len(log) == 2


def test_log_refuses_an_event_with_a_foreign_seq() -> None:
    log = EventLog()
    with pytest.raises(ValueError, match="assigns seq itself"):
        log.emit(Event(seq=5, task_id="t", worker_id="w", kind="call", at=0.0, duration_ms=0, ok=True))


def test_log_accepts_the_next_seq() -> None:
    log = EventLog()
    log.emit(Event(seq=1, task_id="t", worker_id="w", kind="call", at=0.0, duration_ms=0, ok=True))
    assert len(log) == 1


def test_log_filters_by_kind_and_worker() -> None:
    log = EventLog()
    log.record(kind="call", task_id="t", worker_id="w1", at=0.0)
    log.record(kind="tool_result", task_id="t", worker_id="w1", at=1.0, ok=False)
    log.record(kind="call", task_id="t", worker_id="w2", at=2.0)
    assert len(log.of_kind("call")) == 2
    assert len(log.for_worker("w1")) == 2
    assert log.of_kind("gate_check") == ()


def test_detail_is_structured_not_prose() -> None:
    log = EventLog()
    event = log.record(kind="tool_result", task_id="t", worker_id="w", at=0.0, detail={"tool": "read", "args_ok": True})
    assert event.detail["tool"] == "read"
    assert event.detail["args_ok"] is True


def test_event_log_is_an_event_sink() -> None:
    assert isinstance(EventLog(), EventSink)
