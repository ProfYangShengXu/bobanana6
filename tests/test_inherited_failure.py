"""上游判失败 ⇒ **不连坐下游**（乙）—— 实测暴露的壳的账。

证据（`.bobanana6/ab/archives/qwen4b14-A-c1.jsonl`）：
`output/effective_holdings.csv` 那条判据（weight 按 manager×period 归 1，tol=1e-10）
在 T-03 / T-04 / T-05 身上各跑一遍，三块全挂，读数逐字相同 ——
而这三块**产出的是别的文件**，它们只是**拿到了** T-02 的那份产物。

链路：

    T-02 产出 effective_holdings.csv，被 503 打挂
      ↓ "失败 ≠ 不存在" ⇒ 产物照发给下游（`_publish_disk_artifact`）
    T-03/04/05 拿到它
      ↓ `services/ci.py::_grouped_invariant` 扫 payload 里的**每一份**产物
    判据在它们身上也跑 ⇒ 三块全挂
      ★ 而它们**写不了那个文件**（写区不许碰别人的产物）⇒ 改无可改
      ★ 报告上还长得像"这三块自己算错了"

修的规矩（**结构性的，与任务领域无关**）：
  **一份输入，产出它的人已经判失败（或弃权）⇒ 它不参与本块的判据。**
  只记录不拒 —— "下游拿着坏输入还硬交"仍是事实，报告要能追到，但不记在它头上。

★ 另一半（缺了它这条就是掩盖）：**上游自己跑得掉吗？跑不掉** ——
  本块产出自己那份照旧进 payload，所以产出者只要跑得完，就是第一个挨判的。
  测试里 `test_the_blocks_own_product_is_always_judged` 钉住这一条。

★ 这里测的是**这条规矩**，不是 13f 的数字：夹具用 `holdings.csv` / `book` / `quarter`，
  另一个领域的名字 —— 规则搬得走，读数才对得上。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.assemble_impl.runner import Runner
from bobanana6.core.model import Artifact, Column, Subtask

# --------------------------------------------------------------------------- #
# 最小替身：`_content_judgements` 只用到 document / event_log / epoch
# --------------------------------------------------------------------------- #


class _Checks:
    def __init__(self, invariants: tuple[dict, ...]) -> None:
        self.declared = True
        self.invariants = invariants


class _Document:
    def __init__(self, invariants: tuple[dict, ...]) -> None:
        self.content_checks = _Checks(invariants)
        self.contract = type("_C", (), {"task_id": "t"})()


class _Log:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, **kw) -> None:  # type: ignore[no-untyped-def]
        self.events.append(kw)


class _JudgeCase:
    def __init__(self, invariants: tuple[dict, ...]) -> None:
        self.document = _Document(invariants)
        self.event_log = _Log()
        self.epoch = lambda: 0.0


GROUPED = {
    "applies_to": "holdings.csv",
    "left": "weight",
    "op": "eq",
    "aggregate": "sum",
    "right": 1.0,
    "group_by": ["book", "quarter"],
    "tol": 1e-10,
}
INVARIANTS = (GROUPED,)

_COLUMNS = (Column("weight", "float"), Column("book", "str"), Column("quarter", "str"))


def _subtask(block_id: str, output: str) -> Subtask:
    return Subtask(
        id=block_id, goal="g", inputs=(), output=output, contract_ref="t", axis="product", mode="write"
    )


def _artifact(name: str, *, first: float, second: float) -> Artifact:
    """两份产物形状相同，只有数值不同 —— 求和 1.0 才算合格。"""
    return Artifact(
        name=name,
        columns=_COLUMNS,
        rows=({"weight": first, "book": "1", "quarter": "1"},
              {"weight": second, "book": "1", "quarter": "1"}),
    )


BAD = lambda name: _artifact(name, first=0.5, second=0.4992)  # 求和 0.9992 -> 违反
GOOD = lambda name: _artifact(name, first=0.25, second=0.75)  # 求和 1.0   -> 合格


def _judge(subtask, own, pieces, handoffs=None):
    case = _JudgeCase(INVARIANTS)
    failures, notices = Runner._content_judgements(case, subtask, own, pieces, handoffs)
    return failures, notices


# --------------------------------------------------------------------------- #
# ① 上游判失败 ⇒ 不连坐
# --------------------------------------------------------------------------- #


def test_an_input_from_a_failed_block_is_not_charged_to_this_block() -> None:
    """★ 反证主体：上游判失败的那份产物，不再判到本块头上。

    改前：`_grouped_invariant` 扫 payload 里的**每一份**产物 —— 上游那份照扫，
    于是本块拿到一条它修不了的失败。
    """
    me = _subtask("T-03", "metrics.csv")
    pieces = {"holdings.csv": BAD("holdings.csv"), "metrics.csv": GOOD("metrics.csv")}
    handoffs = {"holdings.csv": {"from": "T-02", "producer_verdict": "failed"}}
    failures, notices = _judge(me, GOOD("metrics.csv"), pieces, handoffs)
    assert failures == (), failures
    # ★ 但必须留痕：报告要能追到"这口锅是谁的"
    assert any(n["judgement"] == "input_from_failed_block" for n in notices), notices
    assert any(str(n["who"]) == "T-02" for n in notices), notices


def test_the_notice_says_which_block_and_what_verdict() -> None:
    """★ 留痕要能追责：点明【是谁产的、当时判成什么】。"""
    me = _subtask("T-05", "overlap.csv")
    pieces = {"holdings.csv": BAD("holdings.csv"), "overlap.csv": GOOD("overlap.csv")}
    handoffs = {"holdings.csv": {"from": "T-02", "producer_verdict": "declined"}}
    _f, notices = _judge(me, GOOD("overlap.csv"), pieces, handoffs)
    note = next(n for n in notices if n["judgement"] == "input_from_failed_block")
    assert "holdings.csv" in note["got"] and "declined" in note["got"], note
    assert "T-02" in note["violated"], note


def test_a_downstream_that_is_itself_the_producer_is_still_judged() -> None:
    """★ 另一半：**上游自己跑得掉吗？跑不掉。**

    产出者本块产出自己那份**照旧**进判据 ⇒ 它只要跑得完，就是第一个挨判的。
    少了这条，乙 就变成了"谁都别判"的掩盖。
    """
    me = _subtask("T-02", "holdings.csv")
    # 手上只有自己那份（坏的），没人给它送过 —— 它就是产出者
    failures, _n = _judge(me, BAD("holdings.csv"), {"holdings.csv": BAD("holdings.csv")}, {})
    assert failures, "the producer escaped its own criterion"


# --------------------------------------------------------------------------- #
# ② 不许借这条逃判
# --------------------------------------------------------------------------- #


def test_an_input_from_a_healthy_block_is_still_judged() -> None:
    """★ 上游**合格** ⇒ 照旧判。这条一松，任何下游都能靠"不是我产的"躲掉判据。"""
    me = _subtask("T-03", "metrics.csv")
    pieces = {"holdings.csv": BAD("holdings.csv"), "metrics.csv": GOOD("metrics.csv")}
    handoffs = {"holdings.csv": {"from": "T-02", "producer_verdict": "ok"}}
    failures, notices = _judge(me, GOOD("metrics.csv"), pieces, handoffs)
    assert failures, "a bad input from a healthy block went unjudged"
    assert not [n for n in notices if n["judgement"] == "input_from_failed_block"], notices


def test_without_handoff_records_nothing_is_skipped() -> None:
    """★ 拿不到来路（没给 `handoffs`）⇒ **判**（fail-closed：不能证明它来自失败块，就不放行）。"""
    me = _subtask("T-03", "metrics.csv")
    pieces = {"holdings.csv": BAD("holdings.csv"), "metrics.csv": GOOD("metrics.csv")}
    failures, notices = _judge(me, GOOD("metrics.csv"), pieces, None)
    assert failures, "an unlabelled input was silently excused"
    assert not [n for n in notices if n["judgement"] == "input_from_failed_block"], notices


def test_an_unknown_verdict_is_not_treated_as_a_failure() -> None:
    """★ 只有 `failed` / `declined` 免判；来路写着别的（含空）一律照判。"""
    me = _subtask("T-03", "metrics.csv")
    pieces = {"holdings.csv": BAD("holdings.csv")}
    for verdict in ("", "ok", "unknown"):
        failures, _n = _judge(me, GOOD("metrics.csv"), pieces, {"holdings.csv": {"from": "T-02", "producer_verdict": verdict}})
        assert failures, verdict


# --------------------------------------------------------------------------- #
# ③ 自己那份永远判
# --------------------------------------------------------------------------- #


def test_the_blocks_own_product_is_always_judged() -> None:
    """★ 本块产出**永远**进判据 —— 哪怕来路记录说它来自一个失败块。

    （构造上不该发生；这条钉的是"跳过名单不许吃掉本块产出"这个不变量。）
    """
    me = _subtask("T-02", "holdings.csv")
    pieces = {"holdings.csv": GOOD("holdings.csv")}
    handoffs = {"holdings.csv": {"from": "T-02", "producer_verdict": "failed"}}
    failures, _n = _judge(me, BAD("holdings.csv"), pieces, handoffs)
    assert failures, "the block's own product was excused by a stale handoff record"


def test_a_healthy_block_with_a_healthy_input_passes_clean() -> None:
    """★ 回归：正常路径一个字不变 —— 上游合格 + 自己合格 ⇒ 无失败、无免判记录。"""
    me = _subtask("T-03", "metrics.csv")
    pieces = {"holdings.csv": GOOD("holdings.csv"), "metrics.csv": GOOD("metrics.csv")}
    handoffs = {"holdings.csv": {"from": "T-02", "producer_verdict": "ok"}}
    failures, notices = _judge(me, GOOD("metrics.csv"), pieces, handoffs)
    assert failures == (), failures
    assert not [n for n in notices if n["judgement"] == "input_from_failed_block"], notices
