import json
import re

# 验证 summary.json
with open('output/summary.json', 'r') as f:
    summary = json.load(f)

print("=== Deliverables 一致性检查 ===")

# 验证 actual 数据
with open('output/jd-matrix.md', 'r') as f:
    jd = f.read()
jd_count = len([l for l in jd.split('\n') if l.startswith('| ') and not '|------' in l]) - 1

with open('output/interview-bank.md', 'r') as f:
    bank = f.read()
q_count = len([l for l in bank.split('\n') if l.strip().startswith('**Q')])

with open('output/roadmap.md', 'r') as f:
    roadmap = f.read()
s_count = len(re.findall(r'## 阶段 (\d+)', roadmap))

print(f"summary.json:           sources_used={summary['sources_used']}, companies={summary['companies_covered']}, questions={summary['questions_collected']}, stages={summary['stages']}, degraded={summary['degraded_sources']}")
print(f"JD-matrix.md:           rows={jd_count} (expected: {summary['companies_covered']})")
print(f"interview-bank.md:      q={q_count} (expected: {summary['questions_collected']})")
print(f"roadmap.md:             stages={s_count} (expected: {summary['stages']})")

# 一致性检查
if summary['companies_covered'] == jd_count and summary['questions_collected'] == q_count and summary['stages'] == s_count:
    print("\n✓ 所有数字一致")
else:
    print("\n✗ 数字不一致，需要修正")

print("\n=== deliverables 清单 ===")
print("1. output/jd-matrix.md - 11 家公司，覆盖字节/腾讯/阿里/百度/月之暗面/DeepSeek/DeepMind/微软/京东/美团/Vercel")
print("2. output/interview-bank.md - 47 道面试题，按主题分类")
print("3. output/roadmap.md - 5 个阶段学习攻略")
print("4. output/summary.json - 统计数据，包含 degraded_sources=1 (hunt:7 不可读)")