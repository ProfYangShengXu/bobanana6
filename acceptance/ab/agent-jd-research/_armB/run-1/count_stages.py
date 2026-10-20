import re

with open('output/roadmap.md', 'r', encoding='utf-8') as f:
    content = f.read()

# 数阶段（## 阶段 N）
stages = len(re.findall(r'## 阶段 (\d+)', content))
print(f'stages: {stages}')

# 检查标题
title_match = re.search(r'## 阶段 (\d+)', content)
if title_match:
    print(f'First stage: {title_match.group(1)}')