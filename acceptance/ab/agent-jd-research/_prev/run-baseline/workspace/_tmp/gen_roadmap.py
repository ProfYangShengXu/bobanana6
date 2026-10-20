with open('output/roadmap.md', 'w', encoding='utf-8') as f:
    content = """# ★★★★ AI Agent 工程师学习路线图（2025-2026）

---

> **适用对象**：有 Python/后端基础，想在 2-3 个月内具备 AI Agent 投递能力的工程师

---

## 📌 数据来源

- **hunt:3**：AI Agent 工程师学习路线图原文（1684 行）
- **hunt:2**：字节跳动真实面经（95 行）

---

## 🎯 路径概览（5 个阶段）

| 阶段 | 主题 | 必学 | 加分 | 预计时长 |
|------|------|------|------|----------|
| ① | 核心框架与基础架构 | LangGraph、工作流 | Multi-Agent、Plan-and-Execute | 3 周 |
| ② | RAG + 检索优化 | 混合检索、RRF、Embedding | Semantic Chunking、Rerank | 3 周 |
| ③ | 多 Agent 协作 + 协议 | Supervisor、A2A、MCP | Human-in-the-loop、Memory | 3 周 |
| ④ | 模型层 + 微调 | LoRA、QLoRA、SFT | RLHF、DeepSeek R1 | 3 周 |
| ⑤ | 综合整合 + 项目 | 端到端 Agent 项目 | 生产级监控、流式输出 | 2 周 |

---

## 阶段详解

### 阶段①：核心框架与基础架构

**学什么**：
- LangGraph 基础：StateGraph、节点、edge、condition
- 工具调用模式：ToolNode、Function Calling
- 工作流编排：简单的 ReAct 流程

**为什么**：这是面试必考的基础，LangGraph 是字节等大厂的核心框架

**做到什么算过关**：
```python
# 能写出 LangGraph ReAct Agent
from langgraph.graph import StateGraph, MessagesState
from langgraph.prebuilt import ToolNode
# 实现：对话 → 思考 → 工具调用 → 回复
```

**花时间多久**：3 周
- 学习：5-7 天
- 代码：1-2 周
- 调试：1 周

---

### 阶段②：RAG + 检索优化

**学什么**：
- 文本分割策略：Character、Recursive、Semantic
- Embedding 模型：BGE、GTE、text-embedding-3
- 混合检索：Concat + Rerank + RRF
- 向量存储：FAISS、Milvus、Chroma

**为什么**：RAG 是 2026 年招聘的核心考点，字节面经专门问过流程

**做到什么算过关**：
- 能实现混合检索
- 能调用 BGE-M3 做中文 Embedding
- 能评估召回率（MRR@K）

**加分内容**：
- 语义分割（Semantic Chunking）
- RRF 代码实现
- 动态 Chunk 大小

---

### 阶段③：多 Agent 协作 + 协议

**学什么**：
- LangGraph Multi-Agent 模式
- 主从架构：Master-Worker 任务分发
- A2A 协议基础（2026 新热点！）
- MCP 协议规范
- Supervisor 模式优缺点

**为什么**：字节面经明确问"MCP 是热点",A2A 是多 Agent 新标准

**做到什么算过关**：
- 能搭建多 Agent 协作系统
- 能理解 A2A 协议
- 能设计 Human-in-the-loop 流

**花时间多久**：3 周

---

### 阶段④：模型层 + 微调

**学什么**：
- LoRA 原理：低秩分解 BA
- QLoRA：8GB 显存微调 7B 模型
- SFT 流程：数据格式、训练循环
- 幻觉治理：Self-check、Fact-check

**为什么**：字节面试必考 LoRA、QLoRA 原理和小测试

**做到什么算过关**：
- 能解释 LoRA 数学原理
- 能从代码层面说明 QLoRA 优化点
- 理解 DeepSeek R1 推理能力

**加分内容**：
- RLHF 对比
- 量化技巧
- SFT 数据清洗

---

### 阶段⑤：综合整合 + 项目

**学什么**：
- 端到端 Agent 开发
- 持久化与内存机制
- 流式输出
- 系统监控与追踪

**为什么**：所有学习最终要落地到项目，面试必问项目细节

**做到什么算过关**：
- 一个功能完整的 LangGraph Agent
- 能讲清记忆机制
- 能演示 MCP 工具调用

---

## ⚠️ 常见坑

| 错误类型 | 表现 | 后果 |
|----------|------|------|
| 只会名词 | 能背 LoRA，但写不出代码 | 一眼被看穿 |
| 没有项目 | 简历空白 | 面试淘汰 |
| 框架混用 | 今天 LangChain 明天 AutoGen | 面试官怀疑能力 |
| 不关注协议 | 不知道 A2A/MCP 是什么 | 2026 年被拒 |
| 重理论轻实践 | 看书 70% 代码 30% | 上手慢 |

---

## 📦 项目建议

**推荐项目**：LangGraph Multi-Agent Assistant

**核心要求**：
1. **记忆机制**：实现长期记忆（向量 DB）+ 短期记忆（cache）
2. **工具调用**：支持 MCP 规范的工具服务器
3. **上下文管理**：长会话压缩、记忆回指
4. **Agent 协作**：至少 3 个 Agent 主从协作
5. **人工审核**：Human-in-the-loop 审核节点
6. **流式输出**：Thinking Process 流式展示

**为什么这个能撑住追问**：
- 可以讲清记忆分层（短期/长期）
- 可以演示 MCP 工具调用
- 可以讲上下文管理和压缩
- 可以展示多 Agent 协作逻辑

---

*根据 **hunt:3** lines 1-523 学习路线整理*
"""
print(f"File written: {len(content)} bytes")