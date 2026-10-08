#!/usr/bin/env python
"""判据唯一性（防「判定逻辑长出第二个副本」）—— 变异**判据**，看**报告**会不会变。

```
做法：给一个 checker 注入判据变异 → 跑同一份快照 → 报告必须出现差异
  有差异 ⇒ 走对了（判定逻辑只有一份）
  无差异 ⇒ ★ 报警：调度层/别处有一份【硬编码的判据副本】
```

★ 与 `tools/reverse_validation.py`（变异测试）是同一族、方向相反：

```
reverse_validation   变异【代码】 ⇒ 看**测试**会不会红
本工具                变异【判据】 ⇒ 看**报告**会不会变
```

边界：

```
① 只挑【有明确数值判据】的 checker（presence / range / coverage / corroboration）
② 变异必须可还原，且还原后读数逐字回到原样（本工具自己验证 sha256）
③ 输出必须直接说清"哪一份判据没跟着变"
```

用法：

    .venv/bin/python tools/criterion_uniqueness.py            # 跑全部变异（JSON）
    .venv/bin/python tools/criterion_uniqueness.py --self-test # 反证：把"无差异"喂给判定 ⇒ 必须报警
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: 每个变异：哪个文件、哪一行、改成什么、期望哪条 violation 出现/消失。
#: ★ 只动**判据本身**（阈值挪一位 / 恒真恒假），不动调度、不动数据。
@dataclass(frozen=True)
class CriterionMutation:
    ident: str
    relative_path: str
    old: str
    new: str
    #: 期望在报告里看到的那个 violation 的 `who`（`<块>/<约束>`）。
    expect: str
    note: str


MUTATIONS: tuple[CriterionMutation, ...] = (
    CriterionMutation(
        "presence-threshold-shifted",
        "core/checkers/presence.py",
        "    if artifact.row_count < min_rows:",
        "    if artifact.row_count < min_rows + 1:",
        "T-01/C-p",
        "阈值挪一位：1 行的产物在 min_rows=1 下从通过变成不通过",
    ),
    CriterionMutation(
        "range-lower-bound-shifted",
        "core/checkers/range.py",
        "        if low is not None and value < low - tol:",
        "        if low is not None and value < low - tol + 1:",
        "T-01/C-r",
        "下界收紧一格：值恰好落在下界上的那一行本应通过，变异后不通过 ⇒ 报告里应当**多出**那条 violation",
    ),
    CriterionMutation(
        "coverage-always-passes",
        "core/checkers/coverage.py",
        "    if undeclared:",
        "    if False:  # criterion mutated: coverage can never fail",
        "T-01/C-cov",
        "让 coverage 恒过：本应报的缺来源 violation 应当消失",
    ),
)


#: 子进程里那份「同一快照」的构造 + 判定的源码。
#: ★ 它只 import 产品代码与 `services/ci`，不 import 本工具 —— 变异要在**新解释器**里生效
#:   （已 import 的模块不会因为文件被改而变）。
CHILD = (
    r"""
import json, sys
sys.path.insert(0, "__REPO__")
from bobanana6.core.model import Artifact, Column, Constraint, ConstraintBundle, SplitPlan, Subtask, Verdict
from bobanana6.services.ci import BlockCapture, LAYER_BLOCK, build_snapshot, run_ci


def constraint(cid, kind, spec, target):
    return Constraint(id=cid, kind=kind, target=target, spec=spec, rationale="fixture")


def artifact(name, rows):
    names = sorted({k for row in rows for k in row})
    return Artifact(name=name, columns=tuple(Column(name=n, dtype="int") for n in names), rows=tuple(rows))


class Source:
    # 每个块一份切片（真跑里这一步是 runtime.block_bundle）

    def __init__(self, slices): self.slices = slices
    def contract(self): return None
    def ontology(self): return None
    def bundle(self, plan): raise AssertionError("the delivery layer must not run in this fixture")
    def block_bundle(self, block_id, plan): return self.slices[block_id]
    def artifact(self, pointer): raise AssertionError
    def snapshot(self, entry): raise AssertionError
    def snapshot_body(self, entry): return None


def plan_of():
    return SplitPlan(task_id="fixture", version=1, round=1, shape="star", subtasks=(
        Subtask(id="T-01", goal="g", inputs=(), output="a.csv", contract_ref="c", axis="source", mode="write"),
        Subtask(id="T-02", goal="g", inputs=(), output="b.csv", contract_ref="c", axis="source", mode="write"),
    ))


# ★ 夹具的安排（每一臂都成立）：
#   T-01 第 1 层**全过**（1 行够、值恰好落在下界上）⇒ 被变异的判据能把报告翻转；
#   T-02 第 2 层**必失败**（coverage 一个来源都没引）⇒ 交付级门不跑，夹具不需要契约。
slices = {
    "T-01": ConstraintBundle(task_id="fixture", version=1, hard=(
        constraint("C-p", "presence", {"min_rows": 1}, "T-01"),
        constraint("C-r", "range", {"column": "y", "min": 10, "max": 20}, "T-01"),
        constraint("C-cov", "coverage", {"required_sources": ["panel-a"]}, "T-01"),
    )),
    "T-02": ConstraintBundle(task_id="fixture", version=1, hard=(
        constraint("C-cov-b", "coverage", {"required_sources": ["panel-b"]}, "T-02"),
        # ★ 这一条**不参与任何变异**：它保证"每一臂都有一次第 2 层停块"，
        #   于是交付级门在每一臂都不跑（夹具不需要契约），
        #   否则 coverage 的"恒过"变异会顺手把停块也弄没，读数就被别的东西污染了。
        constraint("C-rel-b", "relation", {"op": "lt", "left": {"column": "y"}, "right": {"column": "x"}}, "T-02"),
    )),
}
document = build_snapshot(task_id="fixture", plan=plan_of(), blocks=(
    BlockCapture(subtask_id="T-01", output="a.csv", verdict=Verdict(ok=True),
                 artifacts=(artifact("a.csv", [{"x": 1, "y": 10}]),)),
    BlockCapture(subtask_id="T-02", output="b.csv", verdict=Verdict(ok=True),
                 artifacts=(artifact("b.csv", [{"x": 1, "y": 10}]),)),
))
report = run_ci(document, Source(slices))
print(json.dumps({
    "block_layer": sorted(v.who for v in report.violations if v.layer == LAYER_BLOCK),
    "assembly_layer": sorted(v.who for v in report.violations if v.layer == "assembly"),
    "blocks": report.blocks,
    "layers_run": list(report.layers_run),
    "digest": report.digest(),
}, sort_keys=True))
"""
).replace("__REPO__", str(REPO))


def _run_child() -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", CHILD], capture_output=True, text=True, cwd=str(REPO), check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"the fixture run failed:\n{completed.stderr[-2000:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def compare(baseline: dict, mutated: dict) -> list[str]:
    """★ 判据变了，报告就必须变。没变 = 报警（判定逻辑有第二个副本）。"""
    if baseline == mutated:
        return [
            (
                "the report did NOT change after the criterion was mutated -- there is a second, "
                "hardcoded copy of the criterion somewhere between the snapshot and the report"
            )
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true", help="反证：同一个报告 ⇒ 必须报警")
    parser.add_argument("--only", help="只跑一个变异 ident")
    arguments = parser.parse_args(argv)

    if arguments.self_test:
        same = {"block_layer": [], "blocks": {"T-01": "ok"}, "digest": "x"}
        problems = compare(same, dict(same))
        print(json.dumps({"schema": "bobanana6.criterion-uniqueness.selftest/1", "problems": problems}, indent=1))
        return 1 if problems else 0

    baseline = _run_child()
    findings: list[dict[str, object]] = []
    failures: list[str] = []
    for mutation in MUTATIONS:
        if arguments.only and arguments.only != mutation.ident:
            continue
        path = REPO / "src" / "bobanana6" / mutation.relative_path
        original = path.read_bytes()
        digest_before = hashlib.sha256(original).hexdigest()
        try:
            text = original.decode("utf-8")
            if text.count(mutation.old) != 1:
                failures.append(
                    f"{mutation.ident}: the criterion anchor is not exactly once "
                    f"({text.count(mutation.old)}) -- the criterion this mutation names is gone"
                )
                continue
            path.write_text(text.replace(mutation.old, mutation.new), encoding="utf-8", newline="\n")
            mutated = _run_child()
        finally:
            path.write_bytes(original)
        restored = hashlib.sha256(path.read_bytes()).hexdigest()
        problems = compare(baseline, mutated)
        # ★ 还原后读数必须逐字回到原样（否则"可还原"是句空话）
        if restored != digest_before:
            problems.append(f"{mutation.ident}: restore changed the file ({digest_before} -> {restored})")
        if _run_child() != baseline:
            problems.append(f"{mutation.ident}: after restore the report did not return to the baseline")
        entry = {
            "mutation": mutation.ident,
            "criterion": f"{mutation.relative_path}: {mutation.old.strip()} -> {mutation.new.strip()}",
            "note": mutation.note,
            "baseline_block_layer": baseline["block_layer"],
            "mutated_block_layer": mutated["block_layer"],
            "changed": baseline != mutated,
            "expect": mutation.expect,
            "restored_sha256": restored,
            "problems": problems,
        }
        findings.append(entry)
        failures.extend(f"{mutation.ident}: {problem}" for problem in problems)

    print(
        json.dumps(
            {
                "schema": "bobanana6.criterion-uniqueness/1",
                "baseline": baseline,
                "mutations": findings,
                "problems": failures,
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    if failures:
        print("\n判据唯一性不成立：", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
