#!/usr/bin/env python
"""默认值审计（fail-open 扫描）—— 找出所有「可省略的默认值」，逼它三选一。

**为什么这一族能被机制吸收**：审计出的 15 处形态完全统一（dataclass 字段默认值 /
函数默认参数）。形态统一 ⇒ 可枚举 ⇒ 一次写完永久生效。

```
每个默认值必须三选一（★ 机读，不许是散文注释）：
  a. `# blame: fail-closed`                   保守方向：未声明时【拒绝】
  b. `# blame: fail-open — <理由>`            危险方向：必须附理由 + 反证读数
  c. `# blame: whitelist — <理由>`            确实不需要动（理由照写，一并 review）
```

标记写在**默认值所在行或上一行**；`fail-open` / `whitelist` **没有理由 = 没标**。

★ 边界（这条最容易做歪）：

```
① 不许一次性全标成同一类 —— 分类必须逐条给出，工具只负责【发现 + 逼问】，不负责替你判断
② 报错必须带 file:line + 当前默认值 + 该怎么标（只说"有未标注的默认值"等于没说）
③ 扫描器自己要被测（`tests/test_default_audit.py`：未标注 / 已标 / 白名单三种样例）
④ 纯 `ast`，**不 import 被测模块** —— 否则"扫源码"会变成"执行源码"，
   而有些默认值恰恰是在 import 期就生效的
```

用法：

    .venv/bin/python tools/default_audit.py                 # 全量清单（JSON）
    .venv/bin/python tools/default_audit.py --check         # 有任何【未标注】⇒ exit 1
    .venv/bin/python tools/default_audit.py --bucket whitelist   # 白名单视图（供 review）
    .venv/bin/python tools/default_audit.py --only agentloop/metrics.py:101
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
SOURCE_ROOT = REPO / "src" / "bobanana6"

MARKER_PREFIX = "# blame:"
BUCKET_FAIL_CLOSED = "fail-closed"
BUCKET_FAIL_OPEN = "fail-open"
BUCKET_WHITELIST = "whitelist"
BUCKET_UNANNOTATED = "unannotated"
BUCKETS = (BUCKET_FAIL_CLOSED, BUCKET_FAIL_OPEN, BUCKET_WHITELIST, BUCKET_UNANNOTATED)

#: `fail-open` 与 `whitelist` 都必须带理由：没有理由的标记等于没标。
REASON_REQUIRED = (BUCKET_FAIL_OPEN, BUCKET_WHITELIST)

#: 理由的分隔符（破折号，全角半角都认）。
REASON_SEPARATORS = ("—", "——", "--", "-", ":")

#: 审计点名的位置（path 片段 + 符号）。**按符号钉，不按行号** —— 行号会漂。
AUDIT_SITES: tuple[tuple[str, str], ...] = (
    ("agentloop/attribution.py", "BLAME_KINDS"),
    ("agentloop/attribution.py", "CONFIDENCE_LEVELS"),
    ("assemble_impl/runtime.py", "edit_clock"),
    ("services/checks.py", "last_edit_at"),
    ("agentloop/metrics.py", "faithful"),
    ("agentloop/tools.py", "max_calls_per_turn"),
    ("agentloop/tools.py", "repeat_block_after"),
    ("agentloop/tools.py", "same_tool_block_after"),
    ("agentloop/loop.py", "block_verifiable"),
    ("agentloop/recovery.py", "block_verifiable"),
    ("agentloop/events.py", "ok"),
    ("assemble_impl/runtime.py", "budget"),
    ("assemble_impl/runner.py", "budget"),
    ("services/workspace.py", "limit"),
    ("core/model.py", "required"),
    ("core/model.py", "nullable"),
    ("core/checkers/presence.py", "min_rows"),
    ("core/checkers/coverage.py", "declared_missing"),
    ("core/capabilities.py", "max_output"),
    ("agentloop/loop.py", "LoopConfig"),
)


def enforced(finding: Finding) -> bool:
    """Is this finding inside the scope `--check` blocks on? See `ENFORCED_*` above."""
    if finding.shape == SHAPE_BOOL_TRUE:
        return True
    if any(
        finding.path.endswith(path) and symbol in finding.symbol
        for path, symbol in AUDIT_SITES
    ):
        return True
    return finding.shape == SHAPE_GET_DEFAULT and finding.path.startswith(
        tuple(f"src/bobanana6/{prefix}" for prefix in ENFORCED_PACKAGE_PREFIXES)
    )


#: 任何默认值都可能是这两种方向；工具**不替人判断**，只要求人给出一个并写明理由。
_TROUBLE = "默认值就是『省略时的行为』：漏写/漏传一处，判据就静默走向宽松那一侧"


@dataclass(frozen=True)
class Finding:
    """One omittable default, with where it is and how it must be annotated."""

    path: str  # 相对仓库根
    line: int
    symbol: str  # Class.field / func(arg) / Class.method(arg)
    default_src: str  # 默认值的源码原文（就是报错里要贴出来的那个）
    bucket: str
    shape: str = "other"
    reason: str = ""
    how_to_annotate: str = ""
    #: `get` 形态的来源（`constraint.spec.get("min_rows", 0)` 里的 `spec.get(...)`）。
    via: str = ""

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def marker_location(self) -> str:
        return f"{self.symbol}"

    def as_document(self) -> dict[str, Any]:
        document = {
            "location": self.location,
            "symbol": self.symbol,
            "default": self.default_src,
            "bucket": self.bucket,
            "shape": self.shape,
            "enforced": enforced(self),
        }
        if self.via:
            document["via"] = self.via
        if self.reason:
            document["reason"] = self.reason
        if self.bucket == BUCKET_UNANNOTATED:
            document["how_to_annotate"] = self.how_to_annotate
        return document


# --------------------------------------------------------------------------- #
# 什么算「可省略的默认值」
# --------------------------------------------------------------------------- #


def _is_literal(node: ast.AST) -> bool:
    """A value the reader can see at a glance: constants and containers of them."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(_is_literal(item) for item in (*node.keys, *node.values) if item is not None)
    return False


def _field_default(call: ast.Call) -> ast.AST | None:
    """``field(default=...)`` / ``field(default_factory=lambda: <literal>)`` 里的那一份。"""
    for keyword in call.keywords:
        if keyword.arg == "default":
            return keyword.value
        if keyword.arg == "default_factory":
            factory = keyword.value
            if isinstance(factory, ast.Lambda) and _is_literal(factory.body):
                return factory.body
            return factory  # 不透明工厂：照样算一处默认值，只是贴不出字面量
    return None


def _default_node(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "field":
        return _field_default(node)
    return node


def _get_default(node: ast.AST) -> ast.AST | None:
    """``mapping.get(key, <literal>)`` 的第二参 —— 「该有却没有」那一族的落点。

    ★ 这一形态不进函数签名，所以"函数默认参数"的扫描看不见它：
    `constraint.spec.get("min_rows", 0)` 里的 `0` 才是那个让约束**恒过**的默认值。
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return None
    if len(node.args) != 2 or node.keywords:
        return None
    return node.args[1] if _is_literal(node.args[1]) else None


SHAPE_BOOL_TRUE = "bool_true"
SHAPE_BOOL_FALSE = "bool_false"
SHAPE_NONE = "none"
SHAPE_NUMERIC = "numeric"
SHAPE_EMPTY = "empty_container"
SHAPE_GET_DEFAULT = "get_default"
SHAPE_OTHER = "other"
SHAPES = (
    SHAPE_BOOL_TRUE,
    SHAPE_GET_DEFAULT,
    SHAPE_NUMERIC,
    SHAPE_NONE,
    SHAPE_EMPTY,
    SHAPE_BOOL_FALSE,
    SHAPE_OTHER,
)

#: ★★ 强制范围（`--check` 只在这里 exit 1）—— 三条并集，全部**机械可导出**：
#:
#:   ① 审计点名的那些位置（`AUDIT_SITES`，按 path + symbol 钉，不按行号 —— 行号会漂）
#:   ② 任何地方 `= True` 的字面量默认（省略即宣称"是"）
#:   ③ `core/` 与 `contract/` 里的 `.get(key, <literal>)` 默认（这些默认值直接决定判据
#:      与约束包：例如 `sources_share_state` 缺省 False ⇒ 切分器照样扇出；`pins` 缺省 ""
#:      ⇒ 回落到字典序口径）
#:
#: 其余 ~600 处是普通数据管道（`detail = ""` / `rows = ()` / `args.get("term", "")`）——
#: 逐条判断的成本远超收益。它们**不静默通过**：由【逐形态棘轮】看着
#: （`default_audit_ratchet.json`：每种形态的未标注数只许变少，不许变多），
#: 且 `--report` 会把它们全量列出来。
ENFORCED_PACKAGE_PREFIXES = ("core/", "contract/")


def shape_of(value_source: str) -> str:
    text = value_source.strip()
    if text == "True":
        return SHAPE_BOOL_TRUE
    if text == "False":
        return SHAPE_BOOL_FALSE
    if text == "None":
        return SHAPE_NONE
    if text in ("()", "[]", "{}", "''", '""'):
        return SHAPE_EMPTY
    if text and (text[0].isdigit() or text[0] == "-"):
        return SHAPE_NUMERIC
    return SHAPE_OTHER


def _dataclass_names(tree: ast.AST) -> set[str]:
    """Class names decorated with ``@dataclass`` (or ``@dataclass(...)`` / ``@dataclass(frozen=True)``)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Name) and target.id == "dataclass" or isinstance(target, ast.Attribute) and target.attr == "dataclass":
                names.add(node.name)
    return names


# --------------------------------------------------------------------------- #
# 标记
# --------------------------------------------------------------------------- #


def _marker_lines(lines: list[str]) -> dict[int, tuple[str, str]]:
    """``line number -> (bucket, reason)`` for every ``# blame:`` marker in a file.

    ``fail-open`` / ``whitelist`` without a reason are returned as
    ``(bucket, "")`` so the caller can treat them as *not annotated*.
    """
    found: dict[int, tuple[str, str]] = {}
    for index, text in enumerate(lines, start=1):
        position = text.find(MARKER_PREFIX)
        if position < 0:
            continue
        payload = text[position + len(MARKER_PREFIX) :].strip()
        bucket = ""
        for candidate in BUCKETS:
            if payload.startswith(candidate):
                bucket = candidate
                break
        if not bucket:
            continue
        reason = ""
        remainder = payload[len(bucket) :].strip()
        for separator in REASON_SEPARATORS:
            if remainder.startswith(separator):
                reason = remainder[len(separator) :].strip()
                break
        else:
            reason = remainder
        found[index] = (bucket, reason)
    return found


def _resolve(
    markers: dict[int, tuple[str, str]], *, definition_line: int, expression_line: int
) -> tuple[str, str]:
    """The marker for one default: on the value's line, or on the line above it."""
    for candidate in (expression_line, definition_line, expression_line - 1):
        if candidate in markers:
            return markers[candidate]
    return BUCKET_UNANNOTATED, ""


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #


def scan_source(source: str, *, path: str) -> list[Finding]:
    """Every omittable default in one module, already resolved against its marker."""
    tree = ast.parse(source)
    lines = source.splitlines()
    markers = _marker_lines(lines)
    dataclasses = _dataclass_names(tree)
    findings: list[Finding] = []

    def record(
        node: ast.AST,
        symbol: str,
        definition_line: int,
        expression_line: int,
        value: ast.AST,
        *,
        via: str = "",
    ) -> None:
        bucket, reason = _resolve(
            markers, definition_line=definition_line, expression_line=expression_line
        )
        if bucket in REASON_REQUIRED and not reason:
            bucket, reason = BUCKET_UNANNOTATED, ""  # 没理由的标记 = 没标
        default_src = ast.get_source_segment(source, value) or ast.unparse(value)
        findings.append(
            Finding(
                path=path,
                line=expression_line,
                symbol=symbol,
                default_src=default_src,
                bucket=bucket,
                shape=SHAPE_GET_DEFAULT if via else shape_of(default_src),
                via=via,
                reason=reason,
                how_to_annotate=(
                    f"{path}:{expression_line} 的 {symbol} 默认值是 {ast.unparse(value)!r}；"
                    f"在该行或上一行标一个：`{MARKER_PREFIX} {BUCKET_FAIL_CLOSED}`（保守方向，"
                    f"未声明就拒绝）或 `{MARKER_PREFIX} {BUCKET_FAIL_OPEN} — <理由 + 反证读数>`"
                    f"（危险方向）或 `{MARKER_PREFIX} {BUCKET_WHITELIST} — <理由>`"
                ),
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in dataclasses:
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and statement.value is not None:
                    value = _default_node(statement.value)
                    if value is None:
                        continue
                    record(
                        statement.value,
                        f"{node.name}.{ast.unparse(statement.target)}",
                        statement.lineno,
                        statement.value.lineno,
                        value,
                    )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = list(node.args.posonlyargs) + list(node.args.args)
            positional = positional[len(positional) - len(node.args.defaults) :]
            for argument, default in zip(positional, node.args.defaults, strict=True):
                value = _default_node(default)
                if value is None:
                    continue
                record(
                    default, f"{node.name}({argument.arg})", node.lineno, default.lineno, value
                )
            for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
                if default is None:
                    continue
                value = _default_node(default)
                if value is None:
                    continue
                record(
                    default, f"{node.name}({argument.arg})", node.lineno, default.lineno, value
                )
        # ★ 「该有却没有」那一族：`mapping.get(key, <literal>)` 的第二参。
        get_value = _get_default(node)
        if get_value is not None:
            owner = ast.unparse(node.func.value) if isinstance(node.func, ast.Attribute) else "?"
            key = ast.unparse(node.args[0])
            record(
                node,
                f"{owner}.get({key})",
                node.lineno,
                get_value.lineno,
                get_value,
                via=f"{owner}.get",
            )
    return sorted(findings, key=lambda finding: (finding.path, finding.line))


def scan_tree(root: Path = SOURCE_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(REPO).as_posix()
        findings.extend(scan_source(path.read_text(encoding="utf-8"), path=relative))
    return findings


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def summarise(findings: list[Finding]) -> dict[str, Any]:
    counts: dict[str, Any] = {bucket: 0 for bucket in BUCKETS}
    shapes = {shape: 0 for shape in SHAPES}
    enforced_counts = {bucket: 0 for bucket in BUCKETS}
    for finding in findings:
        counts[finding.bucket] += 1
        shapes[finding.shape] += 1
        if enforced(finding):
            enforced_counts[finding.bucket] += 1
    counts["total"] = len(findings)
    counts["enforced_total"] = sum(enforced_counts.values())
    counts["enforced_unannotated"] = enforced_counts[BUCKET_UNANNOTATED]
    return {"buckets": counts, "shapes": shapes, "enforced": enforced_counts}


def as_document(findings: list[Finding]) -> dict[str, Any]:
    return {
        "schema": "bobanana6.default-audit/1",
        "counts": summarise(findings),
        "findings": [finding.as_document() for finding in findings],
    }


RATCHET_PATH = REPO / "tools" / "default_audit_ratchet.json"


def unannotated_by_shape(findings: list[Finding]) -> dict[str, int]:
    """【未标注】数按形态分开数 —— 棘轮按形态比，不许用总数互相掩盖。"""
    counts = {shape: 0 for shape in SHAPES}
    for finding in findings:
        if finding.bucket == BUCKET_UNANNOTATED:
            counts[finding.shape] += 1
    return counts


def read_ratchet(path: Path = RATCHET_PATH) -> dict[str, int]:
    if not path.is_file():
        return {}
    return {str(key): int(value) for key, value in json.loads(path.read_text(encoding="utf-8"))["unannotated_by_shape"].items()}


def check(findings: list[Finding], ratchet: dict[str, int]) -> list[str]:
    """两条规则：强制范围内一条不许未标注；每种形态的未标注数只许变少。"""
    problems: list[str] = []
    for finding in findings:
        if enforced(finding) and finding.bucket == BUCKET_UNANNOTATED:
            problems.append(f"UNANNOTATED {finding.location} {finding.symbol} = {finding.default_src}")
    for shape, count in sorted(unannotated_by_shape(findings).items()):
        allowed = ratchet.get(shape)
        if allowed is not None and count > allowed:
            problems.append(f"RATCHET {shape}: {count} 处未标注 > 记录上限 {allowed}（只许变少）")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="未标注 ⇒ exit 1")
    parser.add_argument("--bucket", choices=BUCKETS, help="只打印这一类")
    parser.add_argument("--only", help="只看一个位置，例如 agentloop/metrics.py:101")
    parser.add_argument(
        "--update-ratchet",
        action="store_true",
        help=f"把当前的逐形态未标注数写进 {RATCHET_PATH.name}（唯一允许『调高上限』的动作）",
    )
    arguments = parser.parse_args(argv)

    findings = scan_tree()
    if arguments.bucket:
        findings = [finding for finding in findings if finding.bucket == arguments.bucket]
    if arguments.only:
        needle = arguments.only.replace("\\", "/")
        findings = [
            finding
            for finding in findings
            if needle in finding.location or needle in finding.symbol
        ]

    print(json.dumps(as_document(findings), ensure_ascii=False, indent=1))

    if arguments.update_ratchet:
        payload = {
            "_why": (
                "逐形态的【未标注】数。强制范围（审计点名 + `= True` + core//contract/ 的 "
                "`.get` 默认）必须为 0；范围外只许变少。调高这张表是唯一允许的放松动作，"
                "所以它必须出现在 diff 里被 review。"
            ),
            "unannotated_by_shape": unannotated_by_shape(scan_tree()),
        }
        RATCHET_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {RATCHET_PATH.relative_to(REPO)}", file=sys.stderr)

    if arguments.check:
        problems = check(scan_tree(), read_ratchet())
        if problems:
            print(f"\n{len(problems)} 条默认值问题：", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print(
                "\n强制范围内的每一条都要三选一：`# blame: fail-closed` / "
                "`# blame: fail-open — <理由 + 反证读数>` / `# blame: whitelist — <理由>`"
                "（findings 里每条的 how_to_annotate 直接给了该怎么标）",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
