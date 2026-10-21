import json
from pathlib import Path

shell_dir = Path('.') / '_shell'

if (shell_dir / 'tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt').exists():
    with open(shell_dir / 'tool-01a15a0487ff316d1a3f1e88-54296-8140b1-0060.txt', 'r', encoding='utf-8') as f:
        data = json.load(f)
        print(json.dumps(data, indent=2)[:2000])