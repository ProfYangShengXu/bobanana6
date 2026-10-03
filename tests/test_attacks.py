"""注入集：逐行开关、三步分开报、以及 A11 那条【会红】的验收证据。

★ 这里判的是**机制**：每条攻击能不能被独立开关、三步是不是分开的、
以及被注入的缺陷今天到底有没有被发现。**红了就是产出。**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acceptance.inject.attacks import (
    HIT,
    MISS,
    NA,
    load_attacks,
    render_gap_notes,
    render_table,
    run_attacks,
)
from acceptance.inject.injections import INJECTORS

ATTACKS = Path(__file__).resolve().parents[1] / "acceptance" / "stress" / "attacks.yaml"


# --------------------------------------------------------------------------- #
# 执行器本身：逐行开关 + 三步分开
# --------------------------------------------------------------------------- #


def test_every_row_in_the_table_has_an_injector() -> None:
    """★ 不许有「表里有、没人跑」的行 —— 那会让覆盖声明说谎。"""
    ids = {attack.id for attack in load_attacks()}
    assert ids == set(INJECTORS), f"unimplemented: {sorted(ids - set(INJECTORS))}"


def test_every_row_can_be_switched_off_individually(tmp_path: Path) -> None:
    """★ §14.2「每行一个可独立开关的攻击」—— 开关读的是**表里那一行**。"""
    import yaml

    table = yaml.safe_load(ATTACKS.read_text(encoding="utf-8"))
    table["attacks"][0]["enabled"] = False
    custom = tmp_path / "attacks.yaml"
    custom.write_text(yaml.safe_dump(table, allow_unicode=True), encoding="utf-8")

    rows = load_attacks(custom)
    assert rows[0].id == table["attacks"][0]["id"]
    assert rows[0].enabled is False, "the per-row switch in the table was ignored"
    assert all(row.enabled for row in rows[1:])

    outcomes = run_attacks(path=custom)
    switched_off = outcomes[0]
    assert switched_off.ran is False
    assert (switched_off.found, switched_off.blame_step, switched_off.action_step) == (NA, NA, NA)
    assert "disabled" in switched_off.evidence[0]


def test_a_row_with_no_injector_is_a_miss_not_a_pass() -> None:
    """★ 「表里有、没人跑」不许看起来像通过。"""
    outcomes = run_attacks(injectors={})
    assert outcomes, "the table has no rows at all"
    for outcome in outcomes:
        if not outcome.ran:
            continue
        assert outcome.found == MISS, outcome.attack.id
        assert "no injector" in outcome.evidence[0]
        assert outcome.blame_step == MISS and outcome.action_step == MISS


def test_the_report_keeps_the_three_steps_apart() -> None:
    """★ §14.5：发现对了但出口错，不能被「发现=hit」盖过去 —— 三步各报各的。"""
    outcomes = run_attacks()
    table = render_table(outcomes)
    assert "① found" in table and "② blame" in table and "③ action" in table
    for forbidden in ("overall", "total", "score"):
        assert forbidden not in table.lower()
    # A06 这一行就是活证据：发现=hit（停下来了）但出口=miss（走的是前置门拒跑）
    a06 = next(o for o in outcomes if o.attack.id.startswith("A06"))
    assert a06.found == HIT and a06.action_step == MISS
    # 归因那一步同样单独存在：三个 blame 现在互不相同 → hit（A07）
    a07 = next(o for o in outcomes if o.attack.id.startswith("A07"))
    assert a07.found == HIT and a07.blame_step == HIT


def test_an_injection_that_breaks_is_a_miss_not_a_pass() -> None:
    from acceptance.inject.attacks import AttackOutcome, Observation

    attack = load_attacks()[0]
    broken = AttackOutcome(attack=attack, error="RuntimeError: boom")
    assert broken.observation is None
    assert broken.found == MISS and broken.blame_step == MISS and broken.action_step == MISS
    assert "raised" in broken.evidence[0]

    # ★ 真分支：注入器自己炸了 —— 只有经 run_attacks 才走得到（直接构造 AttackOutcome 走不到）
    def boom() -> Observation:
        raise RuntimeError("boom")

    row = attack.id
    outcome = next(o for o in run_attacks(only=[row], injectors={row: boom}) if o.attack.id == row)
    assert outcome.found == MISS and outcome.blame_step == MISS and outcome.action_step == MISS
    assert "raised" in outcome.evidence[0] and "RuntimeError" in outcome.evidence[0]


def test_only_runs_what_was_asked_for() -> None:
    outcomes = run_attacks(only=["A09-impossible"])
    assert [o.attack.id for o in outcomes] == ["A09-impossible"]


def test_the_gap_notes_name_the_rows_that_missed() -> None:
    """★ §14.3 的覆盖声明：没达到期望的攻击必须被点名。"""
    notes = render_gap_notes(run_attacks())
    assert "A06-persistent" in notes
    assert "A08-global-invariant" in notes
    # ★ A11 已经收口：它不再出现在覆盖声明里（收口了就该从缺口名单里消失）
    assert "A11-provenance-content" not in notes


# --------------------------------------------------------------------------- #
# 十条攻击的读数（当前真实状态；红了写着红）
# --------------------------------------------------------------------------- #


def _outcome(prefix: str):
    return next(o for o in run_attacks() if o.attack.id.startswith(prefix))


def test_a01_cross_block_dependency_is_declared() -> None:
    outcome = _outcome("A01")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence


def test_a03_real_conflict_is_recorded_not_failed() -> None:
    outcome = _outcome("A03")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence


def test_a05_transient_failure_is_retried_locally() -> None:
    outcome = _outcome("A05")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence


def test_a09_impossible_block_declines_instead_of_inventing() -> None:
    outcome = _outcome("A09")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence


def test_a07_the_three_root_causes_get_three_distinct_blames() -> None:
    """★ A07 修后：三条根因必须拿到三个**不同**的 blame（§14.2 的 fail_if）。

    三条 trace 的症状一样（都有工具失败）；区别在于病因由哪一层自己声明
    （工具标 deterministic / 判定器标 constraint_rejected / 上下文由分离实验判定）。
    """
    outcome = _outcome("A07")
    assert outcome.found == HIT  # 三条都注意到了
    assert outcome.blame_step == HIT, outcome.evidence  # ★ 三个 blame 互不相同
    blames = outcome.observation.blames
    assert len(set(blames)) == 3, blames
    assert set(blames) == {"tool", "context", "constraint"}, blames


def test_a04_missing_and_zero_are_told_apart() -> None:
    """★ A04 修后：空值与被填平的 0 在判定器眼里必须**不是**同一个东西。

    缺失是一等状态：空白格在「该列不得为空」的列上被点名，`0` 是一个值、照样通过。
    """
    outcome = _outcome("A04")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence
    evidence = " ".join(outcome.evidence)
    assert "holds no value" in evidence, evidence
    assert "filled zero" in evidence, evidence


def test_a06_a_persistent_failure_refuses_the_run_instead_of_degrading() -> None:
    """★ A06：不会无限重试（fail_if 不触发），但出口是「前置门拒跑」而不是降级。"""
    outcome = _outcome("A06")
    assert outcome.found == HIT  # 停下来了，没有无限重试
    assert outcome.action_step == MISS, outcome.evidence
    assert "refusal" in " ".join(outcome.evidence)


def test_a08_the_global_invariant_cannot_see_a_multi_piece_assembly() -> None:
    """★ A08：`target="*"` 的全局不变量在多块装配上只会「看不见产物」。"""
    outcome = _outcome("A08")
    assert outcome.found == MISS and outcome.action_step == MISS
    assert "artifact for target '*' not found" in " ".join(outcome.evidence)


def test_a10_unit_consistency_reads_the_unit_off_the_value() -> None:
    """★ A10 修后：单位检查读值里的单位 —— 值写了另一个单位就被点名。"""
    outcome = _outcome("A10")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence
    evidence = " ".join(outcome.evidence)
    assert "千元" in evidence and "ok=False" in evidence, evidence
    # ★ 残留如实留着：值里不带单位的仍然判不了，不许假装通过
    assert "unjudgeable" in evidence, evidence


def test_a02_polysemy_is_detected_at_compare_time_not_blocked_at_L1() -> None:
    """★ A02：口径不一致**被发现**了（不是静默混用），但发现点在 compare 期 ——
    §14.2 的 `fail_if` 说的是「出现 DefinitionMismatch 说明没在 L-1 拦住」。"""
    outcome = _outcome("A02")
    assert outcome.found == HIT
    assert outcome.action_step == MISS, outcome.evidence
    assert "L-1" in " ".join(outcome.evidence)


# --------------------------------------------------------------------------- #
# ★★ A11：溯源引用的真实性 —— 修后是【正式断言】（xfail 已升级）
# --------------------------------------------------------------------------- #


def test_a11_citing_rows_that_do_not_exist_is_caught() -> None:
    """3 行快照 + 30 行候选（每行都引用它）→ 当场抓住 `row=3..29` 不存在。

    ★ 这条原来是 `xfail(strict=True)`（缺口活着的证据）。补上内容级校验后它
    转绿，于是按 §15.9① 升级成正式断言 —— 不要留一条恒真的 xfail。
    """
    from bobanana6.core.model import (
        Artifact,
        Candidate,
        Column,
        Provenance,
        Snapshot,
        SnapshotBody,
    )
    from bobanana6.core.provenance import validate_points

    rows = 30
    snapshot = Snapshot(ref="panel-a", body_path=Path("/nonexistent/panel-a.csv"), captured_at="2026-10-03T00:00:00")
    body = SnapshotBody(
        ref="panel-a",
        columns=(Column(name="obligor", dtype="str"),),
        rows=tuple({"obligor": f"A-{index:03d}"} for index in range(3)),
    )
    candidate = Candidate(
        subtask_id="T-04",
        source_id="panel-a",
        artifacts=(Artifact(name="panel.csv", columns=(Column(name="obligor", dtype="str"),), rows=tuple({"obligor": f"X{i}"} for i in range(rows))),),
        provenance=tuple(
            Provenance(value_ref=f"panel.csv:row={index}", snapshot_ref="panel-a", locator="L", source_row=index)
            for index in range(rows)
        ),
    )
    required = tuple(f"panel.csv:row={index}" for index in range(rows))
    report = validate_points(required, candidate.provenance, {"panel-a": snapshot}, {"panel-a": body})
    assert not report.ok, (
        "the cited rows 3..29 do not exist in a 3-row snapshot, yet the chain reports full coverage"
    )
    assert any("row 3" in break_.detail and "3 row(s)" in break_.detail for break_ in report.breaks), report.breaks
    # ★ 覆盖也不再是 1.0：名字可达 ≠ 引用为真
    assert report.coverage < 1.0, report.coverage


def test_a11_a_citation_whose_value_does_not_match_is_caught() -> None:
    """引用行存在但**值对不上** —— 同一个缺口的另一半。"""
    from bobanana6.core.checkers.provenance import value_mismatches
    from bobanana6.core.model import (
        Artifact,
        Candidate,
        Column,
        Provenance,
        SnapshotBody,
    )

    artifact = Artifact(
        name="panel.csv",
        columns=(Column(name="obligor", dtype="str"), Column(name="amount", dtype="float")),
        rows=({"obligor": "A-001", "amount": 999.0},),
    )
    candidate = Candidate(
        subtask_id="T-01",
        source_id="panel-a",
        artifacts=(artifact,),
        provenance=(Provenance(value_ref="panel.csv:row=0", snapshot_ref="panel-a", source_row=0),),
    )
    body = SnapshotBody(ref="panel-a", rows=({"obligor": "A-001", "amount": "1200000.0"},))
    facts = value_mismatches(artifact, candidate, {"panel-a": body})
    assert facts and "does not match snapshot" in facts[0], facts
    # 反过来：CSV 里的字符串与答案里的数，是同一个值
    matching = Artifact(
        name="panel.csv",
        columns=(Column(name="amount", dtype="float"),),
        rows=({"amount": 1200000.0},),
    )
    ok_candidate = Candidate(
        subtask_id="T-01",
        source_id="panel-a",
        artifacts=(matching,),
        provenance=(Provenance(value_ref="panel.csv:row=0", snapshot_ref="panel-a", source_row=0),),
    )
    assert value_mismatches(matching, ok_candidate, {"panel-a": body}) == ()


def test_a11_a_converted_quantity_still_replays_and_a_dropped_unit_does_not() -> None:
    """★ ①：换过单位的同一个量不算「引用不真实」；**把单位丢掉没换算**仍然算。

    换算系数与基准来自本体声明的单位表（`实现规格 §5.0.1` 判据②③），
    不是世界知识、也不是「只要数值能扯上关系就算过」。
    """
    from bobanana6.core.checkers.provenance import value_mismatches
    from bobanana6.core.model import (
        Artifact,
        Candidate,
        Column,
        Concept,
        Ontology,
        Provenance,
        SnapshotBody,
        Unit,
    )

    ontology = Ontology(
        domain="d",
        version="1",
        concepts={"amount": Concept(id="amount", label="Amount", kind="measure", domain="d")},
        definitions={},
        units={
            "amount:CNY": Unit(concept_id="amount", symbol="CNY", scale=1.0, base="CNY"),
            "amount:千元": Unit(concept_id="amount", symbol="千元", scale=1000.0, base="CNY"),
        },
    ).freeze()
    body = SnapshotBody(ref="panel-c", rows=({"obligor": "C-002", "amount": "3000 千元"},))
    columns = (Column(name="obligor", dtype="str"), Column(name="amount", dtype="float"))

    def candidate(amount: object) -> Candidate:
        artifact = Artifact(name="panel.csv", columns=columns, rows=({"obligor": "C-002", "amount": amount},))
        return Candidate(
            subtask_id="T-04",
            source_id="panel-c",
            artifacts=(artifact,),
            provenance=(Provenance(value_ref="panel.csv:row=0", snapshot_ref="panel-c", source_row=0),),
        )

    converted = candidate(3_000_000.0)  # 3000 千元 归一到元
    assert value_mismatches(converted.artifacts[0], converted, {"panel-c": body}, ontology) == ()

    dropped = candidate(3000.0)  # ★ 只把单位扔掉、没有换算
    facts = value_mismatches(dropped.artifacts[0], dropped, {"panel-c": body}, ontology)
    assert facts and "does not match snapshot" in facts[0], facts


def test_a11_the_executor_reports_the_gap_as_found() -> None:
    """★ 执行器那一侧：A11 修后必须是 hit（与上面那条是同一件事的两个读数）。"""
    outcome = _outcome("A11")
    assert outcome.found == HIT, outcome.evidence
    assert "30" in " ".join(outcome.evidence)
    assert "breaks=" in " ".join(outcome.evidence)


def test_a_merge_block_may_not_cite_a_source_it_never_read() -> None:
    """★★ A11 第二个读数的正式断言（夜报 #13 报的「纯合并块假 ref」）。

    守卫过去写成 `elif authorized_sources and snapshot_ref not in authorized_sources`，
    纯合并块（`authorized_sources` 为空）**整个绕过它**：写什么 ref 都收下，
    还会变成 `candidate.source_id`；只要那个名字在注册表里，链就报 coverage=1.0。
    现在：块声明了输入，就只能引用它（或它消费的部件）真正读过的快照。
    """
    from bobanana6.assemble_impl.produce import CandidateFormatError, parse_candidate
    from bobanana6.core.model import Subtask

    merge = Subtask(
        id="T-04",
        goal="merge the parts",
        inputs=("part-panel-a.csv",),
        output="panel.csv",
        contract_ref="panel",
        axis="field",
        mode="write",
    )
    with pytest.raises(CandidateFormatError) as error:
        parse_candidate(
            '{"ref": "panel-b", "rows": [{"obligor": "A-001"}]}',
            subtask=merge,
            source_id="T-04",
            authorized_sources=(),  # ★ 它一个源都没读过
        )
    assert error.value.feedback()["code"] == "unknown_ref", error.value.feedback()
    assert "not a snapshot this block was given" in error.value.feedback()["problem"]


def test_a_merge_row_inherits_the_citation_of_the_row_it_was_handed() -> None:
    """★★ A11 修后的第三条：合并块的行必须能回指它拿到的输入行。

    合并块自己没读过快照，所以「第 n 行来自源 X」不是它能做的声明 —— 引用由
    它消费的部件**继承**过来。编出来的一行（没有任何输入行与它相同）会被当场
    拒绝，而不是发给它一个撑不住的引用。
    """
    from bobanana6.assemble_impl.produce import CandidateFormatError, parse_candidate
    from bobanana6.core.model import Subtask

    merge = Subtask(
        id="T-04",
        goal="merge the parts",
        inputs=("part-panel-a.csv",),
        output="panel.csv",
        contract_ref="panel",
        axis="field",
        mode="write",
    )
    parts = {
        "part-panel-a.csv": {
            "rows": [{"obligor": "A-001", "amount": "1"}],
            "provenance": [
                {"value_ref": "part-panel-a.csv:row=0", "snapshot_ref": "panel-a", "source_row": 0, "locator": "L"}
            ],
        }
    }
    answer = '{"ref": "panel-a", "rows": [{"obligor": "A-001", "amount": "1"}]}'
    candidate = parse_candidate(
        answer,
        subtask=merge,
        source_id="T-04",
        authorized_sources=("panel-a",),  # 继承来的（runner 从部件的溯源算出）
        parts=parts,
    )
    assert [(p.snapshot_ref, p.source_row) for p in candidate.provenance] == [("panel-a", 0)]

    # 编出来的一行：与任何输入行都不同 → 拒不收
    with pytest.raises(CandidateFormatError) as error:
        parse_candidate(
            '{"ref": "panel-a", "rows": [{"obligor": "X-999", "amount": "1"}]}',
            subtask=merge,
            source_id="T-04",
            authorized_sources=("panel-a",),
            parts=parts,
        )
    assert error.value.feedback()["code"] == "untraceable_row", error.value.feedback()
