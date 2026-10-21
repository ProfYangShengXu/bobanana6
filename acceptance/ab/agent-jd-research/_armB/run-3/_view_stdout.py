import json
with open('_shell/tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt', 'r', encoding='utf-8') as f:
    outer = json.load(f)

stdout = outer['stdout']

# Write stdout to a temp file, just as-is (not parsing)
import tempfile
import os
tmp_dir = '_shell/temp_download'
if not os.path.exists(tmp_dir):
    os.makedirs(tmp_dir)
with open(os.path.join(tmp_dir, 'stdout_text.txt'), 'w', encoding='utf-8') as f:
    f.write(stdout)

# Also extract stdout raw (as path)
print(f"stdout path placeholder: {_shell/out-54296-8140b1-0001.log}")