#!/usr/bin/env python3
"""
生成三份产出文件：
1. output/jd-matrix.md - 岗位要求矩阵
2. output/interview-bank.md - 面经题库
3. output/roadmap.md - 学习攻略

数据来源：hunt:1, hunt:2, hunt:3, hunt:4, hunt:3(part)
"""

import json
import re
from datetime import datetime

# ========================
# JD 矩阵（从 hunt:1 提取）
# ========================
JD_DATA = [
    {"公司": "字节跳动", "岗位方向": "AI Agent 工程师", "学历经验": "3-5 年经验", "硬性技术栈": "Python + LangChain/LangGraph + RAG + Tool Calling + 向量库 + Docker/K8s", "加分项": "Agent 架构设计、多 Agent 协作、RAG 优化、上下文工程", "薪资量级": "40-60K · 14-18 薪（应届更低，中年限更高）", "来源": "hunt:1"},
    {"公司": "字节跳动", "岗位方向": "豆包大模型算法工程师", "学历经验": "博士优先", "硬性技术栈": "Transformer、RAG、多模态", "加分项": "顶会论文、Research Engineer 经验", "薪资量级": "6-15W+/月", "来源": "hunt:1"},
    {"公司": "阿里巴巴", "岗位方向": "AI Agent 应用开发工程师", "学历经验": "3-5 年经验", "硬性技术栈": "Python + LangGraph + MCP + RAG", "加分项": "多 Agent 协作、Agent 调度、A2A 协议", "薪资量级": "40-70K · 14-16 薪", "来源": "hunt:1"},
    {"公司": "腾讯", "岗位方向": "混元大模型应用工程师", "学历经验": "3-5 年经验", "硬性技术栈": "LLM、RAG、Embedding、向量数据库", "加分项": "多模态、Agent 模式设计", "薪资量级": "35-60K · 14-15 薪", "来源": "hunt:1"},
    {"公司": "华为", "岗位方向": "盘古大模型 Agent 工程师", "学历经验": "本科起、5 年+资深", "硬性技术栈": "模型训练/微调、SFT、RLHF、推理优化", "加分项": "端侧部署、量化、MoE", "薪资量级": "40-80K · 15-18 薪", "来源": "hunt:1"},
    {"公司": "美团", "岗位方向": "AI Agent 应用开发", "学历经验": "3-5 年经验", "硬性技术栈": "LLM 集成、RAG、上下文管理、Agent 编排", "加分项": "多 Agent 协作、Agent 与业务流程融合", "薪资量级": "35-55K · 14-16 薪", "来源": "hunt:1"},
    {"公司": "快手", "岗位方向": "AI Agent 技术开发", "学历经验": "本科起、3-5 年经验", "硬性技术栈": "LLM 应用、RAG、向量检索、Prompt 工程", "加分项": "多模态 Agent、Agent Eval", "薪资量级": "30-50K · 14-15 薪", "来源": "hunt:1"},
    {"公司": "小红书", "岗位方向": "AI Agent 应用开发", "学历经验": "本科 + 2 年", "硬性技术栈": "LLM API、RAG、Agent 框架、工具调用", "加分项": "Agent 与内容生态结合", "薪资量级": "25-40K · 14 薪", "来源": "hunt:1"},
    {"公司": "月之暗面", "岗位方向": "Kimi Agent 实习生", "学历经验": "应届博士优先", "硬性技术栈": "LLM、Agent 架构、RAG", "加分项": "顶会论文、Competitive Coding", "薪资量级": "6-7K/月，连续 6 月", "来源": "hunt:1"},
    {"公司": "DeepSeek", "岗位方向": "深度学习研究员-AGI", "学历经验": "博士生", "硬性技术栈": "Deep Learning、RLHF、AGI", "加分项": "顶会一作、开源贡献", "薪资量级": "154W/年（传闻近亿元）", "来源": "hunt:1"},
]

# ========================
# 面经题库（从 hunt:2 和 hunt:4 及 hunt:1 综合）
# ========================
QUESTIONS = [
    {"topic": "Agent 架构与多 Agent 协作", "题干": "LangGraph 如何处理循环支持（cycle）？与 DAG 有什么区别？", "考察点": "考察对 Graph 模式的理解，尤其是循环边和人工介入点的设计"},
    {"topic": "Agent 架构与多 Agent 协作", "题干": "ReAct Agent 和 Supervisor Multi-Agent 的核心区别是什么？", "考察点": "ReAct 是单 Agent 内部思考 - 行动循环，Supervisor 是多 Agent 协作模式"},
    {"topic": "Agent 架构与多 Agent 协作", "题干": "A2A（Agent-to-Agent）协议如何解决多 Agent 通信问题？", "考察点": "考察 Agent 间通信协议（如 XML/JSON Schema）与协调机制"},
    {"topic": "Agent 架构与多 Agent 协作", "题干": "多 Agent 系统中如何避免 Agent 间信息丢失？状态持久化怎么做？", "考察点": "检查点机制、共享状态管理、中断恢复"},
    {"topic": "工具与协议", "题干": "MCP（Model Context Protocol）解决了什么问题？与 Function Calling 的区别？", "考察点": "MCP 是工具对接标准化，Function Calling 是能力调用机制"},
    {"topic": "工具与协议", "题干": "Skill 是在 MCP 之上的什么抽象？它如何促进 Agent 间能力复用？", "考察点": "Skill 是能力层封装，MCP 是工具层协议"},
    {"topic": "工具与协议", "题干": "Agent 如何安全调用外部工具？权限边界和沙箱机制怎么设计？", "考察点": "安全检查、权限最小化、沙箱隔离"},
    {"topic": "上下文工程与记忆", "题干": "长对话中如何管理上下文？Token 截断和压缩策略有哪些？", "考察点": "滑动窗口、摘要压缩、向量记忆"},
    {"topic": "上下文工程与记忆", "题干": "如何设计 Agent 的长期记忆机制？短期记忆 vs 长期记忆的存贮策略？", "考察点": "短期=会话上下文，长期=向量知识库/向量索引"},
    {"topic": "上下文工程与记忆", "题干": "上下文污染（context pollution）是什么？如何检测和治理？", "考察点": "无关知识混入导致的幻觉，需要重排序/过滤"},
    {"topic": "RAG", "题干": "混合检索（hybrid retrieval）如何实现？BM25 + Dense 的权重怎么调？", "考察点": "检索策略组合、权重学习"},
    {"topic": "RAG", "题干": "RAG 系统中如何实现重排序（rerank）？Re-ranking 提升效果的关键指标是什么？", "考察点": "rerank 模型选择（BGE-Reranker）、重排阈值"},
    {"topic": "RAG", "题干": "RRF（Reciprocal Rank Fusion）如何整合多个查询结果？公式是什么？", "考察点": "公式：rank(a) = 1/(k^2 + k_1·rank(a))"},
    {"topic": "RAG", "题干": "GraphRAG 相比传统 RAG 的优势是什么？图谱在什么场景最有价值？", "考察点": "知识图谱结构信息、社区检测"},
    {"topic": "RAG", "题干": "RAG 系统如何支持多轮对话？指代消解怎么做？", "考察点": "对话历史管理、实体对齐"},
    {"topic": "RAG", "题干": "如何解决 RAG 的幻觉问题？技术路线有哪些？", "考察点": "引用标注、自我反思、重排序、人工审核"},
    {"topic": "模型层", "题干": "LoRA 和 QLoRA 的区别？QLoRA 如何节省显存？", "考察点": "LoRA=低秩适配，QLoRA=4-bit 量化 + LoRA"},
    {"topic": "模型层", "题干": "SFT（监督微调）和 RAG 的使用场景区别是什么？", "考察点": "微调学能力，RAG 补充知识"},
    {"topic": "模型层", "题干": "在什么场景下必须使用微调？RAG 什么时候不够用？", "考察点": "格式输出、稳定推理链路、领域知识深度"},
    {"topic": "模型层", "题干": "DeepSeek R1 的推理能力和强化学习方法是什么？", "考察点": "R1 的 Chain-of-Thought 推理、RL fine-tuning"},
    {"topic": "工程与 AI Coding", "题干": "AI Coding 中 harness 和测试覆盖率怎么衡量？", "考察点": "自动化评测、回归测试"},
    {"topic": "工程与 AI Coding", "题干": "手写 Transformer 的注意力机制：Self-Attention, Softmax, Positional Encoding？", "考察点": "基础算法能力"},
]

# ========================
# 学习攻略（从 hunt:3 提取）
# ========================
ROADMAP_CONTENT = """# 🗺️ AI Agent 工程师学习路线图（2025-2026）

## 目标定位
本路线图面向**国内互联网大厂跳槽**场景，系统梳理从零到拿到 Offer 的完整学习路径。适用于有一定编程基础（Python 熟练）、想转型或深入 AI Agent 方向的工程师。

## 阶段划分

### 第一阶段：基础夯实（第 1-3 周，约 2 周）
**学什么**：
- Python 基础（类、装饰器、并发、asyncio）
- LLM 基础理解（Transformer 架构、注意力机制、Pretraining、SFT）
- LangChain/LangGraph 核心概念

**为什么这个顺序**：
必须先理解模型原理 → 再学框架 → 最后做项目。框架是工具，模型是原理。

**做到什么算过关**：
- 能手写 Transformer 中 Self-Attention 和 Positional Encoding
- 能用 LangChain 跑通一个简单的 Q&A Agent
- 理解 LangGraph 的 State/Node/Edge 概念

**优先级**：⭐⭐⭐⭐⭐（面试必考基础）
**大致时长**：15-20 小时

**常见坑**：
- 只懂调用 langchain.ChainTypeChain 而不理解底层实现
- 把 LangGraph 当 DAG 用，不理解节点间边和循环处理
- 只背名词，不会用代码实现

---

### 第二阶段：Agent 核心能力（第 4-6 周，约 3 周）
**学什么**：
- Agent 模式：ReAct、Plan-and-Execute、Multi-Agent
- 工具调用：Function Calling、MCP、Skill
- 上下文管理：对话历史、记忆机制

**为什么这个顺序**：
Agent 是核心产品力，直接对应面试问答。必须先掌握“如何让模型自动行动”。

**做到什么算过关**：
- 独立设计并实现一个 ReAct Agent
- 能用 MCP 连接外部工具（如 Google Search）
- 实现多轮对话，加入记忆模块（对话历史或向量内存）

**优先级**：⭐⭐⭐⭐⭐（面试必问 Agent 设计和项目）
**大致时长**：40-50 小时

**常见坑**：
- 只会说概念，不会写代码实现
- 忽略上下文窗口限制，导致幻觉
- 没有考虑 Agent 的终止条件

---

### 第三阶段：RAG 专项（第 7-9 周，约 3 周）
**学什么**：
- 索引策略：全文切分、语义索引、图索引（GraphRAG）
- 检索优化：混合检索、重排序（rerank）、RRF
- 重排序与评估：BGE-Reranker、Eval 指标

**为什么这个顺序**：
RAG 是目前最落地场景，大厂 JD 高频关键词。不懂 RAG 很难进入 interview。

**做到什么算过关**：
- 实现端到端 RAG：Query → Embedding → 检索 → Rerank → Answer
- 用 LangChain 集成向量数据库（Chroma/FAISS/Milvus）
- 能用 RAGAS 或 LLM-eval 做简单评测

**优先级**：⭐⭐⭐⭐（面试必考 RAG 流程）
**大致时长**：35-45 小时

**常见坑**：
- 把 RAG 当成“调用向量搜索 API"，不懂 index 策略
- 重排序是装饰器而不是替换 retriever
- 忽略评估，以为"跑通就行"

---

### 第四阶段：Agent 系统深化（第 10-12 周，约 3 周）
**学什么**：
- LangGraph 高级用法：条件边、Checkpointing、人工介入
- 多 Agent 协作：Supervisor 模式、任务分解、Human-in-the-loop
- Agent Eval：定义评测指标（success rate, token usage, response time）

**为什么这个顺序**：
这是"AI Agent 工程师"区别于"Prompt 工程师/LLM 应用开发"的分水岭。大厂开始要 Agent 系统能力。

**做到什么算过关**：
- 用 LangGraph 实现 Supervisor Multi-Agent
- 实现 Checkpointing，支持中断恢复
- 设计并跑通一套 Agent 评测方案

**优先级**：⭐⭐⭐⭐（加分项，决定能否进二面）
**大致时长**：40-50 小时

**常见坑**：
- 只实现状态机，不做持久化
- 多 Agent 没有任务分配/执行机制
- 忽略异常处理和重试

---

### 第五阶段：项目包装 + 面试突击（第 13-14 周，约 2 周）
**学什么**：
- 简历打磨：突出 Agent 项目细节（记忆机制、工具调用、上下文管理）
- 面试模拟：8-16 道核心面试题准备
- 作品集整理：GitHub、Demo、文档

**为什么这个顺序**：
面试不是考“你背了多少”，是考“你能不能讲清楚你的东西”。项目=作品集=面试通行证。

**做到什么算过关**：
- 有 1-2 个完整项目（能在简历上写的）
- 能面试“为什么选这个方案、有没有考虑过 X、失败教训”
- github 有代码、有 README

**优先级**：⭐⭐⭐⭐⭐（必须！）
**大致时长**：30-40 小时

**常见坑**：
- 项目没落地，只能用"我学习了..."带过
- 简历项目 vs 面试追问对不上
- 缺乏思考深度（"照着教程做的"）

---

## 优先级总结

- **必须**（面试必问）：Transformer 原理、Agent 流程、LangGraph、RAG、上下文管理、项目细节
- **加分**：MCP/Skill、GraphRAG、Agent Eval、多 Agent 协作、SFT/RLHF、DeepSeek R1

## 项目建议（贯穿全程）
做一个"多模态 RAG Agent"，能撑住面试追问：

1. **记忆机制**：对话历史（短期）+ 向量索引（长期）
2. **工具调用**：Google Search、文件 read、code interpreter
3. **上下文管理**：滑动窗口、摘要压缩、重排序

交付物：GitHub repo + README（架构图、流程、核心代码、项目亮点）

---

## 💡 快速查阅清单

- 3-5 年经验 → 40-60K·14-18 薪
- 2026 JD 高频词：Agent、RAG、LangGraph、MCP、上下文工程
- Prompt 工程师独立岗位明显萎缩（2025 年比 2023 年减少约 70%）
"

# ========================
# 写入文件
# ========================

print("开始生成 output/jd-matrix.md...")
with open("output/jd-matrix.md", "w", encoding="utf-8") as f:
    f.write("| 公司 | 岗位方向 | 学历/经验门槛 | 硬性技术栈 | 加分项 | 薪资量级 |\n")
    f.write("|------|---------|-------------|------------|--------|----------|\n")
    for row in JD_DATA:
        f.write(f"| {row['公司']} | {row['岗位方向']} | {row['学历经验']} | {row['硬性技术栈']} | {row['加分项']} | {row['薪资量级']}（来源：{row['来源']}） |\n")

print("开始生成 output/interview-bank.md...")
with open("output/interview-bank.md", "w", encoding="utf-8") as f:
    f.write("# 📚 AI Agent 面试题库（2026 版）\n\n")
    f.write("按主题分类整理的真实面试题目，每道题:**题干** + 一句话**考察点**。\n\n")
    f.write("---\n\n")
    f.write("## Agent 架构与多 Agent 协作（A2A / 主从 / workflow）\n\n")
    for q in QUESTIONS[0:6]:
        f.write(f"**{q['题干']}** （考察点：{q['考察点']}）\n\n")
    f.write("---\n\n")
    f.write("## 工具与协议（Function Calling / MCP / Skill / 权限与安全边界）\n\n")
    for q in QUESTIONS[6:9]:
        f.write(f"**{q['题干']}** （考察点：{q['考察点']}）\n\n")
    f.write("---\n\n")
    f.write("## 上下文工程与记忆（长会话、压缩、长期记忆、上下文污染）\n\n")
    for q in QUESTIONS[9:11]:
        f.write(f"**{q['题干']}** （考察点：{q['考察点']}）\n\n")
    f.write("---\n\n")
    f.write("## RAG（检索、改写、混合检索、RRF、rerank、评测）\n\n")
    for q in QUESTIONS[11:16]:
        f.write(f"**{q['题干']}** （考察点：{q['考察点']}）\n\n")
    f.write("---\n\n")
    f.write("## 模型层（微调/SFT、推理优化、幻觉治理、评估）\n\n")
    for q in QUESTIONS[15:19]:
        f.write(f"**{q['题干']}** （考察点：{q['考察点']}）\n\n")
    f.write("---\n\n")
    f.write("## 工程与 AI Coding（框架对比、harness、测试与覆盖率）\n\n")
    for q in QUESTIONS[19:18]:
        f.write(f"**{q['题干']}** （考察点：{q['考察点']}）\n\n")
    
    f.write("---\n\n")
    f.write("> **说明**：以上题库基于 hunt:1, hunt:2, hunt:4 等来源整理。按主题归类覆盖 Agent 全链路。实际面试还会根据候选人背景深入追问项目细节。\n")

print("开始生成 output/roadmap.md...")
with open("output/roadmap.md", "w", encoding="utf-8") as f:
    f.write(ROADMAP)

print("生成完成！")
print(f"JD 矩阵：{len(JD_DATA)} 家公司")
print(f"面经题库：{len(QUESTIONS)} 道题目")
print(f"学习攻略：5 个阶段")