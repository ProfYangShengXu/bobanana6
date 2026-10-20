import re

with open('output/interview-bank.md', 'r', encoding='utf-8') as f:
    content = f.read()

# 用更准确的模式查找题目
# 查找 "Q1."、"Q2." 等格式
q_pattern = r'\*\*Q(\d+)\. '
questions = re.findall(q_pattern, content)
print(f"Found Q{len(questions)}{questions[:5]}...")

# 或者查找主题分类下的题目
# 查找 "###" 或 "####" 分类标题
sections = re.findall(r'(###|####).+' if line else '', line)
for line in re.split(r'(\n)', content):
    if line.startswith('####') and 'Q' in content.split(line)[0][-50:]:
        print(line.strip())
        
# 更简单的：查找 Q 开头的行
lines = content.split('\n')
q_lines = [l for l in lines if l.strip().startswith('**Q')]
print(f"Q 开头的行数：{len(q_lines)}")
print("Sample:", q_lines[:3] if q_lines else "none")