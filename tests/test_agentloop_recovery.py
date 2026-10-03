"""Recovery: retry != reflect, degrade, decline, and the taint hint."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.errors import BobananaAction
from bobanana6.agentloop.gate import GateVerdict
from bobanana6.agentloop.recovery import (
    RecoveryAction,
    TaintTracker,
    decide_recovery,
    declared_feedback,
    reflect_message,
    reformat_message,
)


def decide(action: BobananaAction = BobananaAction.RETRY_LOCAL, **overrides):
    values = {
        "attempts": 0,
        "max_retries": 2,
        "max_reflections": 1,
    }
    values.update(overrides)
    return decide_recovery(action, **values)


# --------------------------------------------------------------------------- #
# taint
# --------------------------------------------------------------------------- #


def test_taint_threshold_validation() -> None:
    with pytest.raises(ValueError):
        TaintTracker(threshold=0)


def test_taint_marks_a_tool_after_consecutive_failures() -> None:
    tracker = TaintTracker(threshold=2)
    assert tracker.observe("read", ok=False) is False
    assert tracker.observe("read", ok=False) is True
    assert tracker.is_tainted("read")
    assert tracker.tainted() == ("read",)


def test_a_success_resets_the_streak() -> None:
    tracker = TaintTracker(threshold=2)
    tracker.observe("read", ok=False)
    tracker.observe("read", ok=True)
    assert tracker.observe("read", ok=False) is False
    assert not tracker.is_tainted("read")


def test_taint_advice_names_the_tool() -> None:
    tracker = TaintTracker(threshold=1)
    tracker.observe("read", ok=False)
    advice = tracker.advice("read")
    assert advice is not None and "read" in advice and "different tool" in advice
    assert tracker.advice("write_file") is None


# --------------------------------------------------------------------------- #
# the three exits
# --------------------------------------------------------------------------- #


def test_a_declined_gate_declines_and_never_retries() -> None:
    decision = decide(gate=GateVerdict(status="declined", scope="full"))
    assert decision.action is RecoveryAction.DECLINE
    assert decision.attempts == 0


def test_an_unverifiable_split_degrades_instead_of_retrying() -> None:
    decision = decide(block_verifiable=False)
    assert decision.action is RecoveryAction.DEGRADE_SINGLE_AGENT


def test_transient_fault_retries_then_reflects_then_degrades() -> None:
    assert decide(attempts=0).action is RecoveryAction.RETRY
    assert decide(attempts=2).action is RecoveryAction.REFLECT
    assert decide(attempts=3).action is RecoveryAction.DEGRADE_SINGLE_AGENT


def test_reflect_carries_the_failure_reason_back_into_the_context() -> None:
    decision = decide(attempts=2, failure="timeout", detail="read timed out")
    assert decision.action is RecoveryAction.REFLECT
    assert decision.injected is not None
    assert "timeout" in decision.injected and "read timed out" in decision.injected


def test_taint_outranks_a_generic_retry() -> None:
    tracker = TaintTracker(threshold=1)
    tracker.observe("read", ok=False)
    decision = decide(attempts=0, taint=tracker, tool_name="read", failure="tool failed")
    assert decision.action is RecoveryAction.REFLECT
    assert decision.injected is not None and "different tool" in decision.injected


def test_non_local_actions_map_through() -> None:
    assert decide(BobananaAction.RESHARD_TASK).action is RecoveryAction.RESHARD
    assert decide(BobananaAction.FIX_CONSTRAINT).action is RecoveryAction.FIX_CONSTRAINT
    assert decide(BobananaAction.SWITCH_SOURCE).action is RecoveryAction.SWITCH_SOURCE
    assert decide(BobananaAction.ABORT).action is RecoveryAction.ABORT


def test_reflect_message_shape() -> None:
    text = reflect_message("context_overflow", detail="input too long", classification="reshard")
    assert "context_overflow" in text
    assert "input too long" in text and "reshard" in text


def test_tainted_tools_are_reported(  ) -> None:
    tracker = TaintTracker(threshold=1)
    tracker.observe("read", ok=False)
    decision = decide(gate=GateVerdict(status="declined", scope="full"), taint=tracker)
    assert decision.tainted_tools == ("read",)


# --------------------------------------------------------------------------- #
# ★ lec9:125 -- the fix that goes back to the model is structured, not prose
# --------------------------------------------------------------------------- #


class WithFeedback(Exception):
    reason = "format_error"

    def feedback(self) -> dict:
        return {"code": "not_json", "problem": "first char is 'n'", "got": "nope", "expected": "{...}"}


class WithoutFeedback(Exception):
    reason = "format_error"


def test_declared_feedback_reads_the_raisers_payload() -> None:
    assert declared_feedback(WithFeedback())["code"] == "not_json"
    assert declared_feedback(WithoutFeedback()) is None  # no method at all
    assert declared_feedback(RuntimeError("x")) is None


def test_declared_feedback_refuses_an_incomplete_payload() -> None:
    class Partial(Exception):
        def feedback(self) -> dict:
            return {"code": "x", "problem": ""}  # no expected, empty problem

    class NotAMapping(Exception):
        def feedback(self) -> list:
            return ["code"]

    class Broken(Exception):
        def feedback(self) -> dict:
            raise RuntimeError("feedback is broken")

    assert declared_feedback(Partial()) is None
    assert declared_feedback(NotAMapping()) is None
    assert declared_feedback(Broken()) is None  # a broken fix must not mask the failure


def test_reformat_message_carries_code_problem_sent_and_required() -> None:
    text = reformat_message("format_error", declared_feedback(WithFeedback()))
    assert "[format_error]" in text
    assert "code: not_json" in text
    assert "problem: first char is 'n'" in text
    assert "what you sent: nope" in text
    assert "required shape: {...}" in text
    assert "Traceback" not in text


def test_reformat_message_truncates_what_the_model_sent() -> None:
    payload = {"code": "c", "problem": "p", "got": "y" * 5000, "expected": "e"}
    text = reformat_message("format_error", payload)
    assert "…[truncated]" in text
    assert len(text) < 2000


def test_reformat_message_omits_what_was_sent_when_there_is_nothing() -> None:
    payload = {"code": "c", "problem": "p", "got": "", "expected": "e"}
    assert "what you sent" not in reformat_message("format_error", payload)


def test_a_labelled_fix_reformats_instead_of_stopping() -> None:
    decision = decide(BobananaAction.FIX_CONSTRAINT, has_feedback=True)
    assert decision.action is RecoveryAction.REFORMAT


def test_the_reformat_round_is_capped_by_the_reflection_budget() -> None:
    """★ Defaults unchanged: max_reflections=1 buys exactly one reformat."""
    assert decide(BobananaAction.FIX_CONSTRAINT, has_feedback=True, attempts=1).action is RecoveryAction.REFORMAT
    exhausted = decide(BobananaAction.FIX_CONSTRAINT, has_feedback=True, attempts=2)
    assert exhausted.action is RecoveryAction.FIX_CONSTRAINT
    assert "budget" in exhausted.reason


def test_without_a_fix_fix_constraint_still_maps_straight_through() -> None:
    """★ The constraint/request direction keeps the old behaviour (hand it over)."""
    assert decide(BobananaAction.FIX_CONSTRAINT, has_feedback=False).action is RecoveryAction.FIX_CONSTRAINT
