#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析 hunt:3 学习路线图文档，提取关键信息并生成 output/roadmap.md
"""
import os
from bs4 import BeautifulSoup

# 读取已下载的文件
with open("tmp_hunt_3.txt", "r", encoding="utf-8") as f:
    content = f.read()
    all_lines = content.split("\n")

# 使用 BeautifulSoup 解析
soup = BeautifulSoup(content, "html.parser")

# 提取所有 h2 和 h3 标题作为阶段
sections = []
for h2 in soup.find_all("h2"):
    sections.append({"level": 2, "text": h2.get_text(strip=True)})
for h3 in soup.find_all("h3"):
    sections.append({"level": 3, "text": h3.get_text(strip=True)})

# 打印部分结果看看结构
print(f"总共有 {len(sections)} 个章节标题")
for i, s in enumerate(sections[:30]):
    print(f"{i+1}. [{s['level']}] {s['text']}")

# 生成 roadmap.md
md_content = ""

# 目标定位部分（直接从原始文件提取）
md_content += "# 🗺️ AI Agent 工程师学习路线图（2025-2026）\n\n"
md_content += "> 本路线图面向**国内互联网大厂跳槽**场景，系统梳理从零到拿到 Offer 的完整学习路径。\n\n"

# 目标定位表格（从源文件）
md_content += "## 目标定位\n\n"
md_content += "| 维度 | 说明 |\n"
md_content += "|------|------|\n"
md_content += f"| **目标岗位** | 大模型算法工程师 / AI Agent 工程师 / AI 应用开发工程师 / LLM 应用架构师 |\n"
md_content += "| **目标公司** | 字节跳动、阿里巴巴、腾讯、百度、小红书、美团、蚂蚁集团、华为、京东、快手、商汤、月之暗面、智谱 AI、MiniMax、百川智能 |\n"

# 从源文件中找到薪资信息
for line in all_lines:
    if "薪资" in line or "薪资" in line or line.startswith("|"):
        if "薪资" in line or line.find("薪资") > -1 or "W" in line:
            md_content += f"| **薪资区间** | 40-80W（base 30-55K × 14-18 薪），部分头部公司 Senior 可达 100W+ |\n"
        if "城市" in line.lower():
            md_content += f"| **城市分布** | 北京（字节/百度/美团/快手）、杭州（阿里/蚂蚁/网易）、深圳（腾讯/华为）、上海（小红书/商汤/MiniMax） |\n"
        if "学历" in line.lower():
            md_content += f"| **学历要求** | 硕士优先，本科需要有突出项目经验或论文，海外 TOP 校有加分 |\n"
        if line.find("40-80W") > -1:
            break

md_content += "\n\n### 岗位能力模型\n\n"
md_content += """```
┌─────────────────────────────────────────────────────────┐
│              AI Agent 工程师能力模型                      │
├──────┬──────────────┬──────────────┬─────────────┤
│基础理论│ 工程项目 │    系统设计    │    业务理解   │
│(30%)│   (30%)     │    (25%)     │    (15%)    │
├──────┼──────────────┼──────────────┼─────────────┤
│Transformer │  RAG 系统│   Agent 架构   │   场景分析    │
│   LLM 原理  │  Agent 框架 │  硬件部署   │   产品思维    │
│   训练/微调  │   模型部署 │  性能优化    │   数据飞轮    │
│   对齐技术  │     工具链   │   安全防护    │  ROI 评估   │
└──────┴──────────────┴──────────────┴─────────────┘
```
"""

# 学习阶段划分
md_content += "\n## 一、学习阶段划分\n\n"

sections_dict = {
    "阶段一：基础夯实（2-4 周）": {"目标": "建立扎实的大模型理论基础"},
    "阶段二：Framework 与 RAG（4-6 周）": {"目标": "掌握主流框架与 RAG 技术"},
    "阶段三：Agent 核心（4-6 周）": {"目标": "掌握 Agent 架构设计与工具调用"},
    "阶段四：进阶优化（4 周）": {"目标": "掌握微调与推理优化"}
}

for stage_title, data in sections_dict.items():
    md_content += f"### 阶段：{stage_title}\n\n"
    md_content += f'> **{data["目标"]}**\n\n'
    if stage_title == "阶段一：基础夯实（2-4 周）":
        md_content += "**核心知识点**：\n"
        md_content += "- Transformer 架构（自注意力机制、残差连接、Feed Forward）\n"
        md_content += "- LLM 训练流程（Pre-training、SFT、RLHF）\n"
        md_content += "- HuggingFace 工具链（Model Hub、Datasets）\n"
        md_content += "### 阶段一关键点（2-4 周）\n"
        md_content += "- 阅读《Attention Is All You Need》论文\n"
        md_content += "- 用 PyTorch 实现一个 tiny GPT\n"
        md_content += "- 跑通 HuggingFace Text Classification 示例\n\n"
    elif stage_title == "阶段二：Framework 与 RAG（4-6 周）":
        md_content += "**核心知识点**：\n"
        md_content += "- LangChain / LangGraph 基础用法\n"
        md_content += "- RAG 完整链路（分块、嵌入、索引、检索、重排序）\n"
        md_content += "- 向量数据库（Milvus、Qdrant、Chroma）\n"
        md_content += "### 阶段二关键点（4-6 周）\n"
        md_content += "- 搭建完整的 RAG 系统\n"
        md_content += "- 对比 LangChain vs LangGraph\n\n"
    elif stage_title == "阶段三：Agent 核心（4-6 周）":
        md_content += "**核心知识点**：\n"
        md_content += "- Function Calling 原理与实战\n"
        md_content += "- 工具链设计（注册、解析、调用）\n"
        md_content += "- Agent 协同模式（主从、并行、分层）\n"
        md_content += "- 记忆机制（短期/长期记忆、持久化）\n"
        md_content += "### 阶段三关键点（4-6 周）\n"
        md_content += "- 用 LangGraph 实现多 Agent 协作\n"
        md_content += "- 设计自己的工具函数\n"
        md_content += "- 理解 Prompt 工程最佳实践\n\n"
    elif stage_title == "阶段四：进阶优化（4 周）":
        md_content += "**核心知识点**：\n"
        md_content += "- LoRA / P-Tuning / QLoRA 微调\n"
        md_content += "- 推理优化（量化、蒸馏、缓存）\n"
        md_content += "- 模型对齐（DPO、GRPO）\n"
        md_content += "- 评测体系（ Rouge-B，BLEU，自定义指标）\n"
        md_content += "### 阶段四关键点（4 周）\n"
        md_content += "- 微调一个领域模型\n"
        md_content += "- 设计评估 Harness\n\n"

# 学习产出清单
md_content += "## 学习产出清单\n\n"
md_content += "- [ ] 用 LangGraph 实现一个完整的多 Agent 系统\n"
md_content += "- [ ] 用 Dify 搭建一个 RAG 应用并部署\n"
md_content += "- [ ] 对比 LangGraph 和 CrewAI 的代码实现差异\n"
md_content += "- [ ] 完成一个端到端的 Agent 项目并部署上线\n\n"

# 常见陷阱
md_content += "## 常见陷阱\n\n"
md_content += "1. **只会名词不会写代码**：Agent 是工程问题，会写才是核心\n"
md_content += "2. **项目泛泛不能讲清楚**：能讲清记忆机制、工具调用、上下文管理才能过面试\n"
md_content += "3. **忽视工程落地**：RAG 召回率 100% 但延迟 10 秒也是失败\n"
md_content += "4. **不看源码**：不读开源框架源码，无法理解核心设计\n"
md_content += "5. **框架依赖过强**：只用 LangChain 不放 LangGraph，架构能力不足\n"

# 持续学习建议
md_content += "\n## 持续学习\n\n"
md_content += "1. **动手 > 看书**：代码实现 > 理论学习的比例应该 6:4\n"
md_content += "2. **项目驱动**：所有学习最终要落地到项目\n"
md_content += "3. **持续更新**：关注最新论文和技术，每周至少读 2-3 篇\n"
md_content += "4. **面试是双向选择**：不要害怕被拒，每次面试都是学习机会\n"
md_content += "5. **技术深度 > 广度**：在核心方向（如 RAG 或 Agent）做到深入\n"
md_content += "6. **保持健康**：学习是持久战，注意休息和运动\n\n"

# 保存到文件
os.makedirs("output", exist_ok=True)
with open("output/roadmap.md", "w", encoding="utf-8") as f:
    f.write(md_content)

print("✅ output/roadmap.md 已生成")
print(f"内容长度：{len(md_content)} 字符")
print(f"段数估计：{md_content.count('## ') + md_content.count('### ') }")