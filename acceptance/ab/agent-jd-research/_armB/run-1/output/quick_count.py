#!/usr/bin/env python3
"""简单统计每个 Markdown 主体下的题目数"""

import re

content = open('output/interview-bank.md', encoding='utf-8').read()

# 提取每个 ### 标题及其位置
sections = []
last_pos = 0

print(f"文件总字符数：{len(content)}")

for match in re.finditer(r'^### ([0-9]+\. .+?)(?:\n|$)', content, re.MULTILINE):
    cat_text = match.group(0).strip()
    start_pos = match.start()
    rest = content[start_pos + len(cat_text):]
    next_match = re.search(r'^### [0-9]+\. ', rest, re.MULTILINE)
    if next_match:
        next_pos = start_pos + len(cat_text) + next_match.start()
    else:
        next_pos = len(content)
    
    section_text = content[last_pos:next_pos]
    q_count = len(re.findall(r'\*\*Q\d+\.', section_text))
    
    sections.append((q_count, cat_text[:40], start_pos, next_pos))
    
    last_pos = next_pos
    if q_count > 0:
        print(f"  Q{q_count} in: category from line {start_pos+1} to {next_pos}")