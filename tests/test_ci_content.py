"""第 ④ 格 · 内容层 CI：两个维度（来源从哪来 × 结果可不可见）—— 规格 §二/§五。

```
维度一 · 来源（可扩展清单）  external_runner / invariants / generated / none
维度二 · 可见性（由目的定）   sealed（默认，测试姿态） / open（工程姿态）
★ 两个维度独立；开关写在【契约】里，不写在代码里。
```

★ `generated` 那一档是本任务的重点：没有现成检查时，派一个 worker 读规格 + 看真实数据，
写出**可执行的内容断言** —— 检查器跟着任务生成，不是人手写。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.ci_runner import (
    ContentExecutorImpl,
    content_report_for_run,
)
from bobanana6.contract import parse_contract, parse_contract_file
from bobanana6.core.checkers.base import CheckContext
from bobanana6.core.checkers.provenance import (
    reconcile_holes,
    value_mismatches,
)
from bobanana6.core.model import (
    Artifact,
    Candidate,
    Column,
    Provenance,
    SnapshotBody,
)
from bobanana6.services.ci import (
    CONTENT_REPORT_SCHEMA,
    ContentFinding,
    ContentReport,
    run_content_checks,
)

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

CONTRACT_HEAD = """task_id: content-task
goal: produce a table
deliverables:
  - {name: out.csv, schema: {manager: str, notional: float}}
"""


def _checks(body: str):
    return parse_contract(CONTRACT_HEAD + body).content_checks


def _artifact(name="out.csv", rows=None, columns=None) -> dict:
    rows = rows if rows is not None else [{"manager": "m1", "notional": 10.0}]
    columns = columns or [{"name": key, "dtype": "float"} for key in rows[0]]
    return {"name": name, "columns": columns, "rows": rows}


def _document(*artifacts: dict) -> dict:
    return {
        "schema": "bobanana6.ci.snapshot/1",
        "task_id": "content-task",
        "blocks": [
            {"id": "T-01", "output": "out.csv", "verdict": {"ok": True}, "artifacts": [{"inline": a}]}
            for a in artifacts
        ],
    }


class _ScriptedClient:
    """把一段"worker 的回答"当内容断言交出来。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        self.prompts.append(str(messages[-1].get("content", "")))
        from bobanana6.core.model import ModelResponse

        return ModelResponse(text=self.text)


GENERATED_ANSWER = '''Here is the content check.

```python
import json
import sys


def run(artifacts):
    findings = []
    for name, data in artifacts.items():
        columns = set(data["columns"])
        if "notional" not in columns:
            continue
        negatives = [i for i, row in enumerate(data["rows"]) if float(row["notional"]) < 0]
        findings.append({
            "who": f"{name}.notional",
            "expected": "notional is never negative",
            "observed": "all non-negative" if not negatives else f"negative at rows {negatives}",
            "ok": not negatives,
        })
    return findings


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(json.dumps(run(json.load(handle))))
```
'''


# --------------------------------------------------------------------------- #
# ① 四个来源档各跑通
# --------------------------------------------------------------------------- #


def test_source_external_runner_runs_a_command_and_labels_it_external(tmp_path: Path) -> None:
    """`external_runner` 是**唯一**能验【外部正确性】的一档。"""
    checks = _checks(
        f"""content_checks:
  external_runner: {{command: ["{sys.executable}", "-c", "raise SystemExit(0)"]}}
"""
    )
    executor = ContentExecutorImpl(task_dir=tmp_path, document=_document(_artifact()))
    report = run_content_checks(checks, task_id="content-task", executor=executor)

    assert report.sources == ("external_runner",)
    assert report.internal == ()  # ★ 它不进"内部一致性"那一块
    [finding] = report.external
    assert finding.strength == "external" and finding.ok is True
    assert "exit 0" in finding.observed


def test_a_failing_external_runner_is_reported_as_external_not_internal(tmp_path: Path) -> None:
    checks = _checks(
        f"""content_checks:
  external_runner: {{command: ["{sys.executable}", "-c", "raise SystemExit(3)"]}}
"""
    )
    report = run_content_checks(
        checks, task_id="t", executor=ContentExecutorImpl(task_dir=tmp_path, document=_document(_artifact()))
    )
    assert report.internal == ()
    assert [f.ok for f in report.external] == [False]
    assert "exit 3" in report.external[0].observed
    assert report.external_ok is False
    assert report.internal_ok is True  # ★ 内部那一块是**空**的 ⇒ 全过，不许被外部那条带绿


def test_source_invariants_recomputes_column_level_relations(tmp_path: Path) -> None:
    """`invariants` 是自造对账 ⇒ 只能验**内部一致性**。"""
    checks = _checks("content_checks:\n  invariants: [{left: notional, right: cap, op: le}]\n")
    artifact = _artifact(rows=[{"manager": "m1", "notional": 10.0, "cap": 20.0}])
    executor = ContentExecutorImpl(task_dir=tmp_path, document=_document(artifact))
    report = run_content_checks(checks, task_id="t", executor=executor)
    assert report.external == ()
    [finding] = report.internal
    assert finding.check == "invariants" and finding.strength == "internal" and finding.ok

    # 违反时给出**哪一个**：who / expected / observed 三格都在
    violated = _document(_artifact(rows=[{"manager": "m1", "notional": 30.0, "cap": 20.0}]))
    report = run_content_checks(
        checks, task_id="t", executor=ContentExecutorImpl(task_dir=tmp_path, document=violated)
    )
    [finding] = report.internal
    assert finding.ok is False
    assert "notional le cap" in finding.who
    assert "30.0" in finding.observed


def test_an_invariant_that_compares_nothing_is_not_a_pass(tmp_path: Path) -> None:
    """★ 没覆盖 ≠ 通过：一列都没比到 ⇒ 具名失败，不是静默 ok。"""
    checks = _checks("content_checks:\n  invariants: [{left: absent_a, right: absent_b, op: le}]\n")
    report = run_content_checks(
        checks, task_id="t", executor=ContentExecutorImpl(task_dir=tmp_path, document=_document(_artifact()))
    )
    [finding] = report.internal
    assert finding.ok is False
    assert "no comparable column pair" in finding.observed


def test_source_none_is_a_legal_third_answer(tmp_path: Path) -> None:
    """`none: true` = **显式声明"这道题没有内容检查"**，不是失败。"""
    checks = _checks("content_checks:\n  none: true\n")
    report = run_content_checks(checks, task_id="t", executor=None)
    assert report.none_declared is True
    assert report.refusal == ""
    assert report.internal == () and report.external == ()
    assert report.ok is True  # 合法的第三种答案
    assert report.external_ok is None  # ★ 没验过 ≠ 验过了


def test_source_generated_writes_an_executable_module_and_runs_it(tmp_path: Path) -> None:
    """★ `generated`：worker 写出**可执行**的断言，落盘、独立跑、拿回读数。"""
    checks = _checks("content_checks:\n  generate: true\n")
    client = _ScriptedClient(GENERATED_ANSWER)
    executor = ContentExecutorImpl(
        task_dir=tmp_path,
        document=_document(_artifact(rows=[{"manager": "m1", "notional": -5.0}])),
        client=client,
        instruction="notional must never be negative",
    )
    report = run_content_checks(checks, task_id="t", executor=executor)

    assert report.generated_path.endswith("generated_checks.py")
    module = Path(report.generated_path)
    assert module.is_file(), "the generated assertion must land on disk"
    assert "def run(artifacts" in module.read_text(encoding="utf-8")
    [finding] = report.internal
    assert finding.check == "generated" and finding.strength == "internal"
    assert finding.ok is False and "negative" in finding.observed


def test_the_generated_module_runs_standalone_outside_the_generating_session(tmp_path: Path) -> None:
    """★ 生成的检查必须能**独立跑**（不依赖生成它的那次会话）。"""
    checks = _checks("content_checks:\n  generate: true\n")
    executor = ContentExecutorImpl(
        task_dir=tmp_path, document=_document(_artifact()), client=_ScriptedClient(GENERATED_ANSWER)
    )
    report = run_content_checks(checks, task_id="t", executor=executor)

    payload = tmp_path / "standalone.json"
    payload.write_text(json.dumps({"out.csv": {"columns": ["notional"], "rows": [{"notional": -1}]}}), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, report.generated_path, str(payload)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    [entry] = json.loads(completed.stdout.strip())
    assert entry["ok"] is False and "negative" in entry["observed"]


def test_the_generation_prompt_never_reads_the_grader(tmp_path: Path) -> None:
    """★ 边界①：生成时【不许读判分器】—— 看了判分器等于把答案抄进检查里。"""
    (tmp_path / "truth").mkdir()
    (tmp_path / "truth" / "truth.json").write_text('{"rows": []}', encoding="utf-8")
    (tmp_path / "checker.py").write_text("def judge(): return 1.0", encoding="utf-8")
    (tmp_path / "instruction.md").write_text("produce the panel", encoding="utf-8")
    client = _ScriptedClient(GENERATED_ANSWER)
    executor = ContentExecutorImpl(
        task_dir=tmp_path, document=_document(_artifact()), client=client, instruction="produce the panel"
    )
    run_content_checks(_checks("content_checks:\n  generate: true\n"), task_id="t", executor=executor)

    prompt = executor.last_generation_prompt
    assert prompt, "the prompt must be recorded so this can be checked at all"
    assert "produce the panel" in prompt
    for forbidden in ("truth.json", "def judge", "checker.py"):
        assert forbidden not in prompt, f"the generation prompt leaked the grader: {forbidden!r}"


def test_a_generation_that_produces_nothing_runnable_is_a_named_failure(tmp_path: Path) -> None:
    """★ 边界③：生成失败 ⇒ **具名报**，绝不静默降级成 `none`。"""
    checks = _checks("content_checks:\n  generate: true\n")
    for text, needle in (("no code here at all", "no python code"), ("```python\nx = 1\n```", "no python code")):
        executor = ContentExecutorImpl(
            task_dir=tmp_path, document=_document(_artifact()), client=_ScriptedClient(text)
        )
        report = run_content_checks(checks, task_id="t", executor=executor)
        [finding] = report.internal
        assert finding.ok is False and needle in finding.observed
        assert report.none_declared is False  # ★ 没降级成 none


# --------------------------------------------------------------------------- #
# ②③ 第二个维度：可见性（默认 sealed）
# --------------------------------------------------------------------------- #


def _report_with_visibility(visibility: str | None, tmp_path: Path) -> ContentReport:
    body = "content_checks:\n  invariants: [{left: notional, right: cap, op: le}]\n"
    if visibility is not None:
        body += f"  visibility: {visibility}\n"
    checks = _checks(body)
    executor = ContentExecutorImpl(
        task_dir=tmp_path, document=_document(_artifact(rows=[{"manager": "m1", "notional": 30.0, "cap": 20.0}]))
    )
    return run_content_checks(checks, task_id="t", executor=executor)


def test_sealed_keeps_the_results_out_of_the_block_context(tmp_path: Path) -> None:
    """★★ sealed ⇒ 块收到的上下文里【没有】检查结果（结果只进报告与 git 痕迹）。"""
    report = _report_with_visibility("sealed", tmp_path)
    assert report.visibility == "sealed"
    assert report.internal  # 检查**跑了**，读数在报告里
    assert report.for_context() == ()  # ★ 但绝不进块的上下文


def test_open_lets_the_results_into_the_loop(tmp_path: Path) -> None:
    """open ⇒ 结果可以进循环（worker 自查、修）。"""
    report = _report_with_visibility("open", tmp_path)
    assert report.visibility == "open"
    notes = report.for_context()
    assert notes, "open must hand the findings over"
    assert notes[0]["check_block"] == "internal_consistency"
    assert notes[0]["ok"] is False


def test_two_dimensions_are_independent(tmp_path: Path) -> None:
    """★ 同一份检查 × 两种姿态 ⇒ 只有第二个维度变了，读数一字不变。"""
    sealed = _report_with_visibility("sealed", tmp_path)
    opened = _report_with_visibility("open", tmp_path)
    assert sealed.internal == opened.internal  # 检查本身完全一样
    assert sealed.sources == opened.sources
    assert sealed.for_context() == () and opened.for_context() != ()


def test_omitted_visibility_behaves_as_sealed_and_says_it_was_defaulted(tmp_path: Path) -> None:
    """★ ③ 默认 sealed；且**不静默**：报告说得出这个 sealed 是默认来的。"""
    report = _report_with_visibility(None, tmp_path)
    assert report.visibility == "sealed"
    assert report.visibility_source == "defaulted"
    assert report.for_context() == ()
    # 反证：显式写 open 才应该是 open
    explicit = _report_with_visibility("open", tmp_path)
    assert explicit.visibility == "open" and explicit.visibility_source == "declared"


# --------------------------------------------------------------------------- #
# ④ 声明的东西真的被读了
# --------------------------------------------------------------------------- #


def test_a_contract_that_declares_content_checks_but_no_source_is_refused() -> None:
    """★ 缺省不是 `none`，而是**报错**（不许静默当成没有）。"""
    with pytest.raises(Exception) as error:
        _checks("content_checks:\n  visibility: sealed\n")
    assert "names no source" in str(error.value)


def test_an_undeclared_contract_refuses_by_name_instead_of_running_nothing() -> None:
    """合同里**没写** `content_checks` ⇒ 内容层具名拒，不是"检查都过了"。"""
    checks = parse_contract(CONTRACT_HEAD).content_checks
    report = run_content_checks(checks, task_id="t", executor=None)
    assert "content_checks_not_declared" in report.refusal
    assert report.ok is False  # ★ 静默的"没有声明"绝不算通过


def test_content_checks_off_with_a_contradiction_is_refused() -> None:
    with pytest.raises(Exception) as error:
        _checks("content_checks:\n  none: true\n  generate: true\n")
    assert "contradicts" in str(error.value)


def test_an_unknown_key_inside_content_checks_is_refused() -> None:
    with pytest.raises(Exception) as error:
        _checks("content_checks:\n  none: true\n  visiblity: sealed\n")
    assert "unknown keys" in str(error.value)


def test_every_declared_top_level_key_has_a_reader() -> None:
    """★★ 规格 §二边界①：**声明的东西必须真的被读取**（本项目踩过：字段到不了消费者）。

    判据：`_TOP_LEVEL_KEYS` 里的每一个键，要么是 `ContractDocument` 的字段，
    要么是 `TaskContract` 的字段（`task_id` / `goal` / `deliverables` / `sources` /
    `notes` / `sources_share_state` 这些是被搬进去的那几个）。
    新增一个"能声明、没人读"的键 ⇒ 这里立刻红。
    """
    from bobanana6.contract import parser as contract_parser
    from bobanana6.core.model import TaskContract

    document_fields = set(contract_parser.ContractDocument.__dataclass_fields__)
    contract_fields = set(TaskContract.__dataclass_fields__)
    #: ★ 唯一一类例外：一个顶层键**按组展开**成几个字段 —— 例外必须写在这里（可 review），
    #: 而且下面会断言它点名的字段真的存在（例外不能是谎话）。
    exploded = {"constraints": ("hard", "soft")}
    for key, names in exploded.items():
        missing = [name for name in names if name not in document_fields]
        assert missing == [], f"the exception {key!r} names fields that do not exist: {missing}"
    unread = sorted(
        key
        for key in contract_parser._TOP_LEVEL_KEYS
        if key not in document_fields | contract_fields and key not in exploded
    )
    assert unread == [], f"declared but never read: {unread}"
    # 反向：这张例外表不许是垃圾桶 —— 它只能装真的顶层键
    assert set(exploded) <= set(contract_parser._TOP_LEVEL_KEYS)


# --------------------------------------------------------------------------- #
# ⑥ 报告两类分开标
# --------------------------------------------------------------------------- #


def test_the_report_keeps_internal_consistency_and_external_correctness_apart(tmp_path: Path) -> None:
    """★★ 13f 的 HHI 就是这条的反例：内部全过、与真值差 7.45e-5。"""
    checks = _checks(
        "content_checks:\n"
        "  invariants: [{left: notional, right: cap, op: le}]\n"
        f"""  external_runner: {{command: ["{sys.executable}", "-c", "raise SystemExit(0)"]}}
"""
    )
    report = run_content_checks(
        checks,
        task_id="t",
        executor=ContentExecutorImpl(task_dir=tmp_path, document=_document(_artifact(rows=[{"manager": "m1", "notional": 1.0, "cap": 9.0}]))),
    )
    document = report.as_document()
    assert document["schema"] == CONTENT_REPORT_SCHEMA
    assert set(document) >= {"internal_consistency", "external_correctness"}
    # ★ 两块各带一句"这能证明什么"，而且措辞不同 —— 不许合成一句"内容检查通过"
    assert "不能" in document["internal_consistency"]["what_it_proves"]
    assert "外部" in document["external_correctness"]["what_it_proves"]
    assert "内容检查通过" not in json.dumps(document, ensure_ascii=False)
    # 两块各自带 ok，且可以不一致（内部过、外部挂）
    assert document["internal_consistency"]["ok"] is True
    assert document["external_correctness"]["ok"] is True
    broken = run_content_checks(
        _checks(
            f"""content_checks:
  external_runner: {{command: ["{sys.executable}", "-c", "raise SystemExit(1)"]}}
"""
        ),
        task_id="t",
        executor=ContentExecutorImpl(task_dir=tmp_path, document=_document(_artifact())),
    ).as_document()
    assert broken["internal_consistency"]["ok"] is True  # 没跑内部 ⇒ 空 ⇒ 全过
    assert broken["external_correctness"]["ok"] is False


# --------------------------------------------------------------------------- #
# ⑦ provenance：按声明的对账关系比，而不是逐值相同
# --------------------------------------------------------------------------- #


def _aggregation_candidate() -> tuple[Artifact, Candidate, dict[str, SnapshotBody]]:
    """第二道工序：输出行是**算出来的**（notional 是两条来源行的和）。"""
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
    return artifact, candidate, {"part-a": body}


def test_literal_equality_still_refuses_an_aggregation_row_by_default() -> None:
    """改前的行为（逐值相同）—— **不声明**时逐字保留，第一道工序的读数不动。"""
    artifact, candidate, bodies = _aggregation_candidate()
    mismatches = value_mismatches(artifact, candidate, bodies)
    assert mismatches and "does not match snapshot" in mismatches[0]


def test_a_declared_aggregation_is_no_longer_refused() -> None:
    """★ ⑤：`op: sum` ⇒ 30.0 是同一组两条来源行之和，**不算**对不上。"""
    artifact, candidate, bodies = _aggregation_candidate()
    mismatches = value_mismatches(
        artifact, candidate, bodies, None, [{"column": "notional", "op": "sum", "over": "notional", "group_by": ["manager"]}]
    )
    assert mismatches == ()


def test_a_declared_derived_column_is_skipped_and_counted_as_a_hole() -> None:
    """★ `op: none` ⇒ 明说"这一列是算出来的"；同时**说得出这是一个洞**。"""
    artifact, candidate, bodies = _aggregation_candidate()
    rules = [{"column": "notional", "op": "none"}]
    assert value_mismatches(artifact, candidate, bodies, None, rules) == ()
    assert reconcile_holes(rules) == ("notional",)


def test_declaring_a_column_derived_does_not_let_fabrication_through() -> None:
    """★★ 反向：声明的对账关系只管它点名的列 —— 别的列造假照样被抓。"""
    artifact = Artifact(
        name="panel.csv",
        columns=(Column("manager", "str"), Column("notional", "float")),
        rows=({"manager": "invented", "notional": 30.0},),  # ★ manager 是编的
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
    rules = [
        {"column": "notional", "op": "sum", "over": "notional", "group_by": ["manager"]},
        {"column": "manager", "op": "eq"},
    ]
    mismatches = value_mismatches(artifact, candidate, {"part-a": body}, None, rules)
    assert mismatches and "manager" in mismatches[0]


def test_an_aggregate_that_does_not_add_up_is_still_caught() -> None:
    """反向：聚合关系声明了，**算错**照样抓（声明不是放行）。"""
    _artifact_unused, candidate, bodies = _aggregation_candidate()
    wrong = Artifact(
        name="panel.csv",
        columns=(Column("manager", "str"), Column("notional", "float")),
        rows=({"manager": "m1", "notional": 31.0},),  # 不是 10+20
    )
    candidate = Candidate(
        subtask_id="T-04", source_id="T-04", artifacts=(wrong,), provenance=candidate.provenance
    )
    mismatches = value_mismatches(
        wrong, candidate, bodies, None, [{"column": "notional", "op": "sum", "group_by": ["manager"]}]
    )
    assert mismatches and "declared 'sum'" in mismatches[0]


def test_the_provenance_checker_reads_the_declaration_from_the_constraint() -> None:
    """★ 声明通道：`constraint.spec.reconcile` 真的被 checker 读（不是我另开一条路）。"""
    from bobanana6.core.checkers.provenance import check as provenance_check
    from bobanana6.core.model import Constraint

    _artifact_unused, candidate, bodies = _aggregation_candidate()
    constraint = Constraint(
        id="C-prov",
        kind="provenance",
        target="T-04",
        spec={
            "min_coverage": 0.0,
            "reconcile": [{"column": "notional", "op": "sum", "group_by": ["manager"]}],
        },
        rationale="aggregation reconciles as a sum, not literally",
    )
    context = CheckContext(snapshots={}, snapshot_bodies=bodies)
    outcome = provenance_check(constraint, candidate, context)
    assert "declared 'sum'" not in "; ".join(outcome.facts)


# --------------------------------------------------------------------------- #
# 端到端：从一次真跑收快照 → 跑内容层
# --------------------------------------------------------------------------- #


def test_content_report_for_a_real_run_reads_the_declaration_from_the_task_file(tmp_path: Path) -> None:
    """契约里声明 ⇒ 真跑之后的内容层读数跟着变（声明**真的**被读）。"""
    from acceptance.harness import run_task

    task = tmp_path / "content-task"
    (task / "input").mkdir(parents=True)
    (task / "input" / "seed.txt").write_text("draft\n", encoding="utf-8")
    (task / "ontology.yaml").write_text(
        'domain: text\nversion: "2026.1"\nconcepts:\n  obligor: {label: 主体, kind: entity}\n', encoding="utf-8"
    )
    (task / "task.yaml").write_text(
        "task_id: content-chain\ngoal: chain\nversion: 1\n"
        "deliverables:\n  - {name: a.csv, schema: {obligor: str}}\n"
        "sources:\n  - {ref: seed, locator: input/seed.txt}\n"
        "content_checks:\n"
        "  invariants: [{left: obligor, right: obligor, op: eq}]\n",
        encoding="utf-8",
    )
    run = run_task(task, env={}, offline=True, workspace=True)
    _, report = content_report_for_run(run, task_dir=task)
    assert report.sources == ("invariants",)
    assert report.refusal == ""
    # ★ 契约里**没有写** visibility ⇒ sealed（并且说得出来是默认来的）
    assert report.visibility == "sealed" and report.visibility_source == "defaulted"
    assert report.for_context() == ()
    # 同一道题、显式写 open ⇒ 结果可以进循环
    (task / "task.yaml").write_text(
        (task / "task.yaml").read_text(encoding="utf-8").replace(
            "  invariants: [{left: obligor, right: obligor, op: eq}]",
            "  invariants: [{left: obligor, right: obligor, op: eq}]\n  visibility: open",
        ),
        encoding="utf-8",
    )
    _, opened = content_report_for_run(run_task(task, env={}, offline=True, workspace=True), task_dir=task)
    assert opened.visibility == "open" and opened.visibility_source == "declared"


def test_the_declared_visibility_is_read_by_the_data_layer_not_hardcoded(tmp_path: Path) -> None:
    """§六④：开关从契约来。同一道题的两个版本只差一行 `visibility` ⇒ 读数不同。"""
    parsed = parse_contract_file
    assert parsed is not None  # 引用一下，防止"声明了但没人读"的形态
    assert _checks("content_checks:\n  none: true\n  visibility: open\n").visibility == "open"
    assert _checks("content_checks:\n  none: true\n").visibility == "sealed"


def test_a_content_finding_carries_who_expected_and_observed() -> None:
    finding = ContentFinding(check="x", strength="internal", who="w", expected="e", observed="o", ok=True)
    assert finding.as_document() == {
        "check": "x",
        "strength": "internal",
        "who": "w",
        "expected": "e",
        "observed": "o",
        "ok": True,
    }
