"""判分器：三口径 + 强制覆盖声明（`实现规格.md §14.3`）。

三件事在这里被写成结构，而不是写成文档：

  * **禁合成单一总分** —— 块级 / 装配 / 归因会朝不同方向动，一个总分把它们掩盖掉。
    所以 :class:`AcceptanceReport` 里没有 `overall`，`render()` 也不产出分值。
  * **强制覆盖声明** —— 没被走到的出口 = 未验证的代码，必须显式列出来。
  * **冒烟用通过数对比容差** —— 不用 reward 二值（那会把能力波动误诊成架构回归）。

判分器只读被评系统交出来的结果，不参与判定 —— 判定在 `core/`。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_BASELINE",
    "EXIT_KINDS",
    "HIT",
    "NOT_HIT",
    "AcceptanceReport",
    "load_baseline",
    "smoke_gate",
]

HIT = "hit"
NOT_HIT = "not_hit"

#: Every exit the acceptance set is supposed to reach. A run that never reaches one
#: of these has *not tested* that code path, whatever its other numbers say.
EXIT_KINDS: tuple[str, ...] = (
    "deliver",
    "refuse_split",
    "single_agent",
    "decline",
    "degrade_single",
    "reshard",
    "retry_local",
    "taint",
    "abort",
)

DEFAULT_BASELINE = Path(__file__).resolve().parent / "smoke" / "baseline.json"


@dataclass(frozen=True)
class AcceptanceReport:
    """Three axes, kept apart on purpose (``架构设计 §4.2②``)."""

    block_pass_rate: float
    assembly_passed: bool
    attribution_accuracy: float
    #: ``{exit_kind: "hit" | "not_hit"}`` -- every kind in :data:`EXIT_KINDS` is present.
    coverage: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        missing = sorted(set(EXIT_KINDS) - set(self.coverage))
        if missing:
            raise ValueError(f"coverage must declare every exit, missing {missing!r}")
        unknown = sorted(set(self.coverage) - set(EXIT_KINDS))
        if unknown:
            raise ValueError(f"unknown exits in coverage {unknown!r}")

    # -- the two declarations the spec insists on --------------------------- #

    def uncovered(self) -> tuple[str, ...]:
        """The exits this run did not reach."""
        return tuple(kind for kind in EXIT_KINDS if self.coverage.get(kind) == NOT_HIT)

    def coverage_is_complete(self) -> bool:
        return not self.uncovered()

    def render(self) -> str:
        """Human view. ★ No total, and the uncovered list is never optional."""
        lines = [
            "acceptance report",
            f"  block pass rate   : {self.block_pass_rate:.3f}",
            f"  assembly passed   : {self.assembly_passed}",
            f"  attribution acc.  : {self.attribution_accuracy:.3f}",
            "  exits hit         : " + ", ".join(k for k in EXIT_KINDS if self.coverage.get(k) == HIT),
            "  ★ not hit (未验证) : " + (", ".join(self.uncovered()) or "<none>"),
        ]
        lines.extend(f"  note: {note}" for note in self.notes)
        return "\n".join(lines)


def load_baseline(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """The smoke tolerance table (``§14.6``): per task, a *count*, never a reward.

    The value is the lower bound of a same-window 5-run distribution of pass
    counts -- not an average, and not a single run.
    """
    target = Path(path) if path is not None else DEFAULT_BASELINE
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not document:
        raise ValueError(f"{target}: the baseline must be a non-empty object")
    for task, row in document.items():
        if not isinstance(row, dict) or not isinstance(row.get("min_pass_count"), int):
            raise TypeError(f"{target}: {task!r} needs an integer min_pass_count")
        if "reward" in row or "pass" in row:
            raise ValueError(f"{target}: {task!r} declares a reward; the gate compares pass counts")
    return document


def smoke_gate(observed_pass_count: int, minimum_pass_count: int) -> bool:
    """Count against count -- never a boolean reward (``§15.3``)."""
    return observed_pass_count >= minimum_pass_count


def coverage_from_exits(exits: Iterable[str]) -> dict[str, str]:
    """Turn the exits a run actually took into a complete coverage map."""
    seen = set(exits)
    unknown = sorted(seen - set(EXIT_KINDS))
    if unknown:
        raise ValueError(f"unknown exits {unknown!r}")
    return {kind: (HIT if kind in seen else NOT_HIT) for kind in EXIT_KINDS}
