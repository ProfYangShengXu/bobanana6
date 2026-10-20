"""C-length：体量。契约声明了 min_chars=4000，这里是它的判据落点。

★ 这条是本探针的核心 —— 它把"写足"从愿望变成一条会被具名拒的判据。
"""
import pathlib
import re
import sys

TARGET = pathlib.Path("_run/workspace/output/brief.md")
FLOOR = 4000

if not TARGET.is_file():
    sys.exit("FAIL: 交付物不在 _run/workspace/output/brief.md")

text = TARGET.read_text(encoding="utf-8")
# 计有效字符：去掉空白与 Markdown 记号，避免用空格/符号灌水
body = re.sub(r"[\s#*->|`\[\]()]", "", text)
n = len(body)

if n < FLOOR:
    sys.exit(
        f"FAIL: 有效字符 {n}，少于契约声明的 {FLOOR}。"
        f"（提示：分多次 `files.write` + `append=true` 续写，每段写完用 `files.read` 回读尾巴）"
    )

print(f"ok chars={n} floor={FLOOR}")
