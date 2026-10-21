import json
from pathlib import Path

shell_dir = Path('.') / '_shell'

# 读取原始的 stdout 文件（这是 grep 的完整输出）
with open(shell_dir / 'out-54296-8140b1-0001.log', 'r', encoding='utf-8') as f:
    data = json.load(f)

print(f"hunt-interview:4 - Total matches: {data['total_matches']}")
print(f"Total lines: {data['total_lines']}")
print(f"\nFirst 5 matches:")

for m in data['matches'][:5]:
    line = m['line']
    text = m['text']
    context = m.get('context', [])
    
    # 从 context 中找到匹配的行文本
    actual_text = None
    for c in context:
        if 'text' in c and c['text']:
            actual_text = c['text']
            break
    
    print(f"L{line}:")
    if actual_text:
        print(f"  \"{actual_text[:80]}...\"")
    else:
        print(f"  \"{text[:80]}...\"")