#!/usr/bin/env python3
"""
读取 hunt 来源，提取 JD 需求数据
"""
import os
import json
import re

SNAPSHOTS_DIR = "_run/snapshots"
INDEX_FILE = "snapshots/sources.index.json"

def read_sources():
    # 先读 index 了解来源映射
    index_path = os.path.join(os.path.dirname(__file__), INDEX_FILE)
    with open(index_path, 'r', encoding='utf-8') as f:
        index = json.load(f)
    
    results = {}
    for key, data in index.items():
        if key in ['instruction', 'hunt']:  # 跳过这些
            continue
        body_path = os.path.join(SNAPSHOTS_DIR, data['body'])
        try:
            with open(body_path, 'r', encoding='utf-8') as f:
                content = f.read()
            results[key] = content
        except Exception as e:
            print(f"Error reading {key}: {e}")
    
    return results

if __name__ == "__main__":
    sources = read_sources()
    print(json.dumps(sources, ensure_ascii=False, indent=2))