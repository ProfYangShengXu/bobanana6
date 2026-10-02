"""Provenance chain validation: a removed middle link must be reported, not
absorbed."""

from __future__ import annotations

from fakes import provenance, snapshot

from bobanana6.core.provenance import (
    coverage,
    unreachable_points,
    validate_chain,
    validate_points,
)

SNAPSHOTS = {"s1": snapshot("s1"), "s2": snapshot("s2"), "s3": snapshot("s3")}


def test_intact_chain_has_no_breaks() -> None:
    chain = (provenance("a", "s1"), provenance("b", "s2"), provenance("c", "s3"))
    report = validate_chain(chain, SNAPSHOTS)
    assert report.ok
    assert report.breaks == ()


def test_missing_snapshot_is_reported_with_both_ends() -> None:
    report = validate_chain((provenance("a", "ghost"),), SNAPSHOTS)
    assert not report.ok
    assert report.breaks[0].value_ref == "a"
    assert report.breaks[0].missing_snapshot == "ghost"


def test_removing_a_middle_link_is_reported_at_the_break() -> None:
    chain = (provenance("a", "s1"), provenance("b", "s2"), provenance("c", "s3"))
    trimmed = {key: value for key, value in SNAPSHOTS.items() if key != "s2"}
    report = validate_chain(chain, trimmed)
    assert [(b.value_ref, b.missing_snapshot) for b in report.breaks] == [("b", "s2")]


def test_unreachable_points_are_listed() -> None:
    required = ("p1", "p2", "p3")
    chain = (provenance("p1", "s1"), provenance("p2", "ghost"))
    assert unreachable_points(required, chain, SNAPSHOTS) == ("p2", "p3")


def test_coverage_counts_only_reachable_citations() -> None:
    required = ("p1", "p2")
    assert coverage(required, (provenance("p1", "s1"), provenance("p2", "s2")), SNAPSHOTS) == 1.0
    assert coverage(required, (provenance("p1", "s1"), provenance("p2", "ghost")), SNAPSHOTS) == 0.5
    assert coverage(required, (), SNAPSHOTS) == 0.0


def test_no_points_is_zero_coverage_not_full_coverage() -> None:
    assert coverage((), (), SNAPSHOTS) == 0.0


def test_validate_points_combines_breaks_and_unreachable() -> None:
    report = validate_points(("p1", "p2"), (provenance("p1", "ghost"),), SNAPSHOTS)
    assert report.breaks[0].missing_snapshot == "ghost"
    assert report.unreachable_points == ("p1", "p2")
    assert not report.ok
