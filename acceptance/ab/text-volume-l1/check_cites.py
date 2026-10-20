"""C-cites：用没用到读到的材料 —— 这一条把它变成可判的。

判据（三条一起）：
  ① 正文里至少 12 处 `L<数字>` 形式的坐标；
  ② 至少 6 个【不同的】行号（防一条引用抄 12 遍）；
  ③ 这些行号至少有 9 成落在【材料的真实行数】之内（防编行号）。

★ 为什么这条要紧：这是"用上读到的材料"唯一能机械核的东西。
  没它，一份 4000 字、字字与材料无关的稿子也能过 —— 那就成了假完成。
"""
import pathlib
import re
import sys

TARGET = pathlib.Path("_run/workspace/output/brief.md")
SOURCE = pathlib.Path("notes.md")

if not TARGET.is_file():
    sys.exit("FAIL: 交付物不在 _run/workspace/output/brief.md")
if not SOURCE.is_file():
    sys.exit("FAIL: 材料 notes.md 找不到（探针题目坏了，不是模型的账）")

text = TARGET.read_text(encoding="utf-8")
total_lines = len(SOURCE.read_text(encoding="utf-8").splitlines())

found = [int(m) for m in re.findall(r"\bL(\d{1,5})\b", text)]
if len(found) < 12:
    sys.exit(
        f"FAIL: 只有 {len(found)} 处 `L<数字>` 坐标（要求 >=12）。"
        f"每一条实质结论后面都要标它的来源行号，写法 `L<数字>`"
    )

distinct = sorted(set(found))
if len(distinct) < 6:
    sys.exit(f"FAIL: 只用到 {len(distinct)} 个不同的行号（要求 >=6）—— 引用的覆盖面太窄")

in_range = [n for n in found if 1 <= n <= total_lines]
ratio = len(in_range) / len(found)
if ratio < 0.9:
    bad = sorted({n for n in found if not (1 <= n <= total_lines)})[:8]
    sys.exit(
        f"FAIL: {len(found) - len(in_range)}/{len(found)} 处行号超出材料范围"
        f"（材料共 {total_lines} 行）；这些行号不存在：{bad}"
    )

print(f"ok cites={len(found)} distinct={len(distinct)} in_range={ratio:.2f} source_lines={total_lines}")
