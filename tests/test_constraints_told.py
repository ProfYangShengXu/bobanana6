"""把【约束声明的"要求"】告诉块 —— 只给要求，不给机检细节。

缺口（可复核）：`produce.py` 把契约的 `content_checks`（判据）放进了块 brief，
但 `constraints.hard` / `constraints.soft`（真正的判定命令）一个字节都没进 ⇒
块是在**看不见尺子**的情况下写的。实测（`acceptance/ab/text-volume-l1`，5 轮）里
引用条数 131 / 0 / 20 / 37 / 2（通过率 2/5），而字数那条基本稳 —— 失败几乎全在引用上。

修的规矩（**结构性的，与任务领域无关**）：
  **约束声明它管哪份产物（`target`），就把「它要求什么」给产出那份产物的块。**
  每个约束只给三样：`id` + `expects`（契约自己声明的 `rationale`，一句人话）+ `target`。

★★★ 判据（review 这段代码时照这句话看）：
  这段文字要能让一个**不知道机检怎么实现**的人照着做对。所以
  `spec`（`command` 原文、正则、阈值）**一个字都不进 brief** —— 给了它，
  模型会去凑正则，而不是满足要求。下面的夹具故意把正则与阈值写进命令里，
  再断言它们**没有**漏进块收到的 brief。

★ 这里测的是**这条规矩**，不是某道题的读数：夹具用 `a.csv` / `b.csv` 的产品链，
  换个领域也成立；「至少 N 处 `L<数字>`」若真写在契约里是要求（可以给），
  `re.findall(...)` 是机检（不能给）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl.produce import LoopIOImpl
from bobanana6.core.model import Constraint, ModelResponse
from tests.test_convert_three import _block_of, _briefing, _tool_call

# --------------------------------------------------------------------------- #
# 夹具：一道两段链的题 + 契约声明的硬约束
# --------------------------------------------------------------------------- #

#: 三条约束：一条管 `a.csv`、一条管 `b.csv`、一条是任务级（`target: "*"`）。
#: ★ 每条都带 `spec.command`，命令里**故意**塞了正则与阈值 —— 它们是机检细节，
#:   断言里要求它们一个字都不出现在 brief。`rationale` 才是给模型看的要求。
_CONSTRAINTS: tuple[dict, ...] = (
    {
        "id": "C-a",
        "kind": "custom",
        "target": "a.csv",
        "spec": {"check": "shell_command", "command": 'python3 check_a.py --tol 1e-10'},
        "rationale": "数值要写全有效精度，别按给人看的习惯截短",
    },
    {
        "id": "C-b",
        "kind": "custom",
        "target": "b.csv",
        "spec": {"check": "shell_command", "command": 'python3 check_b.py --key "ID(\\d{3})"'},
        "rationale": "b.csv 的每一行都要能对回 a.csv 的某一行",
    },
    {
        "id": "C-never-empty",
        "kind": "presence",
        "target": "*",
        "spec": {"min_rows": 1},
        "rationale": "任何产物都不许交空",
    },
)


def _task(
    tmp_path: Path,
    *,
    name: str,
    constraints: tuple[dict, ...] | None = _CONSTRAINTS,
    single: bool = False,
) -> Path:
    """`test_criteria_told::_task_with_checks` 的同构夹具，只多 `constraints` 一节。

    ``single=True`` 只留一个交付物（单块，没有 `upstream`）—— 逐字对比 brief 时用，
    免得块间交接里的 `producer_commit`（每次运行都不同）混进对比。
    """
    task = tmp_path / name
    (task / "input" / "data").mkdir(parents=True, exist_ok=True)
    (task / "input" / "data" / "seed.txt").write_text("draft\n", encoding="utf-8")
    (task / "ontology.yaml").write_text(
        'domain: text\nversion: "2026.1"\nconcepts:\n  obligor: {label: 主体, kind: entity}\n',
        encoding="utf-8",
    )
    deliverables = ["  - {name: a.csv, schema: {obligor: str}}"]
    if not single:
        deliverables.append("  - {name: b.csv, schema: {obligor: str}, depends_on: [a.csv]}")
    lines = [
        "task_id: constraints",
        "goal: chain two products",
        "version: 1",
        "statement: seed",
        "deliverables:",
        *deliverables,
        "sources:",
        "  - {ref: seed, locator: input/data/seed.txt}",
    ]
    if constraints is None:
        lines.append("constraints: {}")
    else:
        lines.append("constraints:")
        lines.append("  hard:")
        for constraint in constraints:
            lines.append("    - " + json.dumps(constraint, ensure_ascii=False))
    (task / "task.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return task


def _capture(
    tmp_path: Path,
    name: str,
    *,
    constraints: tuple[dict, ...] | None = _CONSTRAINTS,
    single: bool = False,
):
    """跑一道真题，把每个块收到的 brief 原样抓下来（假 client，零真模型）。"""
    task = _task(tmp_path, name=name, constraints=constraints, single=single)
    briefs: dict[str, dict] = {}
    turns: dict[str, int] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages) or "?"
            briefs.setdefault(block, _briefing(messages))
            turn = turns.get(block, 0)
            turns[block] = turn + 1
            # 先真读一次来源（不然会被"读了来源才准答"那条前置门挡住），再交答案。
            if turn == 0:
                return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}],
                        "rows": [{"obligor": "A-1"}],
                        "done": True,
                    }
                )
            )

    run = run_task(task, client=_Client(), env={}, workspace=True)
    return run, briefs


def _requirements(block_id: str, output: str, constraints: tuple[dict, ...] = _CONSTRAINTS) -> list[dict]:
    """契约自己声明的期望：`target ∈ {*, 本块 id, 本块产物}` ⇒ 给本块。"""
    return [
        {"id": c["id"], "expects": c["rationale"], "target": c["target"]}
        for c in constraints
        if c["target"] in ("*", block_id, output)
    ]


def _canon(briefs: dict[str, dict]) -> str:
    return json.dumps(briefs, ensure_ascii=False, sort_keys=True)


def _assert_requirements_told(tmp_path: Path, name: str) -> None:
    """共享断言：每个块拿到的 `constraints` 段**逐字等于**契约声明的期望。"""
    _run, briefs = _capture(tmp_path, name)
    assert briefs, "no block was ever asked"
    told = 0
    for block, brief in briefs.items():
        block_id = str(brief["subtask"]["id"])
        output = str(brief["subtask"]["output"])
        expected = _requirements(block_id, output)
        entry = brief.get("constraints")
        if not expected:
            assert entry is None, (block, output, entry)
            continue
        told += 1
        assert entry == {"applies_to": output, "requirements": expected}, (block, entry, expected)
    assert told, "no block was told any requirement"


# --------------------------------------------------------------------------- #
# ① 真题级：约束的要求进本块 brief，且只有要求
# --------------------------------------------------------------------------- #


def test_every_block_is_told_the_requirements_that_bind_it(tmp_path: Path) -> None:
    """★ 反证主体：**约束本块产物的约束**，`id` + `expects` + `target` 出现在本块 brief 里。

    改前：`constraints.*` 只走到判定器，brief 里没有这个键 —— 模型只知道"要写 a.csv"，
    不知道有"数值写全精度 / 不许交空"这些要求 ⇒ 按常识写、被判据拒。
    """
    _assert_requirements_told(tmp_path, "constraints-own")


def test_a_requirement_does_not_spread_to_a_block_it_does_not_target(tmp_path: Path) -> None:
    """★ 要求**不扩散**：管 `b.csv` 的 `C-b` 不许出现在产出 `a.csv` 的块 brief 里。"""
    _run, briefs = _capture(tmp_path, "constraints-scope")
    assert briefs
    for block, brief in briefs.items():
        output = str(brief["subtask"]["output"])
        entry = brief.get("constraints")
        if not entry:
            continue
        ids = {item["id"] for item in entry["requirements"]}
        if output == "a.csv":
            assert "C-b" not in ids, (block, ids)
        if output == "b.csv":
            assert "C-a" not in ids, (block, ids)
        # 任务级（`target: "*"`）那条**每个块都有** —— 它约束的是全部产物
        assert "C-never-empty" in ids, (block, ids)


def test_the_machine_check_details_never_reach_the_brief(tmp_path: Path) -> None:
    """★★★ 反过拟合闸：命令原文、正则、阈值**一个字都不许进** brief。

    为什么要有这条：给了 `spec.command`，模型会去**凑正则**而不是满足要求；
    这也正是 `visibility` 纪律要挡的"给判分器看"。要求（`rationale`）可以给，
    机检（`re.findall` / `--tol` / 阈值）不能给。
    """
    _run, briefs = _capture(tmp_path, "constraints-leak")
    assert briefs
    blob = _canon(briefs)
    for constraint in _CONSTRAINTS:
        command = str(constraint["spec"].get("command") or "")
        if command:  # `presence` 那条没有命令；有命令的逐字断言其不出现
            assert command not in blob, (constraint["id"], command)
    for leaked in (
        "--tol",
        "1e-10",
        "--key",
        r"ID(\d{3})",
        "check_a.py",
        "check_b.py",
        "shell_command",
        '"spec"',
    ):
        assert leaked not in blob, leaked
    seen = 0
    for brief in briefs.values():
        entry = brief.get("constraints")
        if not entry:
            continue
        for item in entry["requirements"]:
            seen += 1
            # 每个约束**只有**三样：多一个键都可能是机检细节漏出
            assert set(item) == {"id", "expects", "target"}, item
            assert isinstance(item["expects"], str) and item["expects"]
    assert seen, "no requirement reached any brief"


def test_without_declared_constraints_the_brief_is_byte_identical(tmp_path: Path, monkeypatch) -> None:
    """★ 缺省行为不变：**没声明约束**的任务，brief 与"关掉这段注入"逐字相同。

    用同一个任务目录跑两臂（开 / 关注入），JSON 逐字对比 —— 不是"看着差不多"。
    用单块题（没有 `upstream` 的 `producer_commit`，那东西每次运行都不同）。
    """
    _, armed = _capture(tmp_path, "no-constraints", constraints=None, single=True)
    monkeypatch.setattr(LoopIOImpl, "_constraints_entry", lambda self: {})
    _, disarmed = _capture(tmp_path, "no-constraints", constraints=None, single=True)
    assert armed and disarmed
    assert all("constraints" not in brief for brief in armed.values())
    assert _canon(armed) == _canon(disarmed)


def test_reverse_with_the_injection_off_the_requirement_test_goes_red(tmp_path: Path, monkeypatch) -> None:
    """★★ 反向：把这段注入关掉 ⇒ 上面的主体测试**必须红**。

    这条不是"再测一遍"，是证明主体测试的绿**只**来自这段注入（没有别的路径把要求送到）。
    """
    monkeypatch.setattr(LoopIOImpl, "_constraints_entry", lambda self: {})
    with pytest.raises(AssertionError):
        _assert_requirements_told(tmp_path, "constraints-reverse")


# --------------------------------------------------------------------------- #
# ② 替身级：归属规则与 fail-closed 本身（边界比真题更全）
# --------------------------------------------------------------------------- #


class _ConstraintCase:
    """驱动 `_constraints_entry` 的最小替身：它只读 `subtask` / `constraints`。"""

    def __init__(self, output: str, constraints: tuple[Constraint, ...], subtask_id: str = "T-01") -> None:
        self.subtask = type("_S", (), {"id": subtask_id, "output": output})()
        self.constraints = constraints


def _one(constraint_id: str, target: str, rationale: str) -> Constraint:
    return Constraint(id=constraint_id, kind="presence", target=target, spec={"min_rows": 1}, rationale=rationale)


def test_the_entry_carries_the_requirement_verbatim() -> None:
    """★ `expects` 是契约自己声明的那句话，**原样**：壳不解释、不改写、不翻译。"""
    case = _ConstraintCase("a.csv", (_one("C-x", "a.csv", "keep the totals consistent"),))
    assert LoopIOImpl._constraints_entry(case) == {
        "applies_to": "a.csv",
        "requirements": [{"id": "C-x", "expects": "keep the totals consistent", "target": "a.csv"}],
    }


def test_a_task_level_requirement_reaches_every_block() -> None:
    """★ `target: "*"` = 任务级 ⇒ 约束全部产物 ⇒ 每个块都看得到。"""
    entry = LoopIOImpl._constraints_entry(_ConstraintCase("a.csv", (_one("C-g", "*", "no empty product"),)))
    assert entry["requirements"] == [{"id": "C-g", "expects": "no empty product", "target": "*"}]


def test_a_requirement_naming_this_block_by_id_is_given() -> None:
    """★ `target` 的词表是 `*` / subtask id / 交付物名 —— 点名本块 id 也算管本块。"""
    entry = LoopIOImpl._constraints_entry(
        _ConstraintCase("a.csv", (_one("C-t", "T-07", "this block"),), subtask_id="T-07")
    )
    assert entry["requirements"][0]["id"] == "C-t"


def test_a_requirement_naming_another_product_is_not_given() -> None:
    """★ 不扩散：点名别的产物 ⇒ 一个字都不加。"""
    assert LoopIOImpl._constraints_entry(_ConstraintCase("a.csv", (_one("C-b", "b.csv", "other"),))) == {}


def test_an_empty_output_never_matches_a_named_target() -> None:
    """★ 产物名为空时不许把"具名约束"误配上去（空串 ≠ 任意产物）。"""
    assert (
        LoopIOImpl._constraints_entry(_ConstraintCase("", (_one("C-a", "a.csv", "named"),), subtask_id="T-01"))
        == {}
    )


def test_without_declared_constraints_there_is_nothing_to_say() -> None:
    """★ fail-closed：拿不到契约声明的约束 ⇒ 一个字都不加（键都不出现）。"""
    assert LoopIOImpl._constraints_entry(_ConstraintCase("a.csv", ())) == {}


def test_the_entry_is_a_copy_not_a_handle() -> None:
    """★ 交给模型的是一份**拷贝**：brief 被下游改写时不许动到契约。"""
    constraint = _one("C-x", "a.csv", "keep the totals consistent")
    entry = LoopIOImpl._constraints_entry(_ConstraintCase("a.csv", (constraint,)))
    assert entry is not None
    entry["requirements"][0]["expects"] = "tampered"
    again = LoopIOImpl._constraints_entry(_ConstraintCase("a.csv", (constraint,)))
    assert again["requirements"][0]["expects"] == "keep the totals consistent"


def test_the_harness_adds_no_domain_wording() -> None:
    """★★ 领域无关：壳只搬 `id` / `rationale` / `target`，不往里塞任何题目领域词。

    做法：给一句**中性的** rationale，把整段序列化 —— 题目特征词一个都不许出现。
    """
    case = _ConstraintCase("a.csv", (_one("C-x", "a.csv", "keep the totals consistent"),))
    blob = json.dumps(LoopIOImpl._constraints_entry(case), ensure_ascii=False)
    for word in ("压测", "坐标", "检索", "行号", "L<数字>", "citation"):
        assert word not in blob, word
