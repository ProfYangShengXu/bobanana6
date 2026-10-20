"""``item_coverage`` -- every item on a frozen checklist must be accounted for.

This is the fifth gate, and it answers a question none of the other four can:

    presence / schema / concept_ref / ...   "这个形状合法吗 / 值对不对"
    item_coverage                           "清单上的 N 项，产物里交代了几项"

Three rules are load-bearing and each one is a measured decision, not a style
preference:

  1. **A miss is named.** The outcome never says "coverage below threshold"; it
     says "缺：智谱、商汤、Replit". A ratio alone cannot be acted on: the block
     that failed has to know *which* rows to add.
  2. **Compare after normalising aliases.** ``"月之暗面 (Kimi)"`` and
     ``"月之暗面"`` are the same company. The normalisation order is fixed
     (NFKC -> casefold -> drop bracketed suffix -> collapse whitespace) because
     NFKC itself *creates* brackets -- ``㈱`` becomes ``(株)``, ``①②③`` becomes
     ``123`` -- so dropping brackets before NFKC would miss full-width ones.
  3. **Never raise.** A missing item is a *decidable failure*, not a program
     error. Garbage in the artifact or in the checklist must come back as a
     failed result with a readable fact, because the caller's whole job is to
     decide "this block is not done", not to handle an exception.

Deliberately **no fuzzy ratio**. Measured on the real alias pair, the standard
``SequenceMatcher`` ratio for ``"月之暗面 (Kimi)"`` vs ``"月之暗面"`` is 0.571 and
for the upper-case variant 0.533 -- both below the 0.8 threshold such matchers
ship with, so a fuzzy scorer would report a correct row as missing. Bracket
stripping solves the case exactly; a fuzzy threshold would trade a certain error
for an uncertain one.

The denominator is always the checklist that was frozen *before* the work
started. An empty checklist scores 0, never 1: "nothing to check" is absence of
evidence, not full coverage (the same reading the external recall metrics take).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..model import Artifact, Candidate, Constraint
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "item_coverage"

#: Default share of the checklist a product has to account for. Declared in the
#: contract as ``coverage.threshold``; this is only the fallback.
DEFAULT_THRESHOLD = 0.65

#: A checklist shorter than this share of the independently counted items is not
#: trustworthy enough to judge a product with (spec 3.4).
UNDERCOUNT_FLOOR = 0.8

#: Bracket pairs that are treated as an *optional alias suffix*. Both widths are
#: present because NFKC folds the full-width forms into the ASCII ones only after
#: it has run -- see the normalisation order above.
_BRACKETED = re.compile(r"[\(（\[【〈{｛][^\)）\]】〉}｝]*[\)）\]】〉}｝]")
_SPACES = re.compile(r"[\s\u3000]+")
_EDGE_CHARS = " \t·•-—–:：|,，;；。.、/\\*#`"

#: Markdown table separator rows (``|---|---|``), used to tell a real table from
#: a paragraph that happens to contain a pipe.
_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


@dataclass(frozen=True)
class CoverageResult:
    """One checklist against one product. The five fields are the spec's."""

    total: int  # core:checkers -- 分母永远是冻住的清单，不是产物的长度
    covered: int  # core:checkers -- 交代了几项
    missing: tuple[str, ...]  # external:report -- ★ 具名漏项，报告与 verdict 都读它
    ratio: float  # core:checkers -- 与 threshold 比
    passed: bool  # core:kernel -- 判定的唯一出口
    #: ``"column"`` = the item names were read out of the table column whose
    #: header is the axis (or one of its aliases); ``"text"`` = no such column
    #: was found, so presence was decided over the whole product. Kept because
    #: the two readings are not equally strong and a report must not blur them.
    mode: str = "text"  # blame: whitelist — 只用于读数/展示，不参与 pass/fail
    #: Human sentence for why a result is not a pass when ``missing`` is empty
    #: (an empty checklist has nothing missing but is still not coverage).
    note: str = ""  # blame: whitelist — 只用于读数/展示，不参与 pass/fail

    def fact(self) -> str:
        """The one sentence a verdict carries. Names the misses, never the score alone."""
        if self.note:
            return self.note
        if self.missing:
            return (
                f"coverage_missing: 清单 {self.total} 项，产物交代了 {self.covered} 项"
                f"（ratio={self.ratio:.2f}），缺 {len(self.missing)} 项：" + "、".join(self.missing)
            )
        return f"清单 {self.total} 项全部交代（ratio={self.ratio:.2f}）"


# --------------------------------------------------------------------------- #
# Normalisation -- one definition, read by both sides of every comparison
# --------------------------------------------------------------------------- #


def normalize(text: str) -> str:
    """The canonical form two spellings of the same item collapse to.

    Order matters and is asserted by a regression test: NFKC first (it folds
    full-width brackets/punctuation *and* manufactures brackets from single
    glyphs), then casefold, then drop bracketed suffixes, then collapse runs of
    whitespace, then trim decoration characters.
    """
    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    unbracketed = _BRACKETED.sub(" ", folded)
    return _SPACES.sub(" ", unbracketed).strip(_EDGE_CHARS)


def _flat(text: str) -> str:
    """NFKC + casefold + whitespace collapse, **keeping** bracket contents.

    Needed as the second half of the comparison: if the checklist item *is* the
    alias (``"Kimi"``) and the product writes ``"月之暗面 (Kimi)"``, dropping the
    bracketed suffix from the product would hide the match. Trying both forms
    makes the equivalence symmetric.
    """
    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    return _SPACES.sub(" ", folded).strip(_EDGE_CHARS)


def variants(text: str) -> tuple[str, str]:
    """(with brackets, without brackets) -- the two forms a match is tried in."""
    return _flat(text), normalize(text)


def _present_in(item: str, text_flat: str, text_norm: str) -> bool:
    """The presence rule, with the text's two forms already computed.

    ★ 拆出来是为了成本：一份产物的归一化只需要算**一次**，而早先的版本对清单里
      的每一项都把整份产物重算一遍（O(items × len(text))，实测 2000 项 × 36KB
      要 1.79s）。判定发生在块循环里，慢判定会吃掉预算。
    """
    if not str(item).strip():
        return False
    whole = normalize(item)
    flat = _flat(item)
    if whole and whole in text_norm:
        return True
    if flat and flat in text_flat:
        return True
    pieces = [piece for piece in flat.split(" ") if len(piece) >= 2]
    return len(pieces) >= 2 and all(piece in text_flat for piece in pieces)


def item_present(item: str, *texts: str) -> bool:
    """Is ``item`` accounted for in any of ``texts``?

    Three ways to be present, cheapest first:

      * the whole normalised item occurs as a substring -- handles CJK names,
        which have no word boundaries to split on;
      * the whole item occurs with its bracketed suffix kept -- covers the case
        where the alias only exists inside the brackets;
      * every whitespace-separated piece of the item occurs -- covers word order
        (``"Microsoft Azure"`` vs ``"Azure, Microsoft"``) without a fuzzy score.
    """
    for text in texts:
        if not text:
            continue
        if _present_in(item, *variants(text)):
            return True
    return False


def ungrounded(checklist: Sequence[str], source_text: str) -> tuple[str, ...]:
    """Checklist items that do **not** occur in the frozen source material.

    This is the first half of "the checklist itself must be trustworthy": an
    enumeration that invents an item is not an enumeration of this material, and
    using it as a denominator would let a product pass by covering a fiction.
    """
    return tuple(str(item) for item in checklist if not item_present(str(item), source_text))


def undercount(checklist: Sequence[str], expected: int, *, floor: float = UNDERCOUNT_FLOOR) -> bool:  # blame: fail-closed — 缺省即规格给出的保守值；调宽松要显式声明
    """Is the checklist too short to be trusted? (spec 3.4 ②)

    ``expected`` is counted independently of the checklist (a second enumeration
    of the same material), so this is not "the checklist compared with itself".
    A non-positive ``expected`` means nothing independent was obtained -- that is
    *not* evidence of undercounting, and refusing on it would make every offline
    run fail.
    """
    if expected <= 0:
        return False
    return len(checklist) < float(expected) * float(floor)


# --------------------------------------------------------------------------- #
# Reading item names out of a product
# --------------------------------------------------------------------------- #


def _cells(line: str) -> tuple[str, ...]:
    row = line.strip()
    row = row.removeprefix("|")
    row = row.removesuffix("|")
    return tuple(cell.strip() for cell in row.split("|"))


def _axis_cell(header: Sequence[str], wanted: Sequence[str]) -> int | None:
    """Which header cell names the axis.

    ★ 命中分两档，**精确优先**：先找归一化后等于某个别名/轴名的表头，找不到才
      退到"包含"。"包含"单独用会挑错列 —— `alias: [公司]` 会命中 `公司规模`，
      而它如果排在 `公司` 前面，读到的是一列规模数字（覆盖率要么恒 0 要么恒 1，
      两种都不可归因）。同档内取**最短**的那个表头：`公司` 比 `公司名称/所在地` 更像。
    """
    cells = [(position, normalize(cell)) for position, cell in enumerate(header)]
    cells = [(position, name) for position, name in cells if name]
    for position, name in cells:
        if name in wanted:
            return position
    candidates = [(len(name), position) for position, name in cells if any(want in name for want in wanted)]
    return min(candidates)[1] if candidates else None


def item_column(text: str, axis: str, alias: Sequence[str]) -> tuple[str, ...] | None:
    """The cells of the table column whose header names the axis, if there is one.

    Why bother: a company name mentioned once in prose is not "that company's row
    was accounted for". When the product is a table -- which is exactly the shape
    this mechanism was measured on -- reading the named column is the honest
    reading, and it is also the reading that stops a product from passing by
    listing every company in a closing sentence.

    Returns ``None`` (not an empty tuple) when no such column exists, so the
    caller can fall back to whole-text presence and *say so* in ``mode``.
    """
    wanted = [normalize(axis)] if axis else []
    wanted.extend(normalize(alias_text) for alias_text in alias if normalize(alias_text))
    if not wanted:
        return None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        block = []
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            block.append(lines[index])
            index += 1
        if len(block) < 2 or not _SEPARATOR.match(block[1]):
            continue
        header = _cells(block[0])
        hit = _axis_cell(header, wanted)
        if hit is None:
            continue
        values: list[str] = []
        for row in block[2:]:
            cells = _cells(row)
            if hit < len(cells) and cells[hit]:
                values.append(cells[hit])
        if not values:
            # ★ 表头命中但没有数据行 ⇒ 这**不是**"读到了 0 项"，是"这张表还没写"。
            #   继续找下一张表；都不行才回落整篇文本 —— 否则第一张小表会把
            #   后面那张真表挡在外面，读数变成 0/N。
            continue
        return tuple(values)
    return None


def _artifact_text(artifact: Artifact) -> str:
    """The product as text, from ``text`` or -- for a row-shaped artifact -- rows."""
    if artifact.text:
        return artifact.text
    if not artifact.rows:
        return ""
    names = [column.name for column in artifact.columns]
    lines = ["| " + " | ".join(names) + " |"] if names else []
    for row in artifact.rows:
        lines.append("| " + " | ".join(str(row.get(name, "")) for name in names) + " |")  # blame: whitelist — 只用于读数/展示，不参与 pass/fail
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The judgment
# --------------------------------------------------------------------------- #


def check_coverage(
    artifact_text: str,
    checklist: Sequence[str],
    alias: Sequence[str],
    threshold: float,
    *,
    axis: str = "",  # blame: fail-closed — 缺省即规格给出的保守值；调宽松要显式声明
) -> CoverageResult:
    """Count how many checklist items the product accounts for.

    Never raises: every input is coerced, and an unusable input becomes a failed
    result that names the problem instead of an exception.
    """
    items = tuple(str(item) for item in checklist if str(item).strip())
    total = len(items)
    if total == 0:
        return CoverageResult(
            total=0,
            covered=0,
            missing=(),
            ratio=0.0,
            passed=False,
            mode="text",
            note="没有可判的清单（0 项）：无依据不等于全覆盖，按不通过记",
        )
    try:
        minimum = float(threshold)
    except (TypeError, ValueError):
        minimum = DEFAULT_THRESHOLD
    if not 0.0 < minimum <= 1.0:
        minimum = DEFAULT_THRESHOLD
    text = "" if artifact_text is None else str(artifact_text)
    aliases = tuple(str(one) for one in (alias if isinstance(alias, Sequence) and not isinstance(alias, str) else ()) if str(one).strip())

    column = item_column(text, axis, aliases)
    if column is not None:
        haystack = "\n".join(column)
        mode = "column"
    else:
        haystack = text
        mode = "text"

    covered = 0
    missing: list[str] = []
    #: ★ 归一化只算一次（见 `_present_in` 的成本注记）。
    haystack_flat, haystack_norm = variants(haystack)
    for item in items:
        if _present_in(item, haystack_flat, haystack_norm):
            covered += 1
        else:
            missing.append(item)
    ratio = covered / total
    return CoverageResult(
        total=total,
        covered=covered,
        missing=tuple(missing),
        ratio=ratio,
        passed=ratio >= minimum,
        mode=mode,
    )


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    """Checker-protocol adapter: the frozen checklist travels on the context.

    ``core`` never opens the checklist file; the shell reads it and hands the
    items over in ``CheckContext.checklists``. A target with no checklist is a
    *named refusal*, never a silent pass -- "I could not check" and "it passed"
    are different answers.
    """
    spec: Mapping[str, Any] = constraint.spec
    checklist = context.checklists.get(constraint.target, ())  # blame: fail-closed — 缺省即规格给出的保守值；调宽松要显式声明
    if not checklist:
        return fail(
            constraint,
            f"no frozen checklist for target {constraint.target!r}; refusing to judge without one",
        )
    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return fail(
            constraint,
            f"no artifact named {constraint.target!r} in this block; nothing to count against the checklist",
        )
    alias = spec.get("alias", ())  # blame: fail-closed — 缺省即规格给出的保守值；调宽松要显式声明
    if isinstance(alias, str):
        alias = (alias,)
    if not isinstance(alias, Sequence):
        alias = ()
    axis = str(spec.get("axis", ""))  # blame: fail-open — axis 缺失时回落整篇文本匹配（比列匹配宽）；axis 由契约解析期必填，走不到这里；反证 tests/test_coverage.py
    result = check_coverage(
        _artifact_text(artifact),
        checklist,
        alias,
        spec.get("threshold", DEFAULT_THRESHOLD),
        axis=axis,
    )
    #: The expectation (how many items the *material* was independently counted
    #: to have) travels with the constraint only so a failure can say which side
    #: is short. The undercount refusal itself already happened in the shell.
    expected = spec.get("expected", 0)  # blame: whitelist — 只用于读数/展示，不参与 pass/fail
    if result.passed:
        return ok(constraint, result.fact(), f"checklist={result.total} expected={expected} mode={result.mode}")
    return fail(constraint, result.fact(), f"mode={result.mode}")
