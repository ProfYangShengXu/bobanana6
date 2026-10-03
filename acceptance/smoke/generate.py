"""冒烟集：3 个小任务（`实现规格.md §14.1`）。

每个都是**单源、单块或小块**，目的是**分钟级门禁**：改动前后能不能跑通。
题目由本脚本生成到 `smoke/<task>/input/`（真值不需要：这 3 道题没有注入攻击，
判据是「块级通过率不低于基线容差」，见 `baseline.json`）。

★ 为什么 3 道题都是单源：确定性的离线替身（`EchoToolResultClient`）在**授权来源
多于 1 个**时会拒绝「你自己挑一个」的调用（这是有意的歧义拒绝），所以多源题在
`--offline` 下跑不出通过数。多源/多块那条链路由 `main/` 的脚本化机制测试与真模型
跑测覆盖，不塞进冒烟集。
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

ROWS = ("A-001,0.011,1200000", "A-002,0.024,800000", "A-003,0.007,400000")

ONTOLOGY = """
domain: credit-risk
version: "2026.1"
concepts:
  obligor: {label: 债务人主体号, kind: entity}
  pd: {label: 违约概率, kind: measure, unit: ratio}
  amount: {label: 敞口金额, kind: measure, unit: CNY}
units:
  pd: {symbol: ratio, scale: 1.0, base: ratio}
  amount: {symbol: CNY, scale: 1.0, base: CNY}
"""


def _task_text(task_id: str, deliverable_block: str, schema: str, extra: str = "") -> str:
    return f"""task_id: {task_id}
goal: 把一份来源整理成可复核的面板
version: 1

deliverables:
{deliverable_block}

sources:
  - {{ref: src-1, locator: input/panel.csv}}

constraints:
  hard:
    - id: C-rows
      kind: presence
      target: out.csv
      spec: {{min_rows: 1}}
      rationale: "面板不允许为空"
    # ★ 刻意不用 target="*"：装配关把所有 piece 拼成一个候选，
    #   而 artifact_for 只在候选恰好一个产物时解析 `*` —— 多块时 `*` 检查
    #   必然报「artifact not found」。这一条已作为发现上报。
    - id: C-prov
      kind: provenance
      target: out.csv
      spec: {{min_coverage: 0.95}}
      rationale: "每个值都要能指回一份快照"
{extra}
notes: 冒烟题（单源、分钟级）
"""


def generate(*, root: Path | None = None) -> tuple[Path, ...]:
    base = HERE if root is None else Path(root)
    written: list[Path] = []

    tasks = {
        # ① 最小形状：单源单交付物
        "smoke-panel-one-source": _task_text(
            "smoke-panel-one-source",
            "  - {name: out.csv, schema: {obligor: str, pd: float, amount: float}}",
            "{obligor: str, pd: float, amount: float}",
        ),
        # ② 多个交付物：两个同层写块，partition 必须互斥
        "smoke-panel-two-deliverables": _task_text(
            "smoke-panel-two-deliverables",
            "  - {name: out.csv, schema: {obligor: str, pd: float, amount: float}}\n"
            "  - {name: summary.csv, schema: {obligor: str, pd: float}}",
            "{obligor: str, pd: float, amount: float}",
            extra=(
                "    - id: C-shape\n"
                "      kind: schema\n"
                "      target: out.csv\n"
                "      spec: {columns: [{name: obligor, dtype: str}, {name: pd, dtype: str}, {name: amount, dtype: str}]}\n"
                '      rationale: "合并后列名与类型必须仍然一致"\n'
            ),
        ),
        # ③ 交付物上再压一条更紧的界：约束一多，判定顺序与证据都要能出得来
        "smoke-panel-tighter-bound": _task_text(
            "smoke-panel-tighter-bound",
            "  - {name: out.csv, schema: {obligor: str, pd: float, amount: float}}",
            "{obligor: str, pd: float, amount: float}",
            extra=(
                "    - id: C-rows-tight\n"
                "      kind: presence\n"
                "      target: out.csv\n"
                "      spec: {min_rows: 3}\n"
                '      rationale: "三个主体一行都不能少"\n'
            ),
        ),
    }

    for name, text in tasks.items():
        task_dir = base / name
        (task_dir / "input").mkdir(parents=True, exist_ok=True)
        (task_dir / "input" / "panel.csv").write_text(
            "obligor,pd,amount\n" + "\n".join(ROWS) + "\n", encoding="utf-8"
        )
        (task_dir / "ontology.yaml").write_text(ONTOLOGY, encoding="utf-8")
        (task_dir / "task.yaml").write_text(text, encoding="utf-8")
        written.append(task_dir)

    (base / "tasks.json").write_text(
        json.dumps({"tasks": sorted(tasks)}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tuple(written)


def main() -> int:
    for path in generate():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
