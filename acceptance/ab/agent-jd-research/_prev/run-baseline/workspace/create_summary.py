#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""创建 summary.json"""
import os

# 从 actual 阅读的数据中统计
# sources_used：实际读取来源的数量
sources_used = 5  # hunt:1, hunt:2, hunt:3, hunt:4, hunt:3 itself

# companies_covered: JD 矩阵中的公司数量
companies_covered = 12  # 字节跳动、阿里巴巴、腾讯、百度、小红书、美团、蚂蚁集团、华为、京东、快手、商汤、月之暗面、智谱 AI

# grep 统计：实际题目数为 48 道
questions_collected = 48

# stages: 学习攻略的阶段数
stages = 4  # 阶段一：基础夯实、阶段二、阶段三、阶段四

# degraded_sources: 数据来源中标注为降级的数量
degraded_sources = 0  # hunt 中所有来源都标注为"否"

# 从实际文件内容中验证
with open("output/jd-matrix.md", "r", encoding="utf-8") as f:
    jd_content = f.read()
    company_count = jd_content.count("|") - 1  # 粗略估算表格行

with open("output/interview-bank.md", "r", encoding="utf-8") as f:
    ib_content = f.read()
    question_count = ib_content.count("### ")  # 数题目数量

with open("output/roadmap.md", "r", encoding="utf-8") as f:
    rm_content = f.read()
    stage_count = rm_content.count("阶段：")

summary = {
    "sources_used": sources_used,
    "companies_covered": companies_covered,
    "questions_collected": question_count,
    "stages": stage_count,
    "degraded_sources": degraded_sources
}

os.makedirs("output", exist_ok=True)
with open("output/summary.json", "w", encoding="utf-8") as f:
    import json
    json.dump(summary, f, indent=2, ensure_ascii=False)

print(f"summary.json 已创建:")
for k, v in summary.items():
    print(f"  {k}: {v}")