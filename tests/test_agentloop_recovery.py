"""Recovery: retry != reflect, degrade, decline, and the taint hint."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.errors import BobananaAction
from bobanana6.agentloop.gate import GateVerdict
from bobanana6.agentloop.recovery import (
    RecoveryAction,
    TaintTracker,
    decide_recovery,
    reflect_message,
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
