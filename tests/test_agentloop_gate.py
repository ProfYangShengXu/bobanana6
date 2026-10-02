"""Blocking completion gate: exit-code attribution, staleness, and the third exit."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.gate import (
    GATE_SCOPES,
    GATE_STATUSES,
    GateVerdict,
    attribute_exit,
    can_complete,
    evaluate,
    is_stale,
    record_evidence,
    scope_covers,
    should_retry,
    split_shell_segments,
)


def verdict(status: str = "passed", scope: str = "full") -> GateVerdict:
    return GateVerdict(status=status, scope=scope)


def test_statuses_include_the_third_exit() -> None:
    assert GATE_STATUSES == ("unverified", "stale", "passed", "failed", "declined")
    assert GATE_SCOPES == ("targeted", "full")


def test_gate_verdict_rejects_unknown_status_and_scope() -> None:
    with pytest.raises(ValueError):
        GateVerdict(status="maybe", scope="full")
    with pytest.raises(ValueError):
        GateVerdict(status="passed", scope="everywhere")


def test_scope_coverage() -> None:
    assert scope_covers("full", "targeted")
    assert scope_covers("full", "full")
    assert scope_covers("targeted", "targeted")
    assert not scope_covers("targeted", "full")
    with pytest.raises(ValueError):
        scope_covers("targeted", "everything")


def test_only_passed_and_covering_can_complete() -> None:
    assert can_complete(verdict("passed", "full"), "full")
    assert can_complete(verdict("passed", "full"), "targeted")
    assert not can_complete(verdict("passed", "targeted"), "full")
    assert not can_complete(verdict("failed", "full"), "targeted")
    assert not can_complete(verdict("unverified", "full"), "targeted")
    assert not can_complete(verdict("stale", "full"), "targeted")


def test_declined_is_not_a_failure_and_does_not_retry() -> None:
    declined = verdict("declined", "full")
    assert can_complete(declined, "full") is False
    assert should_retry(declined) is False
    assert should_retry(verdict("failed", "full")) is True
    assert should_retry(verdict("unverified", "full")) is True
    assert should_retry(verdict("stale", "full")) is True
    assert should_retry(verdict("passed", "full")) is False


# --------------------------------------------------------------------------- #
# exit-code attribution (fork 坑 17)
# --------------------------------------------------------------------------- #


def test_plain_command_is_attributable() -> None:
    result = attribute_exit("pytest -q", 0)
    assert result.attributable and result.code == 0


def test_only_the_last_semicolon_segment_speaks() -> None:
    assert attribute_exit("false; pytest -q", 1).attributable
    assert attribute_exit("false; pytest -q", 1).code == 1


def test_pipe_hides_the_exit_status() -> None:
    result = attribute_exit("pytest -q | tee out", 0)
    assert result.attributable is False and result.code is None


def test_logical_or_hides_the_exit_status() -> None:
    assert attribute_exit("pytest -q || true", 0).attributable is False


def test_background_hides_the_exit_status() -> None:
    assert attribute_exit("pytest -q &", 0).attributable is False


def test_and_chain_proves_members_only_on_success() -> None:
    assert attribute_exit("flake8 && pytest -q", 0).attributable is True
    failing = attribute_exit("flake8 && pytest -q", 1)
    assert failing.attributable is False and failing.code is None


def test_unparseable_command_is_not_attributable() -> None:
    assert split_shell_segments('pytest "unclosed') is None
    assert attribute_exit('pytest "unclosed', 0).attributable is False


def test_quotes_protect_operators() -> None:
    segments = split_shell_segments('echo "a | b" ; pytest -q')
    assert segments is not None and len(segments) == 2
    assert segments[0].following_operator == ";"


# --------------------------------------------------------------------------- #
# evidence + self-report is not trusted
# --------------------------------------------------------------------------- #


def test_evidence_is_command_exit_and_output_digest_not_prose() -> None:
    evidence = record_evidence("pytest -q", 0, "12 passed", created_at=1.0)
    assert evidence.command == "pytest -q"
    assert evidence.exit_code == 0
    assert evidence.output_digest != "12 passed"
    assert evidence.attributable is True


def test_evidence_from_a_pipe_is_not_attributable() -> None:
    evidence = record_evidence("pytest -q | tee out", 0, "12 passed", created_at=1.0)
    assert evidence.attributable is False


def test_missing_evidence_is_unverified_not_passed() -> None:
    assert evaluate(None, required_scope="full", last_edit_at=0.0).status == "unverified"


def test_staleness_is_an_explicit_timestamp_comparison() -> None:
    evidence = record_evidence("pytest -q", 0, "ok", created_at=10.0)
    assert is_stale(evidence, 11.0) is True
    assert is_stale(evidence, 10.0) is False
    assert evaluate(evidence, required_scope="full", last_edit_at=11.0).status == "stale"
    assert evaluate(evidence, required_scope="full", last_edit_at=9.0).status == "passed"


def test_nonzero_exit_is_failed() -> None:
    evidence = record_evidence("pytest -q", 1, "1 failed", created_at=10.0)
    assert evaluate(evidence, required_scope="full", last_edit_at=0.0).status == "failed"


def test_unattributable_command_fails_rather_than_passing() -> None:
    """One exit 0 through a pipe must never impersonate a verification."""
    evidence = record_evidence("pytest -q | tee out", 0, "ok", created_at=10.0)
    assert evaluate(evidence, required_scope="full", last_edit_at=0.0).status == "failed"
