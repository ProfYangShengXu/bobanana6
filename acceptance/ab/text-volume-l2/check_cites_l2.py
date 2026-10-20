"""C-cites：两份材料【都要用上】—— 这一条把"上游有没有断粮"也压住了。

判据（四条一起）：
  ① 至少 30 处 `A:L<数字>` 或 `B:L<数字>` 形式的坐标；
  ② 两份材料【各自】至少 10 处（只写一份 = 另一份的上游没到）；
  ③ 至少 12 个【不同的】行号（防一条引用抄很多遍）；
  ④ 行号至少有 9 成落在对应材料的真实行数之内（防编行号）。

★ 第 ② 条是 L2 相对 L1 多出来的那一条：真题里合并块上游断粮
  （commit.json 的 inputs 里 commit 全是空串）时，产物就只能编。
"""
import pathlib
import re
import sys

TARGET = pathlib.Path("_run/workspace/output/brief.md")
SOURCES = {"A": pathlib.Path("part-a.md"), "B": pathlib.Path("part-b.md")}

if not TARGET.is_file():
    sys.exit("FAIL: 交付物不在 _run/workspace/output/brief.md")

totals = {}
for tag, path in SOURCES.items():
    if not path.is_file():
        sys.exit(f"FAIL: 材料 {path} 找不到（探针题目坏了，不是模型的账）")
    totals[tag] = len(path.read_text(encoding="utf-8").splitlines())

text = TARGET.read_text(encoding="utf-8")
found = {tag: [int(n) for n in re.findall(rf"\b{tag}:L(\d{{1,5}})\b", text)] for tag in SOURCES}

flat = found["A"] + found["B"]
if len(flat) < 30:
    sys.exit(
        f"FAIL: 只有 {len(flat)} 处坐标（要求 >=30）。"
        f"每条实质结论后面都要标 `A:L<数字>` 或 `B:L<数字>`"
    )

for tag, nums in found.items():
    if len(nums) < 10:
        sys.exit(
            f"FAIL: 材料 {tag} 只被引用了 {len(nums)} 处（每份要求 >=10）—— "
            f"两份材料都要用上，只写一份说明另一份的上游没到"
        )

distinct = len({(l, n) for l, nums in found.items() for n in nums})
if distinct < 12:
    sys.exit(f"FAIL: 只用到 {distinct} 个不同的 (材料, 行号)（要求 >=12）")

bad = {tag: sorted({n for n in nums if not (1 <= n <= totals[tag])})[:6] for tag, nums in found.items()}
total_all = len(flat)
in_range = sum(1 for tag, nums in found.items() for n in nums if 1 <= n <= totals[tag])
if in_range / max(1, total_all) < 0.9:
    sys.exit(f"FAIL: 行号越界太多（材料行数 {totals}）；越界样例 {bad}")

print(f"ok cites={total_all} A={len(found['A'])} B={len(found['B'])} distinct={distinct} in_range={in_range/total_all:.2f}")
