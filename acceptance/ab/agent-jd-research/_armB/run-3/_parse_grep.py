import json
import os

# Read the outer JSON
with open('_shell/tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt', 'r', encoding='utf-8') as f:
    outer = json.load(f)

# stdout is a JSON string - decode it nested
nested_data = json.loads(outer['stdout'])

print(f"Total matches: {nested_data['total_matches']}")
print(f"Offset: {nested_data['offset']}")
print(f"Limit: {nested_data['limit']}")
print(f"\nFirst 15 matches:")
for i, m in enumerate(nested_data['matches'][:15]):
    line = m['line']
    text = m['text']
    context = m.get('context', [{}])[0]
    match = context.get('match', {})
    match_text = match.get('text', '')
    if not match_text.startswith('['):
        print(f"Line {line}: {text[:100]}...")
    else:
        print(f"Line {line}: {match_text.get('text', '')[:100]}...")