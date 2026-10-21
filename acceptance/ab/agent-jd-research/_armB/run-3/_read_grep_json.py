import json

# Read the JSON file as-is (without any escaping)
with open('_shell/out-54296-8140b1-0001.log', 'r', encoding='utf-8') as f:
    data = json.load(f)

print(f"Total matches found: {data['total_matches']}")
print(f"Total lines: {data['total_lines']}")
print(f"\nFirst 20 matches:")

for m in data['matches'][:20]:
    line = m['line']
    text = m['text']
    context = m.get('context', [{}])[0]
    
    # context['match'] is a bool
    is_match = context.get('match', False)
    
    print(f"Line {line}:")
    # The text field contains the matched content
    if 'text' in context and context['text']:
        print(f"  {context['text']}")
    else:
        print(f"  {text[:50]}...")