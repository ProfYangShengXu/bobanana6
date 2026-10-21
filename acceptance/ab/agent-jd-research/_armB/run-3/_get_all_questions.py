import json
import os
from pathlib import Path

# 工作目录路径
workspace = Path('.').resolve()
shell_dir = workspace / '_shell'

# 第一个 grep 的输出是 _shell/tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt
# 第二个 grep 的输出是 _shell/tool-01a15a0a7b362d90de179596-54296-8140b1-0064.txt

print("Reading grep outputs...")

# Read grep for hunt-interview:4
if (shell_dir / 'tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt').exists():
    with open(shell_dir / 'tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt', 'r', encoding='utf-8') as f:
        data = json.load(f)
        print(f"hunt-interview:4 grep: {data['total_matches']} matches")

print("Done")