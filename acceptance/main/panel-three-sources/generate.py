"""合成数据生成器：**ground truth 由生成器持有**（`架构设计.md §15.4`）。

为什么要合成而不是找现成任务：合成才能**精确注入**每一种攻击面（同名不同口径、
缺失格、单位混用、跨源依赖），也才能有 ground truth 去判对错。代价是数据太干净、
模型可能从规律里猜出答案 —— 所以 `main/` 另混少量真实公开源，判「它有没有正确声明
不确定」而不是判答对（见 `acceptance/main/README.md`）。

★ 输入与真值【物理分开】：给系统的是 `input/`，真值写 `truth/`，运行【只看 input】。

用法：`python acceptance/main/panel-three-sources/generate.py`（幂等，重跑覆盖）。
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: 三个源各覆盖不同主体；★ B 与 C 之间有重叠，用来考「跨源实体依赖」（§14.2 A01）。
PANEL_A = (
    ("A-001", 0.011, 1_200_000.0),
    ("A-002", 0.024, 800_000.0),
    ("A-003", 0.007, 400_000.0),
)
PANEL_B = (
    ("A-003", 0.007, 400_000.0),  # 与 A 重叠：同一个主体在两份来源里都出现
    ("B-001", 0.019, 1_500_000.0),
    ("B-002", 0.033, 250_000.0),
)
#: ★ C 的金额单位是【千元】，而且**值里自带单位**（单位攻击 §14.2 A10）：
#: `实现规格.md §5.0.1` 判据② —— 单位绑进值里（Kubernetes `Quantity` 形态），
#: 于是「值 250、单位忘了声明」不可能发生；读取块归一到元（判据①：单位声明在
#: 字段自己的定义处，读的时候正是那个地方）。
PANEL_C = (
    ("B-002", 0.033, "250 千元"),  # 250 千元 = 250_000 元，与 B 一致
    ("C-001", 0.058, "90 千元"),  # 90 千元 = 90_000 元
    ("C-002", 0.002, "3000 千元"),  # 3_000 千元 = 3_000_000 元
)

#: ★ 真值：去重后的合并面板（金额统一到【元】）。生成器持有，系统看不到。
TRUTH_ROWS = (
    {"obligor": "A-001", "pd": 0.011, "amount": 1_200_000.0},
    {"obligor": "A-002", "pd": 0.024, "amount": 800_000.0},
    {"obligor": "A-003", "pd": 0.007, "amount": 400_000.0},
    {"obligor": "B-001", "pd": 0.019, "amount": 1_500_000.0},
    {"obligor": "B-002", "pd": 0.033, "amount": 250_000.0},
    {"obligor": "C-001", "pd": 0.058, "amount": 90_000.0},
    {"obligor": "C-002", "pd": 0.002, "amount": 3_000_000.0},
)


def _write_csv(path: Path, rows: tuple[tuple[str, float, float | str], ...]) -> None:
    lines = ["obligor,pd,amount"]
    lines.extend(f"{name},{pd},{amount}" for name, pd, amount in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate(*, root: Path | None = None) -> dict[str, Path]:
    """Write ``input/`` (for the system) and ``truth/`` (for the judge)."""
    base = HERE if root is None else Path(root)
    given = base / "input"
    truth = base / "truth"
    given.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)

    _write_csv(given / "panel-a.csv", PANEL_A)
    _write_csv(given / "panel-b.csv", PANEL_B)
    _write_csv(given / "panel-c.csv", PANEL_C)

    (truth / "truth.json").write_text(
        json.dumps(
            {
                "deliverable": "panel.csv",
                "rows": list(TRUTH_ROWS),
                "injected": {
                    "A01-mesh-dependency": "A-003 同时出现在 panel-a 与 panel-b",
                    "A10-units": "panel-c 的 amount 是千元，值里自带单位（读取块负责归一到元）",
                },
                "expected_obligors": sorted(row["obligor"] for row in TRUTH_ROWS),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"input": given, "truth": truth}


def main() -> int:
    paths = generate()
    print(f"input : {paths['input']}")
    print(f"truth : {paths['truth']}  (★ 运行只看 input/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
