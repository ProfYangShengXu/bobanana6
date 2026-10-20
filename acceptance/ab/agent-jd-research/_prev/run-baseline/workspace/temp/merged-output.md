# 🗺️ AI Agent 工程师学习路线图（2025-2026）

## 目标定位

本路线图面向**国内互联网大厂跳槽**场景，系统梳理从零到拿到 Offer 的完整学习路径。适用于有一定编程基础（Python 熟练）、想转型或深入 AI Agent 方向的工程师。

---

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
Agent 是核心产品力，直接对应面试问答。必须先掌握"如何让模型自动行动"。

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
- 索引策略：全文切分、语义索引、图指数（GraphRAG）
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
- 把 RAG 当成"调用向量搜索 API"，不懂 index 策略
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
面试不是考"你背了多少"，是考"你能不能讲清楚你的东西"。项目=作品集=面试通行证。

**做到什么算过关**：
- 有 1-2 个完整项目（能在简历上写的）
- 能面试"为什么选这个方案、有没有考虑过 X、失败教训"
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

---

*最后更新：2025 年*
*本文档持续更新中，欢迎提交 PR 补充内容*