import re

with open('output/interview-bank.md', 'r', encoding='utf-8') as f:
    content = f.read()

# 查找 Q 开头的行
lines = content.split('\n')
q_lines = [l for l in lines if l.strip().startswith('**Q')]
print(f"Q 开头的行数：{len(q_lines)}")

# 显示前 5 个
for i in q_lines[:5]:
    print(f"  {i.strip()[:60]}")

# 统计实际题目行数（去掉空行和标题行）
all_question_rows = []
current_q = ""
for line in lines:
    if '**Q' in line:
        all_question_rows.append((current_q, line))
        current_q = line
    elif all_question_rows and line.strip().startswith('**'):
        all_question_rows[-1] = (line.strip(), line)

print(f"\nTotal question entries: {len(all_question_rows)}")