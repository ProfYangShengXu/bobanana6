"""★ `check.local`: the self-check a block runs *before* it submits.

The tool used to ``return self.verdict`` -- and the verdict does not exist until
after submission, so the one moment a block needs it (deciding whether to send)
is exactly the moment it answered ``null``. It now recomputes the contract's own
constraints against the block's current product, read-only, and reports each one.

What is pinned here:

1. the production path (``run_task``) really wires the self-check data in;
2. a constraint that does not belong to this block never appears;
3. the reply carries the judge's human sentence, never ``spec.command`` / a regex;
4. without a contract the reply is ``null`` (the pre-change shape);
5. two-arm proof: an unsatisfying product ⇒ that constraint ``passed=false`` with
   a non-empty ``detail``; make it satisfy ⇒ the same constraint turns ``true``;
6. the wiring is load-bearing: drop any injected piece ⇒ ``null`` (tests 1/5 go red);
7. a failed block's ``commit.json`` records *why* (constraint id + detail).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl.produce import constraint_applies_to
from bobanana6.core.checkers.base import CheckContext, fail
from bobanana6.core.model import Artifact, Candidate, Constraint, ModelResponse, Subtask
from bobanana6.services.tools import BoundChecks
from tests.test_convert_three import _tool_call

# --------------------------------------------------------------------------- #
# unit fixtures: a block whose only constraint runs a "judge command"
# --------------------------------------------------------------------------- #


def _constraint(constraint_id: str, target: str, command: str, rationale: str = "a rule") -> Constraint:
    return Constraint(
        id=constraint_id,
        kind="custom",
        target=target,
        spec={"check": "shell_command", "command": command},
        rationale=rationale,
    )


def _block(output: str = "output/out.txt") -> Subtask:
    return Subtask(id="T-01", goal="g", inputs=(), output=output, contract_ref="c", axis="source", mode="write")


def _fake_shell(*_args, **_kwargs):
    """A custom check that never runs a process: it echoes the command into facts."""

    def run(constraint, candidate, context):  # type: ignore[no-untyped-def]
        return fail(constraint, f"{constraint.spec.get('command')!r} exited 1", "FAIL: the human reason")

    return {"shell_command": run}


def _bound(constraints, *, applies=True, context=True, subtask=None, product=None, now=None):  # type: ignore[no-untyped-def]
    return BoundChecks(
        block="T-01",
        constraints=tuple(constraints),
        applies_to=constraint_applies_to if applies else None,
        subtask=subtask if subtask is not None else _block(),
        local_context=(lambda: CheckContext(custom_checks=_fake_shell(), now=now)) if context else None,
        product=product,
    )


def _provenance(constraint_id: str = "C-provenance", target: str = "output/out.txt") -> Constraint:
    """A real ``kind: provenance`` constraint -- the shape that motivated the third state."""
    return Constraint(
        id=constraint_id,
        kind="provenance",
        target=target,
        spec={"min_coverage": 0.6},
        rationale="every value must point back to a snapshot",
    )


def _corroboration(constraint_id: str = "C-corroboration", field: str = "amount") -> Constraint:
    """A second kind with the same property: its input travels in the answer, not on disk."""
    return Constraint(
        id=constraint_id,
        kind="corroboration",
        target="*",
        spec={"field": field},
        rationale="enough independent sources must agree",
    )


def _text_product(text: str = "line one\nline two\n"):
    """The block's product as ``runner.current_product`` sees it: disk only, no citations."""
    return lambda: Candidate(
        subtask_id="T-01",
        source_id="T-01",
        artifacts=(Artifact(name="output/out.txt", text=text),),
    )


# --------------------------------------------------------------------------- #
# ② belonging: only constraints that concern this block
# --------------------------------------------------------------------------- #


def test_check_local_reports_only_the_constraints_that_belong_to_this_block() -> None:
    own = _constraint("C-own", "output/out.txt", "python3 check.py")
    other_product = _constraint("C-elsewhere", "output/other.txt", "python3 check.py")
    other_block = _constraint("C-T02", "T-02", "python3 check.py")

    result = _bound((own, other_product, other_block)).local()

    assert result is not None
    assert result["applies_to"] == "output/out.txt"
    assert [row["id"] for row in result["constraints"]] == ["C-own"]
    assert set(result["constraints"][0]) == {"id", "expects", "status", "passed", "detail"}


def test_a_wildcard_constraint_applies_to_every_block() -> None:
    result = _bound((_constraint("C-star", "*", "python3 check.py"),)).local()
    assert result is not None
    assert [row["id"] for row in result["constraints"]] == ["C-star"]


# --------------------------------------------------------------------------- #
# ③ no machine detail, bounded output
# --------------------------------------------------------------------------- #


def test_check_local_never_echoes_the_command_or_a_regex() -> None:
    command = "grep -oE 'L[0-9]+' out.txt"
    result = _bound((_constraint("C-regex", "output/out.txt", command),)).local()

    assert result is not None
    rendered = json.dumps(result, ensure_ascii=False)
    assert command not in rendered
    assert "L[0-9]+" not in rendered  # the regex inside the command is withheld too
    assert "<machine detail withheld>" in rendered
    assert "FAIL: the human reason" in rendered
    assert len(result["constraints"][0]["detail"]) <= 401  # 400 + the ellipsis


def test_machine_detail_nested_in_a_spec_is_withheld_too() -> None:
    """★ 审计找到的洞：清单里嵌字典（``columns[].pattern``）也要一起抹掉。"""
    constraint = Constraint(
        id="C-nested",
        kind="custom",
        target="output/out.txt",
        spec={
            "check": "shell_command",
            "command": "python3 check.py",
            "columns": [{"name": "amount", "dtype": "str", "pattern": "^A[0-9]+$"}],
        },
        rationale="r",
    )

    def run(constraint, candidate, context):  # type: ignore[no-untyped-def]
        return fail(constraint, f"judge said: {constraint.spec}")

    checks = BoundChecks(
        block="T-01",
        constraints=(constraint,),
        applies_to=constraint_applies_to,
        subtask=_block(),
        local_context=lambda: CheckContext(custom_checks={"shell_command": run}),
    )
    rendered = json.dumps(checks.local(), ensure_ascii=False)
    assert "python3 check.py" not in rendered
    assert "^A[0-9]+$" not in rendered
    assert "amount" not in rendered


def test_a_verbose_judge_sentence_is_truncated() -> None:
    from bobanana6.core.checkers.base import ok
    from bobanana6.services.tools import CHECK_LOCAL_DETAIL_LIMIT

    def run(constraint, candidate, context):  # type: ignore[no-untyped-def]
        return ok(constraint, "x" * (CHECK_LOCAL_DETAIL_LIMIT * 3))

    checks = BoundChecks(
        block="T-01",
        constraints=(_constraint("C-long", "output/out.txt", "python3 check.py"),),
        applies_to=constraint_applies_to,
        subtask=_block(),
        local_context=lambda: CheckContext(custom_checks={"shell_command": run}),
    )
    detail = checks.local()["constraints"][0]["detail"]
    assert detail.endswith("…")
    assert len(detail) == CHECK_LOCAL_DETAIL_LIMIT + 1


# --------------------------------------------------------------------------- #
# ④ / ⑥ fail-closed and the injection point
# --------------------------------------------------------------------------- #


def test_check_local_is_null_when_nothing_is_declared_or_wired() -> None:
    assert BoundChecks(block="T-01").local() is None  # no contract at all
    assert _bound(()).local() is None  # declared, but none apply to this block
    assert _bound((_constraint("C-own", "output/out.txt", "python3 check.py"),), applies=False).local() is None
    assert _bound((_constraint("C-own", "output/out.txt", "python3 check.py"),), context=False).local() is None


# --------------------------------------------------------------------------- #
# the third state: "cannot be checked yet" is not "failed"
# --------------------------------------------------------------------------- #


def test_a_check_whose_input_does_not_exist_yet_is_not_reported_as_failed() -> None:
    """★ The decisive distinction: citation points are produced at block end.

    The check runs and its coverage reads 0.0000, but that zero is not a finding
    -- the input it would be computed from is not on the disk product. Reporting
    it as ``passed=false`` is the bug this pins down.
    """
    row = _bound((_provenance(),), product=_text_product()).local()["constraints"][0]

    assert row["status"] == "unverified"
    assert row["passed"] is None
    assert row["detail"], "an unverified row must still say why"
    assert "验不了" in row["detail"], "the detail must be a human sentence, in words"
    assert "coverage 0.0000" not in row["detail"], "the misleading judge sentence must be gone"


def test_the_third_state_is_keyed_on_what_the_check_reads_not_on_its_name() -> None:
    """★ 判据是「输入此刻在不在」这个一般性质，不是某条约束的名字/某道题。"""
    # The same check under a name no task.yaml ever wrote: still the third state.
    for constraint_id in ("C-provenance", "C-whatever-this-contract-calls-it"):
        row = _bound((_provenance(constraint_id),), product=_text_product()).local()["constraints"][0]
        assert row["status"] == "unverified", constraint_id
        assert row["passed"] is None, constraint_id

    # A different kind with the same property (its input travels in the answer)
    # answers the same way -- so this is not a provenance special case.
    claim_row = _bound((_corroboration(),), product=_text_product()).local()["constraints"][0]
    assert claim_row["status"] == "unverified"
    assert claim_row["passed"] is None


def test_every_kind_whose_input_is_built_at_block_end_defers() -> None:
    """★ 同一性质在别的 kind 上也成立（不是给 C-provenance 开的后门）。"""
    coverage = Constraint(
        id="C-cov",
        kind="coverage",
        target="output/out.txt",
        spec={"required_sources": ["notes"]},
        rationale="every required source must be covered",
    )
    cov_row = _bound((coverage,), product=_text_product()).local()["constraints"][0]
    assert cov_row["status"] == "unverified"
    assert cov_row["passed"] is None

    freshness = Constraint(
        id="C-fresh", kind="freshness", target="output/out.txt", spec={}, rationale="snapshots must be fresh"
    )
    fresh_row = _bound((freshness,), product=_text_product(), now="2026-01-01T00:00:00").local()["constraints"][0]
    assert fresh_row["status"] == "unverified"
    assert fresh_row["passed"] is None


def test_a_malformed_constraint_is_reported_as_failed_not_deferred() -> None:
    """★ 审计 finding：缺输入 ≠ 契约本身写坏了。后者必须照旧 ``failed``。

    这里既证明第三态没有吞掉真失败，也证明它没有把检查器**自己的**校验错误
    （`min_coverage` / `spec.field`）误报成"此刻验不了"。
    """
    bad_prov = Constraint(
        id="C-bad-prov",
        kind="provenance",
        target="output/out.txt",
        spec={"min_coverage": 2},
        rationale="r",
    )
    prov_row = _bound((bad_prov,), product=_text_product()).local()["constraints"][0]
    assert prov_row["status"] == "failed" and prov_row["passed"] is False
    assert "min_coverage" in prov_row["detail"]

    bad_corr = Constraint(
        id="C-bad-corr", kind="corroboration", target="output/out.txt", spec={}, rationale="r"
    )
    corr_row = _bound((bad_corr,), product=_text_product()).local()["constraints"][0]
    assert corr_row["status"] == "failed" and corr_row["passed"] is False
    assert "spec.field" in corr_row["detail"]


def test_a_real_failure_is_never_swallowed_by_the_third_state() -> None:
    """★ A failure with its input on hand stays ``failed`` -- the whole point."""
    # A judge-run custom check fails, and its input (the product file) is there.
    failed = _bound(
        (_constraint("C-fail", "output/out.txt", "python3 check.py"),), product=_text_product()
    ).local()["constraints"][0]
    assert failed["status"] == "failed"
    assert failed["passed"] is False
    assert "FAIL" in failed["detail"]

    # Provenance on an EMPTY product: there really is nothing to trace -> false.
    empty = _bound(
        (_provenance(),),
        product=lambda: Candidate(
            subtask_id="T-01", source_id="T-01", artifacts=(Artifact(name="output/out.txt"),)
        ),
    ).local()["constraints"][0]
    assert empty["status"] == "failed"
    assert empty["passed"] is False

    # No product at all: "not found" is actionable (write the file) -> false.
    missing = _bound((_provenance(),), product=lambda: None).local()["constraints"][0]
    assert missing["status"] == "failed"
    assert missing["passed"] is False


def test_judgment_still_fails_a_candidate_whose_provenance_input_is_absent() -> None:
    """★ 判定语义一字不动：同一个候选，自查报 unverified，判定器照样 fail。

    检查器只在 ``CheckOutcome.pending_input`` 上**多写一句说明**；``ok`` / ``facts``
    与改前逐字相同，判定器也从不读那个字段。
    """
    from bobanana6.core.checkers import check_kind

    candidate = Candidate(
        subtask_id="T-01",
        source_id="T-01",
        artifacts=(Artifact(name="output/out.txt", text="line one\nline two\n"),),
    )
    outcome = check_kind(_provenance(), candidate, CheckContext())

    assert outcome.ok is False
    assert any("coverage" in fact for fact in outcome.facts), outcome.facts
    assert outcome.pending_input, "the checker must name the absent input for the self-check"


# --------------------------------------------------------------------------- #
# end-to-end: the production assembly point really injects the self-check
# --------------------------------------------------------------------------- #


def _selfcheck_task(
    tmp_path: Path, *, name: str, extra_constraints: str = "", with_constraint: bool = True
) -> Path:
    """A one-block task whose contract declares a shell-command constraint."""
    task = tmp_path / name
    (task / "input" / "data").mkdir(parents=True, exist_ok=True)
    (task / "input" / "data" / "seed.txt").write_text("draft\n", encoding="utf-8")
    (task / "ontology.yaml").write_text(
        'domain: text\nversion: "2026.1"\nconcepts:\n  text: {label: body, kind: attribute}\n',
        encoding="utf-8",
    )
    (task / "check_len.py").write_text(
        "import pathlib, sys\n"
        "p = pathlib.Path('_run/workspace/output/out.txt')\n"
        "if not p.is_file():\n"
        "    sys.exit('FAIL: output/out.txt is not on disk')\n"
        "n = len(p.read_text(encoding='utf-8').strip())\n"
        "if n < 20:\n"
        "    sys.exit(f'FAIL: only {n} chars, need 20')\n"
        "print(f'ok chars={n}')\n",
        encoding="utf-8",
    )
    declared = (
        "constraints:\n"
        "  hard:\n"
        "    - id: C-len\n"
        "      kind: custom\n"
        "      target: output/out.txt\n"
        '      spec: {check: shell_command, command: "python3 check_len.py"}\n'
        '      rationale: "the product must be at least 20 characters"\n'
        + extra_constraints
        if with_constraint
        else ""
    )
    (task / "task.yaml").write_text(
        "task_id: selfcheck\n"
        "goal: write a body\n"
        "version: 1\n"
        "deliverables:\n"
        "  - {name: output/out.txt, schema: {text: str}}\n"
        "sources:\n"
        "  - {ref: seed, locator: input/data/seed.txt}\n"
        + declared,
        encoding="utf-8",
    )
    return task


def _selfcheck_payloads(messages) -> list[dict]:  # type: ignore[no-untyped-def]
    found: list[dict] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "constraints" in payload:
            found.append(payload)
    return found


class _SelfCheckClient:
    """turn 0 write short → 1 check.local → 2 write long → 3 check.local → 4 submit."""

    def __init__(self, long_body: str) -> None:
        self.turn = 0
        self.seen: list[dict] = []
        self.long_body = long_body

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        self.seen.extend(_selfcheck_payloads(messages))
        turn = self.turn
        self.turn += 1
        if turn == 0:
            return ModelResponse(tool_calls=(_tool_call("w0", "files.write", {"path": "output/out.txt", "content": "short"}),))
        if turn == 1:
            return ModelResponse(tool_calls=(_tool_call("c1", "check.local", {}),))
        if turn == 2:
            return ModelResponse(tool_calls=(_tool_call("w1", "files.write", {"path": "output/out.txt", "content": self.long_body}),))
        if turn == 3:
            return ModelResponse(tool_calls=(_tool_call("c2", "check.local", {}),))
        return ModelResponse(text=json.dumps({"ref": "seed", "text": self.long_body}))


#: 长到满足 `check_len.py`（>=20 字符），但仍是领域无关的一句占位文本。
_LONG = "this body is long enough to satisfy the declared rule."


def test_the_production_run_wires_check_local_and_proves_both_arms(tmp_path: Path) -> None:
    """★ ⑤ 两臂 + ⑥ 注入点：不满 → false/有 detail；改到满足 → true。

    走的是**生产装配点**（`run_task`），不是手工拼 `BoundChecks`：删掉
    `runner._bound_checks` 的注入，这个测试立刻红。
    """
    task = _selfcheck_task(tmp_path, name="selfcheck-arms")
    client = _SelfCheckClient(_LONG)
    run_task(task, client=client, env={}, workspace=True, max_calls=6)

    assert client.seen, "check.local was never called (or always answered null)"
    assert len(client.seen) >= 2, client.seen

    first = {row["id"]: row for row in client.seen[0]["constraints"]}
    second = {row["id"]: row for row in client.seen[-1]["constraints"]}
    assert set(first) == {"C-len"} and set(second) == {"C-len"}

    assert first["C-len"]["passed"] is False
    assert first["C-len"]["status"] == "failed"
    assert first["C-len"]["detail"], "an unsatisfying row must carry the judge's sentence"
    assert "FAIL" in first["C-len"]["detail"]
    assert first["C-len"]["expects"] == "the product must be at least 20 characters"

    assert second["C-len"]["passed"] is True
    assert second["C-len"]["status"] == "passed"
    # ★ never the machine detail
    rendered = json.dumps(client.seen, ensure_ascii=False)
    assert "python3 check_len.py" not in rendered


#: A real ``kind: provenance`` constraint appended after the custom one.
_PROV_CONSTRAINT = (
    "    - id: C-prov\n"
    "      kind: provenance\n"
    "      target: output/out.txt\n"
    "      spec: {min_coverage: 0.6}\n"
    '      rationale: "every value must point back to a snapshot"\n'
)


class _SatisfyingClient:
    """turn 0 write a satisfying body → 1 check.local → 2 submit."""

    def __init__(self, body: str) -> None:
        self.turn = 0
        self.body = body
        self.seen: list[dict] = []

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        self.seen.extend(_selfcheck_payloads(messages))
        turn = self.turn
        self.turn += 1
        if turn == 0:
            return ModelResponse(
                tool_calls=(_tool_call("w0", "files.write", {"path": "output/out.txt", "content": self.body}),)
            )
        if turn == 1:
            return ModelResponse(tool_calls=(_tool_call("c1", "check.local", {}),))
        return ModelResponse(text=json.dumps({"ref": "seed", "text": self.body}))


def test_a_product_that_satisfies_everything_reports_no_failure(tmp_path: Path) -> None:
    """★ 决定性验收：全部满足的产物 ⇒ 没有任何一条被报成失败。

    这是本次要修掉的核心：C-prov 的输入（坐标）要到交答案才产生，自查此刻量不到，
    但它不是"没过"。``status`` 因此必须是第三态，而不是 ``failed``。
    """
    task = _selfcheck_task(tmp_path, name="selfcheck-all-pass", extra_constraints=_PROV_CONSTRAINT)
    client = _SatisfyingClient(_LONG)
    run_task(task, client=client, env={}, workspace=True, max_calls=5)

    assert client.seen, "check.local was never called"
    rows = {row["id"]: row for row in client.seen[-1]["constraints"]}
    assert set(rows) == {"C-len", "C-prov"}

    # The custom check really holds (its input, the file, is on disk).
    assert rows["C-len"]["status"] == "passed" and rows["C-len"]["passed"] is True
    # The provenance check cannot be decided yet -- third state, with a reason.
    assert rows["C-prov"]["status"] == "unverified" and rows["C-prov"]["passed"] is None
    assert rows["C-prov"]["detail"] and "验不了" in rows["C-prov"]["detail"]

    # ★ and the decisive claim: nothing is reported as a failure.
    assert [row["id"] for row in client.seen[-1]["constraints"] if row["status"] == "failed"] == []


def test_a_task_without_declared_constraints_still_answers_null(tmp_path: Path) -> None:
    """★ ④ 缺省行为不变：没有任何声明约束 ⇒ `check.local` 仍是 `null`。

    合成的 `AUTO-*` presence（`judge.bundle` 里有）**不算**声明约束，不进这个返回。
    """
    task = _selfcheck_task(tmp_path, name="selfcheck-none", with_constraint=False)

    class _Client:
        def __init__(self) -> None:
            self.turn = 0
            self.reply = ""

        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            for message in messages:
                if message.get("role") == "tool" and message.get("tool_call_id") == "c1":
                    self.reply = str(message.get("content"))
            turn = self.turn
            self.turn += 1
            if turn == 0:
                return ModelResponse(tool_calls=(_tool_call("c1", "check.local", {}),))
            return ModelResponse(text=json.dumps({"ref": "seed", "text": _LONG}))

    client = _Client()
    run_task(task, client=client, env={}, workspace=True, max_calls=4)
    assert client.reply.strip() == "null", client.reply
