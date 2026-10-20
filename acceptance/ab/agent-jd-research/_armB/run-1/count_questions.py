import re

with open('output/interview-bank.md', 'r', encoding='utf-8') as f:
    content = f.read()

# 数表格行数（每行以 | 开头的道题）
lines = content.split('\n')
question_count = sum(1 for line in lines if '|---|' in line or '考察点' in line)
print(f'question_count via table rows: {question_count}')
print(f'Total lines: {len(lines)}')

# 提取标题
title_match = re.search(r'## 共 (\d+) 道题', content)
if title_match:
    print(f'Documented question count from title: {title_match.group(1)}')