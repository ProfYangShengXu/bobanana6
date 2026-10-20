#!/usr/bin/env python3
"""
读取 hunt 目录下的真实快照数据
"""
import json
import os
from pathlib import Path

def read_hunt_snapshot():
    """读取 hunt 目录的所有 snapshot 文件"""
    data = {}
    hunt_base = Path("input") / "hunt"
    
    if not hunt_base.exists():
        return data
    
    # 遍历 hunt 目录下的所有 .txt 文件
    for txt_file in hunt_base.glob("*.txt"):
        data[txt_file.name] = {}
        with open(txt_file, "r", encoding="utf-8") as f:
            content = f.read()
            data[txt_file.name]["text"] = content
    
    return data

def main():
    data = read_hunt_snapshot()
    
    # 打印每个 snapshot 文件的数量
    for name, content in data.items():
        print(f"\n{name}: {len(content.get('text', ''))} chars")
        if 'text' in content:
            lines = content['text'].split('\n')
            print(f"  行数：{len(lines)}")
            if len(lines) > 0:
                print(f"  首行：{lines[0][:100]}")
    
    return data

if __name__ == "__main__":
    main()