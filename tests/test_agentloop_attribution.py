"""Responsibility attribution: evidence-bound, may say unknown, experiment-driven."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.attribution import (
    BLAME_ACTIONS,
    BLAME_KINDS,
    Attribution,
    Blame,
    Experiment,
    actions_for,
    attribute,
)
from bobanana6.agentloop.errors import ClassifiedError
from bobanana6.agentloop.events import Event


def event(kind: str = "call", *, ok: bool = True, detail: dict | None = None, seq: int = 1) -> Event:
    return Event(seq=seq, task_id="t", worker_id="w", kind=kind, at=0.0, duration_ms=0, ok=ok, detail=detail or {})


def test_attribution_is_not_error_classification() -> None:
    assert Attribution is not ClassifiedError
    assert BLAME_KINDS == ("model", "tool", "context", "constraint")


def test_no_trace_means_unknown_not_a_guess() -> None:
    result = attribute("worker exploded", ())
    assert result.confidence == "unknown"
    assert result.blame is None
    assert result.evidence == ()


def test_a_blamed_attribution_must_carry_evidence() -> None:
    with pytest.raises(ValueError, match="must carry Event evidence"):
        Attribution(failure="f", blame="model", evidence=(), experiment="x", confidence="proven")


def test_unknown_and_a_blame_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="must pair with"):
        Attribution(failure="f", blame="model", evidence=(event(),), experiment="x", confidence="unknown")


def test_unknown_blame_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown blame"):
        Attribution(failure="f", blame="vibes", evidence=(event(),), experiment="x", confidence="likely")


def test_tool_failure_is_read_from_the_trace() -> None:
    trace = (event("call"), event("tool_result", ok=False, detail={"tool": "read", "error": "timeout"}, seq=2))
    result = attribute("no deliverable", trace)
    assert result.blame == "tool"
    assert result.confidence == "likely"
    assert len(result.evidence) == 1 and result.evidence[0].kind == "tool_result"


def test_a_deterministic_tool_failure_is_proven() -> None:
    trace = (event("tool_result", ok=False, detail={"deterministic": True}),)
    assert attribute("f", trace).confidence == "proven"


def test_constraint_rejection_is_read_from_the_trace() -> None:
    trace = (event("call"), event("gate_check", detail={"constraint_rejected": True}, seq=2))
    result = attribute("rejected", trace)
    assert result.blame == "constraint"
    assert result.experiment == "trace"


def test_experiment_human_cannot_answer_means_context() -> None:
    result = attribute("wrong", (event(),), experiment=Experiment.HUMAN_CANNOT_ANSWER)
    assert result.blame == "context"
    assert result.confidence == "proven"


def test_experiment_human_can_answer_means_model() -> None:
    result = attribute("wrong", (event(),), experiment=Experiment.HUMAN_CAN_ANSWER)
    assert result.blame == "model"
    assert result.confidence == "proven"


def test_experiment_human_differs_is_model_litm() -> None:
    result = attribute("wrong", (event(),), experiment=Experiment.HUMAN_DIFFERS)
    assert result.blame == "model"
    assert result.experiment == "human_differs"


def test_trace_without_a_signal_and_without_an_experiment_is_unknown() -> None:
    result = attribute("wrong", (event("call"),))
    assert result.confidence == "unknown"
    assert result.blame is None


# --------------------------------------------------------------------------- #
# ★ A07: a declared cause outranks a bare symptom (AGENTS.md §7⑫)
# --------------------------------------------------------------------------- #


def test_a_declared_rejection_outranks_a_bare_tool_failure() -> None:
    """「有工具失败」只是症状；判定器【自己说了】是它拒的，就该听它的。"""
    trace = (
        event("tool_result", ok=False, detail={"tool": "read", "error": "timeout"}),
        event("gate_check", ok=False, detail={"constraint_rejected": True}, seq=2),
    )
    result = attribute("could not compute", trace)
    assert result.blame == "constraint", result
    assert [item.kind for item in result.evidence] == ["gate_check"]


def test_the_experiment_outranks_a_bare_tool_failure() -> None:
    """把上下文交给人都答不出来 → 缺的是上下文，不是那个恰好也失败的工具。"""
    trace = (event("tool_result", ok=False, detail={"tool": "read", "error": "timeout"}),)
    result = attribute("could not compute", trace, experiment=Experiment.HUMAN_CANNOT_ANSWER)
    assert result.blame == "context", result
    assert result.confidence == "proven"


def test_two_layers_blaming_each_other_is_unknown_not_a_coin_flip() -> None:
    """两个层各说自己是病因 —— 归因层不许挑一个，交给人看。"""
    trace = (
        event("tool_result", ok=False, detail={"deterministic": True}),
        event("gate_check", ok=False, detail={"constraint_rejected": True}, seq=2),
    )
    result = attribute("could not compute", trace, experiment=Experiment.HUMAN_CANNOT_ANSWER)
    assert result.blame is None and result.confidence == "unknown", result
    assert {item.kind for item in result.evidence} == {"tool_result", "gate_check"}


def test_blame_to_action_mapping_matches_the_spec_table() -> None:
    assert set(BLAME_ACTIONS) == {Blame.MODEL, Blame.TOOL, Blame.CONTEXT, Blame.CONSTRAINT}
    assert actions_for("context") == ("fix_constraint",)
    assert actions_for("tool") == ("switch_source", "repair_tool", "taint")
    assert actions_for("model") == ("switch_worker", "replenish_semantics", "rearrange_context")
    assert actions_for("constraint") == ("relax_constraint",)


def test_actions_for_an_unknown_blame_is_empty() -> None:
    assert actions_for("vibes") == ()
