"""decompose/: round budget + strategies that produce a plan core/split.py accepts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bobanana6.contract import compile_bundle, parse_contract
from bobanana6.core.model import AssemblyOutcome, Recon
from bobanana6.core.split import check_split, subtask_layers
from bobanana6.decompose import (
    SIZE_AWARE_SPLIT,
    BlockBudget,
    Decomposer,
    RoundBudget,
    RoundBudgetExceeded,
    can_refine,
    estimate_costs,
    split_by_field,
    split_by_source,
    split_single,
)
from bobanana6.ontology import load_ontology

ONTOLOGY = """
domain: credit-risk
version: "2026.1"
concepts:
  pd: {label: PD, kind: measure, aliases: [pd_3y]}
  amount: {label: Amount, kind: measure}
units:
  amount: {symbol: CNY, scale: 1.0, base: CNY}
"""

CONTRACT = """
task_id: task-1
goal: build the credit panel
deliverables:
  - {name: out.csv, schema: {pd: float, amount: float}}
sources:
  - {ref: src-1}
  - {ref: src-2}
  - {ref: src-3}
constraints:
  hard:
    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "the panel must not be empty"}
"""


def ontology():
    return load_ontology(ONTOLOGY).freeze()


def contract():
    return parse_contract(CONTRACT).contract


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #


def test_round_budget_validation_and_ceiling() -> None:
    with pytest.raises(ValueError):
        RoundBudget(max_rounds=-1)
    budget = RoundBudget(max_rounds=3)
    assert budget.allows(3) and budget.allows(1)
    assert not budget.allows(4)
    assert budget.exhausted(3) and not budget.exhausted(2)
    assert budget.next_round(1) == 2
    with pytest.raises(RoundBudgetExceeded):
        budget.next_round(3)


def test_can_refine_uses_the_ceiling() -> None:
    from bobanana6.decompose import split_single as build

    plan = build(contract(), ontology())
    assert can_refine(plan, RoundBudget(max_rounds=3)) is True
    assert can_refine(plan, RoundBudget(max_rounds=1)) is False


def test_cost_estimate_keeps_merge_cheap() -> None:
    for blocks in range(1, 12):
        costs = estimate_costs(blocks)
        assert costs.execute > 0
        assert costs.merge / costs.execute < 0.5


# --------------------------------------------------------------------------- #
# strategies
# --------------------------------------------------------------------------- #


def test_source_strategy_reads_each_source_then_merges() -> None:
    plan = split_by_source(contract(), ontology())
    reads = [block for block in plan.subtasks if block.mode == "read"]
    writes = [block for block in plan.subtasks if block.mode == "write"]
    assert len(reads) == 3 and len(writes) == 1
    assert {block.axis for block in plan.subtasks} == {"source"}
    assert plan.shape == "pipeline"
    assert plan.interfaces["out.csv"] == "schema:amount|pd"
    # every consumer expectation matches the declared interface
    merge = writes[0]
    assert dict(merge.expects) == {name: plan.interfaces[name] for name in merge.inputs}
    # same-layer writers are partition-isolated
    assert len({block.partition for block in writes}) == len(writes)


def test_source_strategy_falls_back_to_single_for_one_source() -> None:
    one = parse_contract(CONTRACT.replace("  - {ref: src-2}\n", "").replace("  - {ref: src-3}\n", "")).contract
    plan = split_by_source(one, ontology())
    assert plan.shape == "star"
    assert len(plan.subtasks) == 1


def test_field_strategy_splits_per_field() -> None:
    plan = split_by_field(contract(), ontology())
    reads = [block for block in plan.subtasks if block.mode == "read"]
    assert [block.output for block in reads] == ["part-amount.csv", "part-pd.csv"]
    assert {block.axis for block in plan.subtasks} == {"field"}
    assert all(block.output_fields for block in reads)


def test_single_strategy_produces_one_write_per_deliverable() -> None:
    text = CONTRACT.replace(
        "  - {name: out.csv, schema: {pd: float, amount: float}}",
        "  - {name: out.csv, schema: {pd: float, amount: float}}\n  - {name: other.csv, schema: {pd: float}}",
    )
    plan = split_single(parse_contract(text).contract, ontology())
    assert plan.shape == "star"
    assert [block.mode for block in plan.subtasks] == ["write", "write"]
    assert len({block.partition for block in plan.subtasks}) == 2
    assert set(plan.interfaces) == {"out.csv", "other.csv"}


def test_two_same_layer_writers_get_distinct_partitions() -> None:
    """``write_serial`` is a real gate, so it needs a real two-writer layer."""
    text = CONTRACT.replace(
        "  - {name: out.csv, schema: {pd: float, amount: float}}",
        "  - {name: out.csv, schema: {pd: float, amount: float}}\n  - {name: other.csv, schema: {pd: float}}",
    )
    document = parse_contract(text)
    plan = split_by_source(document.contract, ontology(), Recon(source_ids=("src-1", "src-2")))
    writes = [block for block in plan.subtasks if block.mode == "write"]
    assert len(writes) == 2
    assert len({block.partition for block in writes}) == 2

    bundle = compile_bundle(document, plan)
    collapsed = replace(
        plan,
        subtasks=tuple(
            replace(block, partition="deliverable:all") if block.mode == "write" else block
            for block in plan.subtasks
        ),
    )
    assert "write_serial" in check_split(collapsed, document.contract, bundle, ontology()).codes()


def test_decomposer_rejects_an_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        Decomposer(strategy="entity")


def test_decomposer_dispatches_every_declared_strategy() -> None:
    """``STRATEGIES`` is the public surface; each name must reach its own builder."""
    field_plan = Decomposer(strategy="field").split(contract(), ontology())
    assert {block.axis for block in field_plan.subtasks} == {"field"}
    assert field_plan.shape == "pipeline"

    single_plan = Decomposer(strategy="single").split(contract(), ontology())
    assert single_plan.shape == "star"
    assert all(block.mode == "write" for block in single_plan.subtasks)


def test_decomposer_versions_increment() -> None:
    decomposer = Decomposer(strategy="source")
    first = decomposer.split(contract(), ontology(), Recon(source_ids=("src-1", "src-2")))
    second = decomposer.split(contract(), ontology(), Recon(source_ids=("src-1", "src-2")))
    assert first.version == 1 and second.version == 2


def test_refine_is_capped_by_the_round_budget() -> None:
    decomposer = Decomposer(strategy="source", budget=RoundBudget(max_rounds=1))
    plan = decomposer.split(contract(), ontology())
    assert plan.round == 1
    refined = decomposer.refine(contract(), ontology(), plan, AssemblyOutcome(passed=False, failed_blocks=("T-01",)))
    assert refined is plan  # ★ the ceiling returns the plan unchanged, no unbounded loop


def test_refine_advances_when_the_budget_allows() -> None:
    decomposer = Decomposer(strategy="source", budget=RoundBudget(max_rounds=3))
    plan = decomposer.split(contract(), ontology())
    refined = decomposer.refine(contract(), ontology(), plan, AssemblyOutcome(passed=False, failed_blocks=("T-01",)))
    assert refined.round == 2
    assert {block.axis for block in refined.subtasks} == {"field"}


# --------------------------------------------------------------------------- #
# acceptance #7: does the emitted plan pass core/split.py?
# --------------------------------------------------------------------------- #


def test_plan_from_strategy_passes_core_split() -> None:
    document = parse_contract(CONTRACT)
    plan = split_by_source(document.contract, ontology(), Recon(source_ids=("src-1", "src-2", "src-3"), evidence="surveyed 3 sources"))
    bundle = compile_bundle(document, plan)
    report = check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash())
    assert report.ok is True
    assert report.codes() == ()


def test_split_still_catches_a_broken_variant_of_that_plan() -> None:
    """The plan passes because it is legal, not because the check is asleep."""
    document = parse_contract(CONTRACT)
    plan = split_by_source(document.contract, ontology(), Recon(source_ids=("src-1", "src-2", "src-3")))
    bundle = compile_bundle(document, plan)

    assert "round_budget" in check_split(replace(plan, round=4), document.contract, bundle, ontology()).codes()
    assert "empty_plan" in check_split(replace(plan, subtasks=()), document.contract, bundle, ontology()).codes()
    duplicated = replace(plan, subtasks=(plan.subtasks[0], plan.subtasks[0]))
    assert "duplicate_id" in check_split(duplicated, document.contract, bundle, ontology()).codes()


def test_an_unmapped_deliverable_field_survives_as_its_raw_name() -> None:
    """★ The strategy must not drop a field the ontology cannot map -- it must
    hand the raw name on so ``split.concept_mapped`` can flag it."""
    text = CONTRACT.replace("schema: {pd: float, amount: float}", "schema: {pd: float, amount: float, mystery: str}")
    document = parse_contract(text)
    plan = split_by_source(document.contract, ontology(), Recon(source_ids=("src-1", "src-2")))
    merge = next(block for block in plan.subtasks if block.mode == "write")
    assert "mystery" in merge.output_fields
    assert "pd" in merge.output_fields

    bundle = compile_bundle(document, plan)
    assert "concept_mapped" in check_split(plan, document.contract, bundle, ontology()).codes()


def test_an_unmapped_output_field_is_flagged_by_split() -> None:
    """concept_mapped is a real gate even though the strategy maps what it can."""
    text = CONTRACT.replace("  - {ref: src-3}\n", "")
    document = parse_contract(text)
    plan = split_by_source(document.contract, ontology())
    bundle = compile_bundle(document, plan)
    broken = replace(plan, subtasks=tuple(replace(block, output_fields=("ghost_field",)) for block in plan.subtasks))
    assert "concept_mapped" in check_split(broken, document.contract, bundle, ontology()).codes()


# --------------------------------------------------------------------------- #
# ★ 层次切分：契约声明产物依赖 -> 一份产物一块、按声明接线
# --------------------------------------------------------------------------- #

AB = Path(__file__).resolve().parents[1] / "acceptance" / "ab" / "13f-crowding"

CHAIN = """
task_id: task-chain
goal: a chain of products
version: 1
deliverables:
  - {name: a.csv, schema: {pd: float}}
  - {name: b.csv, schema: {pd: float}, depends_on: [a.csv]}
  - {name: c.csv, schema: {pd: float}, depends_on: [b.csv]}
  - {name: d.csv, schema: {pd: float}, depends_on: [b.csv]}
  - {name: e.csv, schema: {pd: float}, depends_on: [c.csv, d.csv]}
sources:
  - {ref: src-1}
  - {ref: src-2}
constraints:
  hard:
    - {id: C-1, kind: presence, target: a.csv, spec: {min_rows: 1}, rationale: "x"}
"""


def _chain_plan(strategy: str = "field"):
    document = parse_contract(CHAIN)
    plan = Decomposer(strategy=strategy).split(document.contract, ontology())
    bundle = compile_bundle(document, plan)
    report = check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash())
    return plan, report


def test_declared_dependencies_produce_a_layered_plan() -> None:
    plan, report = _chain_plan()
    assert report.ok, report.codes()
    assert plan.shape == "layered"
    assert [block.inputs for block in plan.subtasks] == [
        (),
        ("a.csv",),
        ("b.csv",),
        ("b.csv",),
        ("c.csv", "d.csv"),
    ]
    assert {block.axis for block in plan.subtasks} == {"product"}
    assert len({block.partition for block in plan.subtasks}) == len(plan.subtasks)


def test_declared_dependencies_beat_the_selected_strategy() -> None:
    """声明了链，`source` 策略也不许把它拆成"读层 + 写层"的零依赖扇出。"""
    source_plan, report = _chain_plan(strategy="source")
    assert report.ok, report.codes()
    assert source_plan.shape == "layered"
    assert source_plan.subtasks[1].inputs == ("a.csv",)


def test_the_layered_capability_can_be_switched_off() -> None:
    """★ 反证：同一条输入，把「层间可依赖」关掉 ⇒ inputs 回到全空。"""
    from bobanana6.decompose import strategies

    document = parse_contract(CHAIN)
    on = Decomposer(strategy="field").split(document.contract, ontology())
    assert any(block.inputs for block in on.subtasks)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(strategies, "LAYERED_SPLIT", False)
        off = Decomposer(strategy="field").split(document.contract, ontology())
    finally:
        monkey.undo()
    assert off.shape == "star"
    assert all(not block.inputs for block in off.subtasks)


def test_the_layered_capability_is_read_at_call_time() -> None:
    from bobanana6.decompose import strategies as strategies_module

    assert strategies_module.LAYERED_SPLIT is True  # the gate `_build` reads


def test_a_chain_without_declarations_is_unchanged() -> None:
    """④ 反向：产物彼此独立 ⇒ 原样 star，一个 input 都不许多出来。"""
    text = CHAIN.replace(", depends_on: [a.csv]", "").replace(", depends_on: [b.csv]", "").replace(
        ", depends_on: [c.csv, d.csv]", ""
    )
    document = parse_contract(text)
    plan = Decomposer(strategy="field").split(document.contract, ontology())
    assert plan.shape == "star"
    assert all(block.inputs == () for block in plan.subtasks)
    # 逐字等于改动前的 split_single
    baseline = split_single(document.contract, ontology())
    assert plan.subtasks == baseline.subtasks
    assert plan.shape == baseline.shape


def test_a_single_deliverable_is_unchanged() -> None:
    """④ 单产物任务逐字不变：仍然按字段扇出（field 策略的老行为），没有多出任何输入。"""
    single = parse_contract(CONTRACT).contract
    plan = Decomposer(strategy="field").split(single, ontology())
    baseline = split_by_field(single, ontology())
    assert plan.shape == baseline.shape == "pipeline"
    assert plan.subtasks == baseline.subtasks
    assert all(block.inputs == () for block in plan.subtasks if block.mode == "read")


def _linear_chain(length: int) -> str:
    rows = ["  - {name: p00.csv, schema: {pd: float}}"]
    for index in range(1, length):
        rows.append(f"  - {{name: p{index:02d}.csv, schema: {{pd: float}}, depends_on: [p{index - 1:02d}.csv]}}")
    return (
        "task_id: task-linear\ngoal: a linear chain\nversion: 1\ndeliverables:\n"
        + "\n".join(rows)
        + "\n"
    )


@pytest.mark.parametrize("length", [3, 8, 20])
def test_chain_lengths_layer_correctly(length: int) -> None:
    document = parse_contract(_linear_chain(length))
    plan = Decomposer(strategy="field").split(document.contract, ontology())
    depth, cycle = subtask_layers(plan)
    assert cycle == ()
    assert plan.shape == "layered"
    assert [depth[block.id] for block in plan.subtasks] == list(range(length))
    for index, block in enumerate(plan.subtasks):
        expected = () if index == 0 else (f"p{index - 1:02d}.csv",)
        assert block.inputs == expected
    bundle = compile_bundle(document, plan)
    report = check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash())
    assert report.ok, report.codes()


def test_a_wide_chain_only_declares_its_own_edges() -> None:
    """§2.4 的接口数算术：链长 8、每层宽 2。

    两条并肩的链（每块只声明自己那条上游）：层内零边、层间 2 条/层 ⇒ 14 条。
    同一张图若"把上层全部产物都当输入"，每块 2 条 ⇒ 28 条；全平铺则 120 条。
    14/28 都是 O(N·w)，120 是 O(N²) —— 纪律把接口数压在层量级。
    """
    rows = []
    for layer in range(8):
        for lane in range(2):
            name = f"L{layer}N{lane}.csv"
            if layer == 0:
                rows.append(f"  - {{name: {name}, schema: {{pd: float}}}}")
            else:
                rows.append(
                    f"  - {{name: {name}, schema: {{pd: float}}, depends_on: [L{layer - 1}N{lane}.csv]}}"
                )
    document = parse_contract("task_id: task-wide\ngoal: wide\ndeliverables:\n" + "\n".join(rows) + "\n")
    plan = Decomposer(strategy="field").split(document.contract, ontology())
    blocks = len(plan.subtasks)
    edges = sum(len(block.inputs) for block in plan.subtasks)
    assert blocks == 16
    assert edges == 14  # 7 transitions x 2 lanes
    assert blocks * (blocks - 1) // 2 == 120  # the flat-cut灾难 this avoids
    bundle = compile_bundle(document, plan)
    assert check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash()).ok


def test_the_13f_contract_now_declares_its_chain() -> None:
    """★ 反证：13f 的 8 个产物 + 真实链关系 —— 逐条贴出的读数就在这里。"""
    from bobanana6.ontology import load_ontology_file

    document = parse_contract((AB / "task.yaml").read_text(encoding="utf-8"))
    onto = load_ontology_file(AB / "ontology.yaml").freeze()
    plan = Decomposer(strategy="field").split(document.contract, onto)
    assert plan.shape == "layered"
    assert all(block.inputs for block in plan.subtasks if block.output != "output/filing_resolution.csv")
    depth, cycle = subtask_layers(plan)
    assert cycle == ()
    assert [depth[block.id] for block in plan.subtasks] == [0, 1, 2, 2, 2, 2, 2, 3]
    bundle = compile_bundle(document, plan)
    assert check_split(plan, document.contract, bundle, onto, frozen_hash=onto.content_hash()).ok


def test_refine_keeps_the_declared_chain() -> None:
    """重切也不许把链拍平：refine 走同一张产物图。"""
    document = parse_contract(CHAIN)
    decomposer = Decomposer(strategy="source", budget=RoundBudget(max_rounds=3))
    plan = decomposer.split(document.contract, ontology())
    refined = decomposer.refine(
        document.contract, ontology(), plan, AssemblyOutcome(passed=False, failed_blocks=("T-02",))
    )
    assert refined.shape == "layered"
    assert refined.round == 2
    assert refined.subtasks[1].inputs == ("a.csv",)


def test_a_contract_written_out_of_order_is_still_emitted_in_topological_order() -> None:
    """契约把下游写在前面是合法的书写顺序，但计划必须排成可执行的顺序。"""
    text = (
        "task_id: t-reordered\ngoal: g\ndeliverables:\n"
        "  - {name: summary.csv, schema: {pd: float}, depends_on: [b.csv]}\n"
        "  - {name: b.csv, schema: {pd: float}, depends_on: [a.csv]}\n"
        "  - {name: a.csv, schema: {pd: float}}\n"
    )
    document = parse_contract(text)
    plan = Decomposer(strategy="field").split(document.contract, ontology())
    assert [block.output for block in plan.subtasks] == ["a.csv", "b.csv", "summary.csv"]
    assert [block.inputs for block in plan.subtasks] == [(), ("a.csv",), ("b.csv",)]
    depth, _ = subtask_layers(plan)
    assert [depth[block.id] for block in plan.subtasks] == [0, 1, 2]
    bundle = compile_bundle(document, plan)
    assert check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash()).ok


# --------------------------------------------------------------------------- #
# ★★ 按大小开并行：一份源超预算 ⇒ 拆窗口；多份小源 ⇒ 合成一块
# --------------------------------------------------------------------------- #

#: 一道最小的"研究来源展开出多份材料"的题：`hunt` 在运行时展开成 `hunt:1`…
RESEARCH = """
task_id: research-1
goal: survey the field
deliverables:
  - {name: notes.csv, schema: {pd: float}}
  - {name: summary.csv, schema: {pd: float}, depends_on: [notes.csv]}
sources:
  - {ref: instr}
  - {ref: hunt, kind: research, question: "which vendors matter", seeds: ["https://a.example"]}
"""


def research_contract():
    return parse_contract(RESEARCH).contract


def research_recon(source_lines=None):
    return Recon(
        evidence="probe",
        source_ids=("hunt", "hunt:1", "hunt:2", "instr"),
        source_lines=source_lines if source_lines is not None else {},
    )


def _small_lines():
    # `hunt:1` (200 short lines) is far over a 1000-char budget; the other three fit.
    return {
        "hunt": (10,) * 5,
        "hunt:1": (5,) * 200,
        "hunt:2": (10,) * 4,
        "instr": (10,) * 6,
    }


def _read_blocks(plan):
    return [block for block in plan.subtasks if block.mode == "read"]


def _cost(block, lines, budget: BlockBudget) -> int:
    return sum(budget.cost_of(lines[ref][start:end]) for ref, start, end in block.reads)


def test_size_aware_read_layer_splits_big_and_merges_small() -> None:
    """★★ 判据是**大小**，不是"源有几份"。

    同一份 recon：`hunt:1` 超过预算 ⇒ 拆成多个窗口；`hunt` / `hunt:2` / `instr`
    都远小于预算 ⇒ 合成一个读块。两侧都断言，缺一侧都不算。
    """
    lines = _small_lines()
    budget = BlockBudget(1000)
    plan = Decomposer(strategy="source", block_budget=budget).split(
        research_contract(), ontology(), research_recon(lines)
    )
    reads = _read_blocks(plan)

    # ① 超预算的源被拆：`hunt:1` 落在 >= 2 个读块里。
    homes = {block.id for block in reads if any(ref == "hunt:1" for ref, _, _ in block.reads)}
    assert len(homes) >= 2, f"hunt:1 没有被拆开：{[(b.id, b.reads) for b in reads]}"

    # ② 小源被合：存在一个读块覆盖 >= 2 份不同的源。
    covered = [{ref for ref, _, _ in block.reads} for block in reads]
    assert max(len(group) for group in covered) >= 2, f"小源没有被合成一块：{covered}"

    # ③ 每个读块的输入都落在预算内（每一行都是行边界）。
    for block in reads:
        assert _cost(block, lines, budget) <= budget.max_chars, (block.id, block.reads)

    # ④ 读层的切口是 `window`，且窗口被**声明**出来（不是只在 goal 里当散文）。
    assert all(block.axis == "window" for block in reads)
    assert all(block.reads for block in reads)


def test_the_size_aware_plan_is_still_legal() -> None:
    lines = _small_lines()
    plan = Decomposer(strategy="source", block_budget=BlockBudget(1000)).split(
        research_contract(), ontology(), research_recon(lines)
    )
    assert plan.shape == "layered"
    bundle = compile_bundle(parse_contract(RESEARCH), plan)
    report = check_split(plan, research_contract(), bundle, ontology(), frozen_hash=ontology().content_hash())
    assert report.ok, report.codes()


def test_switching_the_size_policy_off_returns_the_pre_change_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 反证：关掉按大小切/合 ⇒ 读层回到「一份源一块」（改动前的形状）。"""
    from bobanana6.decompose import strategies

    on = Decomposer(strategy="source", block_budget=BlockBudget(1000)).split(
        research_contract(), ontology(), research_recon(_small_lines())
    )
    monkeypatch.setattr(strategies, "SIZE_AWARE_SPLIT", False)
    off = Decomposer(strategy="source", block_budget=BlockBudget(1000)).split(
        research_contract(), ontology(), research_recon(_small_lines())
    )

    assert SIZE_AWARE_SPLIT is True  # the capability `split_read_then_product` reads
    assert len(_read_blocks(on)) != len(_read_blocks(off))
    assert [block.output for block in _read_blocks(off)] == [
        "part-hunt.csv",
        "part-hunt:1.csv",
        "part-hunt:2.csv",
        "part-instr.csv",
    ]
    assert all(block.axis == "source" and block.reads == () for block in _read_blocks(off))


def test_no_budget_is_the_pre_change_shape_too() -> None:
    """★ 反证的第二臂：预算设成"没有"（/ 无穷大）同样退回改动前的形状。"""
    plan = Decomposer(strategy="source").split(
        research_contract(), ontology(), research_recon(_small_lines())
    )
    reads = _read_blocks(plan)
    assert [block.output for block in reads] == [
        "part-hunt.csv",
        "part-hunt:1.csv",
        "part-hunt:2.csv",
        "part-instr.csv",
    ]
    assert all(block.axis == "source" and block.reads == () for block in reads)


def test_a_recon_without_size_facts_falls_back_to_one_block_per_source() -> None:
    """★ 没有尺寸事实 ⇒ 不拆不合（F8：既有 acceptance 的 recon 不带尺寸）。"""
    plan = Decomposer(strategy="source", block_budget=BlockBudget(1000)).split(
        research_contract(), ontology(), research_recon(None)
    )
    assert [block.output for block in _read_blocks(plan)] == [
        "part-hunt.csv",
        "part-hunt:1.csv",
        "part-hunt:2.csv",
        "part-instr.csv",
    ]


def test_a_single_line_over_budget_is_emitted_alone_not_dropped() -> None:
    """边界：一行就超过预算时，它**单独成块**（不丢、不从中间切开）。

    `sources.read` 是**按行**寻址的 ⇒ 行是这条轴上最小的切口；切在行中间会造出
    一个读工具表达不出来的范围（LangChain 对这种情况只打一条 warning，这里把它
    变成显式、有界的例外）。
    """
    lines = {"hunt": (5000,), "hunt:1": (5,) * 10, "hunt:2": (5,) * 10, "instr": (5,) * 10}
    budget = BlockBudget(1000)
    plan = Decomposer(strategy="source", block_budget=budget).split(
        research_contract(), ontology(), research_recon(lines)
    )
    block = next(b for b in _read_blocks(plan) if any(ref == "hunt" for ref, _, _ in b.reads))
    assert block.reads == (("hunt", 0, 1),), block.reads


def test_the_size_layout_is_deterministic() -> None:
    lines = _small_lines()

    def build():
        return Decomposer(strategy="source", block_budget=BlockBudget(1000)).split(
            research_contract(), ontology(), research_recon(lines)
        )

    first, second = build(), build()
    assert first.subtasks == second.subtasks


def test_the_block_budget_must_be_a_real_budget() -> None:
    """0 不是"无限大"也不是"随便切" —— 它是调用方没声明容量，由调用方表达成 `None`。"""
    with pytest.raises(ValueError):
        BlockBudget(0)
    with pytest.raises(ValueError):
        BlockBudget(-1)


def test_sized_read_blocks_declare_their_windows_to_the_model() -> None:
    """声明必须有送达路径：`reads` 要真的进 `task.mysubtask`（模型的 brief）。"""
    from bobanana6.services.tools import BoundTask

    plan = Decomposer(strategy="source", block_budget=BlockBudget(1000)).split(
        research_contract(), ontology(), research_recon(_small_lines())
    )
    read = next(b for b in _read_blocks(plan) if b.reads)
    view = BoundTask(subtask=read, contract_document=research_contract()).mysubtask()
    assert view["reads"] == [{"ref": ref, "start": start, "end": end} for ref, start, end in read.reads]
    assert view["axis"] == "window"


# --------------------------------------------------------------------------- #
# ★★ 两级预算：读块输入落在 [min, max]；写层预算倒推读块的产出上限
# --------------------------------------------------------------------------- #


def _band_lines():
    """4 份源、framed 总量 3150：3×12 行 + 1×6 行（每行 43 字符 + 32 framing = 75）。

    ⇒ 上界 1000 时至少 4 块；下界 750 时每块都不许低于它。
    改动前的 first-fit 会留一个 450 的尾块 —— 这正是要修的形态。
    """
    return {"hunt": (43,) * 12, "hunt:1": (43,) * 12, "hunt:2": (43,) * 12, "instr": (43,) * 6}


def test_the_read_layer_is_cut_by_amount_of_work_into_the_declared_band() -> None:
    """★★ 判据是【任务量】：块数 = ceil(总字符 / 上界)，每块落在 [下界, 上界]。"""
    lines = _band_lines()
    budget = BlockBudget(max_chars=1000, min_chars=750)
    plan = Decomposer(strategy="source", block_budget=budget).split(
        research_contract(), ontology(), research_recon(lines)
    )
    reads = _read_blocks(plan)
    costs = [_cost(block, lines, budget) for block in reads]
    assert len(reads) == 4, costs
    assert all(budget.min_chars <= cost <= budget.max_chars for cost in costs), costs
    assert all(block.reads and block.axis == "window" for block in reads)


def test_without_a_declared_floor_the_shape_is_the_pre_change_one() -> None:
    """★ 反证：同一份材料，只是不声明下界 ⇒ 回到旧的 fill-to-cap（尾部 450 < 750）。

    判据是"有没有声明下界"，不是"源有几份" —— 源数一个字没变。
    """
    lines = _band_lines()
    on = Decomposer(strategy="source", block_budget=BlockBudget(max_chars=1000, min_chars=750)).split(
        research_contract(), ontology(), research_recon(lines)
    )
    off = Decomposer(strategy="source", block_budget=BlockBudget(1000)).split(
        research_contract(), ontology(), research_recon(lines)
    )
    ruler = BlockBudget(1000)
    on_costs = [_cost(block, lines, ruler) for block in _read_blocks(on)]
    off_costs = [_cost(block, lines, ruler) for block in _read_blocks(off)]
    assert all(cost >= 750 for cost in on_costs), on_costs
    assert any(cost < 750 for cost in off_costs), off_costs


def test_the_read_output_cap_is_back_derived_from_the_write_ceiling() -> None:
    """★★ 写层上限 ÷ 读块数 ⇒ 每块的产出上限；写层下限同理给出下限。"""
    budget = BlockBudget(max_chars=39321, min_chars=29491, write_min_chars=4915, write_max_chars=14745)
    assert budget.read_output_cap(8) == 1843  # 14745 // 8
    assert budget.read_output_floor(8) == 615  # ceil(4915 / 8)
    assert budget.read_output_cap(0) == 14745  # never divide by zero
    assert BlockBudget(1000).read_output_cap(4) == 0  # no write budget -> no cap
    assert BlockBudget(1000).read_output_floor(4) == 0


def test_the_read_output_instruction_carries_both_budgets_and_the_index_discipline() -> None:
    """★ 读块的 prompt 里同时有【输入预算】与【产出预算】+ 固定的索引式产出指令。"""
    lines = _band_lines()
    budget = BlockBudget(max_chars=1000, min_chars=750, write_min_chars=400, write_max_chars=1200)
    plan = Decomposer(strategy="source", block_budget=budget).split(
        research_contract(), ontology(), research_recon(lines)
    )
    reads = _read_blocks(plan)
    cap = budget.read_output_cap(len(reads))  # 1200 // 4
    floor = budget.read_output_floor(len(reads))  # ceil(400 / 4)
    for block in reads:
        instruction = block.read_instruction
        assert f"at most {cap} characters" in instruction
        assert f"at least {floor} characters" in instruction
        assert f"{_cost(block, lines, budget)} characters of material" in instruction
        # 三条纪律：可回指的坐标 / 行号落在窗口内 / 一行一条
        assert "citable points with line numbers" in instruction
        assert "one entry per line" in instruction
        assert "must lie inside one of the windows" in instruction


def test_without_a_write_layer_budget_no_output_instruction_is_handed_down() -> None:
    """★ fail-closed：不知道写层预算 ⇒ 一个上限都不编，也不下发指令。"""
    lines = _band_lines()
    plan = Decomposer(strategy="source", block_budget=BlockBudget(max_chars=1000, min_chars=750)).split(
        research_contract(), ontology(), research_recon(lines)
    )
    reads = _read_blocks(plan)
    assert reads
    assert all(block.read_instruction == "" for block in reads)


def test_the_block_budget_validates_the_band() -> None:
    with pytest.raises(ValueError):
        BlockBudget(max_chars=1000, min_chars=1001)
    with pytest.raises(ValueError):
        BlockBudget(max_chars=1000, min_chars=-1)
    with pytest.raises(ValueError):
        BlockBudget(max_chars=1000, write_min_chars=10, write_max_chars=5)
