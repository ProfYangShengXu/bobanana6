"""三处处置的新契约 + 两条红线（规格 续 §三/§四）。

```
① 取缔逐字匹配 ⇒ 加工块被**接受** + 对不上的行**进报告**
② ref 改**存在性**判据 ⇒ 指得到真东西就过；谁都产不出的名字仍拒
③ missing_ref 按处境判 ⇒ 没可指的东西就不逼它编

红线一：真**编造**的产物 ⇒ CI 从原始数据**重算**必须抓住（位置变了，但必须逮住）
红线二：引用**不存在**的东西 ⇒ 仍必须拒（存在性判据不许变成"什么都不查"）
```

★ 这一层不依赖真模型：全部是确定性的壳层/CI 层读数。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from acceptance.spec_extractor import as_content_checks, extract
from bobanana6.assemble_impl.produce import (
    _citeable_names,
    parse_candidate,
)
from bobanana6.core.model import ModelResponse, Subtask
from bobanana6.services.ci import invariant_findings

CHECKS_DIR = REPO / "acceptance" / "ab" / "13f-crowding" / "input" / "checks"


def _merge(**kw) -> Subtask:
    base = {
        "id": "T-04", "goal": "merge", "inputs": ("part-a.csv",), "output": "panel.csv",
        "contract_ref": "c", "axis": "field", "mode": "write",
    }
    base.update(kw)
    return Subtask(**base)


PARTS = {
    "part-a.csv": {
        "rows": [{"manager": "m1", "weight": 0.5}, {"manager": "m2", "weight": 0.5}],
        "provenance": [
            {"value_ref": "part-a.csv:row=0", "snapshot_ref": "src-a", "source_row": 0, "locator": "L0"},
            {"value_ref": "part-a.csv:row=1", "snapshot_ref": "src-a", "source_row": 1, "locator": "L1"},
        ],
        "handoff": {"from": "T-01", "producer_commit": "abcdef1234567"},
    }
}


# --------------------------------------------------------------------------- #
# §三 新契约的正面断言（本轮改动的主契约，之前没有任何测试正面断言它）
# --------------------------------------------------------------------------- #


def test_an_aggregation_is_accepted_and_its_unmatched_rows_reach_the_report() -> None:
    """★★ 主契约：加工块交【算出来的】行 ⇒ **被接受**，且对不上的行**进报告**。"""
    reported: list = []
    candidate = parse_candidate(
        '{"ref": "src-a", "rows": [{"manager": "m1", "weight": 1.0}]}',
        subtask=_merge(), source_id="T-04", authorized_sources=("src-a",), parts=PARTS,
        on_unverified=reported.append,
    )
    assert candidate is not None  # ① 接受
    assert candidate.artifact("panel.csv") is not None
    assert reported, "② 对不上的行必须进报告（不许静默放行）"
    entry = reported[0]
    assert entry["who"] == "panel.csv:row=0"
    assert entry["column_mismatches"] == [{"column": "weight", "expected": 0.5, "actual": 1.0}]
    assert entry["expected"] and entry["actual"]
    assert "CI recomputation" in entry["reconciled_by"]
    # ③ 溯源没丢：产物级锚点（带产出它的块与 commit）
    anchors = [p for p in candidate.provenance if p.snapshot_ref == "part-a.csv"]
    assert anchors and "T-01" in anchors[0].locator and "abcdef123456" in anchors[0].locator


def test_a_verbatim_pass_through_still_inherits_the_row_citation() -> None:
    """★ 反向：逐字搬运**一个字没变** —— 仍然继承输入行级的引用（不是退化成锚点）。"""
    candidate = parse_candidate(
        '{"ref": "src-a", "rows": [{"manager": "m1", "weight": 0.5}]}',
        subtask=_merge(), source_id="T-04", authorized_sources=("src-a",), parts=PARTS,
    )
    assert [(p.snapshot_ref, p.source_row) for p in candidate.provenance if p.source_row is not None] == [
        ("src-a", 0)
    ]


# --------------------------------------------------------------------------- #
# §二②③ ref 判据
# --------------------------------------------------------------------------- #


def test_ref_may_name_an_upstream_artifact_and_carries_its_producer_anchor() -> None:
    """② 指上游产物 ⇒ 过，且锚点带上产出它的块/commit。"""
    candidate = parse_candidate(
        '{"ref": "part-a.csv", "rows": [{"manager": "m9", "weight": 9.0}]}',
        subtask=_merge(), source_id="T-04", authorized_sources=(), parts=PARTS,
    )
    assert candidate is not None
    anchors = [p for p in candidate.provenance if p.snapshot_ref == "part-a.csv"]
    assert anchors and "T-01" in anchors[0].locator and "abcdef123456" in anchors[0].locator


def test_ref_to_a_name_nobody_produced_is_still_refused() -> None:
    """★★ 红线二：引用**不存在**的东西 ⇒ 仍必须拒（存在性判据不许变成"什么都不查"）。"""
    from bobanana6.assemble_impl.produce import CandidateFormatError

    with pytest.raises(CandidateFormatError) as error:
        parse_candidate(
            '{"ref": "nobody-produced-this.csv", "rows": [{"manager": "m1"}]}',
            subtask=_merge(), source_id="T-04", authorized_sources=("src-a",), parts=PARTS,
        )
    assert error.value.feedback()["code"] == "unknown_ref"
    assert "neither the block read nor received from upstream" in error.value.feedback()["problem"]
    assert error.value.feedback()["expected"]


def test_a_block_with_nothing_to_point_at_is_not_forced_to_invent_a_ref() -> None:
    """③ 没读到来源、也没上游件 ⇒ **不逼它编 ref**（退化成块 id，且这件事可见）。"""
    blank = _merge(inputs=())
    candidate = parse_candidate(
        '{"rows": [{"manager": "m1"}]}', subtask=blank, source_id="T-04",
        authorized_sources=("src-a",), parts=None,
    )
    assert candidate.source_id == "T-04"
    assert candidate.provenance and all(p.snapshot_ref == "T-04" for p in candidate.provenance)


def test_citeable_names_is_the_existence_domain() -> None:
    """② 的取值域是**算出来的**：读过的来源 ∪ 收到的上游件（不是白名单）。"""
    names = _citeable_names(_merge(), ("src-a",), PARTS)
    assert names == ("part-a.csv", "src-a")


# --------------------------------------------------------------------------- #
# §四 红线一：真编造 ⇒ CI 从原始数据重算抓住
# --------------------------------------------------------------------------- #


def test_red_line_one_a_fabricated_weight_table_is_caught_by_recomputation() -> None:
    """★★ 红线一：产物是**凭空造的**（权重和 ≠ 1）⇒ CI 重算必须抓住。

    ★ 用的判据是**从官方判分器里机械提取出来的那一条**（不是手写的）：
      `test_outputs.py:510` 的 `groupby(['manager_order','period_order'])['weight'].sum()`
      ⇒ `{column: weight, op: sum, group_by: [manager_order, period_order]}`
    """
    extracted = as_content_checks(extract(CHECKS_DIR))
    rules = extracted["content_checks"]["invariants"]
    assert rules, "the extractor must produce at least one reconciliation"

    fabricated = {
        "effective_holdings.csv": {
            "columns": [{"name": "manager_order", "dtype": "str"}, {"name": "weight", "dtype": "float"}],
            "rows": [{"manager_order": "m1", "weight": 0.7}, {"manager_order": "m1", "weight": 0.7}],
        }
    }
    findings = invariant_findings(rules, fabricated)
    assert findings, "the extracted reconciliation must be evaluated"
    assert all(entry["ok"] is False for entry in findings), findings
    first = findings[0]
    assert first["violated"] and first["expected"] and first["got"]
    assert "weight" in first["who"]


def test_red_line_one_end_to_end_the_block_is_refused_by_recomputation(tmp_path: Path) -> None:
    """★ 端到端：编造权重的块 ⇒ 被**结果判据**（不是形状判据）判失败，且整跑不升格。"""
    task = tmp_path / "fabricated"
    (task / "input" / "data").mkdir(parents=True)
    (task / "input" / "data" / "seed.txt").write_text("draft\n", encoding="utf-8")
    (task / "ontology.yaml").write_text(
        'domain: t\nversion: "2026.1"\nconcepts:\n  obligor: {label: 主体, kind: entity}\n'
        '  weight: {label: 权重, kind: measure}\n', encoding="utf-8"
    )
    (task / "task.yaml").write_text(
        "task_id: fab\ngoal: two blocks\nversion: 1\n"
        "deliverables:\n"
        "  - {name: a.csv, schema: {obligor: str, weight: float}}\n"
        "  - {name: b.csv, schema: {obligor: str, weight: float}}\n"
        "sources:\n  - {ref: seed, locator: input/data/seed.txt}\n"
        "content_checks:\n"
        # ★ 声明形状与提取器输出一致（这里用手写夹具，真值提取见上一条）
        "  invariants: [{left: weight, right: 1.0, op: eq, aggregate: sum, group_by: [obligor], tol: 1e-10}]\n"
        "constraints:\n  hard:\n"
        '    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "no empty"}\n',
        encoding="utf-8",
    )

    asked: list = []

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = ""
            for message in reversed(messages):
                if message.get("role") == "user" and str(message.get("content", "")).startswith("{"):
                    block = str(json.loads(str(message["content"])).get("subtask", {}).get("id", ""))
                    break
            asked.append(block)
            reads = sum(1 for m in messages if m.get("role") == "tool")
            if reads == 0:
                return ModelResponse(
                    tool_calls=(
                        {
                            "id": "r1",
                            "type": "function",
                            "function": {"name": "sources.snapshot", "arguments": json.dumps({"ref": "seed"})},
                        },
                    )
                )
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}, {"name": "weight", "dtype": "float"}],
                        "rows": [{"obligor": "A-1", "weight": 0.7}],
                        "done": True,
                    }
                )
            )

    run = run_task(task, client=_Client(), env={}, workspace=True)
    assert run.refused is None, "结果判据不合格也必须**只停这一块**，不许升格"
    assert set(asked) >= {"T-01", "T-02"}, asked  # 第二块照常被派活
    # ★ 结果判据（契约声明的对账关系）把它判失败 —— 换的是**位置**，不是"什么都不查"
    first = run.verdicts["T-01"]
    assert first.ok is False, first.evidence
    assert any("weight" in str(key) for key in first.failed), first.failed
    evidence = " ".join(str(value) for value in first.evidence.values())
    assert "sum" in evidence and "0.7" in evidence, evidence
