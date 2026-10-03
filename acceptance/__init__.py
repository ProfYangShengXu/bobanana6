"""验收集（`实现规格.md §14` / `架构设计.md §15`）。

★ 这一层是**评测**，不是产品：它不参与 `core/` 的判定，也不改被评的东西。
★ ground truth 与给系统的输入【物理分开】：`stress/input/` 给系统，`stress/truth/`
  只在评测时读 —— 不隔离时「模型没答对」和「模型偷看了」分不开。
"""

from __future__ import annotations

from .harness import TaskRun, run_task
from .judge import (
    EXIT_KINDS,
    HIT,
    NOT_HIT,
    AcceptanceReport,
    coverage_from_exits,
    load_baseline,
    smoke_gate,
)
from .truth import TruthVerdict, compare_rows, declared_key, load_truth

__all__ = [
    "EXIT_KINDS",
    "HIT",
    "NOT_HIT",
    "AcceptanceReport",
    "TaskRun",
    "TruthVerdict",
    "compare_rows",
    "coverage_from_exits",
    "declared_key",
    "load_baseline",
    "load_truth",
    "run_task",
    "smoke_gate",
]
