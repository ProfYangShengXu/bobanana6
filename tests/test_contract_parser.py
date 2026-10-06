"""contract/parser.py: parse declarations, refuse empty rationale / unmapped reason."""

from __future__ import annotations

from pathlib import Path

import pytest

from bobanana6.contract import (
    ContractParseError,
    compile_bundle,
    parse_contract,
    parse_contract_file,
)

VALID = """
task_id: task-1
goal: build the credit panel
version: 2
deliverables:
  - {name: out.csv, schema: {pd: float, amount: float}}
  - {name: nice.csv, required: false}
sources:
  - {ref: src-1, locator: https://example.invalid/1}
constraints:
  hard:
    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "the panel must not be empty"}
  soft:
    - {id: S-1, kind: freshness, target: out.csv, spec: {max_age_s: 86400}, rationale: "prefer recent data"}
blind: [unknown_column]
unmapped:
  - {field: mystery, reason: "not in the ontology yet"}
limits: {max_rounds: 3}
"""


def test_valid_contract_parses_both_halves() -> None:
    document = parse_contract(VALID)
    assert document.contract.task_id == "task-1"
    assert document.contract.goal == "build the credit panel"
    assert [d.name for d in document.contract.deliverables] == ["out.csv", "nice.csv"]
    assert document.contract.deliverables[1].required is False
    assert document.contract.sources[0].ref == "src-1"
    assert [c.id for c in document.hard] == ["C-1"]
    assert [c.id for c in document.soft] == ["S-1"]
    assert document.blind == ("unknown_column",)
    assert document.unmapped == (("mystery", "not in the ontology yet"),)
    assert document.limits.max_rounds == 3
    assert document.version == 2


# --------------------------------------------------------------------------- #
# the two fields that must be refused here, not in core
# --------------------------------------------------------------------------- #


def test_empty_rationale_is_refused_with_its_path() -> None:
    text = VALID.replace('rationale: "the panel must not be empty"', 'rationale: ""')
    with pytest.raises(ContractParseError) as error:
        parse_contract(text, source="task.yaml")
    message = str(error.value)
    assert message.startswith("task.yaml:")
    assert "$.constraints.hard[0].rationale" in message
    assert "rule: non_empty" in message


def test_whitespace_only_rationale_is_refused() -> None:
    text = VALID.replace('rationale: "the panel must not be empty"', 'rationale: "   "')
    with pytest.raises(ContractParseError, match="non_empty"):
        parse_contract(text)


def test_empty_unmapped_reason_is_refused() -> None:
    """★ An empty reason is the same as no declaration -- it must not vanish."""
    text = VALID.replace('reason: "not in the ontology yet"', 'reason: ""')
    with pytest.raises(ContractParseError) as error:
        parse_contract(text, source="task.yaml")
    message = str(error.value)
    assert "$.unmapped[0].reason" in message and "rule: non_empty" in message


def test_missing_unmapped_reason_key_is_refused() -> None:
    text = VALID.replace('{field: mystery, reason: "not in the ontology yet"}', '{field: mystery}')
    with pytest.raises(ContractParseError, match="reason"):
        parse_contract(text)


# --------------------------------------------------------------------------- #
# fail-fast structural checks
# --------------------------------------------------------------------------- #


def test_unknown_constraint_kind_is_refused() -> None:
    text = VALID.replace("kind: presence", "kind: presencee")
    with pytest.raises(ContractParseError, match="rule: enum"):
        parse_contract(text)


def test_unknown_top_level_key_is_refused() -> None:
    with pytest.raises(ContractParseError, match="additionalProperties"):
        parse_contract(VALID + "\nnonsense: 1\n")


def test_unknown_constraint_key_is_refused() -> None:
    text = VALID.replace('spec: {min_rows: 1}', 'spec: {min_rows: 1}, extra: 2')
    with pytest.raises(ContractParseError, match="unknown keys"):
        parse_contract(text)


def test_missing_deliverables_is_refused() -> None:
    with pytest.raises(ContractParseError, match=r"\$\.deliverables"):
        parse_contract("task_id: t\ngoal: g\n")


def test_empty_task_id_is_refused() -> None:
    with pytest.raises(ContractParseError, match=r"\$\.task_id"):
        parse_contract('task_id: ""\ngoal: g\ndeliverables: [{name: a}]\n')


def test_unknown_limit_key_is_refused() -> None:
    text = VALID.replace("limits: {max_rounds: 3}", "limits: {max_rounds: 3, nonsense: 1}")
    with pytest.raises(ContractParseError, match="unknown limit keys"):
        parse_contract(text)


def test_yaml_syntax_error_reports_position() -> None:
    with pytest.raises(ContractParseError) as error:
        parse_contract("task_id: [oops\n", source="task.yaml")
    assert "task.yaml:" in str(error.value) and "YAML parse error" in str(error.value)


def test_parse_file_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "task.yaml"
    path.write_text(VALID, encoding="utf-8")
    assert parse_contract_file(path).contract.task_id == "task-1"
    with pytest.raises(ContractParseError, match="cannot read"):
        parse_contract_file(tmp_path / "missing.yaml")


# --------------------------------------------------------------------------- #
# compiling per plan
# --------------------------------------------------------------------------- #


def test_compile_bundle_adds_a_decidable_constraint_per_block() -> None:
    """Without this, core/split.py::verifiable would refuse the plan."""
    from fakes import plan, subtask

    document = parse_contract(VALID)
    split_plan = plan(
        subtasks=(
            subtask("T-01", output="out.csv"),
            subtask("T-02", output="nice.csv"),
        ),
        interfaces={"out.csv": "s", "nice.csv": "s"},
    )
    bundle = compile_bundle(document, split_plan)
    assert [c.id for c in bundle.hard] == ["C-1", "AUTO-T-01", "AUTO-T-02"]
    assert bundle.version == 2


def test_a_declared_per_block_constraint_suppresses_the_auto_one() -> None:
    from fakes import plan, subtask

    document = parse_contract(VALID)
    split_plan = plan(subtasks=(subtask("T-01", output="out.csv"),), interfaces={"out.csv": "s"})
    bundle = compile_bundle(document, split_plan)
    declared = compile_bundle(
        parse_contract(VALID.replace('target: "*"', "target: T-01")), split_plan
    )
    assert any(c.id == "AUTO-T-01" for c in bundle.hard)
    assert not any(c.id == "AUTO-T-01" for c in declared.hard)


def test_compile_bundle_carries_declarations_and_limits() -> None:
    from fakes import plan, subtask

    document = parse_contract(VALID)
    split_plan = plan(subtasks=(subtask("T-01", output="out.csv"),), interfaces={"out.csv": "s"})
    bundle = compile_bundle(document, split_plan)
    assert bundle.blind == ("unknown_column",)
    assert bundle.unmapped_fields == frozenset({"mystery"})
    assert bundle.limits.max_rounds == 3


# --------------------------------------------------------------------------- #
# ★ #32：源载体形式的声明（encoding / eol）
# --------------------------------------------------------------------------- #


def test_a_source_may_declare_its_carrier_form() -> None:
    document = parse_contract(
        VALID.replace(
            "  - {ref: src-1, locator: https://example.invalid/1}",
            "  - {ref: src-1, locator: https://example.invalid/1, encoding: gbk, eol: lf}",
        )
    )
    source = document.contract.sources[0]
    assert (source.encoding, source.eol) == ("gbk", "lf")


def test_a_source_without_a_declared_form_stays_undeclared() -> None:
    """★ 没写就是没写（空串）—— 壳按 utf-8 猜并把假设说出来，不许悄悄默认。"""
    document = parse_contract(VALID)
    source = document.contract.sources[0]
    assert (source.encoding, source.eol) == ("", "")


def test_an_unknown_encoding_in_the_declaration_is_refused() -> None:
    with pytest.raises(ContractParseError, match="unknown encoding"):
        parse_contract(
            VALID.replace(
                "  - {ref: src-1, locator: https://example.invalid/1}",
                "  - {ref: src-1, locator: https://example.invalid/1, encoding: not-a-codec}",
            )
        )


# --------------------------------------------------------------------------- #
# ★ 层次切分：deliverables[].depends_on（产物依赖产物）
# --------------------------------------------------------------------------- #

LAYERED = """
task_id: task-1
goal: a chain
deliverables:
  - {name: a.csv}
  - {name: b.csv, depends_on: [a.csv, a.csv]}
  - {name: c.csv, depends_on: []}
"""


def test_depends_on_is_parsed_and_de_duplicated() -> None:
    document = parse_contract(LAYERED)
    assert document.contract.deliverables[0].depends_on == ()
    assert document.contract.deliverables[1].depends_on == ("a.csv",)
    assert document.contract.deliverables[2].depends_on == ()


def test_depends_on_refuses_a_name_that_is_not_a_deliverable() -> None:
    with pytest.raises(ContractParseError, match="not a declared deliverable"):
        parse_contract(LAYERED.replace("[a.csv, a.csv]", "[ghost.csv]"))


def test_depends_on_refuses_a_self_dependency() -> None:
    with pytest.raises(ContractParseError, match="depends on itself"):
        parse_contract(LAYERED.replace("[a.csv, a.csv]", "[b.csv]"))


def test_depends_on_must_be_a_list_of_non_empty_strings() -> None:
    with pytest.raises(ContractParseError, match=r"\$\.deliverables\[1\]\.depends_on"):
        parse_contract(LAYERED.replace("[a.csv, a.csv]", "a.csv"))
    with pytest.raises(ContractParseError, match="non-empty strings"):
        parse_contract(LAYERED.replace("[a.csv, a.csv]", '[""]'))


def test_a_contract_without_depends_on_stays_empty() -> None:
    document = parse_contract(VALID)
    assert all(deliverable.depends_on == () for deliverable in document.contract.deliverables)
