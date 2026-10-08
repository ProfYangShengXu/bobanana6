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
    return found


def as_content_checks(found: list[Extracted]) -> dict[str, Any]:
    """把提取结果落成 `task.yaml` 的 `content_checks` 声明形状（**机械生成**）。"""
    tolerances = [entry for entry in found if entry.kind.startswith("tolerance.")]
    reconciliations = [entry for entry in found if entry.kind == "reconcile"]
    separation = [entry for entry in found if entry.kind == "encoding.sep"]
    declaration: dict[str, Any] = {}
    if reconciliations:
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
