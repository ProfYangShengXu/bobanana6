"""两种**形状**：一段文本挂在哪个标题下，以及一张表长什么样。

★★ 红线：这里只算形状，**没有一条领域规则**。

  ① 一篇带标题的文档里，某一段**挂在哪个标题下** —— 判据是"标题里的某个词
     与契约声明的产物名（最后一段）大小写不敏感地相等"。它只做**归属**：
     不说"所以你应该…"，不改写一个字。
  ② 一张带表头的表里，**哪些列的值有重复**、按它们分组会得到多大的组 ——
     判据是"表头怎么切、行列怎么排"。输出里**只有组的大小，没有一格内容**。

换任何题、任何表、任何语言，同一个函数给出同一个含义；拿不到或解析不了
⇒ 返回空（fail-closed）。这里不猜、不补、不解释。
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

#: 表头切字段用的分隔符（按**切出来的字段数**挑，挑不出两个字段就不是表）。
_DELIMITERS = ("\t", ",", ";", "|")

#: 一份来源最多看多少行（超了如实标 `rows_capped`，不静默）。
_MAX_PROFILE_ROWS = 20000
#: 列名最多列几个（`columns` 仍是真实列数）。
_MAX_PROFILE_COLUMNS = 20
#: 参与"两列分组"的候选列数上限 —— 取**值重复且不同值最少**的那些（最像键的）。
_MAX_KEY_COLUMNS = 8
#: 两列分组最多报几组（按"最大的一组有多大"排）。
_MAX_GROUP_COMBINATIONS = 5
#: 每组最多报几个大小。
_MAX_GROUP_SIZES = 3
#: 单列重复最多报几个。
_MAX_REPEATED_COLUMNS = 4

#: 标题里有这些装饰字符时，比对前先剥掉（`` `x.csv` `` ⇒ ``x.csv``）。
_HEADING_DECORATION = "\"'`*():;[]|,"


def _declared_index(names: Iterable[str]) -> dict[str, str]:
    """产物名 ⇒ 用来在标题里比对的那个词：去掉目录，只留最后一段。"""
    index: dict[str, str] = {}
    for name in names:
        text = str(name).strip()
        base = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
        if base:
            index.setdefault(base.lower(), text)
    return index


def _heading_of(line: str) -> tuple[int, str] | None:
    """ATX 标题 ⇒ (级别, 标题文本)；不是标题 ⇒ None。

    ``#x``（# 后面紧跟非空白）**不是**标题 —— 不按"以 # 开头"一刀切。
    """
    stripped = line.strip()
    hashes = len(stripped) - len(stripped.lstrip("#"))
    if hashes == 0 or hashes > 6:
        return None
    rest = stripped[hashes:]
    if rest and rest[0] not in (" ", "\t"):
        return None
    return hashes, rest.strip()


def _tokens(title: str) -> list[str]:
    """标题里可以拿去和产物名比对的最小单位。"""
    out: list[str] = []
    for raw in title.replace("`", " ").replace("*", " ").split():
        token = raw.strip(_HEADING_DECORATION)
        if token:
            out.append(token)
    return out


def artifact_sections(names: Iterable[str], document: str) -> dict[str, dict[str, str]]:
    """★ ①：产物名 ⇒ 文档里挂在该名字下的那一节（**逐字切片**）。

    节的范围：从那个标题行起，到下一个**同级或更高级**的标题为止（没有就到文末）。
    名字对不上、没有标题、文档为空 ⇒ 空（fail-closed）。
    """
    index = _declared_index(names)
    if not index or not document:
        return {}
    lines = document.splitlines()
    marks: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for position, line in enumerate(lines):
        heading = _heading_of(line)
        if heading is None:
            continue
        level, title = heading
        for token in _tokens(title):
            full = index.get(token.lower())
            if full is not None and full not in seen:
                seen.add(full)
                marks.append((position, level, full))
                break
    sections: dict[str, dict[str, str]] = {}
    for position, level, full in marks:
        end = len(lines)
        for later in range(position + 1, len(lines)):
            heading = _heading_of(lines[later])
            if heading is not None and heading[0] <= level:
                end = later
                break
        sections[full] = {
            "heading": lines[position].strip(),
            "text": "\n".join(lines[position:end]).rstrip(),
        }
    return sections


def clip_section(section: str, budget: int) -> str:
    """一节文本按预算装；超了**可见地**截（`…[truncated]` + 省了多少字符）。"""
    if not section or len(section) <= budget:
        return section
    return section[:budget] + "…[truncated]"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


def _delimiter_of(line: str) -> str:
    """按**表头切出来的字段数**挑分隔符；都切不出两个字段 ⇒ 空串（不是表）。"""
    best = ""
    best_count = 1
    for candidate in _DELIMITERS:
        try:
            count = len(next(csv.reader([line], delimiter=candidate)))
        except (csv.Error, StopIteration):
            continue
        if count > best_count:
            best, best_count = candidate, count
    return best


def _rows_of(text: str, delimiter: str, limit: int) -> tuple[list[list[str]], bool]:
    """前 ``limit + 1`` 行（表头 + 数据）；``capped`` = 后面还有。"""
    rows: list[list[str]] = []
    capped = False
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        if not any(cell.strip() for cell in row):
            continue
        rows.append(row)
        if len(rows) > limit:
            capped = True
            break
    return rows, capped


def table_shape(text: str) -> dict[str, Any] | None:
    """★ ②：一张表的形状 —— 行数、列数、列名，以及按列分组后的**组大小**。

    ★ 严格判据（宁可不给，也不把散文当表）：每一行的字段数必须**恰好等于**表头，
      且至少有一行数据。散文里也有逗号，宽松判据会把一段散文算成表。
    ★ 输出的"最大几组"是**大小**，不是内容 —— 这里不吐任何一格的值。
    解析不了 ⇒ None（fail-closed）。
    """
    head = _first_line(text)
    if not head:
        return None
    delimiter = _delimiter_of(head)
    if not delimiter:
        return None
    rows, capped = _rows_of(text, delimiter, _MAX_PROFILE_ROWS)
    if len(rows) < 2:
        return None
    columns = [cell.strip() for cell in rows[0]]
    if len(columns) < 2 or any(not name for name in columns):
        return None
    if any(len(row) != len(columns) for row in rows):
        return None  # 不是规则的表格 ⇒ 不当表
    body = rows[1:]
    shape: dict[str, Any] = {"rows": len(body), "columns": len(columns)}
    if capped:
        shape["rows_capped"] = True
    names = columns[:_MAX_PROFILE_COLUMNS]
    shape["column_names"] = names
    if len(columns) > len(names):
        shape["column_names_capped"] = len(columns) - len(names)

    counters: list[Counter[str]] = [Counter() for _ in columns]
    for row in body:
        for index in range(len(columns)):
            counters[index][row[index].strip()] += 1
    repeated = [
        {"name": columns[index], "distinct": len(counters[index]), "largest": max(counters[index].values())}
        for index in range(len(columns))
        # ★ 只有一个取值的列**不折叠任何东西**（它没有分组信息）⇒ 不算重复列。
        if 1 < len(counters[index]) < len(body)
    ]
    repeated.sort(key=lambda item: (-item["largest"], item["distinct"], item["name"]))
    if repeated:
        shape["repeated_columns"] = repeated[:_MAX_REPEATED_COLUMNS]
        if len(repeated) > _MAX_REPEATED_COLUMNS:
            shape["repeated_columns_total"] = len(repeated)

    # 候选键列 = 值有重复、且**不同值最少**的那些（最像分组键），最多取 _MAX_KEY_COLUMNS 个。
    # ★ 恒定的列（distinct == 1）不是键：拿它分组只会把整张表折成一组，不是形状。
    candidates = sorted(
        (index for index in range(len(columns)) if 1 < len(counters[index]) < len(body)),
        key=lambda index: (len(counters[index]), index),
    )[:_MAX_KEY_COLUMNS]
    combinations: list[dict[str, Any]] = []
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            first, second = candidates[left], candidates[right]
            pairs: Counter[tuple[str, str]] = Counter(
                (row[first].strip(), row[second].strip()) for row in body
            )
            if not pairs:
                continue
            sizes = sorted(pairs.values(), reverse=True)
            if sizes[0] <= 1:
                continue  # 这两列合起来不折叠任何东西 ⇒ 不是形状
            combinations.append(
                {
                    "on": [columns[first], columns[second]],
                    "groups": len(pairs),
                    "largest_groups": sizes[:_MAX_GROUP_SIZES],
                }
            )
    combinations.sort(key=lambda item: (-item["largest_groups"][0], item["groups"], item["on"]))
    if combinations:
        shape["grouped_columns"] = combinations[:_MAX_GROUP_COMBINATIONS]
        if len(combinations) > _MAX_GROUP_COMBINATIONS:
            shape["grouped_columns_total"] = len(combinations)
    return shape


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _fit(shape: dict[str, Any], quota: int) -> dict[str, Any]:
    """按优先级把形状装进一份额度：行/列 → 两列分组 → 重复列 → 列名。

    ★ 为什么分组排第一：**列名模型读一次表头就有，分组结构它自己算不出来**。
    装不下的部分**具名**写进 `omitted`（可见，不静默）。
    """
    keep: dict[str, Any] = {"rows": shape["rows"], "columns": shape["columns"]}
    for key in ("rows_capped", "column_names_capped", "repeated_columns_total", "grouped_columns_total"):
        if key in shape:
            keep[key] = shape[key]
    omitted: list[str] = []
    for key in ("grouped_columns", "repeated_columns", "column_names"):
        value = shape.get(key)
        if not value:
            continue
        if _size(keep) + _size({key: value}) <= quota:
            keep[key] = value
        else:
            omitted.append(key)
    if omitted and _size(keep) + _size(omitted) + 16 <= quota:
        keep["omitted"] = omitted
    return keep


def source_shapes(texts: Mapping[str, str], budget: int, minimum: int) -> dict[str, dict[str, Any]]:
    """★ ②：每份文本 ⇒ 形状，再按**总预算**摊平。

    额度 = `max(minimum, budget // 表的个数)` —— 与 `_upstream_brief` 同一套办法：
    **整段大小与来源个数无关地有界**，不用写"超过 N 个就……"这种阈值。
    不是表的、读不到的 ⇒ 跳过（fail-closed）；一份都给不出 ⇒ 空。
    """
    shaped: dict[str, dict[str, Any]] = {}
    for ref in sorted(texts):
        shape = table_shape(texts[ref])
        if shape:
            shaped[ref] = shape
    if not shaped:
        return {}
    quota = max(minimum, budget // len(shaped))
    return {ref: _fit(shape, quota) for ref, shape in shaped.items()}
