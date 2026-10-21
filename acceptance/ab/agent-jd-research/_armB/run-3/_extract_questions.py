import json
from pathlib import Path

shell_dir = Path('.') / '_shell'

# 读取 grep 输出
with open(shell_dir / 'tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt', 'r', encoding='utf-8') as f:
    outer = json.load(f)

# stdout 是嵌套的 JSON (被转义了)
nested = json.loads(outer['stdout'])
print(f"hunt-interview:4 - Total matches: {nested['total_matches']}")
print()

# 收集所有匹配
questions = []
for m in nested['matches']:
    line = m['line']
    text = m['text']
    
    # 解析 context，找到实际的匹配文本
    context = m.get('context', [{}, {}][0])  # 可能有多个 context 项
    match_context = context.get('match', context)  # match 可能是一个对象引用
    
    # 获取实际匹配的行文本
    actual_line_text = None
    if 'context' in m:
        for c in m['context']:
            if 'text' in c and c['text'] and not match_context or 'match' in c and c.get('match') == True:
                actual_line_text = c.get('text', '')
                break
    
    # 如果 match 是一个 bool，则实际的文本在 text 字段中
    if not actual_line_text:
        actual_line_text = text
    
    questions.append((line, actual_line_text))

print(f"Total questions found: {len(questions)}")
for line, text in questions[:30]:
    print(f"L{line}: {text[:100]}...")