#!/usr/bin/env python
"""Reverse validation: break the logic on purpose and prove the tests go red.

An assertion that cannot fail is not a test. This script applies one targeted
mutation at a time to the kernel source, runs the test(s) that are supposed to
catch it in a subprocess, restores the source, and reports whether the mutation
was actually caught.

Run:  .venv/bin/python tools/reverse_validation.py
Exit: 0 when every mutation is caught, 1 otherwise.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "bobanana6" / "core"


@dataclass(frozen=True)
class Mutation:
    ident: str
    relative_path: str
    old: str
    new: str
    selector: str
    note: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M01-divergence-types-merged",
        "model.py",
        "DisagreementTypes: tuple[type, ...] = (BlindSpot, Conflict, DefinitionMismatch)",
        "DisagreementTypes: tuple[type, ...] = (BlindSpot, BlindSpot, DefinitionMismatch)",
        "tests/test_model.py::test_three_divergence_types_have_disjoint_shapes",
        "three divergence types collapse into two",
    ),
    Mutation(
        "M02-validation-removed",
        "model.py",
        "def _require(condition: bool, message: str) -> None:\n    if not condition:\n        raise ValueError(message)",
        "def _require(condition: bool, message: str) -> None:\n    return None",
        "tests/test_model.py",
        "construction-time validation becomes a no-op",
    ),
    Mutation(
        "M03-alias-conflict-ignored",
        "ontology.py",
        "if prior is not None and prior != key:",
        "if False and prior is not None and prior != key:",
        "tests/test_ontology.py::test_duplicate_alias_is_an_ambiguity",
        "ambiguous aliases stop being reported",
    ),
    Mutation(
        "M04-cycle-detection-broken",
        "ontology.py",
        "    sorter.prepare()",
        "    pass  # sorter.prepare()",
        "tests/test_ontology.py::test_parent_cycle_is_reported_with_its_members",
        "the hierarchy is never checked for cycles",
    ),
    Mutation(
        "M05-gap-and-short-not-disjoint",
        "capabilities.py",
        "if not any(capability in w.provides for w in ordered_workers):",
        "if False:",
        "tests/test_capabilities.py::test_no_provider_is_a_gap",
        "a missing provider is reported as both gap and short",
    ),
    Mutation(
        "M06-over-not-reported",
        "capabilities.py",
        "        if surplus:\n            issues.append(",
        "        if False:\n            issues.append(",
        "tests/test_capabilities.py::test_over_qualified_provider_is_reported_as_cost",
        "over-qualified assignment stops being a cost signal",
    ),
    Mutation(
        "M07-short-not-reported",
        "capabilities.py",
        "        if demand > capacity:\n            issues.append(",
        "        if False:\n            issues.append(",
        "tests/test_capabilities.py::test_provider_exists_but_concurrency_is_short",
        "concurrency shortfall stops being reported",
    ),
    Mutation(
        "M08-corroboration-disagreement-fails",
        "checkers/corroboration.py",
        "    if len(distinct_values) <= 1:\n        return ok(",
        "    if True:\n        return ok(",
        "tests/test_checkers.py::test_corroboration_disagreement_is_a_conflict_not_a_failure",
        "a conflicting value stops producing a Conflict",
    ),
    Mutation(
        "M09-definition-pin-as-conflict",
        "checkers/definition_pin.py",
        "            divergences=(mismatch,),",
        "            divergences=(),",
        "tests/test_checkers.py::test_definition_pin_mismatch_is_a_definition_mismatch_not_a_conflict",
        "version drift stops being reported as a divergence",
    ),
    Mutation(
        "M10-custom-exception-swallowed",
        "checkers/custom.py",
        '        return fail(constraint, f"custom check {name!r} raised {type(exc).__name__}: {exc}")',
        '        return ok(constraint, "swallowed")',
        "tests/test_checkers.py::test_custom_exception_is_visible_not_swallowed",
        "a raising checker silently passes",
    ),
    Mutation(
        "M11-unknown-kind-passes",
        "checkers/__init__.py",
        '        return fail(constraint, f"unknown constraint kind {constraint.kind!r}")',
        "        return CheckOutcome(constraint_id=constraint.id, ok=True)",
        "tests/test_checkers.py::test_unknown_kind_fails_closed",
        "an unknown constraint kind passes by default",
    ),
    Mutation(
        "M12-range-tolerance-ignored",
        "checkers/range.py",
        '    tol = constraint.spec.get("tol", context.limits.tol)',
        "    tol = 0.0",
        "tests/test_checkers.py::test_range_tolerance_is_inclusive_on_both_sides",
        "the tolerance window is dropped",
    ),
    Mutation(
        "M13-invariant-allows-attribute-access",
        "checkers/invariant.py",
        '    raise InvariantError(f"expression node {type(node).__name__} is not allowed")',
        "    return None",
        "tests/test_checkers.py::test_invariant_refuses_attribute_access",
        "the safe-expression allow-list leaks attribute access",
    ),
    Mutation(
        "M14-admit-short-circuits",
        "kernel.py",
        "return tuple(check_kind(constraint, candidate, context) for constraint in _ordered(bundle.hard))",
        "return tuple(check_kind(constraint, candidate, context) for constraint in _ordered(bundle.hard)[:1])",
        "tests/test_kernel.py::test_admit_reports_every_failure_not_just_the_first",
        "only the first failure is reported",
    ),
    Mutation(
        "M15-classify-skips-definition-first",
        "kernel.py",
        "        if len(versions) > 1:",
        "        if False:",
        "tests/test_kernel.py::test_classify_definition_is_checked_before_conflict",
        "definition drift is no longer checked before conflict",
    ),
    Mutation(
        "M16-converge-arbitrate-lost",
        "kernel.py",
        "    if not passed:",
        "    if False:",
        "tests/test_kernel.py::test_converge_with_no_candidate_passing_arbitrates",
        "the zero-passing-candidate branch is unreachable",
    ),
    Mutation(
        "M17-converge-identical-lost",
        "kernel.py",
        "    if len({candidate_fingerprint(candidate) for candidate in passed}) == 1:",
        "    if False:",
        "tests/test_kernel.py::test_converge_with_identical_fingerprints_delivers",
        "identical candidates no longer short-circuit to delivery",
    ),
    Mutation(
        "M18-star-not-enforced",
        "split.py",
        '    if plan.shape == "star":',
        "    if False:",
        "tests/test_split.py::test_star_shape_rejects_block_to_block_input",
        "lateral dependencies pass in a star plan",
    ),
    Mutation(
        "M19-round-budget-lost",
        "split.py",
        "    if plan.round > limits.max_rounds:",
        "    if False:",
        "tests/test_split.py::test_round_budget_rejects_the_fourth_round",
        "the iteration ceiling is not enforced",
    ),
    Mutation(
        "M20-write-serial-lost",
        "split.py",
        '            add("write_serial", f"layer {layer} has concurrent writes without partitions", *(s.id for s in writers))',
        "            pass",
        "tests/test_split.py::test_write_serial_rejects_unpartitioned_parallel_writes",
        "unpartitioned parallel writes pass",
    ),
    Mutation(
        "M21-empty-plan-passes",
        "split.py",
        "    if not plan.subtasks:",
        "    if False:",
        "tests/test_split.py::test_empty_plan_is_rejected",
        "a plan with no blocks satisfies every rule vacuously",
    ),
    Mutation(
        "M22-semantics-frozen-lost",
        "split.py",
        "    elif not ontology.is_frozen:",
        "    elif False:",
        "tests/test_split.py::test_semantics_frozen_rejects_unfrozen_ontology",
        "an unfrozen ontology may start workers",
    ),
    Mutation(
        "M23-merge-cheap-fails-open",
        "split.py",
        '        add("merge_cheap", "merge/execute cost estimates are not declared; cheapness cannot be proven")',
        "        pass",
        "tests/test_split.py::test_merge_cheap_fails_closed_when_costs_are_undeclared",
        "undeclared costs silently pass the merge check",
    ),
    Mutation(
        "M24-concept-mapped-lost",
        "split.py",
        "                if map_field(field_name, ontology) is None and field_name not in bundle.unmapped_fields",
        "                if False and field_name not in bundle.unmapped_fields",
        "tests/test_split.py::test_concept_mapped_rejects_unmapped_output_field",
        "an unmapped output field passes",
    ),
    Mutation(
        "M25-cut-completeness-lost",
        "assemble.py",
        "        if output not in pieces:",
        "        if False:",
        "tests/test_assemble.py::test_missing_block_is_named",
        "a missing block is not detected at assembly",
    ),
    Mutation(
        "M26-assembly-provenance-lost",
        "assemble.py",
        "    if unreachable:",
        "    if False:",
        "tests/test_assemble.py::test_broken_provenance_names_the_block",
        "a broken provenance chain passes assembly",
    ),
    Mutation(
        "M27-partition-conflict-lost",
        "assemble.py",
        "        if any(partition is None for partition in partitions) or len(set(partitions)) != len(partitions):",
        "        if False:",
        "tests/test_assemble.py::test_partition_conflict_names_the_writers",
        "same-level writes share a partition unnoticed",
    ),
    Mutation(
        "M28-chain-always-reachable",
        "provenance.py",
        "    return provenance.snapshot_ref in snapshots",
        "    return True",
        "tests/test_provenance.py::test_removing_a_middle_link_is_reported_at_the_break",
        "a missing snapshot is treated as reachable",
    ),
    Mutation(
        "M29-contradiction-lost",
        "consist.py",
        "                if min(high_a, high_b) < max(low_a, low_b):",
        "                if False:",
        "tests/test_consist.py::test_contradiction",
        "mutually exclusive constraints are accepted",
    ),
    Mutation(
        "M30-tool-inventory-fails-open",
        "consist.py",
        '        return [ConsistencyViolation("tool_missing", "available capability inventory was not provided")]',
        "        return []",
        "tests/test_consist.py::test_tool_missing_fails_closed_without_an_inventory",
        "a missing capability inventory silently passes",
    ),
    Mutation(
        "M31-def-version-mix-lost",
        "consist.py",
        "        if len(grouped[concept]) > 1:\n            violations.append(\n                ConsistencyViolation(\n                    \"def_version_mix\",",
        "        if False:\n            violations.append(\n                ConsistencyViolation(\n                    \"def_version_mix\",",
        "tests/test_consist.py::test_def_version_mix",
        "mixed definition versions are accepted",
    ),
    Mutation(
        "M32-unit-conflict-lost",
        "consist.py",
        '        if len(grouped[concept]) > 1:\n            violations.append(\n                ConsistencyViolation(\n                    "unit_conflict",',
        '        if False:\n            violations.append(\n                ConsistencyViolation(\n                    "unit_conflict",',
        "tests/test_consist.py::test_unit_conflict",
        "conflicting units are accepted",
    ),
    Mutation(
        "M33-guard-vocabulary-exemption-too-wide",
        "tests/test_arch_guard.py",
        "for token in MANDATED_IDENTIFIERS:",
        "for token in ():",
        "tests/test_arch_guard.py::test_core_is_environment_free",
        "the guard scanner never fires (mutated into its own scanner)",
    ),
    Mutation(
        "M34-guard-outbound-always-clean",
        "tests/test_arch_guard.py",
        "    outbound: list[str] = []",
        "    return []",
        "tests/test_arch_guard.py::test_guard_detects_outbound_edge",
        "the outbound-edge scanner can never report anything",
    ),
    Mutation(
        "M35-field-reader-marker-removed",
        "model.py",
        "    round: int  # core:split —— round_budget 读它",
        "    round: int",
        "tests/test_arch_guard.py::test_every_field_declares_a_reader",
        "a field loses its declared reader and the guard stays green",
    ),
    Mutation(
        "M36-passthrough-reason-emptied",
        "model.py",
        "    path: str | None = None  # passthrough: 原始载体路径，供报告/边界外读；core 不读",
        "    path: str | None = None  # passthrough:",
        "tests/test_arch_guard.py::test_every_field_declares_a_reader",
        "an empty passthrough marker is accepted",
    ),
    Mutation(
        "M37-mixed-rulers-not-detected",
        "consist.py",
        "        if len(rulers) > 1:",
        "        if False:",
        "tests/test_consist.py::test_ruler_consistency_rejects_mixed_rulers_in_one_dimension",
        "mixed rulers in one cost dimension pass",
    ),
    Mutation(
        "M38-ruler-not-compared-to-limits",
        "consist.py",
        "        if rulers[0] != reference:",
        "        if False:",
        "tests/test_consist.py::test_ruler_consistency_rejects_a_ruler_that_disagrees_with_limits",
        "a ruler differing from limits is not reported",
    ),
)


def run_selector(selector: str) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", selector, "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    caught = result.returncode != 0
    failing = tuple(sorted(set(re.findall(r"^FAILED (\S+)", result.stdout, flags=re.MULTILINE))))
    return caught, ", ".join(failing) if failing else ("ERROR/collection" if caught else "none")


def main() -> int:
    failures: list[str] = []
    print(f"{'mutation':44} {'caught':7} red tests")
    print("-" * 120)
    for mutation in MUTATIONS:
        path = (REPO / mutation.relative_path).resolve() if mutation.relative_path.startswith("tests/") else (SRC / mutation.relative_path).resolve()
        # Keep the exact original bytes: text-mode read normalizes CRLF to LF,
        # and writing that back silently rewrites every line ending in the file.
        original_bytes = path.read_bytes()
        original = original_bytes.decode("utf-8").replace("\r\n", "\n")
        if original.count(mutation.old) != 1:
            failures.append(f"{mutation.ident}: anchor not found exactly once ({original.count(mutation.old)})")
            print(f"{mutation.ident:44} ERROR   anchor not found exactly once")
            continue
        try:
            shutil.rmtree(REPO / "src" / "bobanana6" / "core" / "__pycache__", ignore_errors=True)
            shutil.rmtree(REPO / "tests" / "__pycache__", ignore_errors=True)
            path.write_text(original.replace(mutation.old, mutation.new), encoding="utf-8", newline="\n")
            caught, red = run_selector(mutation.selector)
        finally:
            path.write_bytes(original_bytes)
            shutil.rmtree(REPO / "src" / "bobanana6" / "core" / "__pycache__", ignore_errors=True)
            shutil.rmtree(REPO / "tests" / "__pycache__", ignore_errors=True)
        if caught:
            print(f"{mutation.ident:44} {'yes':7} {red}")
        else:
            print(f"{mutation.ident:44} {'NO':7} {mutation.note}")
            failures.append(f"{mutation.ident}: mutation NOT caught by {mutation.selector}")

    print("-" * 120)
    print(f"mutations: {len(MUTATIONS)}   caught: {len(MUTATIONS) - len(failures)}   not caught: {len(failures)}")
    for failure in failures:
        print(f"  ! {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
