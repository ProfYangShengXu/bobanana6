#!/usr/bin/env python3
"""统计每个主题有多少题"""

import re

content = open('output/interview-bank.md', encoding='utf-8').read()

# 提取所有 Q 编号
all_q = list(re.finditer(r'\*\*Q(\d+)\.', content))
total = len(all_q)
print(f"总题目数：{total}")

# 按位置提取考察点并分组
# 简单的方案：按标题行分割，统计每个标题下的问题数
sections = re.split(r'^\|.*?\|^\s*$|^\s*\*\*.*?^\s*\$', content, flags=re.MULTILINE)

# 更可靠的方法：用正则提取所有 Q 和对应的考察点
questions = []
in_section = False
last_q_idx = 0

for i, line in enumerate(content.split('\n')):
    if '**Q' in line and line.startswith('**Q'):
        match = re.search(r'\*\*Q(\d+)\..', line)
        if match:
            q_num = int(match.group(1))
            questions.append((q_num, i))
            last_q_idx = i

# 输出
print(f"检测到 {len(questions)} 个 Q 题目")
print(f"题目 ID 范围：Q{questions[0][0]} - Q{questions[-1][0]}")
print(f"题目总数应等于：{len(questions)}")

# 统计每个部分（按 Markdown 标题）
# 提取所有#### 行及其序号
headers = re.findall(r'^((?:###|####)\s+.*?)(?:\n|$)', content, re.MULTILINE)
print("\nMarkdown 标题结构:")
for i, header in enumerate(headers):
    print(f"  标题 {i+1}: {header.strip()[:60]}")