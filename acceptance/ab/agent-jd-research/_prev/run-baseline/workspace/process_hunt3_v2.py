#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于 hunt:3 的真实内容生成 roadmap.md"""

import requests
from bs4 import BeautifulSoup
import os

# hunt:3 的 URL
url = "https://raw.githubusercontent.com/AAAAhuihui/agent/main/Agent%E5%B7%A5%E7%A8%8B%E5%B8%88%E5%AD%A6%E4%B9%A0%E8%B7%AF%E7%BA%BF%E5%9B%BE.md"

try:
    response = requests.get(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    
    # 提取所有 h2, h3 标题
    stages = []
    for h2 in soup.find_all("h2"):
        stages.append(h2.get_text(strip=True))
    
    for h3 in soup.find_all("h3"):
        stages.append(h3.get_text(strip=True))
    
    print(f"找到 {len(stages)} 个章节标题")
    for i, s in enumerate(stages[:20]):
        print(f"{i+1}: {s}")
    
except Exception as e:
    print(f"Error: {e}")