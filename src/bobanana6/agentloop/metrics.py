"""Metrics: the two groups from 实现规格 §5.9.6, always reported split apart.

★★ Summary discipline: a headline number that folds "answered and correct"
together with "declined, which is free and correct" reads *better* than the
system is. The fix is structural here -- there is deliberately no composite
field and no single score; every rate is reported over its own denominator, and
the counts that make up each denominator are exposed next to it.

Tool side reads the structured event stream (``tool_result`` events). Delivery
side reads labelled outcomes, because "correct" is a judgement the evaluation
makes, not something the runtime can observe.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .events import Event

__all__ = [
    "DeliveryMetrics",
    "DeliveryOutcome",
    "TextToolCallMetrics",
    "ToolMetrics",
    "delivery_metrics",
    "metrics_as_dict",
    "text_tool_call_metrics",
    "tool_metrics",
]


@dataclass(frozen=True)
class ToolMetrics:
    calls: int
    ok_calls: int
    args_ok_calls: int
    failed_calls: int
    recovered_calls: int
    tasks: int
    tasks_with_success: int
    call_accuracy: float
    argument_correctness: float
    task_success_rate: float
    failure_recovery_rate: float


def _rate(numerator: int, denominator: int) -> float:
    # Absence of evidence is not evidence of perfection: 0/0 is 0.0, not 1.0.
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class TextToolCallMetrics:
    """Tool calls the model wrote as *text* instead of through `tool_calls`.

    Kept apart on purpose: this is neither a call (it never ran) nor "no call" (it
    tried). Folding it into either is exactly how the distinction disappears.
    """

    calls: int
    chars: int


def text_tool_call_metrics(events: Iterable[Event]) -> TextToolCallMetrics:
    """Count ``text_tool_call`` events and the reply length each one burned."""
    hits = [event for event in events if event.kind == "text_tool_call"]
    return TextToolCallMetrics(
        calls=len(hits),
        chars=sum(int(event.detail.get("chars") or 0) for event in hits),
    )


def tool_metrics(events: Iterable[Event]) -> ToolMetrics:
    """Aggregate ``tool_result`` events into the tool-side metrics.

    ``detail["args_ok"]`` (default: same as ``ok``) says the arguments parsed;
    ``detail["recovered"]`` on a later successful call marks a failure that was
    retried successfully. ``recovered_calls`` is kept separate on purpose: a
    headline "success rate" that counts recovered calls as first-try wins is
    exactly the pollution the split is there to prevent.
    """
    results = sorted((event for event in events if event.kind == "tool_result"), key=lambda event: event.seq)

    def args_ok(event: Event) -> bool:
        value = event.detail.get("args_ok", event.ok)
        return bool(value)

    calls = len(results)
    ok_calls = sum(1 for event in results if event.ok)
    args_ok_calls = sum(1 for event in results if args_ok(event))
    failed_calls = calls - ok_calls
    recovered_calls = sum(1 for event in results if event.ok and event.detail.get("recovered") is True)

    successes_by_task: dict[str, bool] = {}
    for event in results:
        successes_by_task[event.task_id] = successes_by_task.get(event.task_id, False) or event.ok
    tasks = len(successes_by_task)
    tasks_with_success = sum(1 for success in successes_by_task.values() if success)

    return ToolMetrics(
        calls=calls,
        ok_calls=ok_calls,
        args_ok_calls=args_ok_calls,
        failed_calls=failed_calls,
        recovered_calls=recovered_calls,
        tasks=tasks,
        tasks_with_success=tasks_with_success,
        call_accuracy=_rate(ok_calls, calls),
        argument_correctness=_rate(args_ok_calls, calls),
        task_success_rate=_rate(tasks_with_success, tasks),
        failure_recovery_rate=_rate(recovered_calls, failed_calls),
    )


@dataclass(frozen=True)
class DeliveryOutcome:
    """One evaluated delivery. The evaluation labels it; the runtime cannot."""

    answered: bool
    correct: bool
    cited: bool = False
    faithful: bool = True  # blame: fail-open — 省略即宣称用量忠实；它进读数不进判据，但报告会谎报
    declined: bool = False


@dataclass(frozen=True)
class DeliveryMetrics:
    total: int
    answered: int
    correct: int
    cited: int
    faithful: int
    declined: int
    declined_correct: int
    answer_accuracy: float
    citation_rate: float
    faithfulness_rate: float
    refusal_accuracy: float


def delivery_metrics(outcomes: Iterable[DeliveryOutcome]) -> DeliveryMetrics:
    """Aggregate labelled outcomes, with the refusal path scored separately.

    ``answer_accuracy`` is over *answered* outcomes only; a declined submission
    never enters it. ``refusal_accuracy`` is over declined outcomes only. Folding
    them together would let "declined everything" score as "perfectly accurate
    on refusals" while looking like a healthy single number.
    """
    rows = tuple(outcomes)
    answered = sum(1 for row in rows if row.answered)
    correct = sum(1 for row in rows if row.answered and row.correct)
    cited = sum(1 for row in rows if row.answered and row.cited)
    faithful = sum(1 for row in rows if row.answered and row.faithful)
    declined = sum(1 for row in rows if row.declined)
    declined_correct = sum(1 for row in rows if row.declined and row.correct)

    return DeliveryMetrics(
        total=len(rows),
        answered=answered,
        correct=correct,
        cited=cited,
        faithful=faithful,
        declined=declined,
        declined_correct=declined_correct,
        answer_accuracy=_rate(correct, answered),
        citation_rate=_rate(cited, answered),
        faithfulness_rate=_rate(faithful, answered),
        refusal_accuracy=_rate(declined_correct, declined),
    )


def metrics_as_dict(tool: ToolMetrics, delivery: DeliveryMetrics) -> Mapping[str, Any]:
    """Flat view for reports. It has no ``overall`` / ``score`` key by design."""
    return {
        "tool.calls": tool.calls,
        "tool.call_accuracy": tool.call_accuracy,
        "tool.argument_correctness": tool.argument_correctness,
        "tool.task_success_rate": tool.task_success_rate,
        "tool.failure_recovery_rate": tool.failure_recovery_rate,
        "tool.recovered_calls": tool.recovered_calls,
        "delivery.answer_accuracy": delivery.answer_accuracy,
        "delivery.citation_rate": delivery.citation_rate,
        "delivery.faithfulness_rate": delivery.faithfulness_rate,
        "delivery.refusal_accuracy": delivery.refusal_accuracy,
    }
