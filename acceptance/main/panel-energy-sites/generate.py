"""主验题（域 2：能源）：三份来源 → 一份风电场站出力面板。

与 `panel-three-sources/` **同形状、不同领域**（`架构设计.md §15.3`：同形状 × 3 域）：
同样的「同一批实体 + 多来源 + 合并成一张表」，实体/字段/业务含义全换掉 ——
验的是泛化，不是过拟合一道题。

★ 生成器持有真值：给系统的是 `input/`，真值写 `truth/`，运行【只看 input】。
用法：`python acceptance/main/panel-energy-sites/generate.py`（幂等，重跑覆盖）。
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: 北区：容量 MW、出力 MWh
SITE_NORTH = (
    ("WF-01", 50.0, 120.0),
    ("WF-02", 30.0, 75.5),
    ("WF-03", 20.0, 48.0),
)
#: 南区：★ WF-03 与北区重叠（跨源同实体，§14.2 A01）
SITE_SOUTH = (
    ("WF-03", 20.0, 48.0),
    ("SF-01", 40.0, 96.5),
    ("SF-02", 25.0, 60.0),
)
#: 沿海：★ 出力用【千瓦时】记账，单位写在值里（单位异构 §14.2 A10）
#:  1 kWh = 0.001 MWh；60000 kWh = 60 MWh
SITE_COAST = (
    ("SF-02", 25.0, "60000 kWh"),
    ("CF-01", 35.0, "90000 kWh"),
    ("CF-02", 15.0, "45000 kWh"),
)

#: ★ 真值：去重后的合并面板（出力统一到 MWh）。系统看不到。
TRUTH_ROWS = (
    {"site": "WF-01", "capacity_mw": 50.0, "output_mwh": 120.0},
    {"site": "WF-02", "capacity_mw": 30.0, "output_mwh": 75.5},
    {"site": "WF-03", "capacity_mw": 20.0, "output_mwh": 48.0},
    {"site": "SF-01", "capacity_mw": 40.0, "output_mwh": 96.5},
    {"site": "SF-02", "capacity_mw": 25.0, "output_mwh": 60.0},
    {"site": "CF-01", "capacity_mw": 35.0, "output_mwh": 90.0},
    {"site": "CF-02", "capacity_mw": 15.0, "output_mwh": 45.0},
)


def _write_csv(path: Path, rows: tuple[tuple[str, float, float | str], ...]) -> None:
    lines = ["site,capacity_mw,output_mwh"]
    lines.extend(f"{site},{capacity},{output}" for site, capacity, output in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate(*, root: Path | None = None) -> dict[str, Path]:
    """Write ``input/`` (for the system) and ``truth/`` (for the judge)."""
    base = HERE if root is None else Path(root)
    given = base / "input"
    truth = base / "truth"
    given.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)

    _write_csv(given / "site-north.csv", SITE_NORTH)
    _write_csv(given / "site-south.csv", SITE_SOUTH)
    _write_csv(given / "site-coast.csv", SITE_COAST)

    (truth / "truth.json").write_text(
        json.dumps(
            {
                "deliverable": "output.csv",
                "key": "site",
                "rows": list(TRUTH_ROWS),
                "injected": {
                    "A01-mesh-dependency": "WF-03 同时出现在 site-north 与 site-south",
                    "A10-units": "site-coast 的 output_mwh 用 kWh 记账，值里自带单位（读取块归一到 MWh）",
                },
                "expected_sites": sorted(row["site"] for row in TRUTH_ROWS),
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
