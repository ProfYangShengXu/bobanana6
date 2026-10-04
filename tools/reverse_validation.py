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

import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
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


#: Files currently mutated, with the exact bytes to put back.
#: ★ 进程被 SIGTERM/SIGINT 杀掉时（跑长任务很常见）必须还原，
#: 否则一个变异会留在源码树里，让别的测试**假红** —— 2026-10-03 吃过一次。
_RESTORE: dict[Path, bytes] = {}


def restore_all() -> None:
    """Put every mutated file back exactly as it was."""
    for path, original in list(_RESTORE.items()):
        try:
            path.write_bytes(original)
        finally:
            _RESTORE.pop(path, None)


def _on_signal(signum: int, _frame: object) -> None:
    restore_all()
    clear_bytecode_cache()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)  # die the way we were asked to


def install_restore_hooks() -> None:
    atexit.register(restore_all)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        handler = getattr(signal, name, None)
        if handler is not None:
            signal.signal(handler, _on_signal)


def clear_bytecode_cache() -> None:
    """Drop stale bytecode so a mutated source cannot be served from a .pyc.

    Same-second edits can leave a cache entry with an unchanged mtime/size
    stamp, which would make the mutation invisible to the test subprocess.
    """
    shutil.rmtree(REPO / "tests" / "__pycache__", ignore_errors=True)
    shutil.rmtree(REPO / "acceptance" / "__pycache__", ignore_errors=True)
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
        "        if provenance.snapshot_ref not in snapshots:\n",
        "        if False:  # mutation: every citation looks reachable\n",
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
        "    experiment_blame = _EXPERIMENT_BLAME.get(experiment)\n",
        "    experiment_blame = None  # mutation: the experiment declares nothing\n",
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
    # -- the attack executor: per-row switches, three steps kept apart -- #
    Mutation(
        "T01-per-row-switch-ignored",
        "acceptance/inject/attacks.py",
        '                enabled=bool(row.get("enabled", True)),',
        "                enabled=True,  # mutation: the per-row switch is ignored",
        "tests/test_attacks.py::test_every_row_can_be_switched_off_individually",
        "a disabled attack would still run, so the switch means nothing",
    ),
    Mutation(
        "T02-a-missing-injector-looks-like-a-pass",
        "acceptance/inject/attacks.py",
        '            outcomes.append(AttackOutcome(attack=attack, error="no injector is registered for this attack"))',
        "            outcomes.append(AttackOutcome(attack=attack, observation=Observation(found=True)))",
        "tests/test_attacks.py::test_a_row_with_no_injector_is_a_miss_not_a_pass",
        "an attack with no implementation is reported as discovered",
    ),
    Mutation(
        "T03-three-steps-folded-into-one",
        "acceptance/inject/attacks.py",
        '            f"{outcome.attack.id:34} {outcome.found:9} {outcome.blame_step:9} "',
        '            f"{outcome.attack.id:34} {\'overall\':9} {outcome.blame_step:9} "',
        "tests/test_attacks.py::test_the_report_keeps_the_three_steps_apart",
        "a column called overall appears and the steps stop being readable apart",
    ),
    Mutation(
        "T04-a11-bad-chain-sold-as-good",
        "acceptance/inject/injections.py",
        "        found=not report.ok,\n",
        "        found=False,  # mutation: the fabricated chain is reported as fine\n",
        "tests/test_attacks.py::test_a11_the_executor_reports_the_gap_as_found",
        "a fabricated chain is reported as healthy by the executor",
    ),
    Mutation(
        "T05-a07-blames-doctored-to-look-distinct",
        "acceptance/inject/injections.py",
        '    blames = tuple(\n        attribute(name, events, experiment=experiment).blame for name, (events, experiment) in cases.items()\n    )',
        '    blames = ("tool", "tool", "tool")  # mutation: attribution ignores the declared cause',
        "tests/test_attacks.py::test_a07_the_three_root_causes_get_three_distinct_blames",
        "the attribution reading is doctored back to one blame for three root causes",
    ),
    Mutation(
        "T06-a11-gap-claims-to-be-closed",
        "tests/test_attacks.py",
        "    assert not report.ok, (",
        "    assert report.ok, (",
        "tests/test_attacks.py::test_a11_citing_rows_that_do_not_exist_is_caught",
        "the red evidence turns green: XPASS under strict xfail is what flags the gap",
    ),
    Mutation(
        "T07-baseline-whitelist-relaxed",
        "acceptance/judge.py",
        'ALLOWED_BASELINE_KEYS: frozenset[str] = frozenset({"min_pass_count", "baseline_run"})',
        'ALLOWED_BASELINE_KEYS: frozenset[str] = frozenset({"min_pass_count", "baseline_run", "reward"})',
        "tests/test_acceptance.py::test_the_baseline_loader_refuses_a_reward",
        "a reward field is whitelisted straight back into the baseline",
    ),
    Mutation(
        "T08-harness-does-not-restore-on-kill",
        "tools/reverse_validation.py",
        "def restore_all() -> None:\n    \"\"\"Put every mutated file back exactly as it was.\"\"\"\n    for path, original in list(_RESTORE.items()):",
        'def restore_all() -> None:\n    """Put every mutated file back exactly as it was."""\n    for path, original in []:',
        "tests/test_acceptance.py::test_the_reverse_validation_harness_restores_what_it_mutated",
        "a killed harness leaves its mutation behind and other tests go falsely red",
    ),
    Mutation(
        "T09-preflight-blind-to-a-leftover-mutation",
        "tools/reverse_validation.py",
        "    problems: list[str] = []\n    for mutation in mutations:\n",
        "    return []  # mutation: measure whatever tree we find\n    for mutation in mutations:\n",
        "tests/test_acceptance.py::test_the_harness_refuses_to_measure_a_tree_that_already_carries_a_mutation",
        "a tree that already carries a mutation is measured as if it were clean",
    ),
    Mutation(
        "T10-pure-merge-ref-guard-skipped-again",
        "assemble_impl/produce.py",
        "    elif (authorized_sources or subtask.inputs) and snapshot_ref not in authorized_sources:\n",
        "    elif authorized_sources and snapshot_ref not in authorized_sources:  # mutation: the old hole\n",
        "tests/test_attacks.py::test_a_merge_block_may_not_cite_a_source_it_never_read",
        "a merge block can cite a source it never read and the guard lets it through",
    ),
    # -- block 16: the four judgment gaps (A04 / A07 / A10 / A11) -- #
    Mutation(
        "B01-declared-cause-ignored",
        "agentloop/attribution.py",
        "    declared = _trace_declarations(events)\n",
        "    declared = {}  # mutation: back to guessing from the first symptom\n",
        "tests/test_agentloop_attribution.py::test_a_declared_rejection_outranks_a_bare_tool_failure",
        "a declared cause is ignored and the bare tool failure decides again",
    ),
    Mutation(
        "B02-two-blames-resolved-by-a-coin-flip",
        "agentloop/attribution.py",
        "    if len(declared) > 1:\n",
        "    if False:  # mutation: pick one of the two accusers\n",
        "tests/test_agentloop_attribution.py::test_two_layers_blaming_each_other_is_unknown_not_a_coin_flip",
        "two layers blaming each other is resolved by a guess instead of admitting unknown",
    ),
    Mutation(
        "B03-absent-cell-loaded-as-an-empty-string",
        "assemble_impl/produce.py",
        "        clean_rows.append(canonical_row(row))\n",
        "        clean_rows.append(dict(row))  # mutation: an absent cell keeps its raw spelling\n",
        "tests/test_runner.py::test_an_absent_cell_is_loaded_as_missing_and_a_zero_is_left_alone",
        "a missing cell comes back as \"\" and the not-null judge can no longer name it",
    ),
    Mutation(
        "B04-not-null-cells-never-inspected",
        "core/checkers/schema.py",
        "            for index, row in enumerate(artifact.rows):\n                value = row.get(name)\n",
        "            for index, row in []:  # mutation: only the declaration is checked\n                value = row.get(name)\n",
        "tests/test_checkers.py::test_schema_rejects_a_missing_cell_in_a_not_null_column",
        "a not-null column passes with a missing cell in it",
    ),
    Mutation(
        "B05-unit-values-never-read",
        "core/checkers/unit_consistency.py",
        "    symbols = {other.symbol for other in context.ontology.units.values()}\n",
        "    symbols = set()\n    return ok(constraint, f\"unit {declared!r} consistent for {concept!r}\")\n",
        "tests/test_checkers.py::test_unit_consistency_reads_the_unit_off_the_value",
        "the checker is back to comparing declarations only",
    ),
    Mutation(
        "B06-unitless-values-rejected",
        "core/checkers/unit_consistency.py",
        "                if not column_unit:\n                    unlabelled += 1\n                continue\n",
        "                return fail(constraint, f\"row={index} carries no unit\")\n",
        "tests/test_checkers.py::test_unit_consistency_leaves_a_unitless_value_alone",
        "a value that carries no unit is failed instead of being reported as unjudgeable",
    ),
    Mutation(
        "B07-content-check-dropped",
        "core/provenance.py",
        "    body = bodies.get(provenance.snapshot_ref)\n    if body is None:\n        return \"\"  # no content on hand: the name check is all we can honestly claim\n",
        "    return \"\"  # mutation: the name check is all there is again\n",
        "tests/test_attacks.py::test_a11_citing_rows_that_do_not_exist_is_caught",
        "citing row 29 of a 3-row snapshot passes the chain again",
    ),
    Mutation(
        "B08-value-match-dropped",
        "core/checkers/provenance.py",
        "            if not same_quantity(value, snapshot_row[column], map_field(column, ontology) if ontology else column, ontology):\n",
        "            if False:\n",
        "tests/test_attacks.py::test_a11_a_citation_whose_value_does_not_match_is_caught",
        "a citation whose value is wrong is accepted as long as the row exists",
    ),
    Mutation(
        "B09-any-input-row-matches",
        "assemble_impl/produce.py",
        "        matched = [entry for entry in pool if _same_row(row, entry[1])]\n",
        "        matched = list(pool)  # mutation: any input row will do\n",
        "tests/test_attacks.py::test_a_merge_row_inherits_the_citation_of_the_row_it_was_handed",
        "an invented merge row is handed a citation from an unrelated input row",
    ),
    Mutation(
        "B10-runner-skips-the-frozen-bodies",
        "assemble_impl/runner.py",
        "                bodies[ref] = self.reader.body(snapshot)\n",
        "                continue  # mutation: no body is ever read, the chain falls back to names\n",
        "tests/test_acceptance.py::test_a_fabricated_answer_is_only_caught_once_the_chain_reads_the_snapshot",
        "a fabricated 30-row answer passes again because no body is ever read",
    ),
    Mutation(
        "B11-reader-drops-the-rows",
        "sources/reader.py",
        "        return SnapshotBody(ref=snapshot.ref, columns=columns, rows=rows)\n",
        "        return SnapshotBody(ref=snapshot.ref, columns=columns)  # mutation: no rows\n",
        "tests/test_sources.py::test_the_reader_hands_the_frozen_rows_over_as_data",
        "the reader hands over an empty body and the content check sees nothing",
    ),
    # -- block 21: the interview prerequisite (the archive keeps it verbatim) -- #
    Mutation(
        "B36-archive-drops-the-verbatim-context",
        "acceptance/archive.py",
        '            "messages": [dict(message) for message in messages],\n',
        '            "messages": [],  # mutation: the interview can no longer replay the call\n',
        "tests/test_stress_task.py::test_the_archive_keeps_everything_the_interview_needs",
        "A1 can no longer be answered -- there is nothing verbatim to quote",
    ),
    # -- block 20: the pressure task (heterogeneity, degradation, truth isolation) -- #
    Mutation(
        "B32-truth-reported-as-given-to-the-run",
        "acceptance/harness.py",
        '    given = tuple(sorted(str(path.relative_to(base)) for path in (base / "input").glob("*")))\n',
        '    given = tuple(sorted(str(path.relative_to(base)) for path in base.glob("*/*")))\n',
        "tests/test_stress_task.py::test_the_run_is_never_handed_the_truth",
        "the truth directory shows up as if it had been handed to the run",
    ),
    Mutation(
        "B33-the-deliberately-dead-source-is-generated",
        "acceptance/stress/generate.py",
        '    if cover == "dead":\n',
        '    if False:  # mutation: the offline source is written after all\n',
        "tests/test_stress_task.py::test_the_pressure_task_is_a_pipeline_of_blocks_with_one_source_offline",
        "the persistent-failure attack surface disappears from the pressure task",
    ),
    Mutation(
        "B34-pressure-task-shrunk-to-half-the-sources",
        "acceptance/stress/generate.py",
        "    for spec in SOURCE_SPECS:\n        _write_source(given, spec)\n",
        "    for spec in SOURCE_SPECS[:12]:  # mutation: half the sources vanish\n        _write_source(given, spec)\n",
        "tests/test_stress_task.py::test_the_pressure_task_is_a_pipeline_of_blocks_with_one_source_offline",
        "the task shrinks below the 20-source floor and keeps its shape claim",
    ),
    Mutation(
        "B35-html-sources-serve-as-csv",
        "acceptance/stress/generate.py",
        '    elif fmt == "html":\n        _html(path, names, entity, unit, labeled, lang)\n',
        '    elif fmt == "html":  # mutation: the format dimension collapses\n        _csv(path, names, _rows_for(entity, unit, labeled, lang))\n',
        "tests/test_stress_task.py::test_the_five_heterogeneity_dimensions_are_really_in_the_data",
        "an HTML source becomes plain CSV and the format claim is no longer backed by data",
    ),
    # -- block 19: real-model findings (unit parser, unit hand-off, A06 exits) -- #
    Mutation(
        "B26-partial-fetch-failure-still-refuses-the-run",
        "assemble_impl/runner.py",
        "            except SourceError as exc:\n                self.fetch_failures[source.ref] = str(exc)\n",
        '            except SourceError as exc:\n                self.fetch_failures[source.ref] = str(exc)\n                raise RunRefused("sources", "mutation", ("fetch_failed",))\n',
        "tests/test_acceptance.py::test_a_dead_source_degrades_the_run_instead_of_killing_it",
        "one dead source kills the whole run again, usable results and all",
    ),
    Mutation(
        "B27-plan-still-cuts-the-dead-source",
        "decompose/strategies.py",
        "        return tuple(sorted(recon.source_ids))\n",
        "        pass  # mutation: the survey no longer narrows the plan\n",
        "tests/test_acceptance.py::test_a_dead_source_degrades_the_run_instead_of_killing_it",
        "a block is cut for a source nobody holds, so it can only fail",
    ),
    Mutation(
        "B28-degradation-left-unmarked",
        "acceptance/harness.py",
        "    if result.fetch_failures:\n",
        "    if False:  # mutation: a degraded run says nothing about it\n",
        "tests/test_acceptance.py::test_a_dead_source_degrades_the_run_instead_of_killing_it",
        "a run that continued without a source never says so",
    ),
    Mutation(
        "B29-fraction-read-as-a-unit-again",
        "core/ontology.py",
        "    if not symbol or not symbol[0].isalpha():\n",
        "    if not symbol:  # mutation: '.0' counts as a unit symbol\n",
        "tests/test_checkers.py::test_a_plain_decimal_string_is_not_read_as_a_quantity",
        "a plain decimal is read as a quantity in unit '.0' and rejected",
    ),
    Mutation(
        "B30-part-payload-drops-the-unit",
        "services/registry.py",
        '            {"name": c.name, "dtype": c.dtype, "nullable": c.nullable, "unit": c.unit} for c in artifact.columns\n',
        '            {"name": c.name, "dtype": c.dtype, "nullable": c.nullable} for c in artifact.columns\n',
        "tests/test_services.py::test_the_part_payload_carries_the_columns_unit",
        "an established unit dies at the hand-off to the consumer",
    ),
    Mutation(
        "B31-task-does-not-judge-the-panel-unit",
        "acceptance/main/panel-three-sources/task.yaml",
        "    - id: C-units\n",
        "    - id: C-units-disabled\n",
        "tests/test_acceptance.py::test_a_mixed_unit_panel_is_caught_only_once_the_task_judges_units",
        "a panel whose amounts are still in 千元 passes the three axes again",
    ),
    # -- block 18: the unit basis, the named sources gate, the fork-adjacent trap -- #
    Mutation(
        "B21-unit-assumption-left-silent",
        "core/checkers/unit_consistency.py",
        '    if unlabelled:\n',
        '    if False:  # mutation: an unlabelled value is silently taken as the declared unit\n',
        "tests/test_acceptance.py::test_a_source_that_does_not_label_its_unit_is_not_silently_assumed",
        "the panel says nothing about a unit it never established",
    ),
    Mutation(
        "B22-explicit-unit-requirement-ignored",
        "core/checkers/unit_consistency.py",
        '        if constraint.spec.get("unit_source") == "explicit":\n',
        "        if False:  # mutation: the explicit-unit requirement is ignored\n",
        "tests/test_checkers.py::test_unit_consistency_refuses_an_assumed_unit_when_the_task_demands_explicit",
        "a task that requires the unit to be stated gets an unlabelled number anyway",
    ),
    Mutation(
        "B23-column-unit-declaration-ignored",
        "core/checkers/unit_consistency.py",
        "        column_unit = _column_unit(artifact, column)\n",
        '        column_unit = ""  # mutation: what the producer declared about its column is discarded\n',
        "tests/test_checkers.py::test_unit_consistency_accepts_a_column_that_declares_its_unit",
        "a column's declared unit no longer counts as an explicit unit",
    ),
    Mutation(
        "B24-unreadable-carrier-not-a-gate",
        "assemble_impl/runner.py",
        "                raise RunRefused(\n",
        "                raise  # mutation: let the carrier error escape as a generic failure\n",
        "tests/test_acceptance.py::test_an_unreadable_frozen_carrier_is_a_named_gate",
        "an unreadable carrier is no longer reported at a named gate",
    ),
    Mutation(
        "B25-registry-fallback-feeds-the-parser-a-mapping",
        "services/registry.py",
        '                        call.raw_arguments or json.dumps(dict(call.arguments), ensure_ascii=False)\n',
        "                        call.raw_arguments or call.arguments  # mutation: a Mapping into json.loads\n",
        "tests/test_services.py::test_a_call_carrying_only_parsed_arguments_is_not_mistaken_for_bad_json",
        "a call with only parsed arguments is refused as bad JSON again",
    ),
    # -- block 17: the read-time normalisation and the declared-cause producers -- #
    Mutation(
        "B12-quantity-conversion-dropped",
        "core/checkers/provenance.py",
        "    if same_value(left, right):\n        return True\n    if ontology is None:\n        return False\n",
        "    return same_value(left, right)  # mutation: units are compared as text again\n",
        "tests/test_attacks.py::test_a11_a_converted_quantity_still_replays_and_a_dropped_unit_does_not",
        "a converted quantity is refused, so read-time normalisation cannot be cited",
    ),
    Mutation(
        "B13-declared-cause-never-recorded",
        "assemble_impl/produce.py",
        '                cause = self._declared_cause(name, result, calls)\n                if cause:\n                    detail["cause"] = cause\n',
        '                cause = ""  # mutation: the declaration never reaches the event\n',
        "tests/test_acceptance.py::test_a_real_run_attributes_a_missing_input_to_context",
        "the layer's declaration never lands on the event stream",
    ),
    Mutation(
        "B14-missing-input-not-declared",
        "assemble_impl/produce.py",
        '        return "context" if isinstance(payload, Mapping) and payload.get("missing") is True else ""\n',
        '        return ""  # mutation: a missing input declares nothing\n',
        "tests/test_acceptance.py::test_a_real_run_attributes_a_missing_input_to_context",
        "an input nobody produced no longer blames the delegation layer",
    ),
    Mutation(
        "B15-constraint-rejection-not-declared",
        "assemble_impl/runner.py",
        '                if failure_evidence.get("rejected_by"):\n                    failure_detail["constraint_rejected"] = True\n',
        "                if False:  # mutation: the judge no longer declares its rejection\n",
        "tests/test_acceptance.py::test_a_real_run_attributes_a_rejected_block_to_the_constraint",
        "a block rejected by named constraints no longer blames the constraint layer",
    ),
    Mutation(
        "B16-exception-blame-ignored",
        "services/registry.py",
        '                        cause=str(getattr(exc, "blame", "") or ""),\n',
        "                        cause=\"\",  # mutation: the exception declaration is dropped\n",
        "tests/test_agentloop_attribution.py::test_a_broken_carrier_declares_the_tool_layer_on_the_real_event_stream",
        "a raised exception's declared layer never reaches the tool result",
    ),
    Mutation(
        "B17-unknown-tool-blames-the-tool",
        "services/registry.py",
        '                        cause="model",\n',
        '                        cause="tool",  # mutation: back to "a tool failed, so blame the tool"\n',
        "tests/test_acceptance.py::test_a_real_run_attributes_an_unknown_tool_to_the_model",
        "the caller's nonexistent tool name is blamed on a tool that was never there",
    ),
    Mutation(
        "B19-untraceable-rows-silently-skipped",
        "assemble_impl/produce.py",
        '        if not matched:\n            reject(\n                "untraceable_row",\n',
        '        if not matched:\n            continue  # mutation: a row with no source is dropped\n        if False:\n            reject(\n                "untraceable_row",\n',
        "tests/test_acceptance.py::test_a_merge_that_transforms_is_still_refused",
        "a merge that changes what it was handed is no longer refused",
    ),
    Mutation(
        "B20-missing-payload-anonymous",
        "assemble_impl/runner.py",
        '                upstream[name] = {"name": name, "schema": plan.interfaces.get(name, ""), "missing": True}\n',
        '                upstream[name] = {"schema": plan.interfaces.get(name, ""), "missing": True}\n',
        "tests/test_acceptance.py::test_a_real_run_attributes_a_missing_input_to_context",
        "an absent input no longer says which input it is, and the caller keeps asking",
    ),
    # -- one witness per reading: a doctored reading must not look like the real one -- #
    Mutation(
        "T11-a01-reading-claims-a-cut",
        "acceptance/inject/injections.py",
        '        action="declare_cross_block_dependency" if (len(declared) == 3 and matched) else "cut",\n',
        '        action="cut",  # mutation: the reading stops distinguishing\n',
        "tests/test_attacks.py::test_a01_cross_block_dependency_is_declared",
        "A01's reading claims the dependency was cut even when it was declared",
    ),
    Mutation(
        "T12-a02-timing-blamed-on-the-wrong-layer",
        "acceptance/inject/injections.py",
        '        action="no_definition_mismatch" if mismatches == 0 else "definition_mismatch_at_compare_time",\n',
        '        action="no_definition_mismatch",  # mutation: where it was caught stops mattering\n',
        "tests/test_attacks.py::test_a02_polysemy_is_detected_at_compare_time_not_blocked_at_L1",
        "A02's reading hides that the mismatch was caught at compare time, not at L-1",
    ),
    Mutation(
        "T13-a03-conflict-downgraded-to-picked-one",
        "acceptance/inject/injections.py",
        '        action="record_conflict" if conflicts > 0 else "picked_one_or_failed",\n',
        '        action="picked_one_or_failed",  # mutation\n',
        "tests/test_attacks.py::test_a03_real_conflict_is_recorded_not_failed",
        "a recorded conflict is reported as arbitrarily picking one side",
    ),
    Mutation(
        "T14-a04-missing-and-zero-still-folded-together",
        "acceptance/inject/injections.py",
        "    distinguished = blank_outcomes != zero_outcomes\n",
        "    distinguished = False  # mutation: the two are assumed to be the same\n",
        "tests/test_attacks.py::test_a04_missing_and_zero_are_told_apart",
        "the missing-versus-zero distinction is denied again",
    ),
    Mutation(
        "T15-a05-retry-count-check-dropped",
        "acceptance/inject/injections.py",
        '        action="retry_local" if passed and io.attempts == 3 else "other",\n',
        '        action="other",  # mutation: the retry that saved it is not named\n',
        "tests/test_attacks.py::test_a05_transient_failure_is_retried_locally",
        "the local retry exit is no longer identified",
    ),
    Mutation(
        "T16-a06-partial-failure-reported-as-a-refusal",
        "acceptance/inject/injections.py",
        "    degraded = partial.refused is None and bool(marked)\n",
        "    degraded = False  # mutation: a partial failure is read as a full refusal\n",
        "tests/test_attacks.py::test_a06_a_dead_source_degrades_and_a_dead_run_still_refuses",
        "the degradation is reported as the old pre-cluster refusal",
    ),
    Mutation(
        "T17-a08-blind-spot-reported-as-judged",
        "acceptance/inject/injections.py",
        "        found=not report.passed and not blind,\n",
        "        found=False,  # mutation: the old '看不见产物' reading is sold as a miss\n",
        "tests/test_attacks.py::test_a08_the_global_invariant_sees_the_multi_piece_assembly",
        "the '*' blind spot is reported as if the invariant had never seen the assembly",
    ),
    Mutation(
        "T18-a09-decline-exit-unrecognised",
        "acceptance/inject/injections.py",
        '        action="decline" if result.declined else "other",\n',
        '        action="other",  # mutation\n',
        "tests/test_attacks.py::test_a09_impossible_block_declines_instead_of_inventing",
        "the decline exit is no longer recognised",
    ),
    Mutation(
        "T19-a10-wrong-unit-sold-as-consistent",
        "acceptance/inject/injections.py",
        "        found=caught_wrong_unit,\n",
        "        found=False,  # mutation: a value declaring another unit looks consistent\n",
        "tests/test_attacks.py::test_a10_unit_consistency_reads_the_unit_off_the_value",
        "the value-level unit check is reported as passing",
    ),
    Mutation(
        "T20-gap-notes-collect-nothing",
        "acceptance/inject/attacks.py",
        "    misses = [o for o in outcomes if o.ran and MISS in (o.found, o.blame_step, o.action_step)]\n",
        "    misses = []  # mutation: the coverage declaration collects nothing\n",
        "tests/test_attacks.py::test_the_gap_notes_name_the_rows_that_missed",
        "the coverage declaration stops naming the rows that missed",
    ),
    Mutation(
        "T21-only-filter-ignored",
        "acceptance/inject/attacks.py",
        "        if only is not None and attack.id not in only:\n            continue\n",
        "        if only is not None and attack.id not in only:\n            pass\n",
        "tests/test_attacks.py::test_only_runs_what_was_asked_for",
        "asking for a subset of rows silently runs the whole table",
    ),
    Mutation(
        "T22-extra-attack-rows-dropped",
        "acceptance/inject/attacks.py",
        '    rows = list(document.get("attacks", ())) + list(document.get("attacks_extra", ()))\n',
        '    rows = list(document.get("attacks", ()))  # mutation: the extra rows vanish\n',
        "tests/test_attacks.py::test_every_row_in_the_table_has_an_injector",
        "a row disappears from the table and nothing notices",
    ),
    Mutation(
        "T23-raised-injection-sold-as-a-pass",
        "acceptance/inject/attacks.py",
        '            outcomes.append(AttackOutcome(attack=attack, error=f"{type(exc).__name__}: {exc}"))\n',
        '            outcomes.append(AttackOutcome(attack=attack, observation=Observation(found=True)))\n',
        "tests/test_attacks.py::test_an_injection_that_breaks_is_a_miss_not_a_pass",
        "an injection that raised is reported as a discovered defect",
    ),
    # -- the acceptance set: the judge's structure and the split mechanism -- #
    Mutation(
        "U01-judge-grows-a-single-score",
        "acceptance/judge.py",
        "    attribution_accuracy: float\n",
        "    attribution_accuracy: float\n    overall_score: float = 0.0\n",
        "tests/test_acceptance.py::test_report_has_no_single_score",
        "a single total appears and the three axes stop being readable apart",
    ),
    Mutation(
        "U02-render-drops-the-uncovered-exits",
        "acceptance/judge.py",
        '            "  ★ not hit (未验证) : " + (", ".join(self.uncovered()) or "<none>"),',
        '            "  ★ not hit (未验证) : " + "<none>",',
        "tests/test_acceptance.py::test_report_declares_uncovered_exits",
        "a report stops saying which exits were never reached",
    ),
    Mutation(
        "U03-coverage-may-be-partial",
        "acceptance/judge.py",
        "        missing = sorted(set(EXIT_KINDS) - set(self.coverage))\n        if missing:\n            raise ValueError(f\"coverage must declare every exit, missing {missing!r}\")",
        "        missing = []\n        if missing:\n            raise ValueError(f\"coverage must declare every exit, missing {missing!r}\")",
        "tests/test_acceptance.py::test_a_report_without_coverage_is_refused",
        "an incomplete coverage map is accepted, so silence looks like success",
    ),
    Mutation(
        "U04-smoke-gate-uses-a-boolean",
        "acceptance/judge.py",
        "    return observed_pass_count >= minimum_pass_count",
        "    return bool(observed_pass_count)",
        "tests/test_acceptance.py::test_smoke_uses_count_not_boolean_reward",
        "the smoke gate stops comparing counts and becomes a boolean reward",
    ),
    Mutation(
        "U05-cross-block-dependency-not-declared",
        "decompose/strategies.py",
        "                expects=tuple((name, interfaces[name]) for name in parts),",
        "                expects=(),",
        "tests/test_acceptance.py::test_the_cross_block_dependency_is_declared_not_cut",
        "the merge block stops declaring what it consumes (A01: the edge is cut, not named)",
    ),
    Mutation(
        "U06-multi-block-plan-collapses-to-one-block",
        "decompose/strategies.py",
        "    if len(keys) < 2:\n        return split_single(contract, ontology, round=round, version=version)",
        "    if True:\n        return split_single(contract, ontology, round=round, version=version)",
        "tests/test_acceptance.py::test_the_multi_block_task_really_cuts_into_several_blocks",
        "a multi-source task silently stops being cut into several blocks",
    ),
    Mutation(
        "U07-truth-is-handed-to-the-run",
        "acceptance/harness.py",
        '    given = tuple(sorted(str(path.relative_to(base)) for path in (base / "input").glob("*")))',
        '    given = tuple(sorted(str(path.relative_to(base)) for path in base.glob("*/*")))',
        "tests/test_acceptance.py::test_the_isolation_rule_holds_the_run_only_gets_input",
        "the harness starts reporting truth paths among the run's inputs",
    ),
    Mutation(
        "U08-truth-is-never-compared",
        "acceptance/harness.py",
        "        verdict_truth = compare_rows(rows, document)",
        "        verdict_truth = None  # mutation: the answer is never judged against the truth",
        "tests/test_acceptance.py::test_the_scripted_multi_block_run_matches_the_truth_keys",
        "a wrong answer passes every system check and nobody notices",
    ),
    Mutation(
        "U09-truth-comparison-ignores-invented-keys",
        "acceptance/truth.py",
        "    invented = tuple(sorted(actual_keys - expected_keys))",
        "    invented = ()  # mutation: invented keys are forgiven",
        "tests/test_acceptance.py::test_a_wrong_answer_passes_every_system_check_but_fails_the_truth",
        "an answer made of invented entities is judged as acceptable",
    ),
    # -- the three defects the real model exposed in the shell itself -- #
    Mutation(
        "V01-output-cap-is-a-private-constant",
        "assemble_impl/produce.py",
        "    return budget.max_output",
        "    return 512  # mutation: the declaration has no consequence",
        "tests/test_services.py::test_the_model_call_uses_the_declared_output_allowance",
        "the budget declares 8192 and every call spends 512",
    ),
    Mutation(
        "V02-allowance-unit-reinterpreted-as-tokens",
        "assemble_impl/produce.py",
        "    if budget.max_output_unit != TOKEN_UNIT:",
        "    if False:  # mutation: any unit is treated as tokens",
        "tests/test_services.py::test_an_allowance_in_another_unit_is_refused_not_reinterpreted",
        "a byte-denominated allowance is silently spent as tokens",
    ),
    Mutation(
        "V03-truncation-reported-as-a-shape-error",
        "assemble_impl/produce.py",
        '    if not head.rstrip().endswith("}"):',
        "    if False:  # mutation: a cut-off answer looks like a shape error",
        "tests/test_runner.py::test_a_truncated_answer_is_reported_as_truncation_not_as_the_first_character",
        "our own output cap is blamed on the model's shape",
    ),
    Mutation(
        "V04-missing-ref-papered-over-with-the-block-id",
        "assemble_impl/produce.py",
        '        if authorized_sources:\n            keys = sorted(str(key) for key in document)',
        '        if False:\n            keys = sorted(str(key) for key in document)',
        "tests/test_runner.py::test_an_answer_that_cites_no_snapshot_fails_at_the_shape_gate",
        "a missing ref becomes a plausible-looking block id that fails downstream",
    ),
    Mutation(
        "V05-ref-shape-gate-without-the-valid-list",
        "assemble_impl/produce.py",
        '    listed = ", ".join(f\'"{ref}"\' for ref in authorized_sources)',
        '    listed = "the authorized sources"  # mutation: the fix stops naming them',
        "tests/test_runner.py::test_the_shape_gate_names_the_snapshots_that_would_have_been_valid",
        "the model is told a ref is wrong without being told which one is right",
    ),
    Mutation(
        "V06-authorized-sources-not-offered-to-the-gate",
        "assemble_impl/produce.py",
        "            authorized_sources=self.authorized_sources(),",
        "            authorized_sources=(),  # mutation: the block forgets what it may cite",
        "tests/test_runner.py::test_an_answer_that_cites_no_snapshot_fails_at_the_shape_gate",
        "the ref rules cannot fire because the gate never learns the authorized set",
    ),
    Mutation(
        "V08-schema-makes-ref-optional-again",
        "assemble_impl/produce.py",
        '    "required": ["ref", "columns", "rows"],',
        '    "required": ["columns", "rows"],',
        "tests/test_services.py::test_candidate_response_format_carries_the_answer_schema",
        "the request describes the answer differently from the way it is judged",
    ),
    Mutation(
        "V07-runtime-invents-its-own-output-cap",
        "assemble_impl/runtime.py",
        "            budget=budget,",
        "            budget=Budget(max_calls=64, max_wall_s=3600, max_output=512),",
        "tests/test_runner.py::test_the_block_runtime_hands_the_ports_budget_to_the_producer",
        "the port's budget is ignored in favour of a private default",
    ),
    Mutation(
        "Z09-rejected-candidate-has-no-reason",
        "assemble_impl/runner.py",
        '            evidence["rejected_by"] = ", ".join(withheld.failed)',
        '            pass  # mutation: the report stops at the phenomenon layer',
        "tests/test_runner.py::test_a_withheld_candidate_says_which_check_rejected_it",
        "a withheld candidate is reported as 'it failed' without naming the rule",
    ),
    # -- structured output: three layers, and the feedback that reaches the model -- #
    Mutation(
        "Z01-recovery-text-never-reaches-the-transcript",
        "agentloop/loop.py",
        "            messages=assemble_messages(turn.state.messages, turn.state.injected),",
        "            messages=turn.state.messages,  # mutation: the injection is dropped",
        "tests/test_agentloop_loop.py::test_a_rejected_answer_with_a_fix_is_fed_back_and_answered_again",
        "the fix is computed, queued, and then silently discarded before the next call",
    ),
    Mutation(
        "Z02-reformat-without-asking-again",
        "agentloop/recovery.py",
        "            return RecoveryDecision(\n                action=RecoveryAction.REFORMAT,",
        "            return RecoveryDecision(\n                action=RecoveryAction.FIX_CONSTRAINT,",
        "tests/test_agentloop_recovery.py::test_a_labelled_fix_reformats_instead_of_stopping",
        "a fixable answer shape stops the block instead of being fed back",
    ),
    Mutation(
        "Z03-reformat-round-is-unbounded",
        "agentloop/recovery.py",
        "        if attempts <= max_reflections:",
        "        if True:  # mutation: the reflection budget no longer caps it",
        "tests/test_agentloop_recovery.py::test_the_reformat_round_is_capped_by_the_reflection_budget",
        "an unfixable answer is re-asked forever",
    ),
    Mutation(
        "Z04-feedback-loses-what-was-sent",
        "agentloop/recovery.py",
        "    if got:\n        snippet = got if len(got) <= got_limit else got[:got_limit] + \"…[truncated]\"\n        lines.append(f\"what you sent: {snippet}\")",
        "    if False:\n        snippet = got\n        lines.append(f\"what you sent: {snippet}\")",
        "tests/test_agentloop_loop.py::test_a_rejected_answer_with_a_fix_is_fed_back_and_answered_again",
        "the model is told it failed without being shown its own output",
    ),
    Mutation(
        "Z05-schema-sent-on-the-first-call",
        "assemble_impl/produce.py",
        "        return self.response_format.enabled and self.calls > 1",
        "        return self.response_format.enabled  # mutation: schema from turn one",
        "tests/test_services.py::test_the_first_call_is_free_form_and_later_calls_ask_for_the_shape",
        "a strict answer schema on turn one stops the model from calling any tool",
    ),
    Mutation(
        "Z06-schema-refusal-becomes-fatal",
        "assemble_impl/produce.py",
        "            if not rejects_response_format(exc):",
        "            if True:  # mutation: every refusal is fatal",
        "tests/test_services.py::test_a_service_that_refuses_the_schema_degrades_instead_of_failing",
        "a service without structured output can never run a block",
    ),
    Mutation(
        "Z07-transport-error-read-as-a-capability-answer",
        "assemble_impl/produce.py",
        "    if not isinstance(status, int) or isinstance(status, bool) or status not in (400, 404, 415, 422):",
        "    if False:  # mutation: any status is a capability answer",
        "tests/test_services.py::test_a_transport_error_is_not_read_as_a_capability_answer",
        "a 429 or a timeout is misread as 'no structured output' and the schema is dropped",
    ),
    Mutation(
        "Z08-rejection-ships-without-a-shape",
        "assemble_impl/produce.py",
        "            self.response_format.enabled = False\n            self.response_format.refused = True",
        "            self.response_format.refused = True",
        "tests/test_services.py::test_a_refusal_is_learned_once_for_the_whole_run",
        "the refusal is remembered but not acted on, so every call pays the probe again",
    ),
    # -- error classification: declared semantics first, wording as fallback -- #
    Mutation(
        "Y01-declared-reason-ignored",
        "agentloop/errors.py",
        "    declared = declared_failure(error)",
        "    declared = None  # mutation: the raiser's own label is ignored",
        "tests/test_agentloop_errors.py::test_the_measured_failure_chain_classifies_as_measured",
        "a bare CandidateFormatError goes back to unknown/retry_local and burns the budget",
    ),
    Mutation(
        "Y02-wording-outranks-the-declaration",
        "agentloop/errors.py",
        "    declared = declared_failure(error)",
        "    declared = _by_message(text) or declared_failure(error)",
        "tests/test_agentloop_errors.py::test_a_declared_reason_outranks_the_wording",
        "classification depends on how the caller spelled the message again",
    ),
    Mutation(
        "Y03-type-name-outranks-the-declaration",
        "agentloop/errors.py",
        "    declared = declared_failure(error)",
        "    declared = _by_type(error) or declared_failure(error)",
        "tests/test_agentloop_errors.py::test_a_declared_reason_outranks_the_class_name",
        "a class named *Timeout* decides the action even when it says otherwise",
    ),
    Mutation(
        "Y04-action-label-falls-through",
        "agentloop/errors.py",
        "    if isinstance(declared, BobananaAction):",
        "    if False:  # mutation: an action label is treated as a reason",
        "tests/test_agentloop_errors.py::test_a_label_may_name_an_action_when_no_reason_fits",
        "a label that names an action is misread, so the action is lost",
    ),
    Mutation(
        "Y05-declared-reason-typo-ships",
        "assemble_impl/produce.py",
        '    #: which burned the budget re-sending the same request.\n    reason = "format_error"',
        '    #: which burned the budget re-sending the same request.\n    reason = "fmt_error"',
        "tests/test_arch_guard.py::test_every_declared_reason_is_in_the_vocabulary",
        "a typo in the label is accepted and silently degrades to the fallback",
    ),
    Mutation(
        "Y06-wording-fallback-removed",
        "agentloop/errors.py",
        "            or _by_message(text)",
        "            or None  # mutation: the third-party fallback is gone",
        "tests/test_agentloop_errors.py::test_an_exception_without_a_label_still_uses_the_message_fallback",
        "exceptions we cannot annotate lose their only classification path",
    ),
    # -- adapters: the two configuration gaps a real model exposed ---------- #
    Mutation(
        "X01-extra-body-dropped",
        "adapters/openai_compat.py",
        "        payload.update(self.settings.extra_body)",
        "        pass  # mutation: vendor extensions are dropped again",
        "tests/test_adapters.py::test_extra_body_lands_in_the_payload",
        "a vendor-only switch (enable_thinking) is silently discarded",
    ),
    Mutation(
        "X02-protected-keys-not-checked",
        "adapters/openai_compat.py",
        "        clashes = sorted(set(self.extra_body) & set(PROTECTED_PAYLOAD_KEYS))",
        "        clashes = []  # mutation: nothing is protected",
        "tests/test_adapters.py::test_extra_body_may_not_override_model",
        "extra_body may replace the model name without saying so",
    ),
    Mutation(
        "X03-timeout-env-ignored",
        "adapters/openai_compat.py",
        "    raw = source.get(\"BOBANANA6_TIMEOUT_S\")",
        "    return DEFAULT_TIMEOUT_S\n    raw = source.get(\"BOBANANA6_TIMEOUT_S\")",
        "tests/test_adapters.py::test_timeout_reaches_the_transport",
        "a cold service that needs 90s can never be given the time",
    ),
    Mutation(
        "X04-timeout-default-raised",
        "adapters/openai_compat.py",
        "DEFAULT_TIMEOUT_S = 60.0",
        "DEFAULT_TIMEOUT_S = 180.0",
        "tests/test_adapters.py::test_timeout_default_is_unchanged",
        "the default is quietly raised to make one slow service pass",
    ),
    Mutation(
        "X05-extra-body-not-read-from-env",
        "adapters/openai_compat.py",
        '    raw = source.get("BOBANANA6_EXTRA_BODY")',
        '    return {}\n    raw = source.get("BOBANANA6_EXTRA_BODY")',
        "tests/test_adapters.py::test_extra_body_reads_a_json_object_from_the_environment",
        "the escape hatch cannot be configured, so the gap is still there",
    ),
    Mutation(
        "X06-malformed-extra-body-swallowed",
        "adapters/openai_compat.py",
        '        raise LLMConfigError(f"BOBANANA6_EXTRA_BODY must be a JSON object: {exc}") from exc',
        '        return {}  # mutation: a broken setting looks like no setting',
        "tests/test_adapters.py::test_extra_body_refuses_malformed_json",
        "a typo in the JSON is indistinguishable from an unset variable",
    ),
    # -- steps 11-14: sources/ adapters/ services/ assemble_impl/ report/ cli --
    Mutation(
        "W01-block-bundle-not-projected",
        "assemble_impl/runtime.py",
        "            bundle=block_bundle(bundle, subtask, ontology),",
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
        "        if not snapshots:\n",
        "        if False:  # mutation: a run with nothing fetched is not refused\n",
        "tests/test_runner.py::test_a_source_that_cannot_be_fetched_refuses_the_run",
        "a run that fetched nothing longer stops before the cluster starts",
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
    # -- the executor pointed at the pressure task (this block's only new reading) -- #
    Mutation(
        "P01-pressure-readings-ignored",
        "acceptance/inject/injections.py",
        "    if target == \"stress\":\n        return {**INJECTORS, **PRESSURE_READINGS}",
        "    if target == \"stress\":\n        return INJECTORS  # mutation: the pressure readings are never used",
        "tests/test_attacks.py::test_the_executor_can_be_pointed_at_the_pressure_task",
        "the executor claims to measure the pressure task but runs the main-task fixtures",
    ),
    # -- the truth side must use the key the task declares (domains 2/3) -- #
    Mutation(
        "Q01-truth-key-ignored",
        "acceptance/truth.py",
        '    declared = truth.get("key")\n',
        '    declared = None  # mutation: the key column the task declares is ignored\n',
        "tests/test_acceptance.py::test_the_truth_comparison_uses_the_key_the_task_declares",
        "every domain is compared on 'obligor' again, so a differently-shaped task passes vacuously",
    ),
    Mutation(
        "Q02-third-domain-drops-the-unit-constraint",
        "acceptance/main/panel-energy-sites/task.yaml",
        "    - id: C-units\n",
        "    - id: C-units-disabled\n",
        "tests/test_acceptance.py::test_the_other_two_main_domains_are_the_same_shape",
        "a new domain silently loses its unit constraint, so mixed units can pass again",
    ),
    Mutation(
        "Q03-canonical-row-keeps-the-raw-spelling",
        "core/model.py",
        "    return {str(key): value for key, value in row.items() if not is_missing(value)}",
        "    return dict(row)  # mutation: every spelling stays whatever it was",
        "tests/test_runner.py::test_every_spelling_of_a_missing_cell_becomes_the_same_representation",
        "null / empty string / absent key are three different rows again",
    ),
    # -- ① 输入递送：前置预取 / 批量取回 / 前置门 -- #
    Mutation(
        "Q04-declared-inputs-are-not-delivered",
        "assemble_impl/produce.py",
        "        delivered = self._delivered_inputs()\n",
        "        delivered = {}  # mutation: the worker is on its own again\n",
        "tests/test_acceptance.py::test_the_declared_inputs_are_delivered_before_the_merge_block_is_asked",
        "the merge block never sees its inputs and has to trust the briefing's names again",
    ),
    Mutation(
        "Q05-batch-fetch-returns-only-the-first-input",
        "services/registry.py",
        '        names = [str(name) for name in getattr(toolset, "allowed_inputs", ()) or ()]\n',
        '        names = [str(name) for name in getattr(toolset, "allowed_inputs", ()) or ()][:1]  # mutation\n',
        "tests/test_acceptance.py::test_the_batch_tool_returns_every_declared_input_at_once",
        "the one-call fallback silently drops every input but the first",
    ),
    Mutation(
        "Q06-block-with-no-inputs-is-still-dispatched",
        "assemble_impl/runner.py",
        "            if subtask.inputs and not available:\n",
        "            if False:  # mutation: a block with nothing in hand still starts producing\n",
        "tests/test_acceptance.py::test_a_block_whose_inputs_never_arrived_is_not_dispatched",
        "a block whose producers all failed is asked to answer anyway, and invents the table",
    ),
    # -- ③ 该不该拆：判据是声明，不是来源数 -- #
    Mutation(
        "Q07-shared-state-declaration-ignored-by-the-splitter",
        "decompose/strategies.py",
        "    if contract.sources_share_state:\n",
        "    if False:  # mutation: the declaration is ignored, the source count decides again\n",
        "tests/test_acceptance.py::test_the_split_decision_reads_the_declaration_not_the_source_count",
        "a task that declares its sources share one state is fanned out again",
    ),
    Mutation(
        "Q08-fanned-out-shared-state-is-not-caught",
        "core/split.py",
        "    if contract.sources_share_state and len(plan.subtasks) > 1:\n",
        "    if False:  # mutation: a hand-made fan-out of shared sources passes\n",
        "tests/test_acceptance.py::test_a_plan_that_fans_out_declared_shared_sources_is_caught",
        "the legality gate stops noticing that a declared shared state was split apart",
    ),
    Mutation(
        "Q09-delivery-budget-ignored",
        "assemble_impl/produce.py",
        "        if len(body) <= DELIVERED_INPUTS_BUDGET:\n",
        "        if True:  # mutation: hundreds of inputs are dumped into the context whole\n",
        "tests/test_acceptance.py::test_hundreds_of_inputs_do_not_cost_the_worker_any_turns",
        "a 240-input task tries to paste everything and blows the context instead of indexing",
    ),
    # -- #65：结构性坏答案必须自报 blame -- #
    Mutation(
        "Q10-model-failure-is-not-declared",
        "assemble_impl/produce.py",
        "        code = str(getattr(rejected, \"code\", \"\") or \"\")\n",
        "        return  # mutation: the model's own bad shape is never declared\n",
        "tests/test_acceptance.py::test_a_structurally_bad_answer_is_declared_as_the_models_failure",
        "the most common real-run failure goes back to blame=None",
    ),
    # -- A08：`*` 要能看见装配后的全部产物 -- #
    Mutation(
        "Q11-star-target-blind-to-a-multi-piece-assembly",
        "core/checkers/base.py",
        "        return _combined_artifact(candidate.artifacts)\n",
        "        return None  # mutation: '*' is blind again whenever several pieces exist\n",
        "tests/test_attacks.py::test_a08_the_global_invariant_sees_the_multi_piece_assembly",
        "a global invariant on the assembled whole goes back to 'artifact not found'",
    ),
)


def resolve_path(relative_path: str) -> Path:
    """Where one mutation's ``relative_path`` lives on disk."""
    if relative_path.startswith(("tests/", "acceptance/", "tools/")):
        return (REPO / relative_path).resolve()
    if relative_path.split("/")[0] in SOURCE_PACKAGES:
        return (REPO / "src" / "bobanana6" / relative_path).resolve()
    return (SRC / relative_path).resolve()


def preflight(mutations: Sequence[Mutation] = MUTATIONS) -> list[str]:
    """★ Refuse to measure a tree that already carries a mutation.

    ``restore_all()`` covers clean exits and catchable signals, but a SIGKILL
    (``kill -9``, a reaped sandbox job) leaves the mutated bytes on disk and no
    handler ever runs. Measuring on top of that reports garbage, and worse,
    nothing in the output says the tree was dirty: 2026-10-03 a killed run left
    ``core/model.py`` mutated and the next full run was silently untrustworthy.
    So check every anchor before touching anything, and abort if one is gone.
    """
    problems: list[str] = []
    for mutation in mutations:
        path = resolve_path(mutation.relative_path)
        if not path.is_file():
            problems.append(f"{mutation.ident}: no such file {mutation.relative_path}")
            continue
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        found = text.count(mutation.old)
        if found != 1:
            problems.append(
                f"{mutation.ident}: anchor found {found} time(s) in {mutation.relative_path} "
                "-- the tree already carries a mutation (a previous run was killed mid-flight?)"
            )
    return problems


def run_selector(selector: str) -> tuple[bool, str]:
    result = subprocess.run(
        # ★ `-rE` as well as `-rf`: a selector that dies in a *fixture* (before the
        #   test body) is a real catch, and with only `-rf` it is not reported at all
        #   -- the harness then reads it as "no test ran" and calls the mutation a
        #   false red (measured 2026-10-03 on a pressure-task mutation).
        [sys.executable, "-m", "pytest", selector, "-q", "--tb=no", "-rfE", "-p", "no:cacheprovider"],
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
    install_restore_hooks()
    problems = preflight()
    if problems:
        print("refusing to measure: the tree does not match the mutation table")
        for problem in problems:
            print(f"  ! {problem}")
        print("put the files back (e.g. `git checkout -- <path>`) and run again")
        return 2
    failures: list[str] = []
    print(f"{'mutation':44} {'caught':7} red tests")
    print("-" * 120)
    for mutation in MUTATIONS:
        relative = mutation.relative_path
        path = resolve_path(relative)
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
            _RESTORE[path] = original_bytes
            path.write_text(original.replace(mutation.old, mutation.new), encoding="utf-8", newline="\n")
            caught, red = run_selector(mutation.selector)
        finally:
            path.write_bytes(original_bytes)
            _RESTORE.pop(path, None)
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
