"""主验题（域 3：货运）：三份来源 → 一份货主月度运输面板。

与 `panel-three-sources/` **同形状、不同领域**（`架构设计.md §15.3`：同形状 × 3 域）：
同样的「同一批实体 + 多来源 + 合并成一张表」，实体/字段/业务含义全换掉 ——
验的是泛化，不是过拟合一道题。

★ 生成器持有真值：给系统的是 `input/`，真值写 `truth/`，运行【只看 input】。
用法：`python acceptance/main/panel-freight-lanes/generate.py`（幂等，重跑覆盖）。
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: 华东：重量 kg、运费 元
LANE_EAST = (
    ("SH-01", 12_000.0, 48_000.0),
    ("SH-02", 8_500.0, 33_000.0),
    ("SH-03", 21_000.0, 76_500.0),
)
#: 华南：★ SH-03 与华东重叠（跨源同实体，§14.2 A01）
LANE_SOUTH = (
    ("SH-03", 21_000.0, 76_500.0),
    ("SH-04", 5_400.0, 19_800.0),
    ("SH-05", 33_000.0, 118_000.0),
)
#: 西部：★ 重量用【吨】记账，单位写在值里（单位异构 §14.2 A10）
#:  1 吨 = 1000 kg；12 吨 = 12000 kg
LANE_WEST = (
    ("SH-05", "33 吨", 118_000.0),
    ("SH-06", "12 吨", 41_000.0),
    ("SH-07", "4.5 吨", 15_500.0),
)

#: ★ 真值：去重后的合并面板（重量统一到 kg）。系统看不到。
TRUTH_ROWS = (
    {"shipper": "SH-01", "weight_kg": 12_000.0, "cost_cny": 48_000.0},
    {"shipper": "SH-02", "weight_kg": 8_500.0, "cost_cny": 33_000.0},
    {"shipper": "SH-03", "weight_kg": 21_000.0, "cost_cny": 76_500.0},
    {"shipper": "SH-04", "weight_kg": 5_400.0, "cost_cny": 19_800.0},
    {"shipper": "SH-05", "weight_kg": 33_000.0, "cost_cny": 118_000.0},
    {"shipper": "SH-06", "weight_kg": 12_000.0, "cost_cny": 41_000.0},
    {"shipper": "SH-07", "weight_kg": 4_500.0, "cost_cny": 15_500.0},
)


def _write_csv(path: Path, rows: tuple[tuple[str, float | str, float], ...]) -> None:
    lines = ["shipper,weight_kg,cost_cny"]
    lines.extend(f"{shipper},{weight},{cost}" for shipper, weight, cost in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate(*, root: Path | None = None) -> dict[str, Path]:
    """Write ``input/`` (for the system) and ``truth/`` (for the judge)."""
    base = HERE if root is None else Path(root)
    given = base / "input"
    truth = base / "truth"
    given.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)

    _write_csv(given / "lane-east.csv", LANE_EAST)
    _write_csv(given / "lane-south.csv", LANE_SOUTH)
    _write_csv(given / "lane-west.csv", LANE_WEST)

    (truth / "truth.json").write_text(
        json.dumps(
            {
                "deliverable": "shipments.csv",
                "key": "shipper",
                "rows": list(TRUTH_ROWS),
                "injected": {
                    "A01-mesh-dependency": "SH-03 同时出现在 lane-east 与 lane-south",
                    "A10-units": "lane-west 的 weight_kg 用吨记账，值里自带单位（读取块归一到 kg）",
                },
                "expected_shippers": sorted(row["shipper"] for row in TRUTH_ROWS),
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
