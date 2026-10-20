#!/usr/bin/env python3
"""
解析所有 hunt 快照文件并提取真实数据
"""
import json
import os
from pathlib import Path

def load_shell_stdout(path):
    """读取 shell 工具输出的 JSON 字符串"""
    content = Path(path).read_text(encoding='utf-8')
    try:
        return json.loads(content)
    except:
        return None

def extract_hunt_data():
    """从 shell 目录中提取 hunt 数据"""
    hunt_data = {}
    shell_dir = Path("_shell")
    
    if not shell_dir.exists():
        return hunt_data
    
    for txt_file in sorted(shell_dir.glob("*.txt")):
        # 读取 stdout 中的 JSON
        result = load_shell_stdout(txt_file)
        if not result or not isinstance(result, dict):
            continue
        
        # 每个 shell 输出应该是一个包含"lines"的 JSON 结构
        ref = result.get("ref", "unknown")
        lines = result.get("lines", [])
        total_lines = result.get("total_lines", 0)
        total_chars = result.get("total_chars", 0)
        
        # 汇总每条记录
        if ref not in hunt_data:
            hunt_data[ref] = {
                "total_lines": total_lines,
                "total_chars": total_chars,
                "lines": lines
            }
    
    return hunt_data

def main():
    data = extract_hunt_data()
    
    # 打印找到的 hunt 快照统计
    for ref, info in sorted(data.items()):
        print(f"\n{ref}:")
        print(f"  总行数：{info['total_lines']}")
        print(f"  总字符：{info['total_chars']}")
        print(f"  已读取：{len(info['lines'])} 行")
        print(f"  (前 10 行预览):")
        for i, line in enumerate(info['lines'][:10]):
            text = line.get("text", "")[:100] if line else ""
            print(f"    {i+1}: {text}")
    
    # 汇总
    print("\n\n========== 汇总 ==========")
    total_count = sum(len(v.get("lines", [])) for v in data.values())
    print(f"Total: {len(data)} 个 hunt 快照，{total_count} 条记录")

if __name__ == "__main__":
    main()