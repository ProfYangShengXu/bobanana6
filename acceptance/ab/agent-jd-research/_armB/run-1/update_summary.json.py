import json

# 从 deliverables 读取实际数据
# 1. JD 矩阵 - companies_covered
with open('output/jd-matrix.md', 'r', encoding='utf-8') as f:
    jd_content = f.read()
# 表格行：每条公司行都以 | 开头且有公司名
companies = len([l for l in jd_content.split('\n') if l.startswith('| ') and not '|------' in l])
# 排除表头和数据行，只算公司行
companies = len([l for l in jd_content.split('\n') if l.startswith('| ') and '公司' not in l.split('|')[0] if '公司 ' not in l])

# 更简单的方法：数 | 开头的行，但排除表头和分隔线
rows = [l for l in jd_content.split('\n') if l.startswith('| ') and not '|------' in l]
companies = len(rows)  # 包括表头
print(f"Companies from rows analysis: {companies - 1}")

# 2. 面试题库 - questions_collected
with open('output/interview-bank.md', 'r', encoding='utf-8') as f:
    bank_content = f.read()
# 查找 "## 共 X 道题"
import re
match = re.search(r'## 共 (\d+) 道题', bank_content)
if match:
    questions = int(match.group(1))
else:
    questions = 0
print(f"Questions from title: {questions}")

# 3. roadmap - stages
with open('output/roadmap.md', 'r', encoding='utf-8') as f:
    roadmap_content = f.read()
stages = len(re.findall(r'## 阶段 (\d+)', roadmap_content))
print(f"Stages: {stages}")

# 4. sources_used 和 degraded_sources
# instruction: 1
# hunt: 1-6 有数据，hunt:7 缺失
# 实际能读取的 sources: instruction + hunt (系列)
# 但 sources_used 应该是 actual read sources count
# 根据任务书，应该是：instruction (1) + hunt (多个但 hunt:7 降级)
sources_used = 2  # instruction 和 hunt 系列
degraded_sources = 1  # hunt:7 无法读取

print(f"\nSummary to write: sources_used={sources_used}, companies={companies-1}, questions={questions}, stages={stages}, degraded={degraded_sources}")

summary = {
    "sources_used": sources_used,
    "companies_covered": companies - 1,
    "questions_collected": questions,
    "stages": stages,
    "degraded_sources": degraded_sources
}

with open('output/summary.json', 'w', encoding='utf-8') as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print("\noutput/summary.json updated with:")
print(json.dumps(summary, ensure_ascii=False, indent=2))