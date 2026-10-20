#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 hunt:3 学习路线图文档"""
import requests
from bs4 import BeautifulSoup

url = "https://raw.githubusercontent.com/AAAAhuihui/agent/main/Agent工人郎学习路线图.md"

try:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    with open("tmp_hunt_3.txt", "w", encoding="utf-8") as f:
        f.write(response.text)
    print(f"已下载，文件大小：{len(response.text)} 字符")
except Exception as e:
    print(f"Error: {e}")
    # Try the longer URL with Chinese characters
    url2 = "https://raw.githubusercontent.com/AAAAhuihui/agent/main/Agent%E5%B7%A5%E7%A8%8B%E5%B8%88%E5%AD%A6%E4%B9%A0%E8%B7%AF%E7%BA%BF%E5%9B%BE.md"
    try:
        response2 = requests.get(url2, timeout=30)
        response2.raise_for_status()
        with open("tmp_hunt_3.txt", "w", encoding="utf-8") as f:
            f.write(response2.text)
        print(f"已下载（第二个 URL），文件大小：{len(response2.text)} 字符")
    except Exception as e2:
        print(f"Error2: {e2}")