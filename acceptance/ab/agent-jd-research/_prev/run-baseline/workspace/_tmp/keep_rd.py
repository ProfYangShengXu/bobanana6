# 完整写入 roadmap.md
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