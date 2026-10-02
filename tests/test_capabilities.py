"""Capability dictionary and matching: three disjoint issue classes, and
determinism that does not depend on set iteration order."""

from __future__ import annotations

from fakes import block, worker

from bobanana6.core.capabilities import (
    CAPABILITY_TIERS,
    Capability,
    CapabilityTier,
    MatchIssue,
    check_over_permission,
    coverage_holds,
    match,
    tier_of,
)

READ = frozenset({Capability.READ})
SEARCH = frozenset({Capability.SEARCH})
READ_SEARCH = frozenset({Capability.READ, Capability.SEARCH})
WRITE = frozenset({Capability.WRITE_DELIVERABLE})


def test_every_capability_has_exactly_one_tier() -> None:
    assert set(CAPABILITY_TIERS) == set(Capability)
    assert tier_of(Capability.READ) is CapabilityTier.READ_ONLY
    assert tier_of(Capability.COMPUTE) is CapabilityTier.COMPUTE
    assert tier_of(Capability.WRITE_DELIVERABLE) is CapabilityTier.WRITE


def test_exact_fit_has_no_issues() -> None:
    result = match((block("B-1", READ),), (worker("W-1", READ),))
    assert result.assignments[0].worker_id == "W-1"
    assert result.issues == ()
    assert result.coverage_ok


def test_no_provider_is_a_gap() -> None:
    result = match((block("B-1", SEARCH),), (worker("W-1", READ),))
    assert [i.issue for i in result.issues] == [MatchIssue.CAPABILITY_GAP]
    assert result.gaps[0].capabilities == (Capability.SEARCH,)
    assert not result.coverage_ok


def test_provider_exists_but_concurrency_is_short() -> None:
    blocks = (block("B-1", READ), block("B-2", READ))
    workers = (worker("W-1", READ, concurrency=1),)
    result = match(blocks, workers)
    assert [i.issue for i in result.issues] == [MatchIssue.CAPABILITY_SHORT]


def test_over_qualified_provider_is_reported_as_cost() -> None:
    result = match((block("B-1", READ),), (worker("W-1", READ_SEARCH),))
    assert [i.issue for i in result.issues] == [MatchIssue.CAPABILITY_OVER]
    assert result.assignments[0].surplus == (Capability.SEARCH,)
    assert result.over_rate == 1.0


def test_writes_in_distinct_partitions_can_run_concurrently() -> None:
    blocks = (block("B-1", WRITE, partition="p1"), block("B-2", WRITE, partition="p2"))
    workers = (worker("W-1", WRITE, concurrency=2),)
    result = match(blocks, workers)
    assert MatchIssue.CAPABILITY_SHORT not in {i.issue for i in result.issues}


def test_writes_sharing_a_partition_are_serialized_and_reported_short() -> None:
    blocks = (block("B-1", WRITE, partition="p1"), block("B-2", WRITE, partition="p1"))
    workers = (worker("W-1", WRITE, concurrency=4),)
    result = match(blocks, workers)
    assert MatchIssue.CAPABILITY_SHORT in {i.issue for i in result.issues}


def test_matching_is_independent_of_input_order() -> None:
    blocks = (block("B-1", READ), block("B-2", SEARCH))
    workers = (worker("W-1", READ_SEARCH), worker("W-2", READ))
    forward = match(blocks, workers)
    backward = match(tuple(reversed(blocks)), tuple(reversed(workers)))
    assert forward == backward


def test_adding_a_need_changes_the_result() -> None:
    """Monotonicity: if adding a need changes nothing, the declaration is
    decorative and should be deleted."""
    before = match((block("B-1", READ),), (worker("W-1", READ),))
    after = match((block("B-1", READ_SEARCH),), (worker("W-1", READ),))
    assert before != after
    assert after.gaps


def test_coverage_holds_over_the_union_of_providers() -> None:
    blocks = (block("B-1", READ), block("B-2", SEARCH))
    workers = (worker("W-1", READ), worker("W-2", SEARCH))
    assert coverage_holds(blocks, workers)
    assert not coverage_holds(blocks, (worker("W-1", READ),))


def test_over_permission_flags_undeclared_actions() -> None:
    assert check_over_permission(READ, READ) == ()
    assert check_over_permission(READ, frozenset({Capability.READ, Capability.EDIT})) == (Capability.EDIT,)


def test_matching_is_reproducible_across_hash_seeds() -> None:
    """A frozenset iterated without sorting would vary with PYTHONHASHSEED.

    Re-running the same match cannot detect that in-process; instead the same
    inputs are rebuilt from a different insertion order, which is the failure
    mode the guard is about.
    """
    first = match((block("B-1", frozenset({Capability.READ, Capability.SEARCH})),), (worker("W-1", READ_SEARCH),))
    second = match((block("B-1", frozenset({Capability.SEARCH, Capability.READ})),), (worker("W-1", READ_SEARCH),))
    assert first == second
