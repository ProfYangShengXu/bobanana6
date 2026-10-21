import json
import os

tmp_dir = '_shell/temp_download'
if not os.path.exists(tmp_dir):
    os.makedirs(tmp_dir)

# Read the outer JSON and extract stdout path
with open('_shell/tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt', 'r', encoding='utf-8') as f:
    outer = json.load(f)
stdout_path = outer['stdout']
print(f"stdout_path type: {type(stdout_path)}")
print(f"stdout_path value: {stdout_path[:200]}")