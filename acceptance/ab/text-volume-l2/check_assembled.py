"""C-assembled：合并块的上游【真的到了】吗 —— 直接查，不靠推断。

真题实测过的病（T-09/T-10 的 commit.json）：inputs 里每一项的 commit
都是空串 —— 上游的 part-* 从没送到，合并块却照样开工，于是只能拿记忆编。

这里从痕迹树里找读块的产物（`part-*.csv`）。找不到 = 上游没到。
"""
import pathlib
import sys

TREE = pathlib.Path("_run/artifacts/tree")
if not TREE.is_dir():
    sys.exit("FAIL: 找不到 _run/artifacts/tree —— 探针没配好（不是模型的账）")

parts = sorted(
    p for p in TREE.rglob("part-*.csv")
    if p.is_file()
)
sizes = {str(p.relative_to(TREE)): p.stat().st_size for p in parts}
substantial = {k: v for k, v in sizes.items() if v >= 200}

if len(substantial) < 2:
    sys.exit(
        f"FAIL: 读块的产物不足（要求 >=2 份、每份 >=200 字节）—— 上游断粮。"
        f"痕迹树里找到的 part 文件：{sizes or '（一份都没有）'}"
    )

print(f"ok parts={len(substantial)} sizes={substantial}")
