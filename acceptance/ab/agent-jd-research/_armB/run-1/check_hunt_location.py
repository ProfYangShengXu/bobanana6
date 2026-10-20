import os, sys

# 当前目录
print("CWD:", os.getcwd())

# 尝试各种可能的位置
dirs = [
    ".",
    "snapshots",
    "data",
    "sources",
    "input",
    "workspace/snapshots",
    "workspace/data",
]

for d in dirs:
    full = os.path.join(os.getcwd(), d)
    if os.path.isdir(full):
        files = os.listdir(full)
        if any('hunt' in f for f in files):
            print(f"Found 'hunt' in {full}: {files}")

# 列出 snapshot 目录
snap = os.path.join(os.getcwd(), "snapshots")
if os.path.isdir(snap):
    files = os.listdir(snap)
    print(f"Snapshots dir: {snap}")
    print(f"Files: {files[:50]}...")