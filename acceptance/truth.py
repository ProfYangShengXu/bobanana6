"""与真值对照（`架构设计.md §15.4` / `§15.6`）。

合成的意义就在这里：**真值由生成器持有**，于是「答案对不对」是可判的。
而系统自己的三条口径（块级 / 装配 / 归因）**判的不是答案对不对**：

    实测（`acceptance/main/panel-three-sources`，真模型 3/3 跑）
      blocks 4/4 通过 · assembly passed · exit 0
      但合并产物 30 行、主体号是 'B'/'F'/'H'… 单字母 —— 真值 7 行一个都没中。

所以真值对照**不并入那三个维度**（§15.6 明说三口径会朝不同方向动、禁单一总分），
它是**第四份读数**，单独放在 `TaskRun.truth` 里，并且必须与三口径一起看。

★ 真值只给判分器读：`harness` 只把 `input/` 交给运行。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["TruthVerdict", "compare_rows", "declared_key", "load_truth"]


@dataclass(frozen=True)
class TruthVerdict:
    """What the answer got right and wrong, against the hidden truth."""

    row_count_expected: int
    row_count_actual: int
    missing_keys: tuple[str, ...] = ()
    invented_keys: tuple[str, ...] = ()
    detail: str = ""

    @property
    def matched(self) -> bool:
        return self.row_count_expected == self.row_count_actual and not self.missing_keys and not self.invented_keys

    def render(self) -> str:
        return (
            f"truth: rows {self.row_count_actual}/{self.row_count_expected}"
            f", missing {list(self.missing_keys)}"
            f", invented {len(self.invented_keys)}"
            + (f" ({self.detail})" if self.detail else "")
        )


def load_truth(task_dir: str | Path) -> dict[str, Any]:
    """The truth side of a synthetic task. ★ Never handed to the system under test."""
    path = Path(task_dir) / "truth" / "truth.json"
    if not path.is_file():
        raise FileNotFoundError(f"{path}: this task has no truth side")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError(f"{path}: the truth must be an object")
    return document


def declared_key(truth: Mapping[str, Any]) -> str:
    """The key column the task itself names, when it names exactly one.

    ★ 3 个域同形状、不同字段：主验题自己声明键列（`"key": "site"`），
    对照就必须按它比 —— 否则每个新域都拿 `obligor` 去比，两边的键集都是空的，
    只剩行数在比，第四口径【静默失效】。

    ★ 复合键（list，例如压力题声明的 `["obligor", "year"]`）今天**还不支持**：
    只按一列比的对照假装成按两列比，比不按还糟。这里刻意回落并把这个缺口报出来，
    不静默取第一列。
    """
    declared = truth.get("key")
    if isinstance(declared, str) and declared:
        return declared
    return "obligor"


def compare_rows(
    rows: Iterable[Mapping[str, Any]],
    truth: Mapping[str, Any],
    *,
    key: str | None = None,
) -> TruthVerdict:
    """Compare an answer's key set and row count with the truth's."""
    field = key or declared_key(truth)
    expected_rows = list(truth.get("rows", ()))
    expected_keys = {str(row.get(field)) for row in expected_rows if isinstance(row, Mapping) and field in row}
    actual = [dict(row) for row in rows]
    actual_keys = {str(row.get(field)) for row in actual if field in row}
    missing = tuple(sorted(expected_keys - actual_keys))
    invented = tuple(sorted(actual_keys - expected_keys))
    detail = ""
    if invented and not missing:
        detail = "the answer invented keys the truth does not have"
    elif missing and not invented:
        detail = "the answer dropped keys the truth requires"
    elif missing and invented:
        detail = "the answer replaced the truth's keys with invented ones"
    return TruthVerdict(
        row_count_expected=len(expected_rows),
        row_count_actual=len(actual),
        missing_keys=missing,
        invented_keys=invented,
        detail=detail,
    )
