#!/usr/bin/env python3
"""
从 hunt_data.json 提取 JD 矩阵所需的公司信息
"""
import json

# 读取获取的数据
with open("hunt_data.json", "r", encoding="utf-8") as f:
    data = json.load(f)

print("=== Hunt:1 (字节/腾讯/阿里/百度/月之暗面) ===")
if "hunt:1" in data:
    content = data["hunt:1"]
    # 打印前 3000 字符来查看结构
    print(content[:3000])
    print("...")
    print("Total length:", len(content))

print("\n\n==== Hunt:2 (月之暗面) ===")
if "hunt:2" in data:
    print(data["hunt:2"][:2000])
else:
    print("No data")