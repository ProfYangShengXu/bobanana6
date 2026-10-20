# 🗺️ AI Agent 工程师学习路线图（2025-2026）

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