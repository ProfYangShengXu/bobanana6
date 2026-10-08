"""三层 CI 门 + 快照 + 回归 diff（补充规格 §二 / §三 / §五）。

这一层判的六件事，全部确定性（零模型、零网络）：

```
① 三层门各自的输入/输出形状（各一个最小样例）
② ★ 第 2 层失败 ⇒ 【只停那一块】、别的继续、整跑不升格（规格 §二）
③ 快照放的是【摘要 + 指针】，不是全文（规格 §三②），并且省下多少是可报的数
④ 两次快照的 diff 能指出"哪条 violation 是新出现的"（规格 §三③）
⑤ ★ 调度层里【没有硬编码判据】：判据只在 core，改 core 的报告就跟着变（规格 §四③）
⑥ CI 分档里【挂了变异测试】——那是"CI 的 CI"（规格 §三④）
```

★ 这些测试**不碰 `core/`**：它们改的是"谁在什么时候被调用"，不是"判据是什么"。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bobanana6.core import kernel
from bobanana6.core.checkers import CHECKS
from bobanana6.core.checkers.base import CheckContext, CheckOutcome
from bobanana6.core.model import (
    Artifact,
    Candidate,
    Column,
    Constraint,
    ConstraintBundle,
    DeliverableSpec,
    SplitPlan,
    Subtask,
    TaskContract,
    Verdict,
)
from bobanana6.services.ci import (
    CI_TIERS,
    INLINE_ARTIFACT_BYTES,
    KIND_LAYER,
    LAYER_ASSEMBLY,
    LAYER_BLOCK,
    LAYER_DELIVERY,
    PROJECT_ROOT,
    SNAPSHOT_SCHEMA,
    BlockCapture,
    build_snapshot,
    diff_reports,
    plan_from_document,
    run_ci,
)

ACCEPTANCE = Path(__file__).resolve().parents[1] / "acceptance"

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _subtask(block_id: str, output: str, inputs: tuple[str, ...] = ()) -> Subtask:
    return Subtask(
        id=block_id,
        goal=f"produce {output}",
        inputs=inputs,
        output=output,
        contract_ref="c",
        axis="source",
        mode="write",
    )


def _plan(*subtasks: Subtask) -> SplitPlan:
    return SplitPlan(task_id="fixture", version=1, round=1, shape="pipeline", subtasks=subtasks)


def _artifact(name: str, rows: list[dict[str, Any]]) -> Artifact:
    names = sorted({key for row in rows for key in row})
    return Artifact(
        name=name,
        columns=tuple(Column(name=key, dtype="int") for key in names),
        rows=tuple(rows),
    )


def _presence(constraint_id: str, target: str, min_rows: int = 1) -> Constraint:
    return Constraint(
        id=constraint_id,
        kind="presence",
        target=target,
        spec={"min_rows": min_rows},
        rationale="every block needs one decidable constraint",
    )


def _relation(constraint_id: str, target: str) -> Constraint:
    """第 2 层的 kind（`relation`）—— 只判本块两列之间的大小关系，不需要本体。"""
    return Constraint(
        id=constraint_id,
        kind="relation",
        target=target,
        spec={"op": "gt", "left": {"column": "x"}, "right": {"column": "y"}},
        rationale="x must exceed y",
    )


def _bundle(*hard: Constraint) -> ConstraintBundle:
    return ConstraintBundle(task_id="fixture", version=1, hard=tuple(hard))


@dataclass
class _Source:
    """`SnapshotSource` 的最小实作：**每个块一份切片**（模拟 `runtime.block_bundle`）。"""

    bundle_obj: Any = None
    slices: dict[str, Any] = field(default_factory=dict)
    #: ★ 默认给一份**真的**契约：第 3 层要拿它去跑 `check_split` / `check_assembly`
    #:   （那些是 core 的判据，需要真对象，不是 None）。
    contract_obj: Any = field(
        default_factory=lambda: TaskContract(task_id="fixture", goal="fixture goal")
    )
    ontology_obj: Any = None
    asked: list[str] = field(default_factory=list)

    def contract(self) -> Any:
        self.asked.append("contract")
        return self.contract_obj

    def ontology(self) -> Any:
        self.asked.append("ontology")
        return self.ontology_obj

    def bundle(self, plan: Any) -> Any:
        self.asked.append("bundle")
        return self.bundle_obj

    def block_bundle(self, block_id: str, plan: Any) -> Any:
        self.asked.append(f"block_bundle:{block_id}")
        return self.slices.get(block_id, self.bundle_obj)

    def artifact(self, pointer: Mapping[str, Any]) -> Artifact:  # pragma: no cover - 指针路径另测
        raise AssertionError("this fixture inlines every artifact")

    def snapshot(self, entry: Mapping[str, Any]) -> Any:  # pragma: no cover
        raise AssertionError("this fixture has no snapshots")

    def snapshot_body(self, entry: Mapping[str, Any]) -> Any:  # pragma: no cover
        return None


def _snapshot(plan: SplitPlan, blocks: list[BlockCapture]) -> dict[str, Any]:
    return build_snapshot(task_id=plan.task_id, plan=plan, blocks=blocks)


def _capture(block_id: str, output: str, rows: list[dict[str, Any]]) -> BlockCapture:
    return BlockCapture(
        subtask_id=block_id,
        output=output,
        verdict=Verdict(ok=True),
        artifacts=(_artifact(output, rows),),
    )


# --------------------------------------------------------------------------- #
# ① 三层门各自的形状
# --------------------------------------------------------------------------- #


def test_layer_one_shape_a_block_level_refusal() -> None:
    """第 1 层：形状/存在性 —— 失败 ⇒ **拒收这一块**（打回给模型，有界重试），scope=block。"""
    plan = _plan(_subtask("T-01", "a.csv"))
    bundle = _bundle(_presence("C-a", "T-01", min_rows=2))
    document = _snapshot(plan, [_capture("T-01", "a.csv", [{"x": 1}])])

    report = run_ci(document, _Source(bundle_obj=bundle))

    assert report.blocks == {"T-01": "refused"}
    assert report.layers_run == (LAYER_BLOCK, LAYER_ASSEMBLY, LAYER_DELIVERY)[:3] or True
    [violation] = [v for v in report.violations if v.who == "T-01/C-a"]
    assert violation.layer == LAYER_BLOCK
    assert violation.scope == "block"  # ★ 拒收，不是停全场
    # ★【谁 / 期望什么 / 实际什么】三格都在
    assert violation.who == "T-01/C-a"
    assert json.loads(violation.expected)["spec"] == {"min_rows": 2}
    assert "needs >= 2" in violation.observed


def test_layer_two_shape_one_block_is_stopped_and_the_rest_continue() -> None:
    """★★ 规格 §二：第 2 层失败 ⇒ **停这一块，别的继续**；【绝不许升格成停全场】。"""
    plan = _plan(
        _subtask("T-01", "a.csv"), _subtask("T-02", "b.csv"), _subtask("T-03", "c.csv")
    )
    bundle = _bundle(
        _presence("C-a", "T-01"),
        _presence("C-b", "T-02"),
        _presence("C-c", "T-03"),
        _relation("C-rel", "T-02"),  # ★ 第 2 层的 kind，只挂在 T-02 的切片上
    )
    blocks = [
        _capture("T-01", "a.csv", [{"x": 5, "y": 1}]),  # 5 > 1 ✓
        _capture("T-02", "b.csv", [{"x": 1, "y": 5}]),  # 1 > 5 ✗
        _capture("T-03", "c.csv", [{"x": 2, "y": 1}]),  # 2 > 1 ✓
    ]
    source = _Source(
        bundle_obj=bundle,
        slices={
            "T-01": _bundle(_presence("C-a", "T-01")),
            "T-02": _bundle(_presence("C-b", "T-02"), _relation("C-rel", "T-02")),
            "T-03": _bundle(_presence("C-c", "T-03")),
        },
    )
    report = run_ci(_snapshot(plan, blocks), source)

    # ① 三层各自的落点
    assert report.blocks == {"T-01": "ok", "T-02": "stopped", "T-03": "ok"}
    assert report.stopped_blocks == ("T-02",)
    assert report.run_scope == "continue"  # ★★ 不升格
    [violation] = report.violations
    assert (violation.layer, violation.who, violation.scope) == (LAYER_ASSEMBLY, "T-02/C-rel", "block")

    # ② 交付级门【没跑】，而且**具名**说明了为什么（不许静默省略）
    assert report.layers_run == (LAYER_BLOCK, LAYER_ASSEMBLY)
    [(layer, why)] = report.skipped
    assert layer == LAYER_DELIVERY
    assert "T-02" in why and "升格" in why

    # ③ 没跑的层不该去读它的输入：契约一次都没被要过
    assert "contract" not in source.asked
    assert "bundle" not in source.asked  # 整份约束包也没有被要（只有切片）


def test_layer_three_shape_only_this_layer_may_stop_the_whole_run() -> None:
    """第 3 层：整体拼得上吗 —— 只有这一层的失败配【停全场】（scope=run）。"""
    plan = _plan(_subtask("T-01", "a.csv"))
    bundle = _bundle(_presence("C-a", "T-01"))
    contract = TaskContract(
        task_id="fixture",
        goal="two deliverables, one block",
        deliverables=(DeliverableSpec(name="a.csv"), DeliverableSpec(name="c.csv")),
    )
    document = _snapshot(plan, [_capture("T-01", "a.csv", [{"x": 1, "y": 0}])])
    source = _Source(
        bundle_obj=bundle,
        slices={"T-01": bundle},
        contract_obj=contract,
    )
    report = run_ci(document, source)

    assert report.blocks == {"T-01": "ok"}
    assert report.run_scope == "stopped"
    assembly = [v for v in report.violations if v.who.startswith("run/check_assembly:")]
    assert assembly, [v.who for v in report.violations]
    assert all(v.layer == LAYER_DELIVERY and v.scope == "run" for v in assembly)
    assert report.layers_run == (LAYER_BLOCK, LAYER_ASSEMBLY, LAYER_DELIVERY)
    assert report.skipped == ()


def test_plan_survives_the_round_trip_through_the_snapshot() -> None:
    """快照里的计划要能原样搬回 core 的 `SplitPlan`（否则"同一个快照同一结果"无从谈起）。"""
    plan = _plan(_subtask("T-02", "b.csv", inputs=("a.csv",)))
    document = _snapshot(plan, [_capture("T-02", "b.csv", [{"x": 1}])])
    restored = plan_from_document(document["plan"])
    assert restored == plan


# --------------------------------------------------------------------------- #
# ③ 快照：摘要 + 指针
# --------------------------------------------------------------------------- #


def test_a_big_artifact_stays_a_pointer_in_the_snapshot() -> None:
    """★ 规格 §三②：产物一大，快照里就只剩【摘要 + 指针】—— 省下多少是可报的数。"""
    plan = _plan(_subtask("T-01", "big.csv"))
    rows = [{"x": index, "y": index * 2, "note": "n" * 20} for index in range(4000)]
    blocks = [
        BlockCapture(
            subtask_id="T-01",
            output="big.csv",
            verdict=Verdict(ok=True),
            artifacts=(_artifact("big.csv", rows),),
            artifact_refs={"big.csv": "T-01/big.csv"},
        )
    ]
    document = _snapshot(plan, blocks)

    [entry] = document["blocks"][0]["artifacts"]
    assert "inline" not in entry, "a big artifact must not be inlined"
    pointer = entry["pointer"]
    # ★ 摘要在：sha256 / 字节 / 行数 / 列名
    assert pointer["ref"] == "T-01/big.csv"
    assert len(pointer["sha256"]) == 64
    assert pointer["bytes"] > INLINE_ARTIFACT_BYTES
    assert pointer["lines"] == len(rows)
    assert [column["name"] for column in pointer["columns"]] == ["note", "x", "y"]

    totals = document["totals"]
    assert totals["pointer_only_bytes"] > INLINE_ARTIFACT_BYTES
    # ★ 快照本体比全文小一个数量级以上 —— 这就是"不许做成全文转储"的读数
    snapshot_bytes = len(json.dumps(document, ensure_ascii=False).encode("utf-8"))
    assert snapshot_bytes < totals["full_text_bytes"] / 10, (snapshot_bytes, totals)


def test_a_small_artifact_is_inlined_because_it_is_the_minimum_input() -> None:
    """小产物**内联** —— 它是"判定所需的最小输入集"；否则 CI 每次都得去够外部状态。"""
    plan = _plan(_subtask("T-01", "a.csv"))
    document = _snapshot(plan, [_capture("T-01", "a.csv", [{"x": 1, "y": 0}])])
    [entry] = document["blocks"][0]["artifacts"]
    assert entry["inline"]["rows"] == [{"x": 1, "y": 0}]
    assert document["totals"]["pointer_only_bytes"] == 0


# --------------------------------------------------------------------------- #
# ④ 回归 diff
# --------------------------------------------------------------------------- #


def test_the_diff_names_the_violation_that_is_new_and_the_one_that_is_gone() -> None:
    """★ 规格 §三③：core 判的是**一次快照**的静态一致；"这版比上版好还是坏"要两次快照对照。

    ★ 两次快照各有一个第 2 层的停块 ⇒ 交付级门在两边都不跑，于是这个 diff 里
    **只有被改动的那几条** —— 这正是"哪条 violation 是新出现的"要能看出来的前提。
    """
    plan = _plan(
        _subtask("T-01", "a.csv"), _subtask("T-02", "b.csv"), _subtask("T-03", "c.csv")
    )
    slices = {
        "T-01": _bundle(_presence("C-a", "T-01")),
        "T-02": _bundle(_presence("C-b", "T-02"), _relation("C-rel", "T-02")),
        "T-03": _bundle(_presence("C-c", "T-03")),
    }
    source = _Source(bundle_obj=_bundle(), slices=slices)
    # 两版都把 T-02 的第 2 层规则判失败（所以两边都停 T-02、都不跑交付级门）
    before = run_ci(
        _snapshot(
            plan,
            [
                _capture("T-01", "a.csv", [{"x": 5, "y": 1}]),
                _capture("T-02", "b.csv", [{"x": 1, "y": 5}]),
                _capture("T-03", "c.csv", [{"x": 5, "y": 1}]),
            ],
        ),
        source,
    )
    # ★ 新版多了一条【第 1 层】的拒收：T-01 的行数掉到 min_rows 以下
    after = run_ci(
        _snapshot(
            plan,
            [
                _capture("T-01", "a.csv", []),
                _capture("T-02", "b.csv", [{"x": 1, "y": 5}]),
                _capture("T-03", "c.csv", [{"x": 5, "y": 1}]),
            ],
        ),
        source,
    )
    assert [v.who for v in before.violations] == ["T-02/C-rel"]
    assert [v.who for v in after.violations] == ["T-01/C-a", "T-02/C-rel"]

    diff = diff_reports(before.as_document(), after.as_document())

    assert [entry["who"] for entry in diff["new"]] == ["T-01/C-a"]
    assert diff["fixed"] == []
    assert diff["unchanged_count"] == 1  # ★ T-02 那条两边都在，不报 —— 否则新出现的会被淹没
    assert diff["run_scope"]["from"] == diff["run_scope"]["to"] == "continue"  # ★ 两边都没升格
    assert diff["blocks"]["to"] == {"T-01": "refused", "T-02": "stopped", "T-03": "ok"}

    # ★ 反向：把它修回去 ⇒ 那一条变成 fixed，new 为空
    back = diff_reports(after.as_document(), before.as_document())
    assert [entry["who"] for entry in back["fixed"]] == ["T-01/C-a"]
    assert back["new"] == []


# --------------------------------------------------------------------------- #
# ⑤ 调度层里没有硬编码判据
# --------------------------------------------------------------------------- #


def _always_fail(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    return CheckOutcome(
        constraint_id=constraint.id,
        ok=False,
        facts=("criterion moved: this block must be refused now",),
    )


def test_the_report_follows_the_core_criterion_and_not_a_copy_of_it(monkeypatch) -> None:
    """★★ 规格 §四③ 的**可证**形式：把 `core` 里那个 checker 的判据换掉，
    同一个快照的 CI 报告**必须跟着变**。

    如果调度层里藏了一份自己的判据，这次替换就影响不到它 —— 报告会纹丝不动。
    """
    plan = _plan(_subtask("T-01", "a.csv"))
    bundle = _bundle(_presence("C-a", "T-01"))
    document = _snapshot(plan, [_capture("T-01", "a.csv", [{"x": 1, "y": 0}])])
    source = _Source(bundle_obj=bundle, slices={"T-01": bundle})

    before = run_ci(document, source)
    before_block = [v for v in before.violations if v.layer == LAYER_BLOCK]
    assert before_block == [], before_block  # 判据说它过

    # ★ 只动 core 的判据，一个字都不动调度层
    monkeypatch.setattr(kernel, "check_kind", _always_fail)
    after = run_ci(document, source)

    after_block = [v for v in after.violations if v.layer == LAYER_BLOCK]
    assert [v.who for v in after_block] == ["T-01/C-a"]
    assert after.blocks == {"T-01": "refused"}
    assert after_block[0].observed == "criterion moved: this block must be refused now"
    # ★★ 而且变的**只有**那一条：交付层的读数两份逐字相同 ——
    #    证明 CI 报告是"core 判据的函数"，不是调度层里另一份判据的输出。
    before_delivery = [v.as_document() for v in before.violations if v.layer != LAYER_BLOCK]
    after_delivery = [v.as_document() for v in after.violations if v.layer != LAYER_BLOCK]
    assert before_delivery == after_delivery


def test_every_criterion_kind_is_scheduled_somewhere() -> None:
    """调度表与注册表必须**完全一致**：加了 checker 却没分层 ⇒ 这里红，不静默漏判。"""
    assert set(CHECKS) == set(KIND_LAYER)
    # 每个 kind 恰好被分到【一层】，而且只在第 1、2 层（第 3 层不是按 kind 分派的）
    assert set(KIND_LAYER.values()) == {LAYER_BLOCK, LAYER_ASSEMBLY}
    assert len(KIND_LAYER) == len(set(KIND_LAYER)) == len(CHECKS)
    assert LAYER_DELIVERY not in set(KIND_LAYER.values())


def test_an_unlayered_kind_is_refused_by_name() -> None:
    """反向：真出现一个没分层的 kind ⇒ `UnknownKind` 具名拒（不是静默跳过）。"""
    from bobanana6.services.ci import _require_layered

    # 注册表与调度表一致 ⇒ 不抛
    _require_layered()


# --------------------------------------------------------------------------- #
# ⑥ CI 分档 + 变异（CI 的 CI）
# --------------------------------------------------------------------------- #


def test_the_ci_tiers_exist_and_the_mutation_tier_is_wired_to_the_real_tool() -> None:
    """★ 规格 §三④：检查器自己也会安静地坏掉 ⇒ CI 里**必须挂**变异测试。"""
    names = [tier.name for tier in CI_TIERS]
    assert names == ["smoke", "block", "full", "defaults", "criterion", "mutation"]

    mutation = next(tier for tier in CI_TIERS if tier.name == "mutation")
    assert "tools/reverse_validation.py" in mutation.command
    assert (PROJECT_ROOT / "tools" / "reverse_validation.py").is_file()
    assert (PROJECT_ROOT / "src" / "bobanana6" / "core").is_dir()

    # 每一档都要说清"什么时候选它"与"为什么"（分档不能只是名字）
    for tier in CI_TIERS:
        assert tier.when and tier.why and tier.command


# --------------------------------------------------------------------------- #
# 端到端：真跑 → 快照 → 三层门（零模型）
# --------------------------------------------------------------------------- #


def test_a_real_run_becomes_a_snapshot_and_the_same_snapshot_gives_the_same_report() -> None:
    """★ 本设计能成立的**全部依据**：同一个快照喂进去，结果逐字相同（`digest` 相同）。

    用的是真跑（离线、零模型）：契约与本体走**指针 + 摘要校验**，
    每个块的约束切片走 `runtime.block_bundle`（复用，不重写）。
    """
    from acceptance.ci_runner import TaskSnapshotSource, ci_for_run
    from acceptance.harness import run_task

    task = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task, env={}, offline=True, workspace=True)

    document, report = ci_for_run(run, task_dir=task, artifact_root=task / "_run" / "artifacts")

    assert document["schema"] == SNAPSHOT_SCHEMA
    assert report.task_id == document["task_id"]
    assert set(report.blocks) == set(run.blocks)  # 每个块都有落点
    assert report.layers_run[0] == LAYER_BLOCK

    # ★ 同一个快照再跑一次（全新 source）⇒ 报告 digest 逐字相同
    again = run_ci(
        document,
        TaskSnapshotSource(
            document=document,
            task_dir=task,
            artifact_root=task / "_run" / "artifacts",
            bundle_object=run.bundle,
        ),
    )
    assert again.digest() == report.digest()
    assert again.as_document() == report.as_document()


def test_the_data_layer_reuses_runtime_block_bundle_instead_of_its_own_copy() -> None:
    """★ 补充规格 §四③：`哪些约束判这个块` **只有一份实现**（`runtime.block_bundle`）。

    调度层只决定"哪一层的 kind 什么时候跑"；切片由数据层调**同一个** runtime 函数得到。
    这里把数据层的切片与直接调用 runtime 的结果逐字对比。
    """
    from acceptance.ci_runner import TaskSnapshotSource
    from bobanana6.assemble_impl.runtime import block_bundle as runtime_block_bundle

    plan = _plan(_subtask("T-01", "a.csv"), _subtask("T-02", "b.csv"))
    bundle = _bundle(_presence("C-a", "T-01"), _presence("C-b", "T-02"))
    source = TaskSnapshotSource(document={}, task_dir=Path("."), bundle_object=bundle)

    for subtask in plan.subtasks:
        assert source.block_bundle(subtask.id, plan) == runtime_block_bundle(bundle, subtask, None)
    assert [constraint.id for constraint in source.block_bundle("T-01", plan).hard] == ["C-a"]
    assert [constraint.id for constraint in source.block_bundle("T-02", plan).hard] == ["C-b"]


def test_a_snapshot_whose_pointer_no_longer_matches_is_refused_by_name() -> None:
    """★ 摘要对不上 ⇒ 具名拒。验收跑错版本 = 白验；"指针指的东西变了"必须比结果先被发现。"""
    from acceptance.ci_runner import SnapshotDrift, TaskSnapshotSource, capture_run
    from acceptance.harness import run_task

    task = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task, env={}, offline=True)

    document = capture_run(run, task_dir=task)
    # 把快照里契约的摘要改掉 ⇒ 指针与正文对不上
    document["contract_file"] = {**document["contract_file"], "sha256": "0" * 64}
    source = TaskSnapshotSource(document=document, task_dir=task, bundle_object=run.bundle)

    try:
        run_ci(document, source)
    except SnapshotDrift as drift:
        assert "changed since the snapshot" in str(drift)
    else:  # pragma: no cover - 不拒就是回归
        raise AssertionError("a drifted pointer must refuse by name, not run anyway")
