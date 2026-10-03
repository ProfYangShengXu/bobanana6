"""注入集：按 `attacks.yaml` 逐行开关的执行器（`实现规格.md §14.2/§14.5`）。

★ 它只注入与观察，不改被测系统；三步（发现 / 归因 / 出口）分开报。
"""

from __future__ import annotations

__all__: list[str] = []
