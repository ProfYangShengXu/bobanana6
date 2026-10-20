import re
with open("interview-bank.md", "r", encoding="utf-8") as f:
    content = f.read()

# 统计以 "|" 开头的行（表格数据行）
lines = content.split("\n")
start = None
count = 0
for i, line in enumerate(lines):
    if "| 题干 |" in line:
        start = i + 1  # 下一行开始
    if start is not None and line.startswith("|") and not line.strip().startswith("---"):
        # 跳过表头和分隔符
        if "|------|--------|------|" not in line:
            count += 1

print(f"题目总数：{count}")

# 统计阶段数
stage_count = len(re.findall(r"^## 阶段 \d", content, re.MULTILINE))
print(f"阶段数：{stage_count}")