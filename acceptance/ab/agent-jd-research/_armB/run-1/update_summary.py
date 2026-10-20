import json
import re

# 读取四个 deliverables 并统计
def count_companies_in_jd():
    with open('output/jd-matrix.md', 'r', encoding='utf-8') as f:
        content = f.read()
    company_count = sum(1 for line in content.split('\n') if r'^\| (.{3,}[^\n]*)$' in line and r'|猎研\|' not in line)
    return len([l for l in content.split('\n') if r'\| (.{3,}[^\n]+)' in l and not l.startswith('|------') and '<br>' not in l])

def count_questions_in_bank():
    with open('output/interview-bank.md', 'r', encoding='utf-8') as f:
        content = f.read()
    # 查找 "## 共 X 道题"
    match = re.search(r'## 共 (\d+) 道题', content)
    if match:
        return int(match.group(1))
    return 0

def count_stages_in_roadmap():
    with open('output/roadmap.md', 'r', encoding='utf-8') as f:
        content = f.read()
    stages = len(re.findall(r'## 阶段 (\d+)', content))
    return stages

# 执行统计
companies = count_companies_in_jd()
questions = count_questions_in_bank()
stages = count_stages_in_roadmap()

print(f"companies_covered: {companies}")
print(f"questions_collected: {questions}")
print(f"stages: {stages}")

# 确定 sources_used 和 degraded_sources
# sources_used: instruction (1) + hunt (系列数据，根据实际读取情况)
# degraded_sources: hunt:7 无法读取，所以是 1
sources_used = 2  # instruction + hunt
degraded_sources = 1  # hunt:7 无法读取

print(f"sources_used: {sources_used}")
print(f"degraded_sources: {degraded_sources}")