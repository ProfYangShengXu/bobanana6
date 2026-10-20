import re

# 统计 company
with open("jd-matrix.md", "r", encoding="utf-8") as f:
    content = f.read()

companies = len(re.findall(r"\| (?:[^\s|]+) \| #(?! [^\|]+ )*#", content))
print(f"JD 公司数：{companies}")

# 统计 question
with open("interview-bank.md", "r", encoding="utf-8") as f:
    content = f.read()

questions = len(re.findall(r"\| (.*?) \| [\|\s]+[\|\s]+hunt:", content))
print(f"面试题库数：{questions}")

# 统计 stage (roadmap)  
with open("roadmap.md", "r", encoding="utf-8") as f:
    content = f.read()

stages = len(re.findall(r"## 阶段 \d+", content))
print(f"学习阶段数：{stages}")

# 生成 summary.json format
summary = {
    "companies_covered": companies,
    "questions_collected": questions,
    "stages": stages
}
print(f"\nsummary.json 内容：{summary}")