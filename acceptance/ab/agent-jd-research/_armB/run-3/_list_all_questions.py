import json

with open('_shell/out-54296-8140b1-0001.log', 'r', encoding='utf-8') as f:
    data = json.load(f)

print(f"Total questions: {data['total_matches']}")
print()

# Check if we need more matches
start_line = 71
for m in data['matches']:
    if m['line'] >= 71 and m['line'] <= 100:  # Focus on this range
        print(f"Line {m['line']}: {m['text']}")

print("\n\n--- More questions ---")
# Get next page of matches if we already have processed these
# The matches include nested JSON for line 1, etc.