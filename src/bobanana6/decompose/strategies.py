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

from collections.abc import Iterable, Mapping, Sequence

from ..contract import SOURCE_KIND_RESEARCH
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
from ..core.split import declared_layers
from .budget import READ_FRAME_CHARS_PER_LINE, BlockBudget, RoundBudget, can_refine

__all__ = [
    "LAYERED_SPLIT",
    "SIZE_AWARE_SPLIT",
    "STRATEGIES",
    "Decomposer",
    "declared_dependencies",
    "estimate_costs",
    "split_by_dependency",
    "split_by_field",
    "split_by_source",
    "split_read_then_product",
    "split_single",
]

STRATEGIES: tuple[str, ...] = ("source", "field", "module", "single")

#: ★ 层次切分的能力开关（反证用）：`False` = 忽略契约里的 `depends_on`，回到改动前的形状。
#: 它不是部署开关，而是把「层间可依赖」这件事做成**可开可关的一条能力** —— 这样
#: 「不修 → 稳定复现、修 → 消失」才能被同一条输入演示两次。读取点只有一个：
#: `Decomposer._build`（单点收口）。
LAYERED_SPLIT: bool = True

#: ★★ 按大小开并行的能力开关（反证用）：`False` = 忽略来源尺寸，读层回到
#: 「一份源一块」。与 `LAYERED_SPLIT` 同一族 —— 判据做成可开可关的一条能力，
#: 「关掉 ⇒ 块形状退回改动前」才能在**同一条输入**上被演示两次。读取点只有一个：
#: `split_read_then_product`（单点收口；`_build` 只负责选策略）。
SIZE_AWARE_SPLIT: bool = True


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


def declared_dependencies(contract: TaskContract) -> dict[str, tuple[str, ...]]:
    """`{产物名: 它声明的上游产物们}` —— 只收**非空**的声明。

    ★ 依赖信息只有一条来源：契约（`DeliverableSpec.depends_on`）。`decompose/` 不解析
    散文、不猜语言，也**不发明**依赖 —— 这样切分对同一份契约是逐字可复现的。
    一份声明都没有 ⇒ 空字典 ⇒ `Decomposer._build` 走原来的形状（这是 ④ 的回落）。
    """
    return {
        deliverable.name: tuple(deliverable.depends_on)
        for deliverable in contract.deliverables
        if deliverable.depends_on
    }


def _windows_for(line_chars: Sequence[int], budget: BlockBudget) -> list[tuple[int, int]]:
    """One source's lines -> maximal windows each within the block budget.

    Greedy, line-aligned, deterministic. **Line boundary is the repo's own
    convention**: `sources.read` is addressed by `offset`/`limit` in *lines*, so a
    window is the only cut a reader can actually re-request. Cutting at a character
    offset (or on a semantic boundary) would produce a range the read tool cannot
    express -- a declaration with no delivery path.

    The one window that can exceed the budget is a **single line** longer than the
    budget: it is emitted alone rather than dropped or split mid-line (the same case
    LangChain only logs a warning about; here it stays an explicit, bounded
    exception), and `_read_blocks` gives that source no other line.
    """
    windows: list[tuple[int, int]] = []
    start = 0
    total = len(line_chars)
    while start < total:
        end = start
        cost = 0
        while end < total:
            add = line_chars[end] + READ_FRAME_CHARS_PER_LINE
            # ★ the first line always goes in: there is no smaller cut on this axis.
            if end > start and cost + add > budget.max_chars:
                break
            cost += add
            end += 1
        windows.append((start, end))
        start = end
    return windows


def _read_blocks(
    keys: Sequence[str],
    lines_by_ref: Mapping[str, tuple[int, ...]],
    budget: BlockBudget,
) -> list[tuple[tuple[tuple[str, int, int], ...], tuple[str, ...]]]:
    """The read layer: one `(windows, covered refs)` entry per block.

    Two structural rules, both size-driven -- no "source count" proxy anywhere:

    ```
    一份源【超过】单块预算  ⇒ 按行切成多个窗口（每个窗口 <= 预算）
    多份源【都远小于】预算  ⇒ 合成一块（别为一份 1.8 KB 的清单开一个整块）
    ```

    Packing is **greedy first-fit in source order** (not size-descending bin packing):
    each piece goes into the first block it still fits, so a small source can join an
    earlier partly-filled block even when a full window sits between them. Determinism
    is total -- pieces follow `keys` order, blocks are scanned in creation order, and
    no `set`/`dict` iteration enters the decision. A source whose extent we were not
    told gets a whole-source block of its own and is never merged (we cannot bound
    what we cannot measure).
    """
    blocks: list[list[tuple[str, int, int]]] = []
    refs_by_block: list[list[str]] = []
    costs: list[int] = []

    def own_block(ref: str) -> None:
        blocks.append([])
        refs_by_block.append([ref])
        costs.append(budget.max_chars)

    for ref in keys:
        line_chars = lines_by_ref.get(ref)
        if not line_chars:
            # ★ no size fact (or an empty source) ⇒ own block, never merged.
            own_block(ref)
            continue
        for start, end in _windows_for(line_chars, budget):
            piece = (ref, start, end)
            piece_cost = budget.cost_of(line_chars[start:end])
            for index in range(len(blocks)):
                if costs[index] + piece_cost <= budget.max_chars:
                    blocks[index].append(piece)
                    if ref not in refs_by_block[index]:
                        refs_by_block[index].append(ref)
                    costs[index] += piece_cost
                    break
            else:
                blocks.append([piece])
                refs_by_block.append([ref])
                costs.append(piece_cost)
    return [(tuple(windows), tuple(refs)) for windows, refs in zip(blocks, refs_by_block, strict=True)]


def _read_goal(
    windows: Sequence[tuple[str, int, int]],
    refs: Sequence[str],
    lines_by_ref: Mapping[str, tuple[int, ...]],
) -> str:
    """The model-facing coverage line for one read block (lines are 1-based here)."""
    if not windows:
        return f"read source {refs[0]!r}"
    described = []
    for ref, start, end in windows:
        total = len(lines_by_ref[ref]) if ref in lines_by_ref else 0
        if start == 0 and end >= total:
            described.append(f"{ref!r} (all {total} lines)")
        else:
            described.append(f"{ref!r} lines {start + 1}-{end} of {total}")
    if len(described) == 1:
        return f"read source {described[0]}"
    return "read sources " + ", ".join(described)


def split_read_then_product(
    contract: TaskContract,
    ontology: Ontology,
    recon: Recon | None = None,
    *,
    round: int = 1,  # blame: fail-closed — 省略即第 1 轮，不隐式进入 refine
    version: int = 1,  # blame: fail-closed — 省略即 v1；计划版本只增不减
    block_budget: BlockBudget | None = None,  # blame: fail-closed — 缺省即"没声明单块容量"⇒不按大小拆/合，回落一份源一块
) -> SplitPlan:
    """★★ 读层 + 产物层**两层叠加** —— 而不是在两者里挑一个。

    ## 为什么必须两层（实测的账，不是推的）

    ```
    split_by_dependency   有产物依赖、★ 没有读层
                          ⇒ 每个产物块【自己扛全部源】
                          ⇒ 实测：10 份材料 290 KB ≈ 135K tokens
                          ⇒ 上下文爆 ⇒ 压缩 ⇒ ★ 忘了读过 ⇒ 重读
                          ⇒ 撞 max_restarts ⇒ restart_cap 判死

    split_by_source       有读层、★ 表达不了产物依赖
                          ⇒ "声明过的链在切分里蒸发"（`Decomposer.refine` 的顾虑）
    ```

    ## 形状

    ```
    读层   T-01..T-R     read <窗口>      →  part-<序号>.csv   ← 按【大小】切/合，互相零依赖
    产物层 T-(R+1)..     assemble <产物>  →  <产物>            ← 一块一份产物
                         inputs = 全部 part-*.csv + 自己声明的上游产物
    ```

    ★★ 读层的切口维度是**大小**，不是"源有几份"：
      超过单块预算的源按行拆成多块；多份远小于预算的源合成一块。
      `axis="window"` 就是这件事的名字（枚举里一直有它，此前没有实作）。

    ★ 产物块读的是**读层的产物**（小），不是原文（大）。
      产出块再也不需要自己扛全部材料 —— 上下文爆的那条路被切掉了。

    ★ 产物之间的 `depends_on` 照常接线（层号 = 依赖深度），所以
      "声明过的链"不会蒸发；同层产物块之间仍然零依赖、可并行。

    ★ 没有单块预算（`block_budget is None`）时**一个字节都不变**：读层回到
      「一份源一块」、`axis="source"`、`reads=()` —— 这就是反证那一臂的形状。
    """
    keys = _source_keys(contract, recon)
    lines_by_ref: Mapping[str, tuple[int, ...]] = recon.source_lines if recon is not None else {}
    # ★ 判据是"**有没有尺寸事实**"，不是"是不是研究题"：哪怕一条来源也没有尺寸，
    #   整层就回落 —— 一份源一块、`axis="source"`、`reads=()`（反证那一臂的形状）。
    sized = SIZE_AWARE_SPLIT and block_budget is not None and any(lines_by_ref.get(key) for key in keys)
    part_fields = _deliverable_fields(contract) or ("value",)
    interfaces: dict[str, str] = {}
    blocks: list[Subtask] = []
    parts: list[str] = []

    if sized:
        assert block_budget is not None  # narrowed by `sized`
        for index, (windows, refs) in enumerate(_read_blocks(keys, lines_by_ref, block_budget), start=1):
            # ★ 序号命名（不是 `part-<源>`）：一个块可能覆盖多个窗口 / 多份源，
            #   名字必须无歧义且唯一；覆盖范围由 `reads` 与 `goal` 声明并送进 brief。
            output = f"part-{index:02d}.csv"
            parts.append(output)
            interfaces[output] = _descriptor(part_fields)
            blocks.append(
                Subtask(
                    id=f"T-{index:02d}",
                    goal=_read_goal(windows, refs, lines_by_ref),
                    inputs=(),
                    output=output,
                    contract_ref=contract.task_id,
                    axis="window",
                    mode="read",
                    reads=windows,
                    output_fields=_mapped(ontology, part_fields),
                )
            )
    else:
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
    read_count = len(blocks)

    layers = declared_layers(contract)
    ordered = sorted(contract.deliverables, key=lambda deliverable: layers.get(deliverable.name, 0))  # blame: whitelist — 未声明依赖的产物在第 0 层（没有上游）；沿用既有口径
    for offset, deliverable in enumerate(ordered, start=1):
        fields = tuple(sorted(deliverable.schema)) or ("value",)
        interfaces[deliverable.name] = _descriptor(fields)
        upstream = tuple(deliverable.depends_on)
        # ★ 读层的全部产物 + 自己声明的上游产物。两者都要：
        #   少了前者，产出块没有材料可用；少了后者，声明的链会断。
        inputs = tuple(parts) + upstream
        blocks.append(
            Subtask(
                id=f"T-{read_count + offset:02d}",
                goal=f"assemble {deliverable.name!r}",
                inputs=inputs,
                output=deliverable.name,
                contract_ref=contract.task_id,
                axis="product",
                mode="write",
                partition=f"deliverable:{deliverable.name}",
                expects=tuple((name, interfaces[name]) for name in inputs),
                output_fields=_mapped(ontology, fields),
            )
        )
    return SplitPlan(
        task_id=contract.task_id,
        version=version,
        round=round,
        subtasks=tuple(blocks),
        shape="layered",
        interfaces=interfaces,
        recon_evidence=recon.evidence if recon is not None else "",
        costs=estimate_costs(len(blocks)),
    )


def _research_fanout(contract: TaskContract, recon: Recon | None) -> int:
    """研究来源**展开**出了几份材料 —— 只有这种源才需要垫读层。

    ## 为什么不能用"源数 ≥ 2"当判据（实测的账）

    一开始写的是 `len(_source_keys(...)) >= 2`，结果**打坏了既有任务**：
    13f 那种契约声明了产物链、也有 ≥2 个来源，于是被一并改成"读层 + 产物层"，
    `test_declared_dependencies_produce_a_layered_plan` 当场红
    （期望 `[(), ('a.csv',), …]`，实得 `[(), (), ('part-src-1.csv', …), …]`）。

    ★ 两者的差别是**源的来源**，不是源的个数：

    ```
    file 源    契约【写死】两三份，块直接读是对的 —— 它本来就知道自己要读哪份
    research   契约只写了一个【问题】，运行时才展开出 N 份 ⇒ 这才是可并行的独立文档
    ```

    ⇒ 判据：契约里有 `kind: research` 的来源，且它展开了 ≥2 份（`<ref>:1`…）。
    """
    expanded = {ref for ref in (recon.source_ids if recon is not None else ()) if ":" in ref}
    researched = {source.ref for source in contract.sources if source.kind == SOURCE_KIND_RESEARCH}
    return len({ref for ref in expanded if ref.split(":", 1)[0] in researched})


def split_by_dependency(
    contract: TaskContract,
    ontology: Ontology,
    recon: Recon | None = None,
    *,
    round: int = 1,
    version: int = 1,
) -> SplitPlan:
    """★ 层次切分：一份产物一块，按契约声明的 `depends_on` 接线。

    与 `split_single` 的唯一差别是 `inputs`：这里的输入是**上一层产物**，于是
    块与块之间真的有数据流（runner 把上游的 `piece` 递给下一块）。同层的块之间
    **没有**边 —— 层号就是依赖深度（`core/split.py::subtask_layers`），所以
    同一层永远可以并行、且每块只声明它真正需要的那几件上游（接口数 = 声明边数，
    不是 N(N-1)/2）。

    没有收缩成"只按声明的产物图切"的地方：**读源**（`split_by_source` 的读块）不在
    这张图里 —— 产物图是交付物之间的图，叶子产物自己去读它需要的源。这是刻意的：
    把"读哪个源"和"依赖哪份产物"混成一件事，会把两种不同的依赖塞进一个字段。

    形状声明为 `layered`（不是 `pipeline`）：`pipeline` 今天指"读层 → 写层"的两层
    扇出（写块之间零依赖），`layered` 才是真正逐层传动的那一种。两者都不许再含糊。

    ★ **块按拓扑序排**：runner 是普通 for（`runner.py:252`），按 `plan.subtasks` 顺序执行；
    契约里把下游写在前面是合法的（`depends_on` 不规定书写顺序），但那样排出来的计划
    跑不了（下游开工时输入还没产出）。所以这里按**声明层**稳定排序（同层保持契约顺序），
    `core/split.py::layer_wiring` 再把"顺序可执行"写成断言。
    """
    interfaces: dict[str, str] = {}
    for deliverable in contract.deliverables:
        fields = tuple(sorted(deliverable.schema)) or ("value",)
        interfaces[deliverable.name] = _descriptor(fields)
    layers = declared_layers(contract)
    ordered = sorted(contract.deliverables, key=lambda deliverable: layers.get(deliverable.name, 0))
    blocks: list[Subtask] = []
    for index, deliverable in enumerate(ordered, start=1):
        fields = tuple(sorted(deliverable.schema)) or ("value",)
        upstream = tuple(deliverable.depends_on)
        blocks.append(
            Subtask(
                id=f"T-{index:02d}",
                goal=f"produce {deliverable.name!r}",
                inputs=upstream,
                output=deliverable.name,
                contract_ref=contract.task_id,
                axis="product",
                mode="write",
                partition=f"deliverable:{deliverable.name}",
                expects=tuple((name, interfaces[name]) for name in upstream),
                output_fields=_mapped(ontology, fields),
            )
        )
    return SplitPlan(
        task_id=contract.task_id,
        version=version,
        round=round,
        subtasks=tuple(blocks),
        shape="layered",
        interfaces=interfaces,
        recon_evidence=recon.evidence if recon is not None else "",
        costs=estimate_costs(len(blocks)),
    )


class Decomposer:
    """Implements ``core.ports.DecomposerPort`` for the strategies that exist."""

    def __init__(
        self,
        *,
        strategy: str = "source",
        budget: RoundBudget | None = None,
        block_budget: BlockBudget | None = None,  # blame: fail-closed — 缺省即"调用方没声明单块容量"⇒读层回落一份源一块
    ) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}; expected one of {STRATEGIES}")
        self._strategy = strategy
        self._budget = budget or RoundBudget()
        self._block_budget = block_budget
        self._version = 0

    @property
    def strategy(self) -> str:
        return self._strategy

    @property
    def budget(self) -> RoundBudget:
        return self._budget

    @property
    def block_budget(self) -> BlockBudget | None:
        return self._block_budget

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
        # ★ 声明过的链不许在 refine 里蒸发：重切也走同一张产物图（`_build` 的收口点在
        #   这里补一份 —— 否则"第一次分层、第二次拍平"会是一条静默的退化路径）。
        if LAYERED_SPLIT and declared_dependencies(contract):
            return split_by_dependency(contract, ontology, round=plan.round + 1, version=self._version)
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
        # ★★ 层次切分：契约一旦声明了"产物依赖产物"，这条链就是任务自己给的切口，
        #   任何策略都不许把它丢掉 —— 所以在派发之前收口一次（单点判断）。没有声明
        #   （含单产物任务）时一个字节都不变，走下面的原策略。`LAYERED_SPLIT` 是反证开关。
        #
        #   ★★ 但"有产物链"和"有读层"是**两件事**，不是二选一（实测的账）：
        #     `split_by_dependency` 把叶子产物直接对着源 ⇒ 每个产物块自己扛全部源。
        #     源小而少时没事；**研究类任务源 = 290 KB** ⇒ 上下文爆 ⇒ 压缩 ⇒
        #     忘了读过 ⇒ 重读 ⇒ 撞 `max_restarts` 判死（真实读数：
        #     `.bobanana6/ab/` 那轮的 T-01，46 条 trace 里 11 轮重复同一批调用）。
        #   ⇒ 【研究来源展开出多份材料】时才两层叠加（那才是可并行的独立文档）。
        #     file 源是契约写死的两三份、块本来就该直接读 ⇒ 判据不算它，形状一字不变。
        if LAYERED_SPLIT and declared_dependencies(contract) and _research_fanout(contract, recon) >= 2:
            return split_read_then_product(
                contract,
                ontology,
                recon,
                round=round,
                version=version,
                block_budget=self._block_budget,
            )
        if LAYERED_SPLIT and declared_dependencies(contract):
            return split_by_dependency(contract, ontology, recon, round=round, version=version)
        if self._strategy == "source":
            return split_by_source(contract, ontology, recon, round=round, version=version)
        if self._strategy == "field":
            return split_by_field(contract, ontology, round=round, version=version)
        if self._strategy == "module":
            return split_by_module(contract, ontology, recon, round=round, version=version)
        return split_single(contract, ontology, round=round, version=version)
