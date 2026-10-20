#!/usr/bin/env python3
"""
读取 hunt 来源的 JD 数据，并存为 structured data
"""
import requests
import json

# 定义 hunt 来源的 URL
hunt_sources = {
    "hunt:1": "https://raw.githubusercontent.com/harrisliangsu/ai-agent-engineer-handbook/main/interview-prep/jd-requirements.md",
    "hunt:2": "https://raw.githubusercontent.com/AAAAhuihui/agent/main/%E5%AD%97%E8%8A%82%E8%B7%B3%E5%8A%A8/%E7%9C%9F%E5%AE%9E%E9%9D%A2%E7%BB%8F-%E7%89%9B%E5%AE%A2%E5%AE%9E%E5%BD%95.md",
    "hunt:3": "https://raw.githubusercontent.com/AAAAhuihui/agent/main/Agent%E5%B7%A5%E7%A8%8B%E5%B8%88%E5%AD%A6%E4%B9%A0%E8%B7%AF%E7%BA%BF%E5%9B%BE.md",
    "hunt:4": "https://notes.kamacoder.com/interview/llm/20260506bytedance.html",
    "hunt:5": "https://agentic-engineering-jobs.com/langchain-job-market-2026",
    "hunt:6": "https://blog.csdn.net/2401_85343303/article/details/156735755",
    "hunt:7": "https://raw.githubusercontent.com/AAAAhuihui/agent/main/%E5%B9%BF%E4%BD%93%20%E8%AE%BA%20%D0%BA%D0%BE%D0%BA%D1%96/%E7%B4%A2%E9%9D%9E%E6%8C%81%E4%B8%8E%E7%99%BB%E7%A7%A5-%E6%8C%87%E5%8D%97.md",
}

results = {}

for ref, url in hunt_sources.items():
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            text = response.text[:5000]  # 最大读取 5000 字符
            results[ref] = text
            print(f"✓ {ref}: 成功读取")
        else:
            print(f"✗ {ref}: HTTP {response.status_code}")
    except Exception as e:
        print(f"✗ {ref}: {e}")

# 输出 JSON
with open("hunt_data.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print("\n数据已保存到 hunt_data.json")