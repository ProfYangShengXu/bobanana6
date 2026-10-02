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

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "bobanana6" / "core"

#: Packages whose modules can be reached by a bare ``<package>/<module>.py`` path.
#: ``core`` is the default (``SRC``); the rest live beside it.
SOURCE_PACKAGES = (
    "core",
    "agentloop",
    "ontology",
    "contract",
    "decompose",
    "sources",
    "adapters",
    "services",
    "assemble_impl",
    "report",
)


def clear_bytecode_cache() -> None:
    """Drop stale bytecode so a mutated source cannot be served from a .pyc.

    Same-second edits can leave a cache entry with an unchanged mtime/size
    stamp, which would make the mutation invisible to the test subprocess.
    """
    shutil.rmtree(REPO / "tests" / "__pycache__", ignore_errors=True)
    for package in SOURCE_PACKAGES:
        shutil.rmtree(REPO / "src" / "bobanana6" / package / "__pycache__", ignore_errors=True)


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
        "M36-budget-unit-validation-off",
        "model.py",
        "            self.max_output_unit in UNIT_SYMBOLS,",
        "            True,",
        "tests/test_model.py::test_budget_rejects_a_ruler_outside_the_whitelist",
        "a budget may carry a ruler outside the whitelist",
    ),
    Mutation(
        "M37-known-kinds-drift",
        "model.py",
        '        "custom",\n    }\n)',
        '        "customz",\n    }\n)',
        "tests/test_arch_guard.py::test_known_kinds_and_dispatch_table_agree",
        "KNOWN_KINDS silently disagrees with the dispatch table",
    ),
    Mutation(
        "M38-ruler-mixed-not-detected",
        "consist.py",
        "        if len(rulers) > 1:\n            violations.append(",
        "        if False:\n            violations.append(",
        "tests/test_consist.py::test_ruler_mixed_rejects_two_rulers_in_one_dimension",
        "mixed rulers in one cost dimension pass",
    ),
    Mutation(
        "M39-ruler-vs-limits-not-compared",
        "consist.py",
        "    if rulers[0] != budget.max_output_unit:",
        "    if False:",
        "tests/test_consist.py::test_ruler_vs_limits_rejects_a_ruler_the_budget_does_not_use",
        "a declared ruler differing from the budget ruler is not reported",
    ),
    Mutation(
        "M40-required-deliverable-not-enforced",
        "assemble.py",
        "    for deliverable in sorted(contract.required_deliverable_names):",
        "    for deliverable in ():",
        "tests/test_assemble.py::test_required_deliverable_missing_is_still_a_failure",
        "a required deliverable may be missing without failing assembly",
    ),
    Mutation(
        "M41-unit-default-outside-whitelist",
        "model.py",
        '    max_output_unit: str = "tokens"  # core:consist —— ruler_vs_limits 读它；必须 ∈ UNIT_SYMBOLS',
        '    max_output_unit: str = "widgets"  # core:consist —— ruler_vs_limits 读它；必须 ∈ UNIT_SYMBOLS',
        "tests/test_arch_guard.py::test_every_unit_field_uses_a_whitelisted_symbol",
        "a *_unit field defaults to a symbol outside the whitelist",
    ),
    Mutation(
        "M42-ruler-vs-limits-fails-open",
        "consist.py",
        '    if budget is None:\n        return [\n            ConsistencyViolation(\n                "ruler_vs_limits",',
        '    if False:\n        return [\n            ConsistencyViolation(\n                "ruler_vs_limits",',
        "tests/test_consist.py::test_ruler_vs_limits_fails_closed_without_a_budget",
        "a missing budget lets the output ruler go unchecked",
    ),
    Mutation(
        "M43-unit-whitelist-widened",
        "model.py",
        '        "tokens",  # model-output ruler\n        "bytes",  # body-size ruler only; never mixed with the model-output ruler',
        '        "tokens",  # model-output ruler\n        "bytes",  # body-size ruler only; never mixed with the model-output ruler\n        "chars",  # mutated: was removed because nothing reads it',
        "tests/test_model.py::test_every_declared_unit_symbol_is_whitelisted",
        "a symbol with no reader sneaks back into the whitelist",
    ),
    # -- agentloop steps 15-18 --------------------------------------------- #
    Mutation(
        "AL1-429-overflow-priority-lost",
        "agentloop/errors.py",
        "        # Overflow disguised as throttling must not become RETRY_LOCAL.\n        if _is_overflow(message):",
        "        # Overflow disguised as throttling must not become RETRY_LOCAL.\n        if False:",
        "tests/test_agentloop_errors.py::test_429_context_length_becomes_reshard_not_retry",
        "a 429 context-length error is retried instead of resharded",
    ),
    Mutation(
        "AL2-413-ruler-conflated",
        "agentloop/errors.py",
        "    if status == 413:\n        return FailoverReason.PAYLOAD_TOO_LARGE  # byte ruler, before generic 4xx",
        "    if status == 413:\n        return FailoverReason.CONTEXT_OVERFLOW  # byte ruler, before generic 4xx",
        "tests/test_agentloop_errors.py::test_413_is_a_byte_problem_even_with_overflow_wording",
        "413 collapses into the token ruler",
    ),
    Mutation(
        "AL3-validation-after-overflow",
        "agentloop/errors.py",
        "        if _has(message, _VALIDATION_SIGNALS):\n            return FailoverReason.FORMAT_ERROR",
        "        if False:\n            return FailoverReason.FORMAT_ERROR",
        "tests/test_agentloop_errors.py::test_400_validation_precedes_overflow",
        "an unsupported max_tokens parameter is misread as overflow",
    ),
    Mutation(
        "AL4-event-seq-not-owned-by-the-log",
        "agentloop/events.py",
        "        if ev.seq != len(self._events) + 1:",
        "        if False:",
        "tests/test_agentloop_events.py::test_log_refuses_an_event_with_a_foreign_seq",
        "a caller can smuggle in its own seq",
    ),
    Mutation(
        "AL5-resume-collapses-never-started",
        "agentloop/state.py",
        "        return Resume(start_turn=0, completed_actions=(), run=True)",
        "        return Resume(start_turn=checkpoint.turn, completed_actions=checkpoint.done_actions, run=True)",
        "tests/test_agentloop_state.py::test_resume_distinguishes_failed_from_never_started",
        "never_started becomes indistinguishable from failed",
    ),
    Mutation(
        "AL6-recovery-folded-into-success",
        "agentloop/metrics.py",
        '    recovered_calls = sum(1 for event in results if event.ok and event.detail.get("recovered") is True)',
        "    recovered_calls = ok_calls",
        "tests/test_agentloop_metrics.py::test_failure_recovery_is_its_own_number_not_folded_into_success",
        "recovered calls are counted as first-try wins",
    ),
    Mutation(
        "AL7-refusal-pollutes-answer-accuracy",
        "agentloop/metrics.py",
        "    correct = sum(1 for row in rows if row.answered and row.correct)",
        "    correct = sum(1 for row in rows if row.correct)",
        "tests/test_agentloop_metrics.py::test_delivery_refusal_is_scored_on_its_own_denominator",
        "declined outcomes leak into the answered accuracy",
    ),
    Mutation(
        "AL8-unknown-may-carry-a-blame",
        "agentloop/attribution.py",
        '        if (self.blame is None) != (self.confidence == "unknown"):',
        "        if False:",
        "tests/test_agentloop_attribution.py::test_unknown_and_a_blame_are_mutually_exclusive",
        "a hard guess is allowed alongside confidence=unknown",
    ),
    Mutation(
        "AL9-blame-without-evidence",
        "agentloop/attribution.py",
        "        if self.blame is not None and not self.evidence:",
        "        if False:",
        "tests/test_agentloop_attribution.py::test_a_blamed_attribution_must_carry_evidence",
        "a conclusion without evidence is accepted",
    ),
    Mutation(
        "AL10-experiment-branch-lost",
        "agentloop/attribution.py",
        "    if experiment == Experiment.HUMAN_CANNOT_ANSWER:",
        "    if False:",
        "tests/test_agentloop_attribution.py::test_experiment_human_cannot_answer_means_context",
        "the decisive experiment cannot blame the context",
    ),
    Mutation(
        "AL11-agentloop-guard-blind-to-absolute-imports",
        "tests/test_arch_guard.py",
        "                if node.module:\n                    found.append(node.module)",
        "                if False:\n                    found.append(node.module)",
        "tests/test_arch_guard.py::test_agentloop_dependency_guard_detects_a_violation",
        "the agentloop dependency scanner cannot see absolute imports",
    ),
    # -- agentloop steps 19-21 --------------------------------------------- #
    Mutation(
        "BB1-throttle-not-proactive",
        "agentloop/concurrency.py",
        "            while self._active.get(key, 0) >= limit:",
        "            while False:",
        "tests/test_agentloop_concurrency.py::test_acquire_is_proactive_not_reactive",
        "over-budget calls are sent instead of held back",
    ),
    Mutation(
        "BB2-single-flight-leader-off",
        "agentloop/concurrency.py",
        "                if future is None:",
        "                if True:",
        "tests/test_agentloop_concurrency.py::test_single_flight_dedupes_and_every_waiter_gets_the_result",
        "every caller fires its own duplicate call",
    ),
    Mutation(
        "BB3-sessions-shared",
        "agentloop/concurrency.py",
        '            return self._sessions.setdefault(worker_id, {})',
        '            return self._sessions.setdefault("shared", {})',
        "tests/test_agentloop_concurrency.py::test_sessions_are_isolated_per_worker",
        "workers share one mutable session",
    ),
    Mutation(
        "BB4-route-default-dropped",
        "agentloop/concurrency.py",
        "        return self.routes.get(key, self.default)",
        "        return self.routes.get(key, key)",
        "tests/test_agentloop_concurrency.py::test_route_table_maps_keys_to_opaque_routes",
        "an unmapped key stops falling back to the default route",
    ),
    Mutation(
        "BB5-sandbox-escape-allowed",
        "agentloop/sandbox.py",
        "        if root not in candidate.parents and candidate != root:",
        "        if False:",
        "tests/test_agentloop_sandbox.py::test_path_escaping_the_workspace_is_refused",
        "a worker can read outside its workspace",
    ),
    Mutation(
        "BB6-and-chain-failure-attributed",
        "agentloop/gate.py",
        '    if operators == {"&&"} and exit_code != 0:',
        "    if False:",
        "tests/test_agentloop_gate.py::test_and_chain_proves_members_only_on_success",
        "a failing && chain is treated as if it proved the command",
    ),
    Mutation(
        "BB7-pipe-hides-nothing",
        "agentloop/gate.py",
        '        return ExitAttribution(None, False, "pipe or || hides the exit status")',
        '        return ExitAttribution(exit_code, True, "pipe or || hides the exit status")',
        "tests/test_agentloop_gate.py::test_pipe_hides_the_exit_status",
        "an exit 0 through a pipe impersonates a verification",
    ),
    Mutation(
        "BB8-semicolon-sequence-ignored",
        "agentloop/gate.py",
        '    sequence_start = max((i + 1 for i, segment in enumerate(segments[:-1]) if segment.following_operator == ";"), default=0)',
        "    sequence_start = 0",
        "tests/test_agentloop_gate.py::test_only_the_last_semicolon_segment_speaks",
        "an earlier ;-separated command is allowed to speak for the turn",
    ),
    Mutation(
        "BB9-can-complete-ignores-scope",
        "agentloop/gate.py",
        '    return gate.status == "passed" and scope_covers(gate.scope, required_scope)',
        '    return gate.status == "passed"',
        "tests/test_agentloop_gate.py::test_only_passed_and_covering_can_complete",
        "a targeted verification authorises a full completion",
    ),
    Mutation(
        "BB10-declined-retries",
        "agentloop/gate.py",
        '    return gate.status in {"failed", "unverified", "stale"}',
        '    return gate.status != "passed"',
        "tests/test_agentloop_gate.py::test_declined_is_not_a_failure_and_does_not_retry",
        "a declined gate is retried like a failure",
    ),
    Mutation(
        "BB11-staleness-ignored",
        "agentloop/gate.py",
        "    if is_stale(evidence, last_edit_at):",
        "    if False:",
        "tests/test_agentloop_gate.py::test_staleness_is_an_explicit_timestamp_comparison",
        "stale evidence passes as fresh",
    ),
    Mutation(
        "BB12-write-not-a-barrier",
        "agentloop/tools.py",
        '        if call.effect == "write":\n            close_run()',
        '        if False:\n            close_run()',
        "tests/test_agentloop_tools.py::test_a_write_is_a_barrier",
        "a write is batched with reads",
    ),
    Mutation(
        "BB13-results-not-reordered",
        "agentloop/tools.py",
        '        ordered.append(by_id.get(call.call_id) or blocked_tool_result(call.call_id, "no result was produced"))',
        "        ordered.append(by_id[call.call_id])",
        "tests/test_agentloop_tools.py::test_results_are_written_back_in_call_order",
        "a call with no result is left dangling",
    ),
    Mutation(
        "BB14-guardrail-never-blocks",
        "agentloop/tools.py",
        "        repeats = self._repeat_failures.get(signature, 0)",
        "        repeats = 0",
        "tests/test_agentloop_tools.py::test_repeated_identical_failure_is_blocked",
        "a repeated identical failure is never blocked",
    ),
    Mutation(
        "BB15-blocked-result-marked-ok",
        "agentloop/tools.py",
        '    return ToolResult(call_id=call_id, ok=False, content=reason or "blocked", blocked=True, error_type=error_type)',
        '    return ToolResult(call_id=call_id, ok=True, content=reason or "blocked", blocked=True, error_type=error_type)',
        "tests/test_agentloop_tools.py::test_blocked_result_is_synthesized_so_no_call_dangles",
        "a refused call reports success",
    ),
    Mutation(
        "BB16-threshold-keeps-reservation",
        "agentloop/compress.py",
        "    return int((policy.context_length - policy.max_tokens) * policy.pct)",
        "    return int(policy.context_length * policy.pct)",
        "tests/test_agentloop_compress.py::test_threshold_subtracts_the_output_reservation_first",
        "the output reservation is not subtracted from the threshold",
    ),
    Mutation(
        "BB17-truncation-not-retained",
        "agentloop/compress.py",
        "            retained.append(message)",
        "            pass",
        "tests/test_agentloop_compress.py::test_truncate_caps_tool_results_and_keeps_the_original_for_recovery",
        "truncated content can no longer be recovered",
    ),
    Mutation(
        "BB18-orphan-tool-call-kept",
        "agentloop/compress.py",
        '                    if isinstance(call, Mapping) and str(call.get("id", "")) not in declared',
        "                    if isinstance(call, Mapping)",
        "tests/test_agentloop_compress.py::test_sanitize_strips_an_unanswered_tool_call",
        "an unanswered tool call dangles into the next request",
    ),
    Mutation(
        "BB19-non-shrinking-committed",
        "agentloop/compress.py",
        "    if dropped and tokens_after >= tokens_before:",
        "    if False:",
        "tests/test_agentloop_compress.py::test_a_non_shrinking_compaction_is_refused",
        "a compression that grew is committed anyway",
    ),
    Mutation(
        "BB20-recoverable-always-true",
        "agentloop/compress.py",
        "    return all(digest(message) in available for message in original)",
        "    return True",
        "tests/test_agentloop_compress.py::test_recoverable_is_content_based",
        "lost content is reported as recoverable",
    ),
    Mutation(
        "BB21-decline-not-honoured",
        "agentloop/recovery.py",
        "    if gate is not None and gate.is_declined:",
        "    if False:",
        "tests/test_agentloop_recovery.py::test_a_declined_gate_declines_and_never_retries",
        "a decline is treated as a retryable failure",
    ),
    Mutation(
        "BB22-unverifiable-split-retried",
        "agentloop/recovery.py",
        "    if not block_verifiable:",
        "    if False:",
        "tests/test_agentloop_recovery.py::test_an_unverifiable_split_degrades_instead_of_retrying",
        "an unverifiable split keeps burning the budget",
    ),
    Mutation(
        "BB23-taint-outranked",
        "agentloop/recovery.py",
        "    if hint is not None:",
        "    if False:",
        "tests/test_agentloop_recovery.py::test_taint_outranks_a_generic_retry",
        "a tainted tool gets a generic retry instead of a switch hint",
    ),
    Mutation(
        "BB24-taint-never-resets",
        "agentloop/recovery.py",
        "            self._streaks.pop(tool, None)",
        "            pass",
        "tests/test_agentloop_recovery.py::test_a_success_resets_the_streak",
        "a success no longer clears the failure streak",
    ),
    Mutation(
        "BB25-restart-cap-ignored",
        "agentloop/loop.py",
        "        if turn.state.restarts >= self._config.max_restarts:",
        "        if False:",
        "tests/test_agentloop_loop.py::test_a_restart_is_capped",
        "restarts refund the turn budget forever",
    ),
    Mutation(
        "BB26-transcript-tail-not-closed",
        "agentloop/loop.py",
        '    rows.append({"role": "assistant", "content": candidate.hint, "candidate": True})',
        "    pass",
        "tests/test_agentloop_loop.py::test_close_transcript_tail_appends_a_matching_row",
        "a delivered candidate leaves no matching transcript row",
    ),
    Mutation(
        "BB27-budget-fallback-makes-a-call",
        "agentloop/loop.py",
        "    if withheld is not None:\n        return withheld",
        "    if False:\n        return withheld",
        "tests/test_agentloop_loop.py::test_resolve_budget_fallback_prefers_the_withheld_candidate",
        "the withheld candidate is discarded for another fallible call",
    ),
    Mutation(
        "BB28-assemble-tail-first",
        "agentloop/loop.py",
        "    return (*stable_prefix, *dynamic_tail)",
        "    return (*dynamic_tail, *stable_prefix)",
        "tests/test_agentloop_loop.py::test_assemble_messages_keeps_the_stable_prefix_in_front",
        "dynamic content is placed before the stable prefix",
    ),
    Mutation(
        "BB29-overlap-disabled",
        "agentloop/loop.py",
        "    return matched if matched >= min_overlap else 0",
        "    return 0",
        "tests/test_agentloop_loop.py::test_continuation_overlap_dedupes_a_stream_seam",
        "a truncated stream seam is duplicated",
    ),
    Mutation(
        "BB30-guarded-swallows-errors",
        "agentloop/loop.py",
        '        errors.append(f"{label}: {type(exc).__name__}: {exc}")',
        "        pass",
        "tests/test_agentloop_loop.py::test_errors_are_collected_not_swallowed",
        "a failed cleanup step leaves no trace",
    ),
    Mutation(
        "BB31-failed-turn-not-persisted",
        "agentloop/loop.py",
        '        # the loop is about to retry it.\n        self._persist(turn, status="failed")',
        "        # the loop is about to retry it.\n        pass",
        "tests/test_agentloop_loop.py::test_a_failed_turn_is_persisted_before_the_retry",
        "a failed turn is not written to the checkpoint",
    ),
    Mutation(
        "BB32-throttle-not-called",
        "agentloop/loop.py",
        "        if not self._gate.acquire(route):",
        "        if False:",
        "tests/test_agentloop_loop.py::test_the_loop_throttles_before_the_model_call",
        "the loop bypasses the proactive throttle",
    ),
    Mutation(
        "BB33-route-table-bypassed",
        "agentloop/loop.py",
        '        route = self._routes.route(turn.route_key) if self._routes is not None else "default"',
        "        route = turn.route_key",
        "tests/test_agentloop_loop.py::test_the_loop_routes_through_the_route_table",
        "the loop picks the model itself instead of routing",
    ),
    Mutation(
        "BB34-single-flight-bypassed",
        "agentloop/loop.py",
        "            return self._gate.single_flight(request_key)(lambda: self._io.produce(turn.state))",
        "            return self._io.produce(turn.state)",
        "tests/test_agentloop_loop.py::test_the_loop_dedupes_identical_in_flight_requests",
        "a duplicate in-flight request fires a second model call",
    ),
    Mutation(
        "BB35-done-actions-still-empty",
        "agentloop/loop.py",
        "            done_actions=turn.state.done_actions,",
        "            done_actions=(),",
        "tests/test_agentloop_loop.py::test_completed_actions_are_persisted_and_not_replayed",
        "a resumed run cannot tell which actions already ran",
    ),
    Mutation(
        "BB36-throttle-recorded-as-failed",
        "agentloop/loop.py",
        '                self._persist(turn, status="never_started" if turn.throttled else "failed")',
        '                self._persist(turn, status="failed")',
        "tests/test_agentloop_loop.py::test_the_loop_throttles_before_the_model_call",
        "a turn that never started is recorded as a failure",
    ),
    # -- steps 8-10: ontology/ contract/ decompose/ ------------------------ #
    Mutation(
        "L01-empty-unmapped-reason-accepted",
        "contract/parser.py",
        '        if not isinstance(raw.get("reason"), str) or not raw["reason"].strip():',
        "        if False:",
        "tests/test_contract_parser.py::test_empty_unmapped_reason_is_refused",
        "an empty unmapped reason is no longer refused (core does not check it either)",
    ),
    Mutation(
        "L02-whitespace-rationale-accepted",
        "contract/parser.py",
        '        if not isinstance(raw.get("rationale"), str) or not raw["rationale"].strip():',
        '        if not isinstance(raw.get("rationale"), str) or not raw["rationale"]:',
        "tests/test_contract_parser.py::test_whitespace_only_rationale_is_refused",
        "a whitespace-only rationale slips past the non-empty rule",
    ),
    Mutation(
        "L03-auto-constraint-never-synthesized",
        "contract/parser.py",
        "        if subtask.id in covered or subtask.output in covered:",
        "        if True:",
        "tests/test_decompose.py::test_plan_from_strategy_passes_core_split",
        "blocks stop being decidable, so split.verifiable would refuse the plan",
    ),
    Mutation(
        "L04-consumer-suppression-ignored",
        "contract/parser.py",
        "        if subtask.id in covered or subtask.output in covered:",
        "        if False:",
        "tests/test_contract_parser.py::test_a_declared_per_block_constraint_suppresses_the_auto_one",
        "an AUTO constraint is added even for a block that is already covered",
    ),
    Mutation(
        "L05-schema-validation-disabled",
        "ontology/loader.py",
        "    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.absolute_path))\n"
        "    if not errors:\n"
        "        return",
        "    return",
        "tests/test_ontology_loader.py::test_wildcard_version_is_rejected",
        "the JSON Schema stop being consulted at all",
    ),
    Mutation(
        "L06-dangling-unit-dropped",
        "ontology/loader.py",
        "        if unit_name is not None and unit_name not in symbols:",
        "        if False:",
        "tests/test_ontology_loader.py::test_dangling_unit_reference_is_refused_not_dropped",
        "an unresolvable unit name vanishes instead of failing the load",
    ),
    Mutation(
        "L07-concept-domain-default-lost",
        "ontology/loader.py",
        "    concepts = _concepts(data, domain)",
        '    concepts = _concepts(data, "")',
        "tests/test_ontology_loader.py::test_concept_domain_defaults_to_the_file_domain",
        "per-concept domain no longer inherits the file domain",
    ),
    Mutation(
        "L08-unmapped-field-dropped-by-strategy",
        "decompose/strategies.py",
        "    return tuple(map_field(name, ontology) or name for name in sorted(set(fields)))",
        "    return tuple(mapped for mapped in (map_field(name, ontology) for name in sorted(set(fields))) if mapped)",
        "tests/test_decompose.py::test_an_unmapped_deliverable_field_survives_as_its_raw_name",
        "a field the ontology cannot map is silently dropped, so concept_mapped never fires",
    ),
    Mutation(
        "L09-merge-writers-share-a-partition",
        "decompose/strategies.py",
        '                inputs=tuple(parts),\n'
        "                output=deliverable.name,\n"
        "                contract_ref=contract.task_id,\n"
        "                axis=axis,\n"
        '                mode="write",\n'
        '                partition=f"deliverable:{deliverable.name}",',
        '                inputs=tuple(parts),\n'
        "                output=deliverable.name,\n"
        "                contract_ref=contract.task_id,\n"
        "                axis=axis,\n"
        '                mode="write",\n'
        '                partition="deliverable:all",',
        "tests/test_decompose.py::test_two_same_layer_writers_get_distinct_partitions",
        "two same-layer writers collide on one partition (split.write_serial)",
    ),
    Mutation(
        "L10-single-write-loses-its-partition",
        "decompose/strategies.py",
        "                inputs=(),\n"
        "                output=deliverable.name,\n"
        "                contract_ref=contract.task_id,\n"
        '                axis="source",\n'
        '                mode="write",\n'
        '                partition=f"deliverable:{deliverable.name}",',
        "                inputs=(),\n"
        "                output=deliverable.name,\n"
        "                contract_ref=contract.task_id,\n"
        '                axis="source",\n'
        '                mode="write",\n'
        "                partition=None,",
        "tests/test_decompose.py::test_single_strategy_produces_one_write_per_deliverable",
        "a write block declares no partition, so write_serial cannot be satisfied",
    ),
    Mutation(
        "L11-refine-round-never-advances",
        "decompose/strategies.py",
        "            round=plan.round + 1,",
        "            round=plan.round,",
        "tests/test_decompose.py::test_refine_advances_when_the_budget_allows",
        "a refinement round stops being countable, defeating round_budget",
    ),
    Mutation(
        "L12-refine-ceiling-ignored",
        "decompose/budget.py",
        "    return budget.allows(plan.round + 1)",
        "    return True",
        "tests/test_decompose.py::test_refine_is_capped_by_the_round_budget",
        "the round ceiling stops limiting refinement -- an unbounded loop",
    ),
    Mutation(
        "L13-mid-layer-imports-the-volatile-side",
        "contract/parser.py",
        "from ..core.model import (",
        "from ..adapters import glm  # noqa: F401\nfrom ..core.model import (",
        "tests/test_arch_guard.py::test_mid_layer_packages_do_not_reach_the_volatile_side",
        "contract/ reaches for adapters/, which only the assembly point may do",
    ),
    Mutation(
        "L14-core-imports-the-mid-layer",
        "core/split.py",
        "from .model import (",
        "from ..ontology import loader  # noqa: F401\nfrom .model import (",
        "tests/test_arch_guard.py::test_core_does_not_import_the_mid_layer",
        "an arrow points from core/ back at the mid layer (AGENTS.md 1.2)",
    ),
    # -- steps 11-14: sources/ adapters/ services/ assemble_impl/ report/ cli --
    Mutation(
        "W01-block-bundle-not-projected",
        "assemble_impl/runtime.py",
        "            bundle=block_bundle(bundle, subtask),",
        "            bundle=bundle,",
        "tests/test_runner.py::test_two_source_task_runs_the_pipeline_and_the_merge_block",
        "each block is judged against the whole plan's bundle, so neighbours' constraints fail it",
    ),
    Mutation(
        "W02-decline-not-recognised",
        "assemble_impl/produce.py",
        "        if _declined(response.text):",
        "        if False:",
        "tests/test_runner.py::test_a_declined_block_is_reported_as_declined_not_failed",
        "the third legal exit loses its producer and declines look like failures",
    ),
    Mutation(
        "W03-source-id-is-the-block",
        "assemble_impl/produce.py",
        '        source_id=str(document.get("source_id") or document.get("ref") or source_id),',
        "        source_id=source_id,",
        "tests/test_runner.py::test_parse_candidate_reads_rows_columns_and_provenance",
        "the asserting source is replaced by the block id, breaking independence checks",
    ),
    Mutation(
        "W04-empty-snapshot-frozen",
        "sources/store.py",
        "        if not body:",
        "        if False:",
        "tests/test_sources.py::test_empty_body_is_refused_not_frozen",
        "an empty fetch is frozen, so 'no data' reads as 'the data is empty'",
    ),
    Mutation(
        "W05-content-addressing-keyed-by-ref",
        "sources/store.py",
        "        path = self.path_for(body, suffix=suffix)",
        "        path = self.path_for(body + ref.encode(), suffix=suffix)",
        "tests/test_sources.py::test_same_bytes_twice_produce_one_file",
        "the same bytes fetched twice produce two snapshots",
    ),
    Mutation(
        "W06-fetch-failure-swallowed",
        "assemble_impl/runner.py",
        '                raise RunRefused("sources", f"source {source.ref!r} could not be fetched: {exc}", ("fetch_failed",)) from exc',
        "                continue  # mutation: the failure is swallowed",
        "tests/test_runner.py::test_a_source_that_cannot_be_fetched_refuses_the_run",
        "a failed fetch no longer stops the run before the cluster starts",
    ),
    Mutation(
        "W07-ontology-gate-skipped",
        "assemble_impl/runner.py",
        "        if ontology_codes:",
        "        if False:",
        "tests/test_runner.py::test_an_inconsistent_ontology_refuses_the_run",
        "workers start on semantics the kernel already refuted",
    ),
    Mutation(
        "W08-split-gate-skipped",
        "assemble_impl/runner.py",
        "        if not split_report.ok:",
        "        if False:",
        "tests/test_runner.py::test_an_illegal_plan_refuses_the_run",
        "an illegal plan is shipped to the cluster",
    ),
    Mutation(
        "W09-inventory-empty",
        "assemble_impl/runner.py",
        "            available_capabilities=TOOL_NAMES,",
        "            available_capabilities=(),",
        "tests/test_runner.py::test_the_tool_inventory_is_what_the_consistency_check_compares_against",
        "the stated capability inventory stops matching the tool surface",
    ),
    Mutation(
        "W10-registry-effect-not-filled",
        "services/registry.py",
        "                    effect=self.effect(name),",
        '                    effect="read",',
        "tests/test_services.py::test_write_calls_become_a_barrier_because_the_registry_says_so",
        "the caller's claim about a write call is trusted again",
    ),
    Mutation(
        "W11-upstream-permission-dropped",
        "services/tools.py",
        "        if name not in self.allowed_inputs:",
        "        if False:",
        "tests/test_services.py::test_upstream_refuses_an_input_the_block_never_declared",
        "a block can read an input it never declared",
    ),
    Mutation(
        "W12-snapshot-ambiguity-guessed",
        "services/tools.py",
        "        if len(self.authorized) == 1:",
        "        if self.authorized:",
        "tests/test_services.py::test_snapshot_refuses_to_guess_between_two_sources",
        "the registry picks a source instead of refusing an ambiguous call",
    ),
    Mutation(
        "W13-evidence-ignored-by-the-gate",
        "services/checks.py",
        "        if recorded is None:",
        "        if True:",
        "tests/test_services.py::test_verify_uses_the_recorded_command_evidence",
        "the gate stops reading the recorded evidence",
    ),
    Mutation(
        "W14-adapters-import-a-judgment-module",
        "adapters/offline.py",
        "from ..core.model import ModelResponse",
        "from ..core.kernel import admit  # mutation\nfrom ..core.model import ModelResponse",
        "tests/test_arch_guard.py::test_adapters_reach_the_kernel_only_through_ports_and_data",
        "the adapter layer starts deciding",
    ),
    Mutation(
        "W15-second-assembly-point",
        "services/checks.py",
        '        command = constraint.spec.get("command")',
        "        _offline = EchoToolResultClient()\n        command = constraint.spec.get(\"command\")",
        "tests/test_arch_guard.py::test_only_the_cli_constructs_boundary_implementations",
        "a second module new-s up a boundary implementation",
    ),
    Mutation(
        "W19-second-assembly-point-unlisted-name",
        "services/checks.py",
        '        command = constraint.spec.get("command")',
        '        _store = SnapshotStore(root=Path("/tmp"))\n        command = constraint.spec.get("command")',
        "tests/test_arch_guard.py::test_only_the_cli_constructs_boundary_implementations",
        "a class the hand-written list never names still escapes the assembly-point rule",
    ),
    Mutation(
        "W16-summary-drops-the-contract-notes",
        "report/builders.py",
        '            "notes": contract.notes,',
        '            "notes": "",',
        "tests/test_report.py::test_summary_reads_notes_recon_evidence_findings_and_coverage",
        "TaskContract.notes loses its only reader",
    ),
    Mutation(
        "W17-ontology-map-drops-cardinality",
        "report/builders.py",
        '            lines.append(f"| `{relation.src}` | {relation.kind} | `{relation.dst}` | {relation.cardinality} |")',
        '            lines.append(f"| `{relation.src}` | {relation.kind} | `{relation.dst}` |  |")',
        "tests/test_report.py::test_ontology_map_renders_relations_with_kind_and_cardinality",
        "Relation.cardinality loses its only reader",
    ),
    Mutation(
        "W18-summarize-not-passed-to-compact",
        "agentloop/loop.py",
        "        result = compact(turn.state.messages, self._compression, summarize=self._summarize)",
        "        result = compact(turn.state.messages, self._compression)",
        "tests/test_agentloop_loop.py::test_the_compression_callback_is_the_injected_summarizer",
        "compression can only drop messages again; the callback is never reached",
    ),
    Mutation(
        "L16-decomposer-ignores-the-field-strategy",
        "decompose/strategies.py",
        '        if self._strategy == "field":\n'
        "            return split_by_field(contract, ontology, round=round, version=version)",
        "        if False:\n"
        "            return split_by_field(contract, ontology, round=round, version=version)",
        "tests/test_decompose.py::test_decomposer_dispatches_every_declared_strategy",
        "strategy='field' silently falls through to the single-block builder",
    ),
    Mutation(
        "L15-guard-blind-to-relative-imports",
        "tests/test_arch_guard.py",
        '                found.append(f"{base}.{node.module}" if node.module else base)',
        "                found.append(base)",
        "tests/test_arch_guard.py::test_mid_layer_dependency_guard_detects_a_violation",
        "the dependency scanner can no longer resolve a relative import",
    ),
)


def run_selector(selector: str) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", selector, "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    failing = tuple(
        sorted(
            set(re.findall(r"^(?:FAILED|ERROR) (\S+)", result.stdout, flags=re.MULTILINE))
        )
    )
    caught = result.returncode != 0
    if not caught:
        return False, "none"
    # A non-zero exit is not proof by itself: a misspelled selector also exits
    # non-zero. Only a test that actually went red counts as a catch.
    if not failing:
        return False, "SELECTOR ERROR -- no test ran, the mutation proves nothing"
    return True, ", ".join(failing)


def main() -> int:
    failures: list[str] = []
    print(f"{'mutation':44} {'caught':7} red tests")
    print("-" * 120)
    for mutation in MUTATIONS:
        relative = mutation.relative_path
        if relative.startswith("tests/"):
            path = (REPO / relative).resolve()
        elif relative.split("/")[0] in SOURCE_PACKAGES:
            path = (REPO / "src" / "bobanana6" / relative).resolve()
        else:
            path = (SRC / relative).resolve()
        if not path.is_file():
            failures.append(f"{mutation.ident}: no such file {relative}")
            print(f"{mutation.ident:44} ERROR   no such file {relative}")
            continue
        # Keep the exact original bytes: text-mode read normalizes CRLF to LF,
        # and writing that back silently rewrites every line ending in the file.
        original_bytes = path.read_bytes()
        original = original_bytes.decode("utf-8").replace("\r\n", "\n")
        if original.count(mutation.old) != 1:
            failures.append(f"{mutation.ident}: anchor not found exactly once ({original.count(mutation.old)})")
            print(f"{mutation.ident:44} ERROR   anchor not found exactly once")
            continue
        try:
            clear_bytecode_cache()
            path.write_text(original.replace(mutation.old, mutation.new), encoding="utf-8", newline="\n")
            caught, red = run_selector(mutation.selector)
        finally:
            path.write_bytes(original_bytes)
            clear_bytecode_cache()
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
