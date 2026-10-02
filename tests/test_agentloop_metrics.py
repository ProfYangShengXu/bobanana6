"""Metrics: every rate reported over its own denominator, never merged."""

from __future__ import annotations

from dataclasses import fields

import pytest

from bobanana6.agentloop.events import EventLog
from bobanana6.agentloop.metrics import (
    DeliveryOutcome,
    delivery_metrics,
    metrics_as_dict,
    tool_metrics,
)


def tool_events():
    log = EventLog()
    log.record(kind="tool_result", task_id="t1", worker_id="w", at=0.0, ok=True, detail={"args_ok": True})
    log.record(kind="tool_result", task_id="t1", worker_id="w", at=1.0, ok=False, detail={"args_ok": True})
    log.record(kind="tool_result", task_id="t2", worker_id="w", at=2.0, ok=True, detail={"args_ok": False, "recovered": True})
    return log.events


def test_tool_counts_and_rates() -> None:
    metrics = tool_metrics(tool_events())
    assert (metrics.calls, metrics.ok_calls, metrics.args_ok_calls) == (3, 2, 2)
    assert metrics.call_accuracy == pytest.approx(2 / 3)
    assert metrics.argument_correctness == pytest.approx(2 / 3)


def test_failure_recovery_is_its_own_number_not_folded_into_success() -> None:
    metrics = tool_metrics(tool_events())
    assert metrics.failed_calls == 1
    assert metrics.recovered_calls == 1
    assert metrics.failure_recovery_rate == pytest.approx(1.0)
    # The recovered call is not a first-try success, and the headline rate must
    # not silently absorb it.
    assert metrics.recovered_calls != metrics.ok_calls
    assert metrics.call_accuracy == pytest.approx(2 / 3)


def test_task_success_rate_is_over_tasks_not_calls() -> None:
    metrics = tool_metrics(tool_events())
    assert (metrics.tasks, metrics.tasks_with_success) == (2, 2)
    assert metrics.task_success_rate == pytest.approx(1.0)


def test_zero_denominator_is_zero_not_one() -> None:
    metrics = tool_metrics(())
    assert metrics.calls == 0
    assert metrics.call_accuracy == 0.0
    assert metrics.failure_recovery_rate == 0.0
    assert metrics.task_success_rate == 0.0


def test_delivery_refusal_is_scored_on_its_own_denominator() -> None:
    outcomes = (
        DeliveryOutcome(answered=True, correct=True),
        DeliveryOutcome(answered=True, correct=False),
        DeliveryOutcome(answered=True, correct=False),
        DeliveryOutcome(answered=True, correct=False),
        DeliveryOutcome(answered=False, correct=True, declined=True),
    )
    metrics = delivery_metrics(outcomes)
    assert metrics.answer_accuracy == pytest.approx(0.25)  # over 4 answered
    assert metrics.refusal_accuracy == pytest.approx(1.0)  # over 1 declined
    # A merged "correct / total" would read 0.4 -- better than the system is.
    assert metrics.answer_accuracy != pytest.approx(2 / 5)


def test_delivery_zero_denominator_is_zero() -> None:
    metrics = delivery_metrics(())
    assert metrics.answer_accuracy == 0.0
    assert metrics.refusal_accuracy == 0.0


def test_delivery_citation_and_faithfulness_are_separate_rates() -> None:
    outcomes = (
        DeliveryOutcome(answered=True, correct=True, cited=True, faithful=False),
        DeliveryOutcome(answered=True, correct=True, cited=False, faithful=True),
    )
    metrics = delivery_metrics(outcomes)
    assert metrics.citation_rate == pytest.approx(0.5)
    assert metrics.faithfulness_rate == pytest.approx(0.5)


def test_there_is_no_composite_score() -> None:
    tool = tool_metrics(tool_events())
    delivery = delivery_metrics((DeliveryOutcome(answered=True, correct=True),))
    view = metrics_as_dict(tool, delivery)
    assert not any("overall" in key or "score" in key for key in view)
    for cls in (type(tool), type(delivery)):
        assert not any(
            "overall" in f.name or "score" in f.name for f in fields(cls)
        ), f"{cls.__name__} must not expose a single composite number"
