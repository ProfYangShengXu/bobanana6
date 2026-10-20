import re

with open('output/jd-matrix.md', 'r', encoding='utf-8') as f:
    content = f.read()

# 数表格行（每行以 | 开头的公司）
lines = content.split('\n')
company_count = sum(1 for line in lines if re.match(r'\| (.{3,}[^\n]+)', line))
print(f'company_count via table rows: {company_count}')

# 提取标题
title_match = re.search(r'\*\*\*覆盖公司数\*\*: (\d+)', content)
if title_match:
    print(f'Documented company count from title: {title_match.group(1)}')