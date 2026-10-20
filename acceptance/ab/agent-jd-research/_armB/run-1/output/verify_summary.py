#!/usr/bin/env python3
"""验证面试题库统计"""

import re

content = open('output/interview-bank.md', encoding='utf-8').read()

# 提取所有 ### 主分类标题及其位置
cats = []
for m in re.finditer(r'^### (\d+)\. .+', content, re.MULTILINE):
    cats.append((m.start(), m.group(1), m.group(0).strip().split('\n')[0][:50]))

print("主分类列表:")
for start, num, text in cats:
    print(f"  行 {start+1}: .{num} {text}")

# 统计每个主分类的 Q 数量
cats_order = []
for i, (cat_start, cat_num, cat_text) in enumerate(cats):
    if i + 1 < len(cats):
        next_start = cats[i + 1][0]
    else:
        next_start = len(content)
    
    cat_content = content[cat_start:next_start]
    q_count = len(re.findall(r'\*\*Q\d+', cat_content))
    cats_order.append((cat_text, q_count))
    print(f"\n分类 {i}: {cat_text} -> 找到 {q_count} 个 Q")

total = sum([x[1] for x in cats_order])
print(f"\n总计：{total} 题")