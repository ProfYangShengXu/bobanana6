"""The block brief's coverage obligation — the wording, in one place.

★ 这段措辞是**实测有效的版本**，逐字照抄，不要"优化"它。

八臂对照的读数：同一份材料、同一份清单，唯一差别是"要不要逐项覆盖"。

    +F 只给清单（信息在上下文里，没有"必须用"的义务）   覆盖率 0.35
    +G 清单 + 覆盖义务（每项必须出现 / 缺格保留行 / 自己数一遍） 覆盖率 0.75

也就是说：**把信息给它不够，要让"漏掉一项"变成不可能。** 改一个字的代价是
不知道这条读数还成不成立 —— 所以下面三行连标点都不动。

★ 位置：加在**块自己的 brief** 里，不是全局系统提示词里。理由不是风格：
  系统提示词是"你是谁、规则是什么"，对每个块一样；清单是**这一块**要交代的项集，
  它的分母来自这道题的材料。混进系统提示词会让"这一块为什么是这些项"失去出处，
  也会让前缀缓存从第一个字节起就随块失效。
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["render_coverage_obligation"]

#: 规格 §4 的三条义务。逐字。
_OBLIGATION_LINES: tuple[str, ...] = (
    "- 清单上每一项都必须出现在产物里",
    "- 某一项信息不足时，那一格写「材料未提及」，但这一行要保留",
    "- 写完自己数一遍：清单 N 项，产物交代了几项",
)


def render_coverage_obligation(items: Sequence[str]) -> str:
    """Render the obligation block for one block's brief.

    Returns ``""`` when there is nothing to cover -- an empty obligation would be a
    sentence that obliges nothing, and a block reading it would learn the wrong
    thing (that "nothing to cover" is a pass). The shell refuses an empty checklist
    by name before a block ever starts, so this is only the fail-closed tail.
    """
    names = [str(item) for item in items if str(item).strip()]
    if not names:
        return ""
    count = len(names)
    body = "\n".join(f"   {name}" for name in names)
    lines = [line.replace("清单 N 项", f"清单 {count} 项") for line in _OBLIGATION_LINES]
    head = f"★ 这份产物必须逐项覆盖以下清单（共 {count} 项）："
    return "\n".join([head, body, "", *lines])
