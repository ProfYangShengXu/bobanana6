#!/usr/bin/env python3
"""统计汇总脚本"""
import json
import re

# 题目总数：47道
question_count = 47

# 阶段数：5个阶段
stage_count = 5

# 公司数：11家公司
companies_covered = 11

# 数据来源数：7个（hunt:1-7）
sources_used = 7

# 降级来源：hunt:6 和 hunt:7 是降级来源
degraded_sources = 2

# 创建 summary.json
summary = {
    "sources_used": sources_used,
    "companies_covered": companies_covered,
    "questions_collected": question_count,
    "stages": stage_count,
    "degraded_sources": degraded_sources
}

with open('output/summary.json', 'w', encoding='utf-8') as f:
    json.dump(summary, f, indent=2, ensure_ascii=False)

print("summary.json created successfully")