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

from ..core.model import (
    Ontology,
    PlanCosts,
    Recon,
    SourceRef,
    SplitPlan,
    Subtask,
    TaskContract,
)
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

STRATEGIES: tuple[str, ...] = ("source", "field", "module", "single")


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
    """The sources the plan is cut over.

    ★ A06: when a survey is given it is authoritative -- it is the record of what was
    actually fetched. A block cut for a source nobody holds can only fail, and keeping
    it would turn "one input is missing" into "the whole run fails". With no survey
    (planning outside a run) the contract's own list is used.
    """
    if recon is not None and recon.source_ids:
        return tuple(sorted(recon.source_ids))
    return tuple(sorted({source.ref for source in contract.sources}))


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
    # ★ ③: 判据是【声明】：这些来源共享同一份状态，按来源扇出就是 fake parallelism。
    #   注意这不是"来源数不够"—— 5 个来源也照样回落（判据换掉了的证明）。
    #   ★ `#33`: 这一处判断与 split_by_module 共用（同一条判据只写一遍）。
    refusal = _shared_state_refusal(contract, ontology, round=round, version=version, axis="source")
    if refusal is not None:
        return refusal

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


def _shared_state_refusal(
    contract: TaskContract,
    ontology: Ontology,
    *,
    round: int,
    version: int,
    axis: str,
) -> SplitPlan | None:
    """★ 判据只有一条：契约声明"这些来源共享同一份状态" → 拒绝扇出、回落单块。

    ★ `#33`: 按来源与按模块两个策略**共用这一处** —— 判据写在两遍会让它自己的
    mutation 锚点不再唯一（`tools/reverse_validation.py` 的 preflight 会当场报），
    更糟的是两份判断迟早会长歪。
    """
    if contract.sources_share_state:
        return split_single(
            contract,
            ontology,
            round=round,
            version=version,
            refusal=(
                "the contract declares the sources share one state; "
                f"a split by {axis} would cut it"
            ),
        )
    return None


def _modules(contract: TaskContract) -> dict[str, tuple[SourceRef, ...]]:
    """`{模块名: 它的来源们}` —— 只认**声明**（`sources[].module`），不猜路径/语言。"""
    grouped: dict[str, list[SourceRef]] = {}
    for source in contract.sources:
        if source.module:
            grouped.setdefault(source.module, []).append(source)
    return {name: tuple(refs) for name, refs in grouped.items()}


def split_by_module(
    contract: TaskContract,
    ontology: Ontology,
    recon: Recon | None = None,
    *,
    round: int = 1,
    version: int = 1,
) -> SplitPlan:
    """★ `#33`: 一个**模块**一块（写区互斥），共享抽象那一块**排在最前**。

    与 `split_by_source` 的差别就是 §15.5 要的那个轴：切口落在**模块边界**上，而不是
    "一个来源一块"。模块由契约声明（`sources[].module`），壳体不去解析路径猜包结构。

    约定（写在这里，别处不重复）：**契约的第一份交付物 = 共享抽象**。
    它先被实现（T-01），模块块**把它声明成自己的输入**（"接口先冻结、再实现"），
    再各自改自己模块内的文件 —— 所以每块只改自己写区里的那几个文件。

    ★ 全局测试挂在本轮**最后一块**上：那时所有模块都改完了，"块内过 ≠ 整体过"才有意义。

    ★ 少于两个模块 → 回落单块（切口不存在，不硬拆 —— 这就是 §15.5 反例形状的挡板）。
    """
    modules = _modules(contract)
    if len(modules) < 2:
        return split_single(contract, ontology, round=round, version=version)
    refusal = _shared_state_refusal(contract, ontology, round=round, version=version, axis="module")
    if refusal is not None:
        return refusal
    if not contract.deliverables:
        return split_single(contract, ontology, round=round, version=version)

    declared = {deliverable.name: deliverable for deliverable in contract.deliverables}
    shared = contract.deliverables[0]
    #: ★ 约定之二：**第二份交付物 = 冻结的接口清单**（先定，再实现）。没有就只出两层。
    #: 但它必须**【不是】任何模块的产出** —— 否则接口块会和模块块抢同一个 output
    #: （`duplicate_output` 会当场拒绝；那说明契约里根本没有独立的接口产物）。
    module_locations = {refs[0].locator for refs in modules.values() if refs}
    interface_deliverable = None
    if len(contract.deliverables) > 1 and contract.deliverables[1].name not in module_locations:
        interface_deliverable = contract.deliverables[1]

    def fields_of(name: str) -> tuple[str, ...]:
        """这一块产出那份交付物自己的字段 —— 不是全部交付物字段的并集。"""
        spec = declared.get(name)
        return tuple((spec.schema if spec is not None else None) or {"text": "str"})

    interfaces: dict[str, str] = {}
    blocks: list[Subtask] = []
    if interface_deliverable is not None:
        # ① 接口先冻结：这一块把冻结的接口读成数据，后面每一块都拿它当输入。
        interfaces[interface_deliverable.name] = _descriptor(fields_of(interface_deliverable.name))
        blocks.append(
            Subtask(
                id="T-01",
                goal=f"freeze the interface {interface_deliverable.name!r} the implementation must match",
                inputs=(),
                output=interface_deliverable.name,
                contract_ref=contract.task_id,
                axis="module",
                mode="write",
                partition=f"deliverable:{interface_deliverable.name}",
                output_fields=_mapped(ontology, fields_of(interface_deliverable.name)),
            )
        )
    frozen_input = (interface_deliverable.name,) if interface_deliverable is not None else ()
    shared_id = f"T-{len(blocks) + 1:02d}"
    interfaces[shared.name] = _descriptor(fields_of(shared.name))
    blocks.append(
        Subtask(
            id=shared_id,
            goal=f"implement the shared abstraction {shared.name!r} the modules will delegate to",
            inputs=frozen_input,
            output=shared.name,
            contract_ref=contract.task_id,
            axis="module",
            mode="write",
            partition=f"deliverable:{shared.name}",
            expects=tuple((name, interfaces[name]) for name in frozen_input),
            output_fields=_mapped(ontology, fields_of(shared.name)),
        )
    )
    module_inputs = (*frozen_input, shared.name)
    module_outputs: list[str] = []
    for index, name in enumerate(sorted(modules), start=len(blocks) + 1):
        refs = modules[name]
        # 模块的产出 = 它第一份来源的**路径**（工作区相对）—— 与写区、交付物名字同一套。
        output = refs[0].locator or f"{name}.source"
        module_outputs.append(output)
        interfaces[output] = _descriptor(("text",))
        blocks.append(
            Subtask(
                id=f"T-{index:02d}",
                goal=f"converge module {name!r} onto the shared abstraction",
                # ★ 模块块**声明共享抽象（和冻结的接口）为输入** —— 接口因此真的被送到
                #   它手上，而不是"库文件恰好在工作区里"。
                inputs=module_inputs,
                output=output,
                contract_ref=contract.task_id,
                axis="module",
                mode="write",
                # ★ 写区互斥：partition 就是模块本身 —— 同级写者不会共享它。
                partition=f"module:{name}",
                expects=tuple((item, interfaces[item]) for item in module_inputs),
                output_fields=_mapped(ontology, fields_of(output)),
            )
        )
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


def split_single(
    contract: TaskContract,
    ontology: Ontology,
    *,
    round: int = 1,
    version: int = 1,
    refusal: str = "",
) -> SplitPlan:
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
        refusal=refusal,
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
        if self._strategy == "module":
            return split_by_module(contract, ontology, recon, round=round, version=version)
        return split_single(contract, ontology, round=round, version=version)
