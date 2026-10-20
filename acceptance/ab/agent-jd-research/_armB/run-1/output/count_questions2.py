#!/usr/bin/env python3
"""统计面试题数量和分组 - 修复版本"""

import re

content = open('output/interview-bank.md', encoding='utf-8').read()

# 用正则提取每个 Q 和考察点
questions = []
pattern = r'Q(\d+)\.\s*([^Q]*?)(?=Q\d+\.|^$)'
for match in re.finditer(pattern, content, re.DOTALL):
    q_num = int(match.group(1))
    text = match.group(2).strip()
    # 提取考察点（考察点：xxx）
    exam_match = re.search(r'考察点：(.+？)', text)
    exam_point = exam_match.group(1).strip() if exam_match else '未标注'
    questions.append((q_num, text[:50]))

print('题目数量:', len(questions))
print('前 10 题:')
for i, (q_num, text) in enumerate(questions[:10]):
    print(f'  {i+1}. Q{q_num}: {text}')