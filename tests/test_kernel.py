"""Kernel behavior: independent admission, three-way classification, convergence."""

from __future__ import annotations

from fakes import FakeRuntime, artifact, bundle, candidate, constraint, mini_ontology

from bobanana6.core.checkers.base import CheckContext
from bobanana6.core.kernel import (
    admit,
    candidate_fingerprint,
    classify,
    converge,
    run_hard,
)
from bobanana6.core.model import (
    ConvergeDecision,
)


def context() -> CheckContext:
    return CheckContext(ontology=mini_ontology())


def _pair(*, versions: dict | None = None, sources: tuple[str, str] = ("src-1", "src-2"), values: tuple[float, float] = (0.1, 0.2)):
    left = candidate("T-01", sources[0], artifacts=(artifact("out.csv", ({"pd_3y": values[0]},)),), definition_versions=versions or {})
    right = candidate("T-02", sources[1], artifacts=(artifact("out.csv", ({"pd_3y": values[1]},)),), definition_versions=versions or {})
    return left, right


# --------------------------------------------------------------------------- #
# admit
# --------------------------------------------------------------------------- #


def test_worker_self_report_is_not_trusted() -> None:
    """The runtime says "done"; the kernel judges the artifact and disagrees."""
    failing = bundle(hard=(constraint("C-1", "presence", "out.csv", {"min_rows": 5}),))
    cand = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 1},)),))
    runtime = FakeRuntime(cand)
    assert runtime.self_report_ok is True
    verdict = admit(failing, runtime.run(None, failing, None, None, None), context())  # type: ignore[arg-type]
    assert verdict.ok is False
    assert verdict.failed == ("C-1",)


def test_admit_reports_every_failure_not_just_the_first() -> None:
    item = bundle(
        hard=(
            constraint("C-2", "presence", "out.csv", {"min_rows": 5}),
            constraint("C-1", "schema", "out.csv", {"columns": [{"name": "ghost", "dtype": "float"}]}),
        )
    )
    cand = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 1},)),))
    verdict = admit(item, cand, context())
    # `failed` is complete and follows judgment order (presence before schema).
    assert verdict.failed == ("C-2", "C-1")
    assert set(verdict.failed) == {"C-1", "C-2"}


def test_admit_uses_judgment_order_for_evaluation() -> None:
    item = bundle(
        hard=(
            constraint("C-2", "range", "out.csv", {"column": "a", "min": 0, "max": 1}),
            constraint("C-1", "presence", "out.csv", {"min_rows": 5}),
        )
    )
    cand = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 5},)),))
    outcomes = run_hard(item, cand, context())
    assert [o.constraint_id for o in outcomes] == ["C-1", "C-2"]


def test_admit_unknown_kind_fails_closed_and_names_the_kind() -> None:
    item = bundle(hard=(constraint("C-1", "rang3", "out.csv", {}),))
    verdict = admit(item, candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 1},)),)), context())
    assert verdict.ok is False
    assert "unknown constraint kind" in verdict.evidence["C-1"]


def test_soft_constraints_are_scored_but_never_fail_admission() -> None:
    item = bundle(hard=(), soft=(constraint("S-1", "presence", "out.csv", {"min_rows": 100}),))
    cand = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 1},)),))
    verdict = admit(item, cand, context())
    assert verdict.ok is True
    assert "S-1" in verdict.evidence


def test_evidence_records_observed_facts() -> None:
    item = bundle(hard=(constraint("C-1", "presence", "out.csv", {"min_rows": 5}),))
    cand = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 1},)),))
    assert "needs >= 5" in admit(item, cand, context()).evidence["C-1"]


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #


def test_fingerprint_is_content_based_not_identity_based() -> None:
    art = artifact("out.csv", ({"a": 1},))
    first = candidate("T-01", "src-1", artifacts=(art,))
    second = candidate("T-02", "src-2", artifacts=(artifact("out.csv", ({"a": 1},)),))
    different = candidate("T-03", "src-3", artifacts=(artifact("out.csv", ({"a": 2},)),))
    assert candidate_fingerprint(first) == candidate_fingerprint(second)
    assert candidate_fingerprint(first) != candidate_fingerprint(different)


# --------------------------------------------------------------------------- #
# classify
# --------------------------------------------------------------------------- #


def test_classify_definition_drift_is_a_mismatch_not_a_conflict() -> None:
    item = bundle(hard=(constraint("C-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),))
    left, right = _pair(versions={"credit.pd": "v1"})
    right = candidate("T-02", "src-2", artifacts=right.artifacts, definition_versions={"credit.pd": "v2"})
    result = classify(item, left, right, context())
    assert len(result.definition_mismatches) == 1
    assert result.conflicts == ()
    assert result.blind_spots == ()


def test_classify_real_conflict_when_sources_differ() -> None:
    item = bundle(hard=(constraint("C-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),))
    left, right = _pair()
    result = classify(item, left, right, context())
    assert len(result.conflicts) == 1
    assert result.definition_mismatches == ()
    assert result.blind_spots == ()


def test_classify_blind_spot_when_no_constraint_covers_the_field() -> None:
    left, right = _pair()
    result = classify(bundle(), left, right, context())
    assert len(result.blind_spots) == 1
    assert result.conflicts == ()


def test_classify_same_source_difference_is_a_blind_spot() -> None:
    item = bundle(hard=(constraint("C-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),))
    left, right = _pair(sources=("src-1", "src-1"))
    result = classify(item, left, right, context())
    assert len(result.blind_spots) == 1


def test_classify_definition_is_checked_before_conflict() -> None:
    """Version drift plus differing values must not be recorded as a conflict."""
    item = bundle(hard=(constraint("C-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),))
    left = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"pd_3y": 0.1},)),), definition_versions={"credit.pd": "v1"})
    right = candidate("T-02", "src-2", artifacts=(artifact("out.csv", ({"pd_3y": 0.9},)),), definition_versions={"credit.pd": "v2"})
    result = classify(item, left, right, context())
    assert result.definition_mismatches and not result.conflicts


def test_classify_is_symmetric() -> None:
    item = bundle()
    left, right = _pair()
    forward = classify(item, left, right, context())
    backward = classify(item, right, left, context())
    assert len(forward.blind_spots) == len(backward.blind_spots)


# --------------------------------------------------------------------------- #
# converge
# --------------------------------------------------------------------------- #


def test_converge_with_no_candidate_passing_arbitrates() -> None:
    item = bundle(hard=(constraint("C-1", "presence", "out.csv", {"min_rows": 9}),))
    cand = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 1},)),))
    result = converge(item, (cand,), context())
    assert result.decision is ConvergeDecision.ARBITRATE


def test_converge_with_one_candidate_delivers() -> None:
    result = converge(bundle(), (candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 1},)),)),), context())
    assert result.decision is ConvergeDecision.DELIVER


def test_converge_with_identical_fingerprints_delivers() -> None:
    left = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 1},)),))
    right = candidate("T-02", "src-2", artifacts=(artifact("out.csv", ({"a": 1},)),))
    assert converge(bundle(), (left, right), context()).decision is ConvergeDecision.DELIVER


def test_converge_routes_definition_mismatch_to_normalize() -> None:
    item = bundle(hard=(constraint("C-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),))
    left = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"pd_3y": 0.1},)),), definition_versions={"credit.pd": "v1"})
    right = candidate("T-02", "src-2", artifacts=(artifact("out.csv", ({"pd_3y": 0.2},)),), definition_versions={"credit.pd": "v2"})
    result = converge(item, (left, right), context())
    assert result.decision is ConvergeDecision.NORMALIZE_DEFINITION


def test_converge_routes_covering_constraint_conflict_to_record() -> None:
    item = bundle(hard=(constraint("C-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),))
    left, right = _pair()
    result = converge(item, (left, right), context())
    assert result.decision is ConvergeDecision.RECORD_CONFLICT


def test_converge_routes_uncovered_difference_to_refine() -> None:
    left, right = _pair()
    result = converge(bundle(), (left, right), context())
    assert result.decision is ConvergeDecision.REFINE_SPLIT


def test_converge_is_independent_of_candidate_order() -> None:
    item = bundle(hard=(constraint("C-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),))
    left, right = _pair()
    forward = converge(item, (left, right), context())
    backward = converge(item, (right, left), context())
    assert forward.decision is backward.decision
    assert forward.classification == backward.classification


def test_converge_is_idempotent_for_the_same_candidate() -> None:
    item = bundle(hard=(constraint("C-1", "presence", "out.csv", {"min_rows": 1}),))
    cand = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"a": 1},)),))
    assert admit(item, cand, context()) == admit(item, cand, context())
