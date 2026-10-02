"""Split strategies: produce a ``SplitPlan`` that ``core/split.py`` accepts.

This is not "write some strategy" -- it is "emit a plan that passes the eleven
legality checks". The relevant ones, and how the shapes here satisfy them:

  * ``star``             a ``star`` plan carries no block-to-block inputs; a plan
                         with a merge layer is declared ``pipeline`` instead.
  * ``iface_complete``   every output and every input has an entry in ``interfaces``.
  * ``iface_match``      each consumer's ``expects`` equals the producer's entry.
  * ``verifiable``       the compiler adds a per-block constraint (``contract``).
  * ``axis_uniform``     one axis per dependency layer.
  * ``no_orphan``        every output is either a deliverable or consumed.
  * ``write_serial``     same-layer writers get distinct partitions.
  * ``merge_cheap``      ``estimate_costs`` keeps merge/execute well under 0.5.
  * ``concept_mapped``   output fields are mapped through the ontology.
  * ``round_budget``     ``RoundBudget`` caps refinement at ``MAX_ROUNDS``.
  * ``semantics_frozen`` the caller freezes the ontology; the plan only cites it.

Only the dimensions with real input exist: ``source`` (from the contract/recon)
and ``field`` (from the deliverable schemas). ``entity`` / ``window`` / ``module``
are valid ``Subtask.axis`` values but have no data source in the model yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ..core.model import Ontology, PlanCosts, Recon, SplitPlan, Subtask, TaskContract
from ..core.ontology import map_field
from .budget import RoundBudget, can_refine

__all__ = [
    "STRATEGIES",
    "Decomposer",
    "estimate_costs",
    "split_by_field",
    "split_by_source",
    "split_single",
]

STRATEGIES: tuple[str, ...] = ("source", "field", "single")


def estimate_costs(block_count: int) -> PlanCosts:
    """A transparent first-order estimate: execution ~ per-block work, merge ~ joins.

    The constants are arbitrary but fixed; what matters is that they are declared
    at all (``core/split.py::merge_cheap`` refuses an undeclared estimate rather
    than assuming the cut is cheap).
    """
    blocks = max(1, block_count)
    return PlanCosts(merge=float(max(0, blocks - 1)), execute=float(10 * blocks))


def _mapped(ontology: Ontology, fields: Iterable[str]) -> tuple[str, ...]:
    """Field names -> concept ids where the ontology covers them.

    An unmapped field keeps its raw name, so ``concept_mapped`` still flags it
    instead of the strategy silently dropping it.
    """
    return tuple(map_field(name, ontology) or name for name in sorted(set(fields)))


def _deliverable_fields(contract: TaskContract) -> tuple[str, ...]:
    return tuple(sorted({name for deliverable in contract.deliverables for name in deliverable.schema}))


def _descriptor(fields: Iterable[str]) -> str:
    names = sorted(set(fields))
    return "schema:" + "|".join(names) if names else "schema:empty"


def _source_keys(contract: TaskContract, recon: Recon | None) -> tuple[str, ...]:
    keys = {source.ref for source in contract.sources}
    if recon is not None:
        keys |= set(recon.source_ids)
    return tuple(sorted(keys))


def _merge_blocks(
    contract: TaskContract,
    ontology: Ontology,
    parts: Sequence[str],
    interfaces: dict[str, str],
    *,
    first_id: int,
    axis: str,
) -> list[Subtask]:
    blocks: list[Subtask] = []
    for offset, deliverable in enumerate(contract.deliverables):
        fields = tuple(sorted(deliverable.schema)) or ("value",)
        interfaces[deliverable.name] = _descriptor(fields)
        blocks.append(
            Subtask(
                id=f"T-{first_id + offset:02d}",
                goal=f"assemble {deliverable.name!r}",
                inputs=tuple(parts),
                output=deliverable.name,
                contract_ref=contract.task_id,
                axis=axis,
                mode="write",
                partition=f"deliverable:{deliverable.name}",
                expects=tuple((name, interfaces[name]) for name in parts),
                output_fields=_mapped(ontology, fields),
            )
        )
    return blocks


def split_by_source(
    contract: TaskContract,
    ontology: Ontology,
    recon: Recon | None = None,
    *,
    round: int = 1,
    version: int = 1,
) -> SplitPlan:
    """One read block per source, then one write block per deliverable."""
    keys = _source_keys(contract, recon)
    if len(keys) < 2:
        return split_single(contract, ontology, round=round, version=version)

    part_fields = _deliverable_fields(contract) or ("value",)
    interfaces: dict[str, str] = {}
    blocks: list[Subtask] = []
    parts: list[str] = []
    for index, key in enumerate(keys, start=1):
        output = f"part-{key}.csv"
        parts.append(output)
        interfaces[output] = _descriptor(part_fields)
        blocks.append(
            Subtask(
                id=f"T-{index:02d}",
                goal=f"read source {key!r}",
                inputs=(),
                output=output,
                contract_ref=contract.task_id,
                axis="source",
                mode="read",
                output_fields=_mapped(ontology, part_fields),
            )
        )
    blocks.extend(_merge_blocks(contract, ontology, parts, interfaces, first_id=len(keys) + 1, axis="source"))
    return SplitPlan(
        task_id=contract.task_id,
        version=version,
        round=round,
        subtasks=tuple(blocks),
        shape="pipeline",
        interfaces=interfaces,
        recon_evidence=recon.evidence if recon is not None else "",
        costs=estimate_costs(len(blocks)),
    )


def split_by_field(
    contract: TaskContract,
    ontology: Ontology,
    *,
    round: int = 1,
    version: int = 1,
    fields: Iterable[str] | None = None,
) -> SplitPlan:
    """One read block per field, then one write block per deliverable."""
    all_fields = tuple(sorted(set(_deliverable_fields(contract)) | set(fields or ())))
    if len(all_fields) < 2:
        return split_single(contract, ontology, round=round, version=version)

    interfaces: dict[str, str] = {}
    blocks: list[Subtask] = []
    parts: list[str] = []
    for index, field in enumerate(all_fields, start=1):
        output = f"part-{field}.csv"
        parts.append(output)
        interfaces[output] = _descriptor((field,))
        blocks.append(
            Subtask(
                id=f"T-{index:02d}",
                goal=f"extract field {field!r}",
                inputs=(),
                output=output,
                contract_ref=contract.task_id,
                axis="field",
                mode="read",
                output_fields=_mapped(ontology, (field,)),
            )
        )
    blocks.extend(_merge_blocks(contract, ontology, parts, interfaces, first_id=len(all_fields) + 1, axis="field"))
    return SplitPlan(
        task_id=contract.task_id,
        version=version,
        round=round,
        subtasks=tuple(blocks),
        shape="pipeline",
        interfaces=interfaces,
        costs=estimate_costs(len(blocks)),
    )


def split_single(contract: TaskContract, ontology: Ontology, *, round: int = 1, version: int = 1) -> SplitPlan:
    """No fan-out: one write block per deliverable, all at the same layer (star)."""
    interfaces: dict[str, str] = {}
    blocks: list[Subtask] = []
    for index, deliverable in enumerate(contract.deliverables, start=1):
        fields = tuple(sorted(deliverable.schema)) or ("value",)
        interfaces[deliverable.name] = _descriptor(fields)
        blocks.append(
            Subtask(
                id=f"T-{index:02d}",
                goal=f"produce {deliverable.name!r}",
                inputs=(),
                output=deliverable.name,
                contract_ref=contract.task_id,
                axis="source",
                mode="write",
                partition=f"deliverable:{deliverable.name}",
                output_fields=_mapped(ontology, fields),
            )
        )
    return SplitPlan(
        task_id=contract.task_id,
        version=version,
        round=round,
        subtasks=tuple(blocks),
        shape="star",
        interfaces=interfaces,
        costs=estimate_costs(len(blocks)),
    )


class Decomposer:
    """Implements ``core.ports.DecomposerPort`` for the strategies that exist."""

    def __init__(self, *, strategy: str = "source", budget: RoundBudget | None = None) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}; expected one of {STRATEGIES}")
        self._strategy = strategy
        self._budget = budget or RoundBudget()
        self._version = 0

    @property
    def strategy(self) -> str:
        return self._strategy

    @property
    def budget(self) -> RoundBudget:
        return self._budget

    def split(self, contract: TaskContract, ontology: Ontology, recon: Recon | None = None) -> SplitPlan:
        self._version += 1
        return self._build(contract, ontology, recon, round=1, version=self._version)

    def refine(self, contract: TaskContract, ontology: Ontology, plan: SplitPlan, outcome) -> SplitPlan:
        """Refine when the ceiling allows; otherwise return the plan unchanged.

        ★ Returning the plan unchanged is the point: an uncapped refine would
        refund the iteration budget forever.
        """
        if not can_refine(plan, self._budget):
            return plan
        self._version += 1
        failed = tuple(sorted(set(getattr(outcome, "failed_blocks", ()))))
        fields = tuple(sorted({field for block in plan.subtasks if block.id in failed for field in block.output_fields}))
        return split_by_field(
            contract,
            ontology,
            round=plan.round + 1,
            version=self._version,
            fields=fields or None,
        )

    def _build(self, contract: TaskContract, ontology: Ontology, recon: Recon | None, *, round: int, version: int) -> SplitPlan:
        if self._strategy == "source":
            return split_by_source(contract, ontology, recon, round=round, version=version)
        if self._strategy == "field":
            return split_by_field(contract, ontology, round=round, version=version)
        return split_single(contract, ontology, round=round, version=version)
