import re

with open("jd-matrix.md", "r", encoding="utf-8") as f:
    content = f.read()

# 匹配"| 公司 |"行（即公司行）
lines = content.split("\n")
companies = 0
for line in lines:
    if "| 公司 |" in line:  # 这是表头
        pass  # 跳过表头
    elif line.strip().startswith("|") and "公司" in line:
        # 是数据行
        if "公司 " in line and "|" in line.split("公司 ")[0]:
            companies += 1

print(f"公司数：{companies}")

# 显示每个公司行
for line in lines:
    if line.strip().startswith("|") and "公司" in line:
        print(f"  {line.strip()}")