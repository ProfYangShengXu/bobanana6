"""Per-kind checker tests: positive, negative and boundary cases.

Every kind gets at least one failing case, and the two special semantics get
their identity asserted: corroboration disagreement yields a ``Conflict`` while
still passing, and a definition drift yields a ``DefinitionMismatch`` rather
than a ``Conflict``.
"""

from __future__ import annotations

from fakes import artifact, candidate, constraint, mini_ontology, provenance, snapshot

from bobanana6.core.checkers import CHECKS, JUDGMENT_ORDER, check_kind
from bobanana6.core.checkers.base import CheckContext
from bobanana6.core.model import (
    MISSING,
    Artifact,
    Claim,
    Column,
    Conflict,
    DefinitionMismatch,
)


def context(**overrides) -> CheckContext:
    values = {
        "ontology": mini_ontology(),
        "snapshots": {"snap-1": snapshot("snap-1")},
        "now": "2026-01-02T00:00:00",
    }
    values.update(overrides)
    return CheckContext(**values)


def run(kind: str, spec: dict, *, target: str = "out.csv", cand=None, ctx=None):
    item = constraint("C-1", kind, target, spec)
    return check_kind(item, cand if cand is not None else candidate(artifacts=(artifact(),)), ctx or context())


def test_registry_covers_all_sixteen_kinds() -> None:
    assert len(CHECKS) == 16
    assert set(CHECKS) == set(JUDGMENT_ORDER)
    assert len(JUDGMENT_ORDER) == 16


def test_unknown_kind_fails_closed() -> None:
    outcome = run("rang3", {})
    assert outcome.ok is False
    assert "unknown constraint kind" in outcome.facts[0]


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #


def test_schema_accepts_matching_columns() -> None:
    art = artifact("out.csv", ({"pd": 0.1},), columns=(Column("pd", "float"),))
    outcome = run("schema", {"columns": [{"name": "pd", "dtype": "float"}]}, cand=candidate(artifacts=(art,)))
    assert outcome.ok


def test_schema_rejects_missing_column_and_reports_it() -> None:
    art = artifact("out.csv", ({"pd": 0.1},))
    outcome = run("schema", {"columns": [{"name": "amount", "dtype": "float"}]}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok
    assert "missing column 'amount'" in outcome.facts[0]


def test_schema_rejects_a_missing_cell_in_a_not_null_column() -> None:
    """★ A04：列声明了不得为空，缺失格就必须报到具体坐标 —— 不能被静默填平。"""
    art = artifact("out.csv", ({"amount": MISSING},), columns=(Column("amount", "str", nullable=False),))
    outcome = run(
        "schema",
        {"columns": [{"name": "amount", "dtype": "str", "nullable": False}]},
        cand=candidate(artifacts=(art,)),
    )
    assert not outcome.ok
    assert "row=0 col=amount holds no value" in outcome.facts[0]


def test_schema_accepts_a_filled_zero_in_a_not_null_column() -> None:
    """★ A04 的另一半：`0` 是一个值，不得被当成「缺失」而误杀。"""
    art = artifact("out.csv", ({"amount": 0},), columns=(Column("amount", "float", nullable=False),))
    outcome = run(
        "schema",
        {"columns": [{"name": "amount", "dtype": "float", "nullable": False}]},
        cand=candidate(artifacts=(art,)),
    )
    assert outcome.ok, outcome.facts


def test_schema_rejects_wrong_dtype() -> None:
    art = artifact("out.csv", ({"pd": "x"},))
    outcome = run("schema", {"columns": [{"name": "pd", "dtype": "float"}]}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "dtype" in outcome.facts[0]


def test_schema_column_order_is_ignored_unless_asked() -> None:
    art = artifact("out.csv", ({"a": 1, "b": 2},), columns=(Column("b", "float"), Column("a", "float")))
    assert run("schema", {"columns": [{"name": "a", "dtype": "float"}, {"name": "b", "dtype": "float"}]}, cand=candidate(artifacts=(art,))).ok
    assert not run(
        "schema",
        {"columns": [{"name": "a", "dtype": "float"}, {"name": "b", "dtype": "float"}], "order_sensitive": True},
        cand=candidate(artifacts=(art,)),
    ).ok


def test_schema_rejects_malformed_spec() -> None:
    assert not run("schema", {"columns": "not-a-list"}).ok


# --------------------------------------------------------------------------- #
# presence
# --------------------------------------------------------------------------- #


def test_presence_accepts_enough_rows() -> None:
    art = artifact("out.csv", ({"a": 1}, {"a": 2}))
    assert run("presence", {"min_rows": 2}, cand=candidate(artifacts=(art,))).ok


def test_presence_rejects_missing_artifact() -> None:
    outcome = run("presence", {"min_rows": 1}, cand=candidate(artifacts=()))
    assert not outcome.ok and "not found" in outcome.facts[0]


def test_presence_rejects_too_few_rows() -> None:
    art = artifact("out.csv", ({"a": 1},))
    outcome = run("presence", {"min_rows": 2}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "needs >= 2" in outcome.facts[0]


def test_presence_rejects_bad_min_rows() -> None:
    assert not run("presence", {"min_rows": -1}).ok


# --------------------------------------------------------------------------- #
# range
# --------------------------------------------------------------------------- #


def test_range_reports_first_violating_coordinate() -> None:
    art = artifact("out.csv", ({"pd": 0.5}, {"pd": 1.5}, {"pd": 9.0}))
    outcome = run("range", {"column": "pd", "min": 0.0, "max": 1.0}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok
    assert "row=1" in outcome.facts[0]


def test_range_tolerance_is_inclusive_on_both_sides() -> None:
    inside = artifact("out.csv", ({"pd": 1.0000005},))
    outside = artifact("out.csv", ({"pd": 1.001},))
    spec = {"column": "pd", "min": 0.0, "max": 1.0, "tol": 1e-6}
    assert run("range", spec, cand=candidate(artifacts=(inside,))).ok
    assert not run("range", spec, cand=candidate(artifacts=(outside,))).ok


def test_range_rejects_non_numeric_value() -> None:
    art = artifact("out.csv", ({"pd": "n/a"},))
    outcome = run("range", {"column": "pd", "min": 0.0, "max": 1.0}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "not numeric" in outcome.facts[0]


# --------------------------------------------------------------------------- #
# invariant
# --------------------------------------------------------------------------- #


def test_invariant_accepts_a_true_expression() -> None:
    art = artifact("out.csv", ({"a": 1, "b": 2},))
    assert run("invariant", {"expr": "b > a"}, cand=candidate(artifacts=(art,))).ok


def test_invariant_reports_the_first_false_row() -> None:
    art = artifact("out.csv", ({"a": 1, "b": 2}, {"a": 3, "b": 1}))
    outcome = run("invariant", {"expr": "b > a"}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "row=1" in outcome.facts[0]


def test_invariant_refuses_attribute_access() -> None:
    art = artifact("out.csv", ({"a": 1},))
    outcome = run("invariant", {"expr": "a.__class__"}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "not allowed" in outcome.facts[0]


def test_invariant_reports_unknown_identifier() -> None:
    art = artifact("out.csv", ({"a": 1},))
    outcome = run("invariant", {"expr": "nope > 0"}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "unknown identifier" in outcome.facts[0]


# --------------------------------------------------------------------------- #
# relation
# --------------------------------------------------------------------------- #


def test_relation_accepts_equal_columns() -> None:
    art = artifact("out.csv", ({"a": 1, "b": 1},))
    assert run("relation", {"left": {"column": "a"}, "right": {"column": "b"}, "op": "eq"}, cand=candidate(artifacts=(art,))).ok


def test_relation_reports_the_first_break_row() -> None:
    art = artifact("out.csv", ({"a": 1, "b": 1}, {"a": 2, "b": 3}))
    outcome = run("relation", {"left": {"column": "a"}, "right": {"column": "b"}, "op": "eq"}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "row=1" in outcome.facts[0]


def test_relation_detects_row_count_mismatch() -> None:
    cand = candidate(
        artifacts=(
            artifact("left.csv", ({"a": 1},)),
            artifact("right.csv", ({"a": 1}, {"a": 2})),
        )
    )
    outcome = run(
        "relation",
        {"left": {"artifact": "left.csv", "column": "a"}, "right": {"artifact": "right.csv", "column": "a"}, "op": "eq"},
        cand=cand,
    )
    assert not outcome.ok and "row counts differ" in outcome.facts[0]


def test_relation_rejects_bad_op() -> None:
    assert not run("relation", {"left": {"column": "a"}, "right": {"column": "a"}, "op": "≈"}).ok


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #


def test_provenance_accepts_full_coverage() -> None:
    art = artifact("out.csv", ({"a": 1}, {"a": 2}))
    cand = candidate(
        artifacts=(art,),
        provenance=(provenance("out.csv:row=0"), provenance("out.csv:row=1")),
    )
    assert run("provenance", {}, cand=cand).ok


def test_provenance_rejects_missing_coverage() -> None:
    art = artifact("out.csv", ({"a": 1}, {"a": 2}))
    cand = candidate(artifacts=(art,), provenance=(provenance("out.csv:row=0"),))
    outcome = run("provenance", {}, cand=cand)
    assert not outcome.ok and "coverage" in outcome.facts[0]


def test_provenance_rejects_artifact_with_no_points() -> None:
    art = Artifact(name="out.csv")
    outcome = run("provenance", {}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "no traceable points" in outcome.facts[0]


def test_provenance_names_unknown_snapshots() -> None:
    art = artifact("out.csv", ({"a": 1},))
    cand = candidate(artifacts=(art,), provenance=(provenance("out.csv:row=0", "ghost"),))
    outcome = run("provenance", {}, cand=cand)
    assert not outcome.ok and "unknown snapshots" in outcome.facts[0]


# --------------------------------------------------------------------------- #
# corroboration / definition_pin -- the two semantics that must stay distinct
# --------------------------------------------------------------------------- #


def test_corroboration_fails_when_sources_are_too_few() -> None:
    cand = candidate(claims=(Claim("pd", 0.1, "src-1", provenance("a")),))
    outcome = run("corroboration", {"field": "pd", "min_sources": 2}, cand=cand)
    assert not outcome.ok and outcome.divergences == ()


def test_corroboration_disagreement_is_a_conflict_not_a_failure() -> None:
    cand = candidate(
        claims=(
            Claim("pd_3y", 0.1, "src-1", provenance("a")),
            Claim("pd_3y", 0.2, "src-2", provenance("b")),
        )
    )
    outcome = run("corroboration", {"field": "pd_3y", "min_sources": 2}, cand=cand)
    assert outcome.ok, "a real disagreement is a signal, not a failed block"
    assert len(outcome.divergences) == 1
    assert isinstance(outcome.divergences[0], Conflict)
    assert not isinstance(outcome.divergences[0], DefinitionMismatch)


def test_corroboration_equal_values_raise_nothing() -> None:
    cand = candidate(
        claims=(
            Claim("pd_3y", 0.1, "src-1", provenance("a")),
            Claim("pd_3y", 0.1, "src-2", provenance("b")),
        )
    )
    outcome = run("corroboration", {"field": "pd_3y", "min_sources": 2}, cand=cand)
    assert outcome.ok and outcome.divergences == ()


def test_definition_pin_mismatch_is_a_definition_mismatch_not_a_conflict() -> None:
    outcome = run("definition_pin", {"concept": "credit.pd", "version": "v2"})
    assert not outcome.ok
    assert len(outcome.divergences) == 1
    assert isinstance(outcome.divergences[0], DefinitionMismatch)
    assert not isinstance(outcome.divergences[0], Conflict)


def test_definition_pin_match_passes() -> None:
    assert run("definition_pin", {"concept": "credit.pd", "version": "v1"}).ok


def test_definition_pin_without_a_definition_fails() -> None:
    outcome = run("definition_pin", {"concept": "credit.amount", "version": "v1"})
    assert not outcome.ok and "no definition" in outcome.facts[0]


# --------------------------------------------------------------------------- #
# freshness
# --------------------------------------------------------------------------- #


def test_freshness_accepts_recent_snapshot() -> None:
    cand = candidate(provenance=(provenance("a", "snap-1"),))
    assert run("freshness", {"max_age_s": 100000}, cand=cand).ok


def test_freshness_rejects_stale_snapshot() -> None:
    cand = candidate(provenance=(provenance("a", "snap-1"),))
    outcome = run("freshness", {"max_age_s": 10}, cand=cand)
    assert not outcome.ok and "max 10" in outcome.facts[0]


def test_freshness_needs_an_injected_now() -> None:
    cand = candidate(provenance=(provenance("a", "snap-1"),))
    outcome = run("freshness", {"max_age_s": 10}, cand=cand, ctx=context(now=None))
    assert not outcome.ok and "never reads the clock" in outcome.facts[0]


def test_freshness_rejects_snapshot_before_not_before() -> None:
    cand = candidate(provenance=(provenance("a", "snap-1"),))
    outcome = run("freshness", {"not_before": "2026-06-01T00:00:00"}, cand=cand)
    assert not outcome.ok and "not_before" in outcome.facts[0]


def test_freshness_fails_when_no_snapshot_is_referenced() -> None:
    outcome = run("freshness", {"max_age_s": 10}, cand=candidate())
    assert not outcome.ok and "unverifiable" in outcome.facts[0]


# --------------------------------------------------------------------------- #
# coverage
# --------------------------------------------------------------------------- #


def test_coverage_accepts_all_required_sources() -> None:
    cand = candidate(provenance=(provenance("a", "src-1"), provenance("b", "src-2")))
    assert run("coverage", {"required_sources": ["src-1", "src-2"]}, cand=cand).ok


def test_coverage_rejects_undeclared_missing_source() -> None:
    cand = candidate(provenance=(provenance("a", "src-1"),))
    outcome = run("coverage", {"required_sources": ["src-1", "src-2"]}, cand=cand)
    assert not outcome.ok and "src-2" in outcome.facts[0]


def test_coverage_accepts_declared_missing_source() -> None:
    cand = candidate(provenance=(provenance("a", "src-1"),))
    outcome = run("coverage", {"required_sources": ["src-1", "src-2"], "declared_missing": ["src-2"]}, cand=cand)
    assert outcome.ok


# --------------------------------------------------------------------------- #
# concept_ref / unit_consistency / code_valid
# --------------------------------------------------------------------------- #


def test_concept_ref_maps_via_alias() -> None:
    assert run("concept_ref", {"field": "pd_3y"}).ok


def test_concept_ref_rejects_unmapped_field() -> None:
    outcome = run("concept_ref", {"field": "mystery"})
    assert not outcome.ok and "not declared unmapped" in outcome.facts[0]


def test_concept_ref_accepts_explicitly_unmapped_field() -> None:
    outcome = run("concept_ref", {"field": "mystery"}, ctx=context(unmapped_fields=frozenset({"mystery"})))
    assert outcome.ok


def test_concept_ref_requires_the_pinned_concept_to_exist() -> None:
    outcome = run("concept_ref", {"field": "pd_3y", "concept": "ghost"})
    assert not outcome.ok and "not in the ontology" in outcome.facts[0]


def test_concept_ref_needs_an_ontology() -> None:
    outcome = run("concept_ref", {"field": "pd_3y"}, ctx=context(ontology=None))
    assert not outcome.ok and "ontology is required" in outcome.facts[0]


def test_unit_consistency_accepts_matching_unit() -> None:
    assert run("unit_consistency", {"concept": "credit.amount", "unit": "CNY"}).ok


def test_unit_consistency_rejects_undeclared_conversion() -> None:
    outcome = run("unit_consistency", {"concept": "credit.amount", "unit": "USD"})
    assert not outcome.ok and "no conversion" in outcome.facts[0]


def test_unit_consistency_accepts_declared_conversion() -> None:
    outcome = run("unit_consistency", {"concept": "credit.amount", "unit": "USD", "conversion": "7 CNY/USD"})
    assert outcome.ok


def test_unit_consistency_reads_the_unit_off_the_value() -> None:
    """★ A10：值里写了单位就按值判 —— 只比声明会放过「1 千元」。"""
    art = artifact("out.csv", ({"credit.amount": "1 千元"},))
    outcome = run(
        "unit_consistency",
        {"concept": "credit.amount", "unit": "CNY"},
        cand=candidate(artifacts=(art,)),
    )
    assert not outcome.ok
    assert "declares unit '千元'" in outcome.facts[0] and "row=0" in outcome.facts[0]


def test_unit_consistency_accepts_a_value_that_declares_the_same_unit() -> None:
    art = artifact("out.csv", ({"credit.amount": "1 CNY"},))
    outcome = run(
        "unit_consistency",
        {"concept": "credit.amount", "unit": "CNY"},
        cand=candidate(artifacts=(art,)),
    )
    assert outcome.ok, outcome.facts


def test_unit_consistency_leaves_a_unitless_value_alone() -> None:
    """★ 残留如实留着：值里不带单位就无从判，不假装通过也不误杀。"""
    art = artifact("out.csv", ({"credit.amount": "1"},))
    assert run("unit_consistency", {"concept": "credit.amount", "unit": "CNY"}, cand=candidate(artifacts=(art,))).ok


def test_unit_consistency_accepts_a_convertible_unit_when_declared() -> None:
    art = artifact("out.csv", ({"credit.amount": "1 USD"},))
    outcome = run(
        "unit_consistency",
        {"concept": "credit.amount", "unit": "CNY", "conversion": "7 CNY/USD"},
        cand=candidate(artifacts=(art,)),
    )
    assert outcome.ok, outcome.facts


def test_unit_consistency_says_when_the_unit_is_only_assumed() -> None:
    """★ ①(c)：值不带单位、列也没声明 → 单位【靠本体声明】，必须说出来，不许默认成它。"""
    art = artifact("out.csv", ({"credit.amount": 1},), columns=(Column("credit.amount", "float"),))
    outcome = run("unit_consistency", {"concept": "credit.amount", "unit": "CNY"}, cand=candidate(artifacts=(art,)))
    assert outcome.ok
    assert "assumption, not a declaration" in outcome.facts[0], outcome.facts


def test_unit_consistency_refuses_an_assumed_unit_when_the_task_demands_explicit() -> None:
    """★ ①(b)：任务要求单位必须显式给出 → 猜不出来就拒，不把没标的数当成已知单位。"""
    art = artifact("out.csv", ({"credit.amount": 1},), columns=(Column("credit.amount", "float"),))
    outcome = run(
        "unit_consistency",
        {"concept": "credit.amount", "unit": "CNY", "unit_source": "explicit"},
        cand=candidate(artifacts=(art,)),
    )
    assert not outcome.ok
    assert "requires the unit to be explicit" in outcome.facts[0], outcome.facts


def test_unit_consistency_accepts_a_column_that_declares_its_unit() -> None:
    """列自己声明了单位 —— 那是【声明】，不是假设。"""
    art = artifact(
        "out.csv", ({"credit.amount": 1},), columns=(Column("credit.amount", "float", unit="CNY"),)
    )
    outcome = run(
        "unit_consistency",
        {"concept": "credit.amount", "unit": "CNY", "unit_source": "explicit"},
        cand=candidate(artifacts=(art,)),
    )
    assert outcome.ok, outcome.facts
    assert "declares its unit 'CNY'" in outcome.facts[0], outcome.facts


def test_unit_consistency_rejects_a_column_declaring_another_unit() -> None:
    art = artifact("out.csv", ({"credit.amount": 1},), columns=(Column("credit.amount", "float", unit="USD"),))
    outcome = run("unit_consistency", {"concept": "credit.amount", "unit": "CNY"}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "column 'credit.amount' declares unit 'USD'" in outcome.facts[0], outcome.facts


def test_a_plain_decimal_string_is_not_read_as_a_quantity() -> None:
    """★ 真模型跑测抓到的：`"1200000.0"` 被读成「数值 1200000，单位 '.0'」。

    没有这条规则时，一个普普通通的小数会被判成「单位是 .0」→ 合法值被误拒
    （实测：合并块 `value '1200000.0' declares unit '.0', but 'amount' is declared as 'CNY'`）。
    """
    from bobanana6.core.ontology import split_quantity

    assert split_quantity("1200000.0") is None
    assert split_quantity("0.011") is None
    assert split_quantity("1200000") is None
    # 单位符号必须以字母开头：紧挨着写（Quantity 形态）仍然要认出来
    assert split_quantity("100元") == (100.0, "元")
    assert split_quantity("12m") == (12.0, "m")
    assert split_quantity("1.5e3 千元") == (1500.0, "千元")


def test_unit_consistency_does_not_reject_a_plain_decimal() -> None:
    """★ 同一个真模型读数的另一半：小数**没有**单位，它是「没标」，不是「标错了」。"""
    art = artifact("out.csv", ({"credit.amount": "1200000.0"},), columns=(Column("credit.amount", "str"),))
    outcome = run("unit_consistency", {"concept": "credit.amount", "unit": "CNY"}, cand=candidate(artifacts=(art,)))
    assert outcome.ok, outcome.facts
    assert "assumption, not a declaration" in outcome.facts[0], outcome.facts


def test_unit_consistency_detects_peer_conflict() -> None:
    outcome = run(
        "unit_consistency",
        {"concept": "credit.amount", "unit": "CNY"},
        ctx=context(peer_units={"credit.amount": "USD"}),
    )
    assert not outcome.ok and "peer block" in outcome.facts[0]


def test_code_valid_accepts_declared_code() -> None:
    art = artifact("out.csv", ({"rating": "AAA"},))
    assert run("code_valid", {"column": "rating", "code_system": "rating_scale"}, cand=candidate(artifacts=(art,))).ok


def test_code_valid_rejects_unknown_code_with_coordinate() -> None:
    art = artifact("out.csv", ({"rating": "AAA"}, {"rating": "ZZZ"}))
    outcome = run("code_valid", {"column": "rating", "code_system": "rating_scale"}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "row=1" in outcome.facts[0]


def test_code_valid_rejects_unknown_system() -> None:
    art = artifact("out.csv", ({"rating": "AAA"},))
    outcome = run("code_valid", {"column": "rating", "code_system": "ghost"}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "not declared" in outcome.facts[0]


# --------------------------------------------------------------------------- #
# custom
# --------------------------------------------------------------------------- #


def test_custom_registered_check_passes_and_fails() -> None:
    passing = context(custom_checks={"always_ok": lambda c, cand, ctx: True})
    failing = context(custom_checks={"always_no": lambda c, cand, ctx: (False, ["nope"])})
    assert run("custom", {"check": "always_ok"}, ctx=passing).ok
    outcome = run("custom", {"check": "always_no"}, ctx=failing)
    assert not outcome.ok and outcome.facts == ("nope",)


def test_custom_unknown_check_fails_closed() -> None:
    assert not run("custom", {"check": "missing"}).ok


def test_custom_exception_is_visible_not_swallowed() -> None:
    def boom(constraint, candidate, ctx):
        raise RuntimeError("detonated")

    outcome = run("custom", {"check": "boom"}, ctx=context(custom_checks={"boom": boom}))
    assert not outcome.ok
    assert "RuntimeError: detonated" in outcome.facts[0]


def test_schema_rejects_unexpected_nullability() -> None:
    art = artifact("out.csv", ({"a": 1},), columns=(Column("a", "float", nullable=True),))
    outcome = run("schema", {"columns": [{"name": "a", "dtype": "float", "nullable": False}]}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "nullable" in outcome.facts[0]


def test_provenance_traces_text_lines_as_well_as_rows() -> None:
    art = Artifact(name="out.txt", text="alpha\nbeta\n")
    cand = candidate(
        artifacts=(art,),
        provenance=(provenance("out.txt:line=0"), provenance("out.txt:line=1")),
    )
    assert run("provenance", {}, target="out.txt", cand=cand).ok


def test_freshness_rejects_unparseable_timestamp() -> None:
    cand = candidate(provenance=(provenance("a", "snap-bad"),))
    ctx = context(snapshots={"snap-bad": snapshot("snap-bad", captured_at="not-a-date")})
    outcome = run("freshness", {"max_age_s": 100}, cand=cand, ctx=ctx)
    assert not outcome.ok and "not a timestamp" in outcome.facts[0]


def test_coverage_rejects_malformed_declared_missing() -> None:
    cand = candidate(provenance=(provenance("a", "src-1"),))
    outcome = run("coverage", {"required_sources": ["src-1"], "declared_missing": "src-1"}, cand=cand)
    assert not outcome.ok and "must be a list" in outcome.facts[0]


def test_invariant_supports_arithmetic_expressions() -> None:
    art = artifact("out.csv", ({"a": 2, "b": 3},))
    assert run("invariant", {"expr": "a * b == 6 and abs(a - b) == 1"}, cand=candidate(artifacts=(art,))).ok


def test_invariant_rejects_disallowed_operators() -> None:
    art = artifact("out.csv", ({"a": 2},))
    outcome = run("invariant", {"expr": "a @ 2"}, cand=candidate(artifacts=(art,)))
    assert not outcome.ok and "not allowed" in outcome.facts[0]


# --------------------------------------------------------------------------- #
# ★ `#33`: frozen_files —— 冻结的载体在装配期还是不是原文
# --------------------------------------------------------------------------- #


def test_frozen_files_accepts_a_byte_identical_carrier() -> None:
    outcome = run("frozen_files", {"sources": ["test-a"]}, ctx=context(frozen_files={"test-a": ("abc123", "abc123")}))
    assert outcome.ok, outcome.facts


def test_frozen_files_refuses_a_modified_carrier_and_names_both_digests() -> None:
    """★ 反证的判据侧：工作区里那份被改了 → 具名失败，且两个摘要都报出来。"""
    outcome = run("frozen_files", {"sources": ["test-a"]}, ctx=context(frozen_files={"test-a": ("abc123", "def456")}))
    assert not outcome.ok
    assert "was modified in the workspace" in outcome.facts[0]
    assert "abc123" in outcome.facts[0] and "def456" in outcome.facts[0]


def test_frozen_files_fails_closed_when_nothing_was_observed() -> None:
    """★ 没观测到 = 没被验证过 ⇒ 具名失败，绝不静默通过。"""
    outcome = run("frozen_files", {"sources": ["test-a"]})
    assert not outcome.ok
    assert "never observed" in outcome.facts[0]


def test_frozen_files_needs_a_named_source_when_nothing_was_observed() -> None:
    outcome = run("frozen_files", {})
    assert not outcome.ok
    assert "spec.sources must name at least one" in outcome.facts[0]
