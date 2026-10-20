#!/usr/bin/env python3
"""统计每个主分类（### 标题）下的题目数量"""

import re

content = open('output/interview-bank.md', encoding='utf-8').read()

# 定义主分类标题
categories = [
    "### 1. Agent 架构与多 Agent 协作（A2A / 主从 / workflow）",
    "### 2. 工具与协议（Function Calling / MCP / Skill / 权限与安全边界）",
    "### 3. 上下文工程与记忆（长会话、压缩、长期记忆、上下文污染）",
    "### 4. RAG（检索、改写、混合检索、RRF、rerank、评测）",
    "### 5. 模型层（微调/SFT、推理优化、幻觉治理、评估）",
    "### 6. 工程与 AI Coding（框架对比、harness、测试与覆盖率）"
]

# 提取所有### 主分类标题
cat_pattern = r'^### \d+\. ([^-]+)'
cats = re.findall(cat_pattern, content, re.MULTILINE)

# 对于每个主分类，统计其下列出的题目数
def count_questions(in_section, text):
    """统计 in_section 之后的第一个 ### 标题之前的题目数量"""
    after_section = text[in_section:]
    # 找到下一个 ### 开头的位置
    next_hash = len(after_section)
    for i, c in enumerate(after_section):
        if c == '#':
            next_hash = i - 1
            # 确保是 ### 不是 ##
            if i >= 2 and after_section[i-2] == '#':
                break
    
    section_content = after_section[:next_hash]
    q_count = len(re.findall(r'\*\*Q\d+', section_content))
    return q_count

categories_text = {}
in_current = 0
current_section_start = 0
last_cat_idx = -1

# 遍历所有### 标题
for cat_line in cats:
    idx = content.find(cat_line)
    if idx > 0:
        if last_cat_idx >= 0:
            cat_count = count_questions(current_section_start, content)
            categories_text[cat_count] = cat_count
        current_section_start = idx
        in_current = True
    last_cat_idx = idx

# 找到最后一个
if len(cats) > 0:
    last_cat = cats[-1]
    last_idx = content.find(last_cat)
    if last_idx > 0:
        fcnt = count_questions(current_section_start, content)
        categories_text[fcnt] = len(categories_text[fcnt]) + 1

print("题目计数结果:")
for cat in categories:
    # 简单的标题计数方法
    pass

# 更好的方法：按行的行号分段计算
print("\n逐段统计:")
current_count = 0
prev_lineNum = -1
for line in content.split('\n'):
    if re.match(r'^### \d+\. ', line):
        # 这是一个主分类标题
        if current_count > 0:
            print(f"  {current_count} 题之前：{previous_cat}")
        current_count = 0
        prev_lineNum = content.split('\n').index(line) + 1
    if re.match(r'\*\*Q\d+\.', line):
        current_count += 1
        prev_lineNum = content.split('\n').index(line) + 1
    # 遇到空行或主要分割符则结束当前分类
    if line.strip() == "---" and current_count > 0:
        # 检查是否是下一分类的起点之后
        pass

print(f"最后统计：共 {current_count} 题")
# 重新计算