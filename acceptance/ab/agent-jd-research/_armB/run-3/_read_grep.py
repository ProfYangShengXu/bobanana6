import json
with open('_shell/tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt', 'r', encoding='utf-8') as f:
    outer = json.load(f)
with open('_shell/out-54296-8140b1-0001.log', 'r', encoding='utf-8') as f:
    stdout = f.read()
    
with open(stdout, 'r', encoding='utf-8') as f:
    data = json.load(f)

print(f"Total questions found: {data['total_matches']}")
print("\n--- Sample questions ---")
for m in data['matches'][:10]:
    # Extract line and content that looks like a question
    text = m['text']
    context = m.get('context', [{}])[0]
    match_text = context.get('match', {}).get('text', '')
    if not match_text.startswith('['):
        line_num = m['line']
        print(f"Line {line_num}: \"{text}\"")
    else:
        line_num = m['context'][0]['line']
        print(f"Line {line_num}: \"{match_text.get('text', '')}\"")