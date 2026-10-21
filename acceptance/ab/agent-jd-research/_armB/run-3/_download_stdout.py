import json
import sys

# Read the outer JSON
with open('_shell/tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt', 'r', encoding='utf-8') as f:
    outer = json.load(f)

stdout = outer['stdout']

# The stdout itself is a JSON string - decode it
data = json.loads(stdout)

# Write stdout content to a temp file
import tempfile
with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as f:
    f.write(data['stdout'])
    temp_file = f.name

print(f"Temp file: {temp_file}")
print(f"Total matches: {data['total_matches']}")