import json
import re

# 1. 从 JD 矩阵统计公司数
with open('output/jd-matrix.md', 'r', encoding='utf-8') as f:
    jd_content = f.read()
# 数表格数据行（去掉表头 | 分隔行 "------"）
rows = [l for l in jd_content.split('\n') if l.startswith('| ') and not '|------' in l]
companies = len(rows)  # 这个 12 包括表头，实际需要 -1
companies = max(0, len(rows) - 1)  # 去掉表头
print(f"companies_covered: {companies}")

# 2. 从面试题库统计题目数
with open('output/interview-bank.md', 'r', encoding='utf-8') as f:
    bank_content = f.read()
q_count = len([l for l in bank_content.split('\n') if l.strip().startswith('**Q')])
print(f"questions_collected: {q_count}")

# 3. 从 roadmap 统计阶段数
with open('output/roadmap.md', 'r', encoding='utf-8') as f:
    roadmap_content = f.read()
stage_count = len(re.findall(r'## 阶段 (\d+)', roadmap_content))
print(f"stages: {stage_count}")

# 4. sources_used 和 degraded_sources
# 来源清单：instruction, hunt, hunt:1, hunt:2, hunt:3, hunt:4, hunt:5, hunt:6, hunt:7
# 实际能读取：instruction (通过 task.mysubtask), hunt 系列部分
# 因为 hunt:7 无法读取，degraded_sources = 1
# sources_used: 实际读取到的来源数
# 根据任务书，应该是 instruction + hunt 系列 = 2
# 但更准确地说，sources_used 应该反映实际读取的 distinct sources
# 已确认：instruction 可读，hunt:1-6 数据在 output 目录（通过 shell 解析），hunt:7 不可读
sources_used = 2  # instruction + hunt
degraded_sources = 1  # hunt:7 cannot be read

print(f"\nGenerating summary.json:")
print(f"  sources_used: {sources_used}")
print(f"  companies_covered: {companies}")
print(f"  questions_collected: {q_count}")
print(f"  stages: {stage_count}")
print(f"  degraded_sources: {degraded_sources}")

summary = {
    "sources_used": sources_used,
    "companies_covered": companies,
    "questions_collected": q_count,
    "stages": stage_count,
    "degraded_sources": degraded_sources
}

with open('output/summary.json', 'w', encoding='utf-8') as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
    
print("\nDone! summary.json written.")