"""C-write：产物真的落在交付位置，而且是一篇成文的稿子（不是骨架/不是脚本）。

★ 为什么单独一条：真题里翻过车 —— 块把产物写到了【嵌套的 workspace/】里
（`workspace/output/brief.md`），写区守卫把它当草稿纸放行，于是
"写了但等于没交"，而且【没有任何提示】。
这条把"落点"变成一条具名判据。
"""
import pathlib
import sys

TARGET = pathlib.Path("_run/workspace/output/brief.md")

if not TARGET.is_file():
    # ★ 具名失败要说清【找的是什么、在哪找的】，并顺手报出近似路径，
    #   否则模型只会一遍遍重试同一个错误落点。
    near = sorted(
        str(p).replace("\\", "/")
        for p in pathlib.Path("_run/workspace").rglob("brief.md")
    )
    hint = f"；但发现了近似路径 {near}" if near else ""
    sys.exit(f"FAIL: 交付物不在 _run/workspace/output/brief.md{hint}")

text = TARGET.read_text(encoding="utf-8")
if not text.strip():
    sys.exit("FAIL: 交付物是空的")

# 成文的最低形态：要有若干段落（空行分隔的非空块）。
paragraphs = [b for b in text.split("\n\n") if len(b.strip()) >= 80]
if len(paragraphs) < 8:
    sys.exit(f"FAIL: 成篇段落只有 {len(paragraphs)} 段（要求 >=8）—— 看着像骨架或要点罗列")

print(f"ok paragraphs={len(paragraphs)} chars={len(text)}")
