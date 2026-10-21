import json

# Parse the JSON from the file
with open('_shell/tool-01a15a0b8b1741e602fdd5f5-54296-8140b1-0065.txt', 'r', encoding='utf-8') as f:
    outer = json.load(f)

print(f"Total lines in source: {outer['total_lines']}")
print(f"Total chars: {outer['total_chars']}")
print(f"Lines returned: {outer['returned_lines']}")
print()

# Parse and print the first 30 lines
print("--- First 30 lines ---")
for line_data in outer['lines'][:30]:
    print(f"L{line_data['line']}: {line_data['text'][:100]}...")