"""把【判定器核这份产物时用的判据】告诉模型 —— 实测暴露的壳的账。

证据（`.bobanana6/ab/archives/qwen4b14-A-c1.jsonl`，31.8MB 完整归档）：
三个块（T-03/T-04/T-05）连挂三跑，读数一模一样 ——

    invariant[0] sum(weight) == 1.0 by ['manager_order','period_order']
    9 group(s) off; effective_holdings.csv group ('1','1'):
        sum = 0.9992 != 1.0   (tol = 1e-10)

而全归档里 `1e-10` 只出现 6 次，**全部在事后判定记录里**；`atol` 出现 0 次。
⇒ 这个容差从来没有出现在模型收到的任何一句上下文里。

链路（判据从哪来、断在哪）：

    test_outputs.py:510  ← 官方判分器，写死 atol=1e-10
      ↓ acceptance/spec_extractor.py  机械提取（**带上了** tol，见 `extract_grouped_invariants`）
    task.yaml::content_checks.invariants  ← 契约里【有】"tol": 1e-10 + applies_to
      ↓ runner 读 `document.content_checks`  →  判定器用它
      ↓ ✗ 断在这里：brief 里一个字都没有
    模型  只能按常识写（四舍五入到 4 位小数）⇒ 必然挂，且报告上看起来像"运行期出错"

修的规矩（**结构性的，与任务领域无关**）：
  **判据声明它约束哪份产物（`applies_to`），就把它给产出那份产物的块。**
  没写 `applies_to` 的视为任务级 ⇒ 每个块都给；其余的不给（判据不扩散）。
  壳只做归属：不解释判据、不改写、不把 `tol` 翻译成人话。

★ 这里测的是**这条规矩**，不是 13f 的数字：下面的夹具用的是 `a.csv`/`b.csv`，
  另一个领域的产物，`tol` 也换成别的值 —— 规则搬得走，读数才对得上。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl.produce import LoopIOImpl
from bobanana6.core.model import ModelResponse
from tests.test_convert_three import _block_of, _briefing, _tool_call

# --------------------------------------------------------------------------- #
# 夹具：一道两段链的题 + 契约声明的判据
# --------------------------------------------------------------------------- #

#: 三条判据：一条管 a.csv、一条管 b.csv、一条是任务级（没写 applies_to）。
_CHECKS: tuple[dict, ...] = (
    {"applies_to": "a.csv", "left": "obligor", "op": "eq", "right": 1.0, "tol": 1e-10},
    {"applies_to": "b.csv", "left": "obligor", "op": "eq", "right": 2.0, "tol": 1e-12},
    {"left": "obligor", "op": "eq", "right": 3.0, "tol": 1e-9},
)


def _task_with_checks(tmp_path: Path, *, name: str, checks: tuple[dict, ...] | None) -> Path:
    """`test_convert_three::_task` 的同构夹具，只多一项 `content_checks`。"""
    task = tmp_path / name
    (task / "input" / "data").mkdir(parents=True, exist_ok=True)
    (task / "input" / "data" / "seed.txt").write_text("draft\n", encoding="utf-8")
    (task / "ontology.yaml").write_text(
        'domain: text\nversion: "2026.1"\nconcepts:\n  obligor: {label: 主体, kind: entity}\n',
        encoding="utf-8",
    )
    lines = [
        "task_id: criteria",
        "goal: chain two products",
        "version: 1",
        "statement: seed",
        "deliverables:",
        "  - {name: a.csv, schema: {obligor: str}}",
        "  - {name: b.csv, schema: {obligor: str}, depends_on: [a.csv]}",
        "sources:",
        "  - {ref: seed, locator: input/data/seed.txt}",
        "constraints:",
        "  hard:",
        '    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "no empty"}',
    ]
    if checks is not None:
        lines.append("content_checks:")
        lines.append("  invariants:")
        for check in checks:
            lines.append("    - " + json.dumps(check, ensure_ascii=False))
    (task / "task.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return task


def _capture(tmp_path: Path, name: str, checks: tuple[dict, ...] | None = _CHECKS):
    """跑一道真题，把每个块收到的 brief 原样抓下来。"""
    task = _task_with_checks(tmp_path, name=name, checks=checks)
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


# --------------------------------------------------------------------------- #
# ① 真题级：本块的判据进本块的 brief
# --------------------------------------------------------------------------- #


def test_a_block_is_told_the_criteria_that_constrain_its_own_product(tmp_path: Path) -> None:
    """★ 反证主体：约束本块产物的判据，出现在本块的 brief 里。

    改前：判据只走到判定器（`runner::_content_judgements`），brief 里没有这个键 ——
    模型看不到"归 1 要精确到 1e-10"，按常识四舍五入 ⇒ 三跑连挂同一读数。
    """
    _run, briefs = _capture(tmp_path, "criteria-own")
    assert briefs, "no block was ever asked"
    told = 0
    for block, brief in briefs.items():
        output = brief["subtask"]["output"]
        expected = [c for c in _CHECKS if c.get("applies_to") == output or not c.get("applies_to")]
        entry = brief.get("content_checks")
        if not expected:
            assert entry is None, (block, output, entry)
            continue
        told += 1
        assert isinstance(entry, dict), (block, output, sorted(brief))
        assert entry["applies_to"] == output, block
        assert entry["invariants"] == expected, (block, entry["invariants"], expected)
    assert told, "no block received any criterion"


def test_criteria_do_not_spread_to_a_block_they_do_not_constrain(tmp_path: Path) -> None:
    """★ 判据**不扩散**：管 `a.csv` 的那条不许出现在产出 `b.csv` 的块的 brief 里。"""
    _run, briefs = _capture(tmp_path, "criteria-scope")
    assert briefs
    for block, brief in briefs.items():
        output = brief["subtask"]["output"]
        entry = brief.get("content_checks")
        if not entry:
            continue
        before = {c.get("applies_to") for c in entry["invariants"] if c.get("applies_to")}
        assert before <= {output}, (block, output, before)
        # 任务级（无 applies_to）那条**每个块都有** —— 它约束的是全部产物
        assert any(not c.get("applies_to") for c in entry["invariants"]), block


def test_the_tolerance_is_carried_verbatim(tmp_path: Path) -> None:
    """★ 规格常量**原样**到手：`tol` 既不被丢掉，也不被壳翻译成人话。

    这条是整件事的要点 —— 判据里属于【规格】的那部分（容差/单位/口径）必须
    走过 brief；丢掉它，模型就只能按常识写。
    """
    _run, briefs = _capture(tmp_path, "criteria-verbatim")
    assert briefs
    seen: dict[str, float] = {}
    for brief in briefs.values():
        for check in (brief.get("content_checks") or {}).get("invariants", ()):
            seen[str(check.get("applies_to"))] = check["tol"]
    assert seen.get("a.csv") == 1e-10, seen
    assert seen.get("b.csv") == 1e-12, seen
    assert seen.get("None") == 1e-9, seen


def test_no_declared_key_is_dropped_on_the_way_out(tmp_path: Path) -> None:
    """★ 同源：判据里的每个键都原样过去 —— 不只 `tol`，`group_by`/`aggregate` 也一样。

    壳只做归属，不做**摘编**：一旦它在中间挑挑拣拣，判定器与模型看到的就不是同一份规格。
    """
    rich = {
        "applies_to": "a.csv",
        "left": "weight",
        "op": "eq",
        "aggregate": "sum",
        "right": 1.0,
        "group_by": ["k1", "k2"],
        "tol": 1e-10,
    }
    entry = LoopIOImpl._content_checks_entry(_CriteriaCase("a.csv", (rich,)))
    assert entry["invariants"] == [rich], entry
    _run, briefs = _capture(tmp_path, "criteria-no-drop")
    assert briefs
    seen = 0
    for brief in briefs.values():
        for check in (brief.get("content_checks") or {}).get("invariants", ()):
            seen += 1
            # 原样：声明里有几个键，到手就有几个键（任务级那条本来就没有 applies_to）
            assert set(check) <= {"applies_to", "left", "op", "right", "tol"}, check
            assert {"left", "op", "right"} <= set(check), check
            assert isinstance(check["tol"], float), check
    assert seen, "no criterion reached any brief"


# --------------------------------------------------------------------------- #
# ② 替身级：归属规则本身（边界比真题更全）
# --------------------------------------------------------------------------- #


class _CriteriaCase:
    """驱动 `_content_checks_entry` / `_consumption_requirements` 的最小替身：
    它只读 `subtask.output` / `content_checks` / `comparisons` 这三个属性。"""

    def __init__(self, output: str, checks: tuple[dict, ...], comparisons: tuple[dict, ...] = ()) -> None:
        self.subtask = type("_S", (), {"output": output})()
        self.content_checks = checks
        self.comparisons = comparisons


def _comparison(target: str, atol: float = 1e-9) -> dict:
    return {"applies_to": target, "compare": "field-by-field", "numeric_atol": atol, "text": "exact"}


# --------------------------------------------------------------------------- #
# ★★ 消费方式：这份产物会被【怎么比对】—— 与判据是两件事，走同一套归属与 fail-closed
# --------------------------------------------------------------------------- #


def test_the_consumption_requirements_reach_the_block_that_owns_the_artifact() -> None:
    """★ 声明了比对方式 ⇒ 要求进 brief，且带**判分器那把尺子**（容差）。"""
    case = _CriteriaCase("a.csv", (), (_comparison("a.csv", 1e-9),))
    entry = LoopIOImpl._content_checks_entry(case)
    rules = entry["consumed_as"]["requirements"]
    assert entry["consumed_as"]["numeric_atol"] == 1e-9
    assert any("逐行逐列" in rule for rule in rules)
    assert any("1e-09" in rule for rule in rules)
    # ★ 精度与编码两条都必须说到 —— 13f 官方 checker 挂的正是这两条
    assert any("有效精度" in rule for rule in rules)
    assert any("结构化形状" in rule for rule in rules)


def test_consumption_requirements_do_not_spread_to_other_artifacts() -> None:
    """★ 不扩散：只讲它自己那一份产物的消费方式。"""
    case = _CriteriaCase("a.csv", (), (_comparison("b.csv"),))
    assert LoopIOImpl._content_checks_entry(case) == {}


def test_without_a_declared_consumption_mode_nothing_is_said() -> None:
    """★ fail-closed：拿不到比对方式 ⇒ 一个字都不加（既有读数一字不变）。"""
    assert LoopIOImpl._content_checks_entry(_CriteriaCase("a.csv", (), ())) == {}


def test_the_consumption_requirements_never_leak_the_grader_internals() -> None:
    """★★★ 反过拟合闸：要求里**不许出现**判分器的私有实现细节。

    为什么要有这条：把 `|` `;` `,` 这种**分隔符白名单**写进契约，就是给 A 臂开一个
    小口径的偷看口（B 臂偷看 `test_outputs.py` 拿 51/51 是那条路走到黑的样子）。
    壳该说的是**原理**（"别拿可能出现在值内部的字符当分隔符"），不是**判分器写了什么字符**。
    """
    case = _CriteriaCase("a.csv", (), (_comparison("a.csv"),))
    blob = " ".join(LoopIOImpl._content_checks_entry(case)["consumed_as"]["requirements"])
    for leaked in ("`|`", "`;`", "['|'", "relaxed_value_cols", "_parse_sequence", "test_outputs"):
        assert leaked not in blob, leaked


def test_a_task_level_criterion_reaches_every_block() -> None:
    """★ 没写 `applies_to` = 任务级 ⇒ 约束全部产物 ⇒ 每个块都看得到。"""
    entry = LoopIOImpl._content_checks_entry(_CriteriaCase("a.csv", ({"left": "x", "op": "eq", "right": 0},)))
    assert entry == {"applies_to": "a.csv", "invariants": [{"left": "x", "op": "eq", "right": 0}]}


def test_nothing_applies_means_no_entry_at_all() -> None:
    """★ fail-closed：一条都不归本块 ⇒ **键都不出现**，不写一个空壳。"""
    case = _CriteriaCase("c.csv", ({"applies_to": "a.csv", "left": "x", "op": "eq", "right": 0},))
    assert LoopIOImpl._content_checks_entry(case) == {}


def test_without_declared_criteria_there_is_nothing_to_say() -> None:
    """★ 契约没声明判据 ⇒ 一个字都不加（既有读数一字不变）。"""
    assert LoopIOImpl._content_checks_entry(_CriteriaCase("a.csv", ())) == {}


def test_a_missing_output_never_matches_a_named_criterion() -> None:
    """★ 产物名为空时不许把"具名判据"误配上去（空串 ≠ 任意产物）。"""
    case = _CriteriaCase("", ({"applies_to": "a.csv", "left": "x", "op": "eq", "right": 0},))
    assert LoopIOImpl._content_checks_entry(case) == {}


def test_the_entry_is_a_copy_not_a_handle() -> None:
    """★ 交给模型的是一份**拷贝**：brief 被下游改写时不许动到契约。"""
    original = {"applies_to": "a.csv", "left": "x", "op": "eq", "right": 0, "tol": 1e-10}
    checks = (original,)
    entry = LoopIOImpl._content_checks_entry(_CriteriaCase("a.csv", checks))
    entry["invariants"][0]["tol"] = 0.5
    assert original["tol"] == 1e-10, "the brief mutated the declared criterion"


# --------------------------------------------------------------------------- #
# ③ 源头：契约里的容差**是数**（不然给模型的"公差"会是个字符串）
# --------------------------------------------------------------------------- #


def _contract(*, tol_literal: str) -> str:
    return (
        "task_id: t\ngoal: g\nversion: 1\n"
        "deliverables:\n  - {name: a.csv, schema: {x: str}}\n"
        "sources:\n  - {ref: seed, locator: input/data/seed.txt}\n"
        "constraints:\n  hard: []\n"
        "content_checks:\n  invariants:\n"
        "    - {applies_to: a.csv, left: x, op: eq, right: 1.0, tol: " + tol_literal + "}\n"
    )


def test_a_tolerance_lands_as_a_number_however_the_yaml_spells_it() -> None:
    """★ `tol` 是**数** —— 不管 YAML 里写成 `1e-10`（PyYAML 会给字符串）还是 `1.0e-10`。

    实测：13f 契约里 18 条判据的 `tol` **全是字符串** —— YAML 的浮点写法要求带小数点，
    `1e-10` 被解析成 `'1e-10'`。下游每个读者各自 `float(...)` 兜着，但**给模型的那一份**
    会把字符串原样带出去 ⇒ 模型看到的"公差"不是个数。这里在解析期一次归位。
    """
    from bobanana6.contract import parse_contract

    for literal in ("1e-10", "1.0e-10", "0.0000000001", '"1e-10"'):
        doc = parse_contract(_contract(tol_literal=literal), source="<t>")
        tol = doc.content_checks.invariants[0]["tol"]
        assert isinstance(tol, float), (literal, tol, type(tol))
        assert tol == 1e-10, (literal, tol)


def test_a_tolerance_that_is_not_a_number_is_refused_by_name() -> None:
    """★ 给不出数 ⇒ **具名拒**（静默丢掉就是判据悄悄放宽 —— fail-closed 的反面）。"""
    import pytest

    from bobanana6.contract import ContractParseError, parse_contract

    for literal in ("'soon'", "true", "[1]"):
        with pytest.raises(ContractParseError) as caught:
            parse_contract(_contract(tol_literal=literal), source="<t>")
        assert "tol must be a number" in str(caught.value), literal
