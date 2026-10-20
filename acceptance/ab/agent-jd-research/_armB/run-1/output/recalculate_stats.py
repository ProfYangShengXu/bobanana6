#!/usr/bin/env python3
"""重新计算面试题按主题分组统计"""

import re

content = open('output/interview-bank.md', encoding='utf-8').read()

# 提取所有 Q 题目及其考察点
questions = []
in_question = False
current_q_num = None
current_topic = None

for line in content.split('\n'):
    if '**Q' in line and line.startswith('**Q'):
        if in_question:
            # 保存上一个题目
            questions.append((current_q_num, current_topic.strip()))
            current_topic = None
        in_question = True
        match = re.search(r'\*\*Q(\d+)\.\s*(.+)', line)
        current_q_num = int(match.group(1)) if match else 0
        current_topic = content.split(line)[1].split('\n')[0] if content.split(line)[1] else ''
    elif in_question and current_topic:
        # 继续累积题目内容
        pass

# 最后保存一个问题
if in_question and current_topic:
    questions.append((current_q_num, current_topic.strip()))

# 按主题统计
by_topic = {}
for q_num, topic in questions:
    if topic:
        if topic not in by_topic:
            by_topic[topic] = {}
        by_topic[topic][q_num] = 1

print(f"题目总数：{len(questions)}")
print("\n按主题分组统计:")
for topic in sorted(by_topic.keys(), key=lambda x: -len(by_topic[x])):
    q_nums = sorted(by_topic[topic].keys())
    print(f"  {topic}: {len(q_nums)} 题 - Q{q_nums[0]}~Q{q_nums[-1]}")