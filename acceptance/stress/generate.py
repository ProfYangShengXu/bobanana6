"""压力任务 A（信息类，`架构设计.md §15.4`）：24 个异构源 → 实体 × 时间面板。

**五条异构（每条都要在数据里真的出现）**：

| 维度 | 本任务里的形态 |
|---|---|
| 格式 | CSV · HTML 表格 · API JSON · 纯文本年报 · 扫描件（图像式）PDF 的文本模拟 |
| 语言 | 中 / 英 / 日 —— 实体名与字段名都不同写法（本体用 aliases 收） |
| 时间 | 日历年 vs 财年；报告期 vs 公告日；**同一实体两个修订版本** |
| 单位 | 元 / 千元 / 百万元，且**有的源不标**（值里带单位 = §5.0.1② 的 Quantity 形态） |
| 结构 | 长表 vs 宽表（年度做列）· 一实体一行 vs 一实体多年 · 缺失有 `空`/`0`/`-`/`N/A` |

**九条攻击面**（造了什么 → 见 `README.md` 的逐条表）：

```
① 网状依赖  s19 是「内部码 ↔ 实体号」的对照表；s13..s18 用的是内部码
② 多义概念  revenue 在本体里有 4 个口径版本（合并/母公司/含税/不含税）
③ 真冲突    s20 与 s02 对同一个 (实体, 年) 给出不同值，且**都不是错的**
④ 缺失陷阱  缺失有四种写法；`revenue` 列允许缺、`exposure` 列不允许
⑤ 瞬时故障  ★ 见 README：压力题本身没有假故障通道，由注入集 A05 覆盖
⑥ 持久故障  ★ s24 的输入文件【故意不生成】→ 取源失败 → 走 A06 的降级
⑦ 同症异层  由真模型跑测自然产生（哪几条走到见交付报告）
⑧ 全局不变量 面板的 exposure 必须同一单位、revenue 同一口径版本（点名交付物表达）
⑨ 不可能块  ENT-12 的 revenue 只在 s24（已下线）里 → 面板不许出现它
```

**真值纪律**：`truth/` 由本生成器持有，运行期只挂 `input/`（`input/` 与 `truth/`
物理分开；`tests/test_stress_task.py` 有一条测试证明运行期读不到 truth）。

用法：`python acceptance/stress/generate.py`（幂等，重跑覆盖）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

ENTITIES: tuple[str, ...] = tuple(f"ENT-{index:02d}" for index in range(1, 13))
#: ★ ⑨：ENT-12 的关键字段只存在于 s24，而 s24 的输入【故意不生成】。
IMPOSSIBLE = "ENT-12"
YEARS: tuple[int, ...] = (2021, 2022, 2023, 2024)

#: ★ ②：revenue 的四个口径版本（本体里都声明；交付物必须钉在 canonical 上）。
CANONICAL_REVENUE_VERSION = "v1-group-consolidated"
REVENUE_VERSIONS: tuple[str, ...] = (
    CANONICAL_REVENUE_VERSION,
    "v-parent-only",
    "v-incl-tax",
    "v-excl-tax",
)

#: ★ ③：真冲突 —— 这两个源对同一个 (实体, 年) 给出不同值，都不是错的。
CONFLICT = {"entity": "ENT-03", "year": 2022, "field": "revenue", "sources": ("s02", "s20")}

#: ★ ④：缺失的四种写法，源里都出现，且都不标注。
MISSING_TOKENS: tuple[str, ...] = ("", "0", "-", "N/A")
#: ★ ④：这些格子真值是「缺」——面板里填了任何值都是错的（`exposure` 列不允许为空）。
MISSING_CELLS: tuple[tuple[str, int, str], ...] = (
    ("ENT-05", 2022, "revenue"),  # 空
    ("ENT-07", 2023, "revenue"),  # -
    ("ENT-09", 2021, "revenue"),  # 0
    ("ENT-02", 2023, "revenue"),  # N/A
)

#: 语言 → 字段写法（本体用 aliases 收；解析出的 concept id 是同一套）。
FIELD_NAMES: dict[str, dict[str, str]] = {
    "zh": {"obligor": "债务人", "year": "年度", "revenue": "营业收入", "exposure": "敞口金额"},
    "en": {"obligor": "obligor", "year": "year", "revenue": "revenue", "exposure": "exposure"},
    "ja": {"obligor": "取引先", "year": "会計年度", "revenue": "売上高", "exposure": "与信額"},
}
def entity_value(entity: str, lang: str) -> str:
    """★ 语言异构：同一个实体在中/英/日三种源里写法不同（s19 是它们的对照表）。"""
    index = ENTITIES.index(entity) + 1
    return {"zh": f"主体{index:02d}", "en": f"Entity-{index:02d}", "ja": f"取引{index:02d}"}[lang]


def alternate_forms(entity: str) -> tuple[str, ...]:
    return tuple(entity_value(entity, lang) for lang in ("zh", "en", "ja")) + (INTERNAL_CODES[entity],)
#: ★ ①：内部码（只有 s19 知道它对应哪个实体）。
INTERNAL_CODES: dict[str, str] = {entity: f"OBL{index:03d}" for index, entity in enumerate(ENTITIES, start=1)}

#: 单位：symbol -> 缩放（真值以元为基准）。
UNITS: dict[str, float] = {"元": 1.0, "千元": 1000.0, "百万元": 1_000_000.0}


def canonical_revenue(entity: str, year: int) -> float:
    """★ 真值：合并口径的营业收入（元）。"""
    index = ENTITIES.index(entity) + 1
    return float(index * 1_000_000 + (year - 2021) * 10_000)


def canonical_exposure(entity: str, year: int) -> float:
    """★ 真值：敞口金额（元）。"""
    index = ENTITIES.index(entity) + 1
    return float(index * 20_000_000 + (year - 2021) * 100_000)


def _cell(token: str, value: float, unit: str) -> str:
    """把真值写成源里的样子：按该源的单位换算，并按该源是否标注单位决定写法。"""
    scaled = value / UNITS[unit]
    text = str(int(scaled)) if float(scaled).is_integer() else f"{scaled:.6g}"
    return f"{text} {unit}" if token == "labeled" else text


def _rows_for(entity: str, unit: str, labeled: bool, lang: str = "en") -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for year in YEARS:
        revenue = canonical_revenue(entity, year)
        exposure = canonical_exposure(entity, year)
        if (entity, year, "revenue") in MISSING_CELLS:
            revenue_text = MISSING_TOKENS[MISSING_CELLS.index((entity, year, "revenue"))]
        else:
            revenue_text = _cell("labeled" if labeled else "", revenue, unit)
        rows.append(
            (
                entity_value(entity, lang),
                year,
                revenue_text,
                _cell("labeled" if labeled else "", exposure, unit),
            )
        )
    return rows


def _csv(path: Path, names: dict[str, str], rows: list[tuple[Any, ...]]) -> None:
    header = [names["obligor"], names["year"], names["revenue"], names["exposure"]]
    lines = [",".join(header)]
    lines.extend(",".join(str(cell) for cell in row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _csv_wide(path: Path, names: dict[str, str], rows: list[tuple[Any, ...]]) -> None:
    """★ 结构异构：实体一行，年度做列（读取块必须自己 unpivot）。"""
    header = [names["obligor"], *[f"{names['year']}{year}" for year in YEARS]]
    lines = [",".join(header)]
    for row in rows:
        entity, year, revenue, _exposure = row
        cells = {name: "" for name in header}
        cells[names["obligor"]] = str(entity)
        cells[f"{names['year']}{year}"] = str(revenue)
        lines.append(",".join(cells[name] for name in header))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _html_rows(path: Path, names: dict[str, str], rows: list[tuple[Any, ...]]) -> None:
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    path.write_text(
        "<html><body><table border=1><thead><tr>"
        + "".join(f"<th>{names[key]}</th>" for key in ("obligor", "year", "revenue", "exposure"))
        + "</tr></thead><tbody>"
        + body
        + "</tbody></table></body></html>\n",
        encoding="utf-8",
    )


def _html(path: Path, names: dict[str, str], entity: str, unit: str, labeled: bool, lang: str = "en") -> None:
    _html_rows(path, names, _rows_for(entity, unit, labeled, lang))


def _json_rows(path: Path, names: dict[str, str], rows: list[tuple[Any, ...]]) -> None:
    payload = {
        "table": "exposure_panel",
        "columns": [names["obligor"], names["year"], names["revenue"], names["exposure"]],
        "records": [
            {names["obligor"]: row[0], names["year"]: row[1], names["revenue"]: row[2], names["exposure"]: row[3]}
            for row in rows
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _json(path: Path, names: dict[str, str], entity: str, unit: str, labeled: bool, lang: str = "en") -> None:
    _json_rows(path, names, _rows_for(entity, unit, labeled, lang))


def _text(
    path: Path, names: dict[str, str], entity: str, unit: str, labeled: bool, *, lang: str = "en", noise: bool = False
) -> None:
    """纯文本年报 / 扫描件 PDF 的文本模拟（`noise` 打开 OCR 式噪声）。"""
    rows = _rows_for(entity, unit, labeled, lang)
    lines = [f"{names['obligor']} | {names['year']} | {names['revenue']} | {names['exposure']}"]
    for row in rows:
        line = " | ".join(str(cell) for cell in row)
        if noise:
            line = line.replace("-", "—").replace("1", "l").replace("0", "O")
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _code_rows(unit: str, labeled: bool, lang: str) -> list[tuple[Any, ...]]:
    """★ ①：用【内部码】而不是实体号的源（要拿 s19 才查得回实体）。"""
    rows: list[tuple[Any, ...]] = []
    for entity in ENTITIES:
        if entity == IMPOSSIBLE:
            continue
        for year in YEARS:
            rows.append(
                (
                    INTERNAL_CODES[entity],
                    year,
                    _cell("labeled" if labeled else "", canonical_revenue(entity, year), unit),
                    _cell("labeled" if labeled else "", canonical_exposure(entity, year), unit),
                )
            )
    return rows


#: 24 个源：`(ref, 格式, 语言, 单位, 是否标注, 覆盖)`
#: 覆盖：`entity:<实体>` / `year:<年>` / `codes` / `crosswalk` / `conflict` / `revision:<before|after>` / `dead`
SOURCE_SPECS: tuple[tuple[str, str, str, str, bool, str], ...] = (
    ("s01", "csv", "zh", "元", False, "entity:ENT-01"),
    ("s02", "csv", "en", "千元", True, "entity:ENT-03"),
    ("s03", "html", "ja", "百万元", False, "entity:ENT-04"),
    ("s04", "json", "zh", "元", True, "entity:ENT-05"),
    ("s05", "text", "en", "千元", False, "entity:ENT-06"),
    ("s06", "csv", "ja", "元", False, "entity:ENT-07"),
    ("s07", "html", "zh", "千元", True, "entity:ENT-08"),
    ("s08", "json", "en", "百万元", True, "entity:ENT-09"),
    ("s09", "text", "ja", "元", True, "entity:ENT-10"),
    ("s10", "csv", "zh", "千元", False, "entity:ENT-11"),
    ("s11", "json", "ja", "元", True, "entity:ENT-02"),
    ("s12", "pdf", "en", "元", False, "entity:ENT-01"),
    ("s13", "csv", "zh", "千元", True, "codes"),
    ("s14", "json", "en", "百万元", False, "codes"),
    ("s15", "html", "ja", "元", True, "codes"),
    ("s16", "csv", "en", "元", False, "year:2023"),
    ("s17", "json", "zh", "千元", True, "year:2024"),
    ("s18", "text", "ja", "百万元", False, "year:2021"),
    ("s19", "csv", "en", "元", False, "crosswalk"),
    ("s20", "csv", "zh", "元", True, "conflict"),
    ("s21", "csv", "en", "元", False, "revision:before"),
    ("s22", "csv", "en", "元", False, "revision:after"),
    ("s23", "csv", "ja", "元", False, "entity:ENT-06"),
    ("s24", "pdf", "zh", "元", False, "dead"),
)


def _write_source(base: Path, spec: tuple[str, str, str, str, bool, str]) -> Path | None:
    ref, fmt, lang, unit, labeled, cover = spec
    if cover == "dead":
        # ★ ⑥/⑨：声明了、但【故意不生成】—— 取源失败 → A06 的降级；
        #   而 ENT-12 的 revenue 只在这里 → 面板不许编它。
        return None
    names = FIELD_NAMES[lang]
    path = base / f"{ref}.{ {'csv': 'csv', 'html': 'html', 'json': 'json', 'text': 'txt', 'pdf': 'pdf'}[fmt] }"
    if cover == "crosswalk":
        # ★ ① 网状依赖 + 语言异构：内部码 / 中 / 英 / 日三种写法 → 规范主体号。
        lines = ["alias,entity"]
        for entity in ENTITIES:
            lines.extend(f"{form},{entity}" for form in alternate_forms(entity))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    if cover == "conflict":
        # ★ ③：与 s02 同口径同一年、不同值（都不是错的）—— 冲突不能被悄悄抹平。
        entity, year = CONFLICT["entity"], CONFLICT["year"]
        rows = _rows_for(entity, unit, False, lang)
        rows = [
            (row[0], row[1], ("9000000" if row[1] == year else row[2]), row[3]) for row in rows
        ]
        _csv(path, names, rows)
        return path
    if cover.startswith("revision:"):
        # ★ 时间异构：同一个源的两个版本（重述前 / 重述后）。
        rows = _rows_for("ENT-01", unit, False, lang)
        if cover.endswith("before"):
            rows = [(row[0], row[1], ("1234567" if row[1] == 2021 else row[2]), row[3]) for row in rows]
        else:
            rows = [(row[0], row[1], ("7654321" if row[1] == 2021 else row[2]), row[3]) for row in rows]
        _csv(path, names, rows)
        return path
    if cover == "codes":
        rows = _code_rows(unit, labeled, lang)
        code_names = {**names, "obligor": "internal_code"}
        if fmt == "csv":
            _csv(path, code_names, rows)
        elif fmt == "json":
            _json_rows(path, code_names, rows)
        else:
            _html_rows(path, code_names, rows)
        return path
    if cover.startswith("year:"):
        year = int(cover.split(":")[1])
        rows = [
            row
            for entity in ENTITIES
            if entity != IMPOSSIBLE
            for row in _rows_for(entity, unit, labeled, lang)
            if row[1] == year
        ]
        if fmt == "json":
            _json_rows(path, names, rows)
        elif fmt == "text":
            lines = [f"{names['obligor']} | {names['year']} | {names['revenue']} | {names['exposure']}"]
            lines.extend(" | ".join(str(cell) for cell in row) for row in rows)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            _csv(path, names, rows)
        return path

    entity = cover.split(":")[1]
    if fmt == "csv":
        if entity == "ENT-11":
            _csv_wide(path, names, _rows_for(entity, unit, labeled, lang))  # ★ 结构异构：宽表
        else:
            _csv(path, names, _rows_for(entity, unit, labeled, lang))
    elif fmt == "html":
        _html(path, names, entity, unit, labeled, lang)
    elif fmt == "json":
        _json(path, names, entity, unit, labeled, lang)
    elif fmt == "pdf":
        _text(path, names, entity, unit, labeled, lang=lang, noise=True)
    else:
        _text(path, names, entity, unit, labeled, lang=lang)
    return path


def _truth() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for entity in ENTITIES:
        if entity == IMPOSSIBLE:
            continue  # ★ ⑨：拿不到就是拿不到，面板里不该出现它
        for year in YEARS:
            revenue: float | None = canonical_revenue(entity, year)
            if (entity, year, "revenue") in MISSING_CELLS:
                revenue = None  # ★ ④：真值是「缺」，填任何值都是错的
            conflict = entity == CONFLICT["entity"] and year == CONFLICT["year"]
            rows.append(
                {
                    "obligor": entity,
                    "year": year,
                    "revenue": None if conflict else revenue,
                    "exposure": canonical_exposure(entity, year),
                    "conflict": conflict,
                }
            )
    return {
        "deliverable": "panel.csv",
        "key": ["obligor", "year"],
        "rows": rows,
        "injected": {
            "① mesh-dependency": "s13..s18 用内部码，只有 s19 能换回实体号",
            "② polysemy": f"revenue 四个口径版本，交付物必须钉在 {CANONICAL_REVENUE_VERSION}",
            "③ real-conflict": f"{CONFLICT['entity']} {CONFLICT['year']}：{CONFLICT['sources']} 给出不同值，都不是错的",
            "④ missing-trap": f"缺失四种写法 {MISSING_TOKENS}；真值缺格 {list(MISSING_CELLS)}",
            "⑤ transient": "压力题没有假故障通道（由注入集 A05 覆盖）",
            "⑥ persistent": "s24 的输入文件故意不生成 → 取源失败 → 降级",
            "⑦ same-symptom": "由真模型跑测自然产生",
            "⑧ global-invariant": "exposure 同一单位、revenue 同一口径版本（点名交付物表达）",
            "⑨ impossible": f"{IMPOSSIBLE} 的 revenue 只在 s24（已下线）",
        },
        "expected_obligors": sorted({row["obligor"] for row in rows}),
        "expected_keys": [f"{row['obligor']}|{row['year']}" for row in rows],
    }


#: 交付物的 schema（`exposure` 不许为空 = A04 的判定者；`revenue` 允许缺）。
PANEL_SCHEMA: tuple[tuple[str, str, bool], ...] = (
    ("obligor", "str", False),
    ("year", "int", False),
    ("revenue", "float", True),
    ("exposure", "float", False),
)


def _task_yaml() -> str:
    """The contract. Generated from ``SOURCE_SPECS`` so the two cannot drift."""
    sources = []
    for ref, fmt, _lang, _unit, _labeled, cover in SOURCE_SPECS:
        suffix = {"csv": "csv", "html": "html", "json": "json", "text": "txt", "pdf": "pdf"}[fmt]
        # ★ ⑥：s24 声明了、文件【故意不生成】—— 运行期取源失败，走 A06 的降级。
        sources.append(f"  - {{ref: {ref}, locator: input/{ref}.{suffix}}}")
    columns = ", ".join(
        "{" + f"name: {name}, dtype: {dtype}" + ("" if nullable else ", nullable: false") + "}"
        for name, dtype, nullable in PANEL_SCHEMA
    )
    return f"""# 压力任务 A（信息类，`架构设计.md §15.4`）：24 个异构源 → 实体 × 时间面板。
#
# ★ 这份 task.yaml 由 `generate.py` 产出（源清单与 SOURCE_SPECS 同源，不许手改）。
# ★ 真值在 ../truth/truth.json：运行期只挂 input/，真值只给判分器读。
task_id: stress-panel-many-sources
goal: >
  把 20 多个异构来源里的「主体 × 年度」敞口与收入，重建为一张统一口径的面板
  （panel.csv：obligor, year, revenue, exposure）。
  主体号一律写成 s19 对照表里的规范形式（ENT-xx）；revenue 一律用合并口径
  （{CANONICAL_REVENUE_VERSION}）；exposure 一律换算成【元】。
version: 1

deliverables:
  - name: panel.csv
    schema: {{{", ".join(f"{name}: {dtype}" for name, dtype, _ in PANEL_SCHEMA)}}}

sources:
{chr(10).join(sources)}

constraints:
  hard:
    - id: C-rows
      kind: presence
      target: panel.csv
      spec: {{min_rows: 1}}
      rationale: "面板不允许为空"
    - id: C-fields
      kind: schema
      target: panel.csv
      spec: {{columns: [{columns}]}}
      rationale: "主体×年度的面板，四个字段一个都不能少；exposure 不许空（缺了要说拿不到，不许填 0）"
    - id: C-prov
      kind: provenance
      target: panel.csv
      spec: {{min_coverage: 0.95}}
      rationale: "每个值都要能指回一份快照，否则无法复核"
    # ★ ⑧ 全局不变量（点名交付物表达；`target=\"*\"` 在多块装配下必然 not found，见 A08）
    - id: C-units
      kind: unit_consistency
      target: panel.csv
      spec: {{concept: exposure, unit: CNY}}
      rationale: "整列必须是一个单位（元）——源里千元/百万元混用，跨实体口径必须一致"
    # ★ ② 多义概念：revenue 四个口径版本，交付物必须钉在合并口径
    - id: C-revenue-def
      kind: definition_pin
      target: panel.csv
      spec: {{concept: revenue, version: {CANONICAL_REVENUE_VERSION}}}
      rationale: "营收在四个源里四种口径；面板必须钉在合并口径，不许混"
  soft: []

blind:
  - ★ ⑤ 瞬时故障：压力题本身没有假故障通道（可复现的假超时由注入集 A05 覆盖）
  - ★ 有的源不标单位：系统只能说「靠本体假设」，证明不了它真是元（A10 的物理边界）
  - ★ 跨源真冲突（③）：跨块候选今天不互相比较，冲突只能在报告里如实记「没判」

limits: {{max_rounds: 2}}
notes: 压力任务 A：五条异构 × 九条攻击面，详见 acceptance/stress/README.md 的逐条表。
"""


def _ontology_yaml() -> str:
    """The shared semantics. Aliases are where the three languages are reconciled."""
    return f"""# 压力任务的共享语义（由 generate.py 产出）。
domain: entity-exposure
version: "2026.10"

concepts:
  obligor: {{label: 主体, kind: entity, aliases: [债务人, 取引先, entity, internal_code, alias, 别名]}}
  year: {{label: 年度, kind: attribute, aliases: [year, 会計年度, fiscal_year, period, 报告期]}}
  revenue: {{label: 营业收入, kind: measure, unit: CNY, aliases: [売上高, 营收, sales, turnover]}}
  exposure: {{label: 敞口金额, kind: measure, unit: CNY, aliases: [敞口, 与信額, exposure, amount]}}

units:
  revenue: {{symbol: CNY, scale: 1.0, base: CNY}}
  exposure: {{symbol: CNY, scale: 1.0, base: CNY}}
  # ★ 源里出现的两个换算单位（声明过的，不靠"千=1000"的世界知识）
  exposure_千元: {{concept_id: exposure, symbol: 千元, scale: 1000.0, base: CNY}}
  exposure_百万元: {{concept_id: exposure, symbol: 百万元, scale: 1000000.0, base: CNY}}

definitions:
  # ★ ② 四个口径版本都在本体里；交付物钉在 {CANONICAL_REVENUE_VERSION}
  revenue:
    version: {CANONICAL_REVENUE_VERSION}
    text: 合并口径营业收入，含全部子公司，按元计
  revenue_parent:
    concept_id: revenue
    version: v2-parent-only
    text: 母公司口径营业收入，不含子公司
  revenue_incl:
    concept_id: revenue
    version: v3-incl-tax
    text: 含税营业收入
  revenue_excl:
    concept_id: revenue
    version: v4-excl-tax
    text: 不含税营业收入
  obligor:
    version: "2026.10"
    text: 主体号一律用规范形式 ENT-xx（其余写法见 s19 对照表）
  year:
    version: "2026.10"
    text: 日历年；财年与报告期都要归到日历年
  exposure:
    version: "2026.10"
    text: 敞口金额，统一到元

calendar: {{timezone: Asia/Shanghai}}
"""


def generate(*, root: Path | None = None) -> dict[str, Path]:
    """Write ``input/`` (for the system) and ``truth/`` (for the judge)."""
    base = HERE if root is None else Path(root)
    given = base / "input"
    truth = base / "truth"
    given.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)
    for old in given.glob("*"):
        old.unlink()
    for spec in SOURCE_SPECS:
        _write_source(given, spec)
    (truth / "truth.json").write_text(json.dumps(_truth(), ensure_ascii=False, indent=2), encoding="utf-8")
    (base / "task.yaml").write_text(_task_yaml(), encoding="utf-8")
    (base / "ontology.yaml").write_text(_ontology_yaml(), encoding="utf-8")
    return {"input": given, "truth": truth, "task": base / "task.yaml", "ontology": base / "ontology.yaml"}


def main() -> int:
    paths = generate()
    written = sorted(path.name for path in paths["input"].glob("*"))
    print(f"input : {paths['input']}  ({len(written)} files; s24 故意缺席)")
    print(f"truth : {paths['truth']}  (★ 运行只看 input/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
