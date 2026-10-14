#!/usr/bin/env python
"""从**官方判分器**里机械提取【属于规格】的那部分 ⇒ 变成契约声明（规格 §四）。

★ 为什么必须机械化：上一轮查清了 B 臂当时为什么能 51/51 —— 它 `read_file(test_outputs.py)`
把**判分器源码当规格书用**（它"知道"要 1e-10 精度，因为源码里就写着 `atol=1e-10`）。
我们修了污染（把 `checks/` 移出工作区）⇒ 读数干净了，**但规格也跟着少了一块**。
⇒ 判分器里属于规格的那部分，必须被**机械地**提出来，变成契约声明。

```
要提取的：
  · 数值容差      atol / rtol（判分器比数值时用的那把尺子）
  · 编码约定      文件该是什么分隔符 / 什么编码
  · 列级对账关系  "A 列 == B 列按某键分组求和" 这类可判定的关系
★ 判据：机械提取，不是人读着写（手写就是猜，猜错一样挂）
★ 边界：提取器**只读 checks**，绝不把判分器放进工作区（污染那条不回退）
```

    .venv/bin/python acceptance/spec_extractor.py --checks <dir>          # 机读 + YAML
    .venv/bin/python acceptance/spec_extractor.py --checks <dir> --yaml   # 只要 YAML
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

#: ★★ 判分器里"这份产物会被怎么比对"的那两个入口（比对方式的机械判据）。
#: 这是**消费方式**（通用），不是分隔符/宽容列那类实现细节。
_COMPARISON_ENTRIES = {"_assert_csv_matches", "_assert_json_matches"}


@dataclass
class Extracted:
    """一条**机械提取**出来的规格，带来源（哪个文件哪一行）。"""

    kind: str
    value: Any
    source: str
    detail: str = ""

    def as_document(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value, "source": self.source, "detail": self.detail}


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def extract_tolerances(source: str, *, path: str) -> list[Extracted]:
    """★ 数值容差：`assert np.allclose(..., atol=X, rtol=Y)` 与函数默认参数 `atol=Z`。

    尺子来自判分器，不是我猜的 —— 这正是 B 臂"读源码"拿到的那一条。
    """
    tree = ast.parse(source)
    found: list[Extracted] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in ("atol", "rtol"):
                    value = _literal(keyword.value)
                    if value is None:
                        continue
                    found.append(
                        Extracted(
                            kind=f"tolerance.{keyword.arg}",
                            value=value,
                            source=f"{path}:{keyword.value.lineno}",
                            detail=f"called at line {node.lineno} ({_call_name(node.func) or 'call'})",
                        )
                    )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = list(node.args.posonlyargs) + list(node.args.args)
            positional = positional[len(positional) - len(node.args.defaults) :]
            for argument, default in zip(positional, node.args.defaults, strict=True):
                if argument.arg in ("atol", "rtol"):
                    value = _literal(default)
                    if value is None:
                        continue
                    found.append(
                        Extracted(
                            kind=f"tolerance.{argument.arg}",
                            value=value,
                            source=f"{path}:{node.lineno}",
                            detail=f"default of {node.name}({argument.arg})",
                        )
                    )
    return found


def extract_encodings(source: str, *, path: str) -> list[Extracted]:
    """★ 编码 / 分隔符约定：`read_csv(..., sep=..., encoding=...)`。"""
    tree = ast.parse(source)
    found: list[Extracted] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if not name.endswith(("read_csv", "read_table", "to_csv")):
            continue
        for keyword in node.keywords:
            if keyword.arg in ("sep", "encoding", "delimiter"):
                value = _literal(keyword.value)
                if value is None:
                    continue
                found.append(
                    Extracted(
                        kind=f"encoding.{keyword.arg}",
                        value=value,
                        source=f"{path}:{keyword.value.lineno}",
                        detail=f"{name} at line {node.lineno}",
                    )
                )
    return found


def extract_comparison_modes(source: str, *, path: str) -> list[Extracted]:
    """★★ 【这份产物会被怎么对待】—— 消费方式。

    判据（机械，只看判分器源码里有没有这几种调用）：
      · `_assert_csv_matches(<文件名>, ...)` ⇒ 那份 CSV 被**逐行逐列**比对
      · `_assert_json_matches(...)`          ⇒ summary 被**逐键**比对
      容差取调用点显式给的值；没给就取函数定义的默认参数。

    ★★★ 只抽「怎么比对」（**通用**：逐字段 / 容差多大 / 非数值是否严格）——
        这几条换一道题也成立，任何"产物会被机器逐值比对"的题都该让生产者知道。
    ✗ 不抽「判分器内部用了什么字符」—— 分隔符白名单（`['|',';',',']`）、
      哪一列开恩（`relaxed_value_cols`）都是**这一个判分器的实现细节**。
      把它们塞进契约 = 给 A 臂开一个小口径的偷看口；B 臂偷看 `test_outputs.py`
      拿 51/51 就是那条路走到黑的样子。
    ★ 判据一句话：**抽「这份产物会被怎么消费」，不抽「判分器里写了什么字符」。**
    """
    tree = ast.parse(source)
    # 先收函数定义的默认容差（调用点没显式给时用它）
    defaults: dict[str, float] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in _COMPARISON_ENTRIES:
            continue
        # ★ 默认值只覆盖**最后 N 个**参数（`args=[filename, atol]` / `defaults=[1e-9]`），
        #   所以必须**从右边对齐**再 zip。左边对齐会把 `filename` 配到 `1e-9` 上 ——
        #   那样一个都收不到，容差会静默落到下面的 fail-closed 默认值 0（严格），
        #   看起来"没报错"，实际是把判分器给的尺子丢了。
        tail = node.args.args[-len(node.args.defaults):] if node.args.defaults else []
        for argument, fallback in zip(tail, node.args.defaults, strict=False):
            if argument.arg == "atol":
                value = _literal(fallback)
                if isinstance(value, (int, float)):
                    defaults[node.name] = float(value)
    found: list[Extracted] = []
    # ★ 按【函数】作用域扫：判分器把文件名放在循环里批量传
    #   （`for filename in ['a.csv', 'b.csv']: _assert_csv_matches(filename)`）——
    #   变量名跨函数会重名，全局查表会串。解析仍是**机械**的：只认字面量列表，不是猜。
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        loops: dict[str, list[str]] = {}
        for inner in ast.walk(function):
            if not isinstance(inner, ast.For) or not isinstance(inner.target, ast.Name):
                continue
            literal = _literal(inner.iter)
            if isinstance(literal, (list, tuple)) and all(isinstance(x, str) for x in literal):
                loops[inner.target.id] = list(literal)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            # 判分器里这条断言可能是别名调用的（`_assert_csv_matches` / `self._assert...`）
            name = _call_name(node.func).rsplit(".", 1)[-1]
            if name not in _COMPARISON_ENTRIES:
                continue
            atol = None
            for keyword in node.keywords:
                if keyword.arg in ("atol", "abs_tol"):
                    value = _literal(keyword.value)
                    if isinstance(value, (int, float)):
                        atol = float(value)
            if atol is None:
                # blame: fail-closed — 容差取不到就【不许假定宽松】：默认按 0（严格）报，逼调用点显式写
                atol = defaults.get(name, 0.0)
            if name == "_assert_json_matches":
                targets: list[str] = ["output/summary.json"]
                shape = "key-by-key"
            else:
                raw = node.args[0] if node.args else None
                if isinstance(raw, ast.Name) and raw.id in loops:
                    targets = [f"output/{item}" for item in loops[raw.id]]
                elif isinstance(_literal(raw), str):
                    targets = [f"output/{_literal(raw)}"]
                else:
                    continue  # 文件名既不是字面量、也不是字面量列表里的循环变量 ⇒ 具名不抽，不猜
                shape = "field-by-field"
            for target in targets:
                found.append(
                    Extracted(
                        kind="compared",
                        value={
                            "applies_to": target,
                            "compare": shape,
                            "numeric_atol": atol,
                            "text": "exact",
                        },
                        source=f"{path}:{node.lineno}",
                        detail=f"{name} at line {node.lineno}",
                    )
                )
    return found


def extract_reconciliations(source: str, *, path: str) -> list[Extracted]:
    """★ 列级对账关系：`groupby(...)[col].sum()` / `.mean()` / `.count()` 出现在判分器里，
    说明"这一列**该**等于另一份数据按某键分组后的聚合"。

    机械判据：`<expr>.groupby(<键>)[<列>].<op>()` —— 提取 `(列, op, 键)` 三元组。
    """
    tree = ast.parse(source)
    found: list[Extracted] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("sum", "mean", "count", "min", "max"):
            continue
        subscript = node.func.value
        if not isinstance(subscript, ast.Subscript):
            continue
        column = _literal(subscript.slice) or ast.unparse(subscript.slice)
        inner = subscript.value
        if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
            continue
        if inner.func.attr != "groupby" or not inner.args:
            continue
        keys = [ast.unparse(argument) for argument in inner.args]
        found.append(
            Extracted(
                kind="reconcile",
                value={
                    "column": column,
                    "op": node.func.attr,
                    "group_by": keys,
                    "tol": 1e-9,
                },
                source=f"{path}:{node.lineno}",
                detail=f"{ast.unparse(node)[:80]}",
            )
        )
    return found


def extract_artifact_bindings(source: str, *, path: str) -> dict[str, str]:
    """★ 规格 ①(b)：`fixture 名 -> 它约束的那份产物`（**算出来的**，不许人填）。

    机械判据：`@pytest.fixture def <name>(): return _load_output_csv('<file>')`
    再配合 `OUTPUT_DIR = Path('.../output')` ⇒ `<OUTPUT_DIR 的名字>/<file>`。
    13f 里 `effective_holdings_df` ⇒ `output/effective_holdings.csv` —— 于是那条
    "权重按 (manager_order, period_order) 求和归 1" 就挂到了**它真正约束的那份产物**上，
    而不是挂在任务级（上一轮就是那么错挂的，套到 T-01 头上把它判 fail）。
    """
    tree = ast.parse(source)
    output_dir = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.endswith("OUTPUT_DIR"):
                value = node.value
                if isinstance(value, ast.Call) and value.args:
                    literal = _literal(value.args[0])
                    if isinstance(literal, str):
                        output_dir = Path(literal).name
    #: `_load_output_csv` 这一族：函数名 -> 它是不是"读产物"
    loaders = {"_load_output_csv"}
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # ★ 装饰器可能是 `@fixture` 也可能是 `@pytest.fixture(scope=...)`（Call）
        if not any(
            _call_name(d.func if isinstance(d, ast.Call) else d) in ("fixture", "pytest.fixture")
            for d in node.decorator_list
        ):
            continue
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Call):
                continue
            if _call_name(statement.func) not in loaders or not statement.args:
                continue
            literal = _literal(statement.args[0])
            if isinstance(literal, str):
                prefix = output_dir or "output"
                bindings[node.name] = f"{prefix}/{literal}"
    return bindings


def extract_fixture_usage(source: str) -> dict[str, list[str]]:
    """`测试函数用到哪个 fixture` —— 有了它才能把"关系"和"产物"对上。"""
    tree = ast.parse(source)
    usage: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        names = [argument.arg for argument in node.args.args]
        if names:
            usage[node.name] = names
    return usage


def extract_summary_tolerance(source: str, *, path: str) -> list[Extracted]:
    """★ 规格 ②：`summary` 数值键的容差 + 非数值键的严格相等（**机械提取**）。

    判据：`def _assert_json_matches(atol: float = 1e-12)` 的默认参数，
    以及它读的 `SUMMARY_NUMERIC_KEYS` / `SUMMARY_BOOL_KEYS` 两张集合。
    ★ 容差是**判分器的**，不是我们定的 —— 提取器只负责把它搬到声明里。
    """
    tree = ast.parse(source)
    found: list[Extracted] = []
    tables: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
                literals = [_literal(item) for item in node.value.elts]
                tables[target.id] = [str(item) for item in literals if isinstance(item, str)]
    numeric_keys = tables.get("SUMMARY_NUMERIC_KEYS", [])
    bool_keys = tables.get("SUMMARY_BOOL_KEYS", [])
    # `_assert_json_matches` 的 atol 默认值 + 它读哪些键（机械）
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "json" not in node.name and "summary" not in node.name:
            continue
        positional = list(node.args.posonlyargs) + list(node.args.args)
        positional = positional[len(positional) - len(node.args.defaults) :]
        tol: float | None = None
        for argument, default in zip(positional, node.args.defaults, strict=True):
            if argument.arg in ("atol", "abs_tol"):
                value = _literal(default)
                if isinstance(value, (int, float)):
                    tol = float(value)
        if tol is None:
            continue
        for key in numeric_keys:
            found.append(
                Extracted(
                    kind="summary.numeric_tolerance",
                    value={"key": key, "op": "abs_tol", "tol": tol},
                    source=f"{path}:{node.lineno}",
                    detail=f"{node.name}: numeric keys compare with abs_tol={tol!r}",
                )
            )
        for key in bool_keys:
            found.append(
                Extracted(
                    kind="summary.strict_key",
                    value={"key": key, "op": "strict", "tol": 0.0},
                    source=f"{path}:{node.lineno}",
                    detail=f"{node.name}: these keys must be identical (no tolerance)",
                )
            )
    return found


def extract_grouped_invariants(source: str, *, path: str) -> list[Extracted]:
    """★ 机械提取"分组聚合必须等于某常量"的**对账关系**（CI 的 invariant 形状）。

    判据：`np.allclose(<expr>.groupby(K)[C].sum(), np.ones(...), atol=T, rtol=0)`
    ⇒ `{left: C, aggregate: sum, right: 1.0, group_by: K, tol: T}`

    这正是 13f 的 `test_weights_sum_to_one_by_book`（`test_outputs.py:510`）——
    丢的就是「权重按 (manager, period) 分组求和 == 1，atol=1e-10」这一条。
    """
    tree = ast.parse(source)
    bindings = {name: path_ for name, path_ in extract_artifact_bindings(source, path=path).items()}
    # ★ 一步别名解析：`sums = df.groupby([...])['weight'].sum()` 之后再
    #   `np.allclose(sums.to_numpy(), np.ones(...), atol=1e-10, rtol=0)` ——
    #   判分器把聚合结果先赋给一个名字（`test_outputs.py:510`），所以光看 allclose 那一行
    #   是看不见 groupby 的。★ 这仍是**机械**提取（跟着赋值走），不是人读着写。
    aliases: dict[str, tuple[str, str, list[str]]] = {}
    alias_binding: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) and node.targets else node.target
        if not isinstance(target, ast.Name) or node.value is None:
            continue
        parsed = _parse_groupby_chain(node.value)
        if parsed is not None:
            aliases[target.id] = parsed
            alias_binding[target.id] = _base_name(node.value)

    found: list[Extracted] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _call_name(node.func).endswith("allclose") or len(node.args) < 2:
            continue
        left_expr, right_expr = node.args[0], node.args[1]
        # 右边必须是一个"常量表"（`np.ones(n)` / `np.zeros(n)` / 字面量）
        target = _constant_table(right_expr)
        if target is None:
            continue
        # 左边要么直接是 `<...>.groupby(K)[C].<agg>()`，要么是它的**别名**
        parsed = _parse_groupby_chain(left_expr) or _parse_groupby_chain(_base(left_expr))
        fixture = _base_name(left_expr) if _parse_groupby_chain(left_expr) else ""
        if parsed is None:
            resolved = aliases.get(_base_name(left_expr))
            parsed = resolved
            fixture = alias_binding.get(_base_name(left_expr), "")
        if parsed is None:
            continue
        column, aggregate, keys = parsed
        applies_to = bindings.get(fixture, "")
        tol = 0.0
        for keyword in node.keywords:
            if keyword.arg in ("atol", "rtol"):
                value = _literal(keyword.value)
                if isinstance(value, (int, float)) and value:
                    tol = max(tol, float(value))
        found.append(
            Extracted(
                kind="invariant.grouped",
                value={
                    # ★ 规格 ①(b)：`applies_to` **算出来的**（fixture 名 → 它读的那份产物）
                    "applies_to": applies_to,
                    "left": column,
                    # ★ `op` 是**比较**（与契约既有字段一致：`{left, right, op}` 是必填），
                    #   `aggregate` 才是"先怎么把一组值聚起来"。
                    "op": "eq",
                    "aggregate": aggregate,
                    "right": target,
                    "group_by": keys,
                    "tol": tol,
                },
                source=f"{path}:{node.lineno}",
                detail=ast.unparse(node)[:90],
            )
        )
    return found


def _base_name(node: ast.AST) -> str:
    """剥到底：`effective_holdings_df.groupby([...])['weight'].sum()` ⇒ `effective_holdings_df`。

    ★ 这是"哪个 fixture 上做的聚合"的机械判据 —— `applies_to` 靠它算出来。
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _base_name(node.value)
    if isinstance(node, ast.Call):
        return _base_name(node.func)
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    return ""


def _base(node: ast.AST) -> ast.AST:
    """剥掉 `.to_numpy()` / `.values` 这类后缀，回到被调用的那个表达式。"""
    if isinstance(node, ast.Call) and not isinstance(node.func, ast.Attribute):
        return node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return _base(node.func.value)
    if isinstance(node, ast.Attribute):
        return _base(node.value)
    return node


def _parse_groupby_chain(node: ast.AST) -> tuple[str, str, list[str]] | None:
    """`<...>.groupby(K)[C].<agg>()` ⇒ `(列, 聚合, 键)`。"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    aggregate = node.func.attr
    if aggregate not in ("sum", "mean", "count", "min", "max"):
        return None
    subscript = node.func.value
    if not isinstance(subscript, ast.Subscript):
        return None
    column = _literal(subscript.slice) or ast.unparse(subscript.slice)
    inner = subscript.value
    if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
        return None
    if inner.func.attr != "groupby" or not inner.args:
        return None
    keys: list[str] = []
    for argument in inner.args:
        value = _literal(argument)
        if isinstance(value, (list, tuple)):
            keys.extend(str(item) for item in value)
        else:
            keys.append(str(value) if value is not None else ast.unparse(argument))
    return str(column), aggregate, keys


def _constant_table(node: ast.AST) -> float | None:
    """`np.ones(n)` / `np.zeros(n)` / `1.0` ⇒ 目标常量。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name.endswith("ones"):
            return 1.0
        if name.endswith("zeros"):
            return 0.0
    return None


def extract(checks_dir: Path) -> list[Extracted]:
    found: list[Extracted] = []
    for path in sorted(checks_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.as_posix()
        found.extend(extract_tolerances(source, path=relative))
        found.extend(extract_encodings(source, path=relative))
        found.extend(extract_reconciliations(source, path=relative))
        found.extend(extract_grouped_invariants(source, path=relative))
        found.extend(extract_summary_tolerance(source, path=relative))
        found.extend(extract_comparison_modes(source, path=relative))
    return found


def as_content_checks(found: list[Extracted]) -> dict[str, Any]:
    """把提取结果落成 `task.yaml` 的 `content_checks` 声明形状（**机械生成**）。"""
    tolerances = [entry for entry in found if entry.kind.startswith("tolerance.")]
    reconciliations = [entry for entry in found if entry.kind == "reconcile"]
    grouped = [entry for entry in found if entry.kind == "invariant.grouped"]
    summary_numeric = [entry for entry in found if entry.kind == "summary.numeric_tolerance"]
    summary_strict = [entry for entry in found if entry.kind == "summary.strict_key"]
    separation = [entry for entry in found if entry.kind == "encoding.sep"]
    compared = [entry for entry in found if entry.kind == "compared"]
    declaration: dict[str, Any] = {}
    # ★ 规格 ②：summary 的键容差 —— 落成 invariant 形状（`op: abs_tol` / `op: strict`），
    #   于是内容层 CI **知道**判分器的那把尺子，而不是让块自己猜精度。
    summary_rules: list[dict[str, Any]] = []
    for entry in [*summary_numeric, *summary_strict]:
        rule = {"applies_to": "output/summary.json", "left": entry.value["key"], "right": entry.value["op"], "op": entry.value["op"], "tol": entry.value["tol"]}
        summary_rules.append(rule)
    if grouped or summary_rules:
        # ★ 分组聚合形状直接就是 CI 的 invariant 形状（`invariant_findings` 认它）
        declaration["invariants"] = [dict(entry.value) for entry in grouped] + summary_rules
    if reconciliations and "invariants" not in declaration:
        # 去重（同一关系可能在多处出现）
        seen: set[str] = set()
        invariants = []
        for entry in reconciliations:
            key = json.dumps(entry.value, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            invariants.append(dict(entry.value))
        declaration["invariants"] = invariants
    # ★★ 比对方式（消费方式）：这份产物会被【逐字段机器比对】，容差多大。
    #   ★ 抽的是"怎么消费"，不是"判分器里写了什么字符" —— 后者是这一题的实现细节。
    #   去重：同一份产物可能在多个用例里被比对（`test_reference_match_*` 分散在三处）。
    if compared:
        seen_targets: dict[str, dict[str, Any]] = {}
        for entry in compared:
            target = entry.value["applies_to"]
            previous = seen_targets.get(target)
            # 同一份产物出现多次时取【最严】的那把尺子（容差最小）——
            # 宽的那个是别处的口径，严的那个才是它真正会被卡的地方。
            if previous is None or entry.value["numeric_atol"] < previous["numeric_atol"]:
                seen_targets[target] = dict(entry.value)
        declaration["comparisons"] = sorted(seen_targets.values(), key=lambda item: item["applies_to"])
    return {
        "content_checks": declaration,
        "extracted": [entry.as_document() for entry in found],
        "notes": {
            "tolerances": [{"value": entry.value, "source": entry.source} for entry in tolerances],
            "separators": [{"value": entry.value, "source": entry.source} for entry in separation],
            "generated_from": "机械提取（`acceptance/spec_extractor.py`），不是人手写",
        },
    }


def _yaml(document: Any, indent: int = 0) -> str:
    """极简 YAML 输出（只用列表/字典/标量，够这一段声明用）。"""
    pad = "  " * indent
    if isinstance(document, dict):
        if not document:
            return f"{pad}{{}}\n"
        lines = []
        for key, value in document.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:\n{_yaml(value, indent + 1)}")
            else:
                lines.append(f"{pad}{key}: {json.dumps(value, ensure_ascii=False)}\n")
        return "".join(lines)
    if isinstance(document, list):
        if not document:
            return f"{pad}[]\n"
        lines = []
        for item in document:
            if isinstance(item, dict):
                body = _yaml(item, indent + 1).lstrip()
                lines.append(f"{pad}- {body}")
            else:
                lines.append(f"{pad}- {json.dumps(item, ensure_ascii=False)}\n")
        return "".join(lines)
    return f"{pad}{json.dumps(document, ensure_ascii=False)}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checks", required=True, help="官方 checks 目录（★ 只读）")
    parser.add_argument("--yaml", action="store_true", help="只打印 content_checks 的 YAML")
    arguments = parser.parse_args(argv)

    checks_dir = Path(arguments.checks)
    if not checks_dir.is_dir():
        print(f"no such checks directory: {checks_dir}", file=sys.stderr)
        return 2
    document = as_content_checks(extract(checks_dir))
    if arguments.yaml:
        print(_yaml(document["content_checks"]), end="")
        return 0
    print(json.dumps(document, ensure_ascii=False, indent=1))
    print("\n# 落成 task.yaml 的声明：\n" + _yaml(document["content_checks"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
