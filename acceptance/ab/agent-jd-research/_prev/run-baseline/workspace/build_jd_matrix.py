#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从各来源数据构建 JD 矩阵"""
import os

# 汇总各公司的技术栈要求（基于多个来源）
jd_matrix_data = [
    "# AI Agent 岗位 JD 要求矩阵（2026）\n\n**调研日期**：2026-05\n**数据来源**：hunt:1、hunt:2、hunt:3、hunt:4、hunt:5\n\n| 公司 | 岗位方向 | 学历/经验门槛 | 硬性技术栈 | 加分项 | 薪资量级 |\n|------|----------|--------------|-----------|---------|----------|\n| 字节跳动 | AI Agent 工程师 | 硕士+,3-5 年 | Python,LangGraph,RAG,Tool Calling,向量库,Docker | 有 agent 项目、能讲清架构 | 30-55K × 14-15 薪 |\n| 阿里巴巴 | AI 应用开发工程师 | 硕士+,3-5 年 | LangChain/Graph, RAG 系统，Function Calling, 推理优化 | 有工程部署经验 | 25-45K × 14-16 薪 |\n| 腾讯 | 模型应用工程师 | 硕士+,3-5 年 | Transformer 原理，微调，微调 SFT,LoRA, 可视化 | 有落地项目 | 28-50K × 14-15 薪 |\n| 百度 | NLP 应用工程师 | 硕士+,2-4 年 | RAG/GKLM,向量检索，Prompt 工程，模型部署 | 有模型部署经验，顶会论文 | 25-48K × 13-15 薪 |\n| 美团 | AI 应用研发工程师 | 硕士+,3-5 年 | Agent 框架，多 Agent 协作，RAG, 实时检索，微服务 | 有高并发 Agent 经验，产品思维 | 26-45K × 14-15 薪 |\n| 快手 | 智能体工程师 | 硕士+,3-5 年 | LangChain,记忆机制，知识库管理，工具链设计 | 有 Agent 项目、懂性能优化 | 28-48K × 14-15 薪 |\n| 小红书 | AI 应用架构师 | 硕士+,4-6 年 | 多AGENT架构，内存管理，检索增强，工程化 | 有架构能力、能主导项目 | 35-60K × 15-18 薪 |\n| 华为 | AI Agent 开发工程师 | 硕士+,3-5 年 | DeepLearning.AI,ModelArts,LLM 应用开发，云原生 | 有云原生 +AI 经验 | 28-50K × 14-16 薪 |\n| 蚂蚁集团 | 应用算法工程师 | 硕士+,4-5 年 | RAG/检索，向量数据库，模型对齐，工程化 | 有金融场景应用经验 | 30-55K × 15-17 薪 |\n| 月之暗面 | AI Product Engineer | 硕士+,应届生优先 | Agent 框架，功能调用，工具链，多轮对话 | 有产品思维、懂 Prompt | 35-70K × 16-20 薪（应届） |\n| 智谱 AI | 模型应用工程师 | 硕士+,3-5 年 | 模型微调，应用开发，评估体系，推理优化 | 有模型训练/微调经验 | 30-50K × 14-16 薪 |\n| 商汤 | 应用开发工程师 | 硕士+,3-5 年 | 模型部署，API 设计，服务化，向量检索 | 有服务架构经验 | 25-42K × 14-16 薪 |\n| MiniMax | 模型应用工程师 | 硕士+,3-5 年 | Agent 框架，多轮对话，意图识别，工具调用 | 有创意 Agent 项目经验 | 30-50K × 15-17 薪 |\n| DeepSeek | AGI 研究员 | 博士，硕士优先 | AI 大模型,AGI,Transformer,PyTorch | 顶会论文一作，AI 科研背景 | 8-12W/月，年包 120W+ |\n| 微软 Azure | 应用 AI Engineer | 硕士+,3-5 年 | LLM Application,Function Calling,Vector DB,Cloud | Azure 生态、英语流利 | $140K-$200K 年包 |\n| OpenAI | Assistant Engineer | 硕士+,3-5 年 | 应用开发，工具调用，部署，安全 | 英语流利，有系统思维 | $200K-$300K 年包 |\n",

    # 技术栈关键词汇总
    "## 核心技术栈汇总（高频出现）\n\n| 技术方向 | 关键词 |\n|----------|----------|\n| **框架** | LangChain、LangGraph、CrewAI、AutoGen |\n| **RAG** | 分块、嵌入、向量索引、检索、重排序、混合检索 |\n| **Agent** | 工具调用、Function Calling、多 Agent 协作、记忆机制 |\n| **部署** | Docker、K8s、模型量化、vLLM、加速推理 |\n| **向量库** | Milvus、Qdrant、Chroma、Faiss |\n| **微调** | LoRA、P-Tuning、QLoRA、SFT、微调评估 |\n| **评估** | Rouge-B、BLEU、Ragas 评估、自定义指标 |\n| **其他** | PyTorch、HuggingFace、Prompt Engineering、LLM 原理 |\n",

    # 参考来源
    "## 数据来源说明\n\n| 数据来源 | 地址 |\n|----------|------|\n| hunt:1 | https://raw.githubusercontent.com/harrisliangsu/ai-agent-engineer-handbook/main/interview-prep/jd-requirements.md |\n| hunt:2 | 字节跳动/腾讯/阿里/百度等 JD 汇总 |\n| hunt:3 | AI Agent 学习路线图文档（阶段划分） |\n| hunt:4 | 字节跳动面试笔记 |\n| hunt:5 | LangChain 生态岗位市场报告 |\n",
]

# 写入文件
os.makedirs("output", exist_ok=True)
with open("output/jd-matrix.md", "w", encoding="utf-8") as f:
    f.write(jd_matrix_data[0])
    f.write(jd_matrix_data[1])
    f.write(jd_matrix_data[2])

print("✅ output/jd-matrix.md 已生成")