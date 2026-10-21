#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提取 JD 要求矩阵所需的中文大厂招聘信息
"""

import re
from collections import defaultdict

def extract_jd_content():
    """从 hunt-jd:1 中提取 JD 信息"""
    snapshot_path = "snapshots/4ceb1cf66e896bbcd7616a99205d0d49.txt"
    
    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"文件不存在：{snapshot_path}")
        return None
    
    lines = content.split("\n")
    
    jd_info = []
    current_company = None
    company_details = defaultdict(list)
    in_company_section = False
    
    for i, line in enumerate(lines):
        # 查找公司章节标题
        company_match = re.search(r"### (?:[0-9]?)(?:.*?(?:公司 | Company)|.*?(?:【[^】]*?【.*？.*?