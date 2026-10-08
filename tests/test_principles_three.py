"""按设计原则修三条：产物格式零校验 / 数值被截短 / 第二道工序两死点（规格 §一/§二）。

```
A 产物格式判据（治"手写那条路没有护栏"）—— 确定性判据，不教模型遵守（原则三）
C 第二道工序：分流判据换变量（原则五）+ 声明式对账
```

★ 三条判据只拒**产物**、只影响**这一个块**；不认文件名 / 不认题目 / 不认列名。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl.produce import (
    CandidateFormatError,
    declared_reconcile,
    parse_candidate,
    reject_on_delimiter,
)
from bobanana6.core.checkers.provenance import (
    key_columns,
    normalized_reconcile,
    value_mismatches,
)
from bobanana6.core.model import (
    Artifact,
    Candidate,
    Column,
    Constraint,
    ConstraintBundle,
    ModelResponse,
    Provenance,
    SnapshotBody,
    Subtask,
)
from bobanana6.services.ci import (
    delimiter_finding,
    invariant_findings,
    truncation_findings,
)

TAB_FILE = "crowded_securities_latest.csv"
#: ★ 本轮那个**真实**的 tab 文件：表头 tab=9 / 逗号=0。
TAB_TEXT = "rank\tholder\tCUSIP\tvalue\n1\tX\t123\t2.5\n"
GOOD_CSV = "rank,holder,CUSIP,value\n1,X,123,2.5\n"
QUOTED_CSV = 'rank,holder,note\n1,"A, Inc",ok\n'


def _subtask(**kw) -> Subtask:
    base = {
        "id": "T-04",
        "goal": "merge the parts",
        "inputs": ("part-a.csv",),
        "output": "panel.csv",
        "contract_ref": "c",
        "axis": "field",
        "mode": "write",
    }
    base.update(kw)
    return Subtask(**base)


# --------------------------------------------------------------------------- #
# A ① 分隔符 —— 反证 a / b / c
# --------------------------------------------------------------------------- #


def test_a_the_real_tab_file_is_caught_with_all_three_parameters() -> None:
    """★ A-a：本轮那个真实 tab 文件 ⇒ 判据命中，`violated / expected / got` 一个都不空。"""
    finding = delimiter_finding(TAB_FILE, TAB_TEXT)
    assert finding is not None
    assert finding["violated"] == "分隔符是 TAB"
    assert "逗号" in finding["expected"]
    assert finding["got"] == "rank\tholder\tCUSIP\tvalue"

    captured: dict = {}

    def reject(code, problem, *, expected="", got=""):
        captured.update(code=code, problem=problem, expected=expected, got=got)
        raise CandidateFormatError(problem, code=code, got=got, expected=expected)

    with pytest.raises(CandidateFormatError) as error:
        reject_on_delimiter(TAB_FILE, TAB_TEXT, reject)
    assert captured["code"] == "artifact_delimiter"
    assert captured["expected"] and captured["got"]
    assert error.value.feedback()["expected"] and error.value.feedback()["got"]


def test_b_a_legal_comma_csv_is_not_reported() -> None:
    """★ A-b：合法逗号 CSV ⇒ 必须不报（不许误伤）。"""
    assert delimiter_finding("panel.csv", GOOD_CSV) is None
    assert delimiter_finding(TAB_FILE.replace(".csv", ".txt"), TAB_TEXT) is None  # 非 .csv 不判


def test_c_a_quoted_comma_is_not_reported() -> None:
    """★ A-c：逗号但列里带逗号、被正确引号包裹 ⇒ 必须不报。"""
    assert delimiter_finding("panel.csv", QUOTED_CSV) is None


# --------------------------------------------------------------------------- #
# A ② 数值截短 —— 反证 d（只记不拒）
# --------------------------------------------------------------------------- #


def _artifact_payload(name: str, rows: list[dict]) -> dict:
    columns = sorted({key for row in rows for key in row})
    return {"columns": [{"name": key, "dtype": "float"} for key in columns], "rows": rows}


def test_d_truncated_precision_is_a_notice_and_normal_precision_is_not() -> None:
    """★ A-d：正常精度 ⇒ 不报；被截到 ≤6 位的整列（而别处更精确）⇒ 报一条 notice。"""
    precise = {"weights.csv": _artifact_payload("weights.csv", [{"w": 0.123456789012}])}
    assert truncation_findings(precise) == ()

    truncated = {
        "weights.csv": _artifact_payload("weights.csv", [{"w": 0.123456789012}]),
        "weights_rounded.csv": _artifact_payload("weights_rounded.csv", [{"w": 0.123457}]),
    }
    notices = truncation_findings(truncated)
    assert len(notices) == 1
    assert notices[0]["judgement"] == "truncated_precision"
    assert notices[0]["who"] == "weights_rounded.csv.w"
    assert notices[0]["violated"] and notices[0]["expected"] and notices[0]["got"]


def test_d2_the_truncation_judgement_never_rejects() -> None:
    """★ 第 0 档：截短判据**只记录**，返回的永远是 notice 形状（没有 `ok=False` 这种字段用法）。"""
    notices = truncation_findings(
        {
            "a.csv": _artifact_payload("a.csv", [{"w": 0.5}]),
            "b.csv": _artifact_payload("b.csv", [{"w": 0.1234567}]),
        }
    )
    assert all("judgement" in notice for notice in notices)
    assert all(notice["judgement"] == "truncated_precision" for notice in notices)


# --------------------------------------------------------------------------- #
# A ③ 契约声明的对账关系 —— 反证 e（不许硬编码 / 声明了才判）
# --------------------------------------------------------------------------- #


def test_e_a_declared_invariant_is_judged_and_an_undeclared_one_is_not() -> None:
    """★★ A-e：声明了 ⇒ 不一致就判；**没声明 ⇒ 一条都不产生**（不许自己凭空判）。"""
    weights = {"w.csv": _artifact_payload("w.csv", [{"w": 0.999994}, {"w": 1.000006}])}
    declared = ({"left": "w", "right": "one", "op": "eq", "tol": 1e-10},)
    rows = {"w.csv": _artifact_payload("w.csv", [{"w": 0.999994, "one": 1.0}])}

    assert invariant_findings((), rows) == ()  # ★ 没声明 ⇒ 不判
    findings = invariant_findings(declared, rows)
    assert len(findings) == 1
    assert findings[0]["ok"] is False
    assert findings[0]["violated"] and findings[0]["expected"] and findings[0]["got"]
    # 声明了且成立 ⇒ 过
    fine = {"w.csv": _artifact_payload("w.csv", [{"w": 1.0, "one": 1.0}])}
    assert invariant_findings(declared, fine)[0]["ok"] is True
    assert weights  # 保留夹具变量名


def test_e2_no_hardcoded_rule_exists_in_the_source() -> None:
    """★ A-e（"不许硬编码"的静态反证）：产品路径里不许出现"权重和=1"这类规则字面量。"""
    for relative in ("src/bobanana6/services/ci.py", "src/bobanana6/assemble_impl/runner.py"):
        source = (REPO / relative).read_text(encoding="utf-8")
        for banned in ("sum_to_one", "weights_sum", "== 1.0", "tol=1e-10"):
            assert banned not in source, f"{relative} hardcodes {banned!r}"


# --------------------------------------------------------------------------- #
# A-f 拒绝不扩散：块级拒收后整跑必须继续
# --------------------------------------------------------------------------- #


def _chain(tmp_path: Path, *, name: str) -> Path:
    task = tmp_path / name
    (task / "input" / "data").mkdir(parents=True)
    (task / "input" / "data" / "seed.txt").write_text("draft\n", encoding="utf-8")
    (task / "ontology.yaml").write_text(
        'domain: text\nversion: "2026.1"\nconcepts:\n  obligor: {label: 主体, kind: entity}\n', encoding="utf-8"
    )
    (task / "task.yaml").write_text(
        "task_id: fmt\ngoal: chain two products\nversion: 1\n"
        "deliverables:\n"
        "  - {name: a.csv, schema: {obligor: str}}\n"
        "  - {name: b.csv, schema: {obligor: str}, depends_on: [a.csv]}\n"
        "sources:\n  - {ref: seed, locator: input/data/seed.txt}\n"
        "constraints:\n  hard:\n"
        '    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "no empty"}\n',
        encoding="utf-8",
    )
    return task


def test_f_a_tab_artifact_is_refused_at_block_level_and_the_run_continues(tmp_path: Path) -> None:
    """★★ A-f：块**直接写**出一个 TAB 的 .csv ⇒ 该块被拒收；★ 整跑**没有被判死**。"""
    task = _chain(tmp_path, name="tab-refused")
    asked: list[str] = []

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = ""
            for message in reversed(messages):
                if message.get("role") == "user" and str(message.get("content", "")).startswith("{"):
                    block = str(json.loads(str(message["content"])).get("subtask", {}).get("id", ""))
                    break
            asked.append(block)
            tool_count = sum(1 for m in messages if m.get("role") == "tool")
            if block == "T-01" and tool_count == 0:
                # ① 先读一个授权来源（不然会被"读了来源才准答"那条门先挡住）
                return ModelResponse(
                    tool_calls=(
                        {"id": "r1", "type": "function", "function": {"name": "sources.snapshot", "arguments": json.dumps({"ref": "seed"})}},
                    )
                )
            if block == "T-01" and tool_count == 1:
                # ② 通道 2：**直接写**一个 TAB 的 .csv（本轮那个真实反例走的就是这条）
                return ModelResponse(
                    tool_calls=(
                        {
                            "id": "w1",
                            "type": "function",
                            "function": {
                                "name": "files.write",
                                "arguments": json.dumps({"path": "a.csv", "content": TAB_TEXT}),
                            },
                        },
                    )
                )
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

    assert run.refused is None, "块级拒收绝不许升格成整跑失败"
    assert run.verdicts["T-01"].ok is False
    evidence = " ".join(str(value) for value in run.verdicts["T-01"].evidence.values())
    assert "artifact_delimiter" in str(run.verdicts["T-01"].evidence.get("rejected_by", "")) or "TAB" in evidence or "逗号" in evidence
    assert "T-02" in asked, "第二块必须照常被派活（不扩散）"


# --------------------------------------------------------------------------- #
# C ① 分流判据换变量 + ② 声明式对账
# --------------------------------------------------------------------------- #


def _aggregation(**spec) -> tuple[Artifact, Candidate, dict, ConstraintBundle]:
    """第二道工序：输出行是**算出来的**（notional = 两条来源行之和）。"""
    artifact = Artifact(
        name="panel.csv",
        columns=(Column("manager", "str"), Column("notional", "float")),
        rows=({"manager": "m1", "notional": 30.0},),
    )
    candidate = Candidate(
        subtask_id="T-04",
        source_id="T-04",
        artifacts=(artifact,),
        provenance=(Provenance(value_ref="panel.csv:row=0", snapshot_ref="part-a", source_row=0),),
    )
    body = SnapshotBody(
        ref="part-a",
        columns=(Column("manager", "str"), Column("notional", "float")),
        rows=({"manager": "m1", "notional": 10.0}, {"manager": "m1", "notional": 20.0}),
    )
    bundle = ConstraintBundle(
        task_id="fmt",
        version=1,
        hard=(
            Constraint(
                id="C-prov",
                kind="provenance",
                target="T-04",
                spec={"min_coverage": 0.0, "reconcile": [dict(spec)]},
                rationale="aggregation reconciles as a sum, not literally",
            ),
        ),
    )
    return artifact, candidate, {"part-a": body}, bundle


SUM_RULE = {"column": "notional", "op": "sum", "over": "notional", "group_by": ["manager"]}


def test_c_a_an_aggregation_is_refused_without_a_declaration_and_passes_with_one() -> None:
    """★ C-a：同一个聚合块 —— **不声明** ⇒ 逐字匹配拒；**声明** ⇒ 按声明的对账关系过。"""
    artifact, candidate, bodies, bundle = _aggregation(**SUM_RULE)
    # 不声明（第一道工序语义）：逐值相同 ⇒ 30.0 != 10.0 ⇒ 拒
    assert value_mismatches(artifact, candidate, bodies) != ()
    # 声明：30.0 = 10 + 20 ⇒ 过
    rules = declared_reconcile(bundle, _subtask(), "panel.csv")
    assert rules == (SUM_RULE,)
    assert value_mismatches(artifact, candidate, bodies, None, rules) == ()


def test_c_b_a_passthrough_block_still_matches_literally() -> None:
    """★★ C-b（红线）：搬运类块**仍然逐字匹配** —— 不许因为"放宽"让造假行混进来。"""
    artifact = Artifact(
        name="panel.csv",
        columns=(Column("manager", "str"), Column("notional", "float")),
        rows=({"manager": "invented", "notional": 10.0},),  # ★ manager 是编的
    )
    candidate = Candidate(
        subtask_id="T-04",
        source_id="T-04",
        artifacts=(artifact,),
        provenance=(Provenance(value_ref="panel.csv:row=0", snapshot_ref="part-a", source_row=0),),
    )
    body = SnapshotBody(
        ref="part-a",
        columns=(Column("manager", "str"), Column("notional", "float")),
        rows=({"manager": "m1", "notional": 10.0},),
    )
    # 没有声明 ⇒ 空规则 ⇒ 逐值相同 ⇒ 抓到
    assert value_mismatches(artifact, candidate, {"part-a": body}) != ()
    # 即使声明了**别的列**的聚合，主键列 manager 仍按逐字比 ⇒ 照样抓到
    rules = (SUM_RULE,)
    assert value_mismatches(artifact, candidate, {"part-a": body}, None, rules) != ()
    # 主键列是从声明算出来的（不是硬编码）
    assert key_columns(rules, ("manager", "notional")) == ("manager",)
    assert set(normalized_reconcile(rules)) == {"notional"}


def test_c_c_ref_may_name_a_declared_input_but_not_an_unproduced_name() -> None:
    """★ C-c：`ref` 指**上游产物** ⇒ 过；指一个**谁都没产出过**的名字 ⇒ 仍必须拒。"""
    from bobanana6.assemble_impl.produce import CandidateFormatError as FormatError

    merge = _subtask(inputs=("part-a.csv",))
    answer = json.dumps({"ref": "part-a.csv", "rows": [{"obligor": "A-1"}], "columns": [{"name": "obligor", "dtype": "str"}]})
    candidate = parse_candidate(
        answer, subtask=merge, source_id="T-04", authorized_sources=(), read_own_sources=True
    )
    assert candidate is not None  # ★ 上游产物可以当 ref

    with pytest.raises(FormatError) as error:
        parse_candidate(
            json.dumps({"ref": "nobody-made-this.csv", "rows": [{"obligor": "A-1"}], "columns": [{"name": "obligor", "dtype": "str"}]}),
            subtask=merge,
            source_id="T-04",
            authorized_sources=(),
            read_own_sources=True,
        )
    assert error.value.feedback()["code"] == "unknown_ref"
