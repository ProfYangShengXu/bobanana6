"""Responsibility attribution: which layer should change.

This is a different layer from :mod:`bobanana6.agentloop.errors` (实现规格 §5.9.3
/ 架构设计 §7.8):

===============  ==========================  =========================
                 input                       output
===============  ==========================  =========================
errors.py        an exception                a retry strategy
attribution.py   a failure + its trajectory  which layer to change
===============  ==========================  =========================

``context_overflow`` is the canonical example: it can come from "the block is
too big" (reshard) or "the history was never compressed" (fix the constraint).
Classifying only by error type leaves that fork blind, and the three decisions
stand exactly on that fork.

Three hard requirements (实现规格 §5.9.3), all enforced by construction:

  1. an attribution carries ``Event`` evidence -- never a bare conclusion;
  2. ``unknown`` is a legal answer; a blamed attribution cannot claim unknown;
  3. the decisive experiment is executable: hand the worker's exact context to
     a human and ask whether they can answer it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from .events import Event

__all__ = [
    "ATTRIBUTION_EXPERIMENT",
    "BLAME_ACTIONS",
    "BLAME_KINDS",
    "CONFIDENCE_LEVELS",
    "Attribution",
    "Blame",
    "Experiment",
    "actions_for",
    "attribute",
]

BLAME_KINDS: tuple[str, ...] = ("model", "tool", "context", "constraint")
CONFIDENCE_LEVELS: tuple[str, ...] = ("proven", "likely", "unknown")


class Blame(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    CONTEXT = "context"
    CONSTRAINT = "constraint"


class Experiment(StrEnum):
    """The separation experiment from 实现规格 §5.9.3.

    "Hand the very context the worker got to a normal person -- can they
    answer it?"  Yes -> model; no -> context; they answer but differently ->
    model (the model ignored the context / LITM).
    """

    NOT_RUN = "not_run"
    HUMAN_CAN_ANSWER = "human_can_answer"
    HUMAN_CANNOT_ANSWER = "human_cannot_answer"
    HUMAN_DIFFERS = "human_differs"


#: Four exits -> the actions from 实现规格 §5.9.3's table. The strings are stable
#: tokens for the decision layer (agentloop/recovery.py, step 21) to branch on;
#: they are data, not prose, so the mapping stays checkable.
BLAME_ACTIONS: Mapping[Blame, tuple[str, ...]] = {
    Blame.MODEL: ("switch_worker", "replenish_semantics", "rearrange_context"),
    Blame.TOOL: ("switch_source", "repair_tool", "taint"),
    Blame.CONTEXT: ("fix_constraint",),
    Blame.CONSTRAINT: ("relax_constraint",),
}

#: The name of the one experiment this module knows how to interpret.
ATTRIBUTION_EXPERIMENT = "hand_the_context_to_a_human"


def actions_for(blame: str) -> tuple[str, ...]:
    """The spec-table actions for a layer; empty for anything unrecognised."""
    return BLAME_ACTIONS.get(blame, ())  # type: ignore[arg-type]


@dataclass(frozen=True)
class Attribution:
    failure: str
    #: ``None`` means the attribution is unknown -- do not guess a layer.
    blame: str | None
    evidence: tuple[Event, ...]
    experiment: str
    confidence: str

    def __post_init__(self) -> None:
        if self.blame is not None and self.blame not in BLAME_KINDS:
            raise ValueError(f"unknown blame {self.blame!r}; expected one of {BLAME_KINDS}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"unknown confidence {self.confidence!r}; expected one of {CONFIDENCE_LEVELS}")
        # (1) a conclusion without evidence is not an attribution.
        if self.blame is not None and not self.evidence:
            raise ValueError("a blamed attribution must carry Event evidence")
        # (2) "unknown" and a blame are mutually exclusive -- that is the no-guess rule.
        if (self.blame is None) != (self.confidence == "unknown"):
            raise ValueError("blame=None must pair with confidence='unknown' and vice versa")


def _unknown(failure: str, experiment: str) -> Attribution:
    return Attribution(failure=failure, blame=None, evidence=(), experiment=experiment, confidence="unknown")


def attribute(
    failure: str,
    trace: Iterable[Event],
    *,
    experiment: Experiment = Experiment.NOT_RUN,
) -> Attribution:
    """Attribute one failure to a layer, or admit we cannot.

    Without a trajectory there is nothing to attach as evidence, so the answer
    is ``unknown`` even if an experiment was run -- requirement (1) outranks
    having an opinion. Tool failures are read from the trace first (a broken
    tool is the proximate cause), then a constraint rejection, then the
    experiment distinguishes model from context.
    """
    events = tuple(trace)
    if not events:
        return _unknown(failure, experiment.value)

    tool_evidence = tuple(event for event in events if event.kind == "tool_result" and not event.ok)
    if tool_evidence:
        proven = any(event.detail.get("deterministic") is True for event in tool_evidence)
        return Attribution(
            failure=failure,
            blame=Blame.TOOL,
            evidence=tool_evidence,
            experiment="trace",
            confidence="proven" if proven else "likely",
        )

    constraint_evidence = tuple(
        event for event in events if event.kind == "gate_check" and event.detail.get("constraint_rejected") is True
    )
    if constraint_evidence:
        return Attribution(
            failure=failure,
            blame=Blame.CONSTRAINT,
            evidence=constraint_evidence,
            experiment="trace",
            confidence="likely",
        )

    if experiment == Experiment.HUMAN_CANNOT_ANSWER:
        return Attribution(
            failure=failure,
            blame=Blame.CONTEXT,
            evidence=events,
            experiment=experiment.value,
            confidence="proven",
        )
    if experiment in (Experiment.HUMAN_CAN_ANSWER, Experiment.HUMAN_DIFFERS):
        return Attribution(
            failure=failure,
            blame=Blame.MODEL,
            evidence=events,
            experiment=experiment.value,
            confidence="proven",
        )
    return _unknown(failure, experiment.value)
