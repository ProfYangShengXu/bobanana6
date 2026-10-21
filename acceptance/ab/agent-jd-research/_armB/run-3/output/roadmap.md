# 🗺️ AI Agent 工程师学习路线图（2025-2026）

> 本路线图面向**国内互联网大厂跳槽**场景，系统梳理从零到拿到 Offer 的完整学习路径。  
> 适用于有一定编程基础（Python 熟练）、想转型或深入 AI Agent 方向的工程师。
>  
> **适用人群**：已有后端基础，希望在 2-3 个月内具备 AI Agent 岗位投递能力  
> **目标岗位**：AI Agent 工程师 / LLM 应用开发工程师 / AI 应用架构师  
> **目标公司**：字节跳动、阿里巴巴、腾讯、百度、小红书、美团、快手、华为、DeepSeek、Vercel、Replit 等

---

## 📊 薪资与岗位概览

| 公司 | 岗位 | 月薪（税前） | 年薪（14-18 薪） | 备注 |
|------|------|--------------|------------------|------|
| 字节跳动 | AI Agent 工程师 | 35K-60K | 49-108W | 北京为主 |
| 阿里巴巴 | AI 应用开发工程师 | 30K-55K | 42-99W | 杭州为主 |
| 腾讯 | AI Agent 工程师 | 30K-55K | 42-99W | 深圳为主 |
| 百度 | AI 应用开发 | 28K-52K | 39-94W | 北京为主 |
| 华为 | AI 应用工程师 | 28K-52K | 39-94W | 深圳/北京/南京 |
| 小红书 | AI 应用工程师 | 35K-60K | 49-108W | 上海为主 |
| 美团 | AI Agent 工程师 | 28K-52K | 39-94W | 北京为主 |
| 快手 | AI Agent 算法/工程 | 30K-55K | 42-99W | 北京为主 |
| DeepSeek | AI Agent Engineer | 30K-55K | 42-99W | 远程 |
| Vercel | AI Engineer | 40K-65K | 56-117W | 远程 + 美国 |
| 商汤 | AI 应用工程师 | 30K-55K | 42-99W | 北京/上海 |
| 月之暗面 | AI Agent Engineer | 35K-60K | 49-108W | 远程 |

> **来源**：hunt-path:1, hunt-jd:1 (字节、阿里、腾讯、百度 JD)  
> **薪资区间参考**：大部分公司基础档 28-40K，头部公司 35-55K

---

## 🎯 岗位能力模型

```
┌─────────────────────────────────────────────────────────────────┐
│                     AI Agent 工程师核心能力                      │
├─────────────────────────────────────────────────────────────────┤
│                        Layer 1: 核心框架                         │
│  - LangChain / LangGraph 熟练使用                                │
│  - Agent 与 Chain 设计与实现                                     │
│  - Function Calling 完整链路理解                                 │
├─────────────────────────────────────────────────────────────────┤
│                        Layer 2: 进阶能力                         │
│  - 多 Agent 协作（Workflow、主从架构）                            │
│  - RAG 检索增强（混合检索、重排序）                                │
│  - 上下文管理与压缩                                              │
│  - 记忆系统设计                                                  │
├─────────────────────────────────────────────────────────────────┤
│                        Layer 3: 工程与优化                        │
│  - Token 优化与成本控制                                         │
│  - 错误处理与 retry 机制                                         │
│  - 安全与权限控制                                               │
│  - 工程化（测试 Harness、监控）、评测（Evaluation）               │
├─────────────────────────────────────────────────────────────────┤
│                        Layer 4: 加分项                           │
│  - 自主 Agent 设计（Self-Planning、Self-Reflection）              │
│  - Tool 调用自动化（MCP 协议、API）                              │
│  - 微调（LoRA、QLoRA）优化与部署                                 │
│  - 多模态 Agent 能力                                            │
│  - GraphRAG、Agentic RAG 应用                                   │
│  - 模型推理优化（量化、缓存）                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📅 阶段 1：Agent 基础框架（Week 1）

**目标**：能够完整实现一个简单的对话式智能体（Agent），掌握核心工具与基础框架。

### 学什么
- LangChain / LangGraph 核心概念（Chain、Graph、Agent、ReAct）
- 基础对话 Agent 的实现（Tool 调用、工具使用、提示词工程）
- 记忆系统的初步了解（简单记忆存储：内存变量、ConversationBuffer）

### 技术栈
```python
# 核心工具
- Python 3.10+
- LangChain Python SDK
- LangGraph（异步图编排）
- Pydantic（工具 Schema 校验）
- OpenAI API / 其他 LLM 封装
- FastAPI / Flask（可选，用于服务部署）
```

### 为什么必须学 LangChain/LangGraph
**来源**：hunt-jd:1 (字节 JD), hunt-path:1 (能力模型)  
大厂面试中，**90% 的 Agent 岗位**会要求：
- 会用 LangGraph 构建智能体状态机
- 理解 LangChain 的 Schema 校验机制（Pydantic）
- 能实现自主 Agent（ReAct Pattern）

### 实操任务
实现一个简单的 **Customer Support Agent**：可以查询订单、退货、查余额等

```python
from langchain.agents import initialize_agent
from langchain.llms import OpenAI

# 必须会写
async def main():
    agent, config = initialize_agent(llms=llm, tools=tools, verbose=True)
    result = await agent.arun("帮我查订单 20251001 的物流信息")
    print(result)
```

### 常见考核点（来源：hunt-interview:6 Q2, Q3）
- 题目：Agent 和 Agent 的区别是什么？
- 题目：LLM 和 Agent 的区别是什么？工作模式是怎样的？
- 题目：如何设计多 Agent 协作？

---

## 📅 阶段 2：多 Agent 协作系统（Week 2-3）

**目标**：能够设计并实现一个**多 Agent 协作系统**，支持复杂任务拆解与分工。

### 学什么
- LangGraph Workflow 模式（状态图、节点映射）
- CrewAI（角色扮演 Agent 协同）
- 主从架构（Controller + Worker 分离）
- 任务拆解与自动规划（Planning）

### 技术栈
```python
# 进阶工具
- LangGraph 状态节点（StateNode）
- CrewAI Task + Role 配置
- LangGraph Workflow 模板
- Google VertexAI + Vertex Workflows（可选）
```

### 为什么必须学多 Agent 协作
**来源**：hunt-jd:1 (华为 JD), hunt-interview:4 Q8, Q9, Q12  
这是大厂**高频面试题**，且是**Agent 岗的核心能力**。

### 实操任务
实现一个 **多 Agent 研究 Agent**（Research Agent）：
- Agent A：文档搜索者（爬取 docs 并总结）
- Agent B：数据分析者（分析搜索结果）
- Agent C：报告生成者（整合信息）

### 常见考核点（来源：hunt-interview:6 Q6, Q7, Q8, Q12）
- 题目：**LangGraph vs CrewAI** 有什么区别？
- 题目：为什么需要多 Agent？架构设计原则是什么？（角色分离）
- 题目：多 Agent 任务编排时的状态管理（State）怎么设计？

---

## 📅 阶段 3：上下文工程与记忆系统（Week 4-5）

**目标**：能够设计长会话、多轮对话的上下文管理与记忆增强系统。

### 学什么
- 上下文窗口管理（窗口压缩、重要性采样）
- 短期记忆（Message Buffer、Window）
- 长期记忆（嵌入存储、向量检索）
- 记忆系统性能优化（Token 优化）
- 上下文污染问题与解决

### 技术栈
```python
# 记忆系统工具
- LangChain Memory（Conversation Summary, Conversation Buffer）
- Vector Store（FAISS, ChromaDB, Weaviate）
- Memory Window（Context Compression, Summary）
- Pinecone / Weaviate（嵌入存储）
```

### 为什么必须学上下文工程
**来源**：hunt-interview:1 Q5, Q9, Q10  
大厂面试中，这是**必考点**，尤其是 2-3 轮面经中的**二面**。

### 实操任务
实现一个 **长文档分析 Agent**：
- 功能：读取 1 万 + 字的法律合同，总结关键条款
- 要求：上下文压缩、分段总结

### 常见考核点（来源：hunt-interview:6 Q9, Q14）
- 题目：Agent 的记忆系统设计？（短期/长期记忆分离）
- 题目：Token 消耗优化？如何降低 Token 成本？
- 题目：How to handle context window 限制？

---

## 📅 阶段 4：工具调用与协议（Week 6-7）

**目标**：精通工具调用协议、Function Calling、MCP 协议与权限控制。

### 学什么
- Function Calling 底层机制（LLM Schema）
- MCP 协议（Model Context Protocol）
- Tool 调用权限管理（RBAC）
- Tool  Schema 设计与校验

### 技术栈
```python
# 工具协议工具
- LangChain Function Calling
- MCP Protocol
- Pydantic v2（Tool Schema）
- Vector Search Tools
- API Gateway (Kong/Nginx)
```

### 为什么必须学工具协议
**来源**：hunt-interview:6 Q4, Q5, Q6, Q7  
这是**最基础且高频**的面试考点，几乎每家公司都会考。

### 实操任务
实现一个 **Function Calling Tool**：
- 支持：search, calculate, weather_check
- 权限管理：不同用户不同工具权限

### 常见考核点（来源：hunt-interview:6 Q4-7）
- 题目：**Function Calling vs Prompt** 的区别？
- 题目：如何设计 MCP 协议？
- 题目：Tool 调用权限如何管理？
- 题目：如何设计 Schema 校验？

---

## 📅 阶段 5：高级优化与工程化（Week 8-9）

**目标**：掌握 Token 优化、幻觉治理、模型微调与评估体系。

### 学什么
- Token 优化（Prompt 压缩、Token 缓存）
- 幻觉治理（Fact Check、引用）
- 模型微调（LoRA, QLoRA）
- 评估系统（Evaluation Harness）
- 推理优化（量化、缓存）

### 技术栈
```python
# 优化与评估工具
- Tokenizer Analysis
- Prompt Optimization
- LoRA/QLoRA
- LangEval / LangSmith
- Vector Cache / Redis
- vLLM（推理加速）
```

### 为什么必须学优化与评估
**来源**：hunt-interview:6 Q13, Q15  
这是**加分项**，对于高级岗位（阿里/腾讯 Offer）是加分项。

### 实操任务
实现一个 **幻觉检测与修复 Agent**：
- 功能：自动生成答案后，验证答案准确性
- 工具：引用事实核查、外部搜索验证

### 常见考核点（来源：hunt-interview:6 Q13, Q15）
- 题目：如何解决 Agent 幻觉问题？
- 题目：Agent 工程优化？Token 消耗优化？
- 题目：如何评估 Agent 质量？

---

## ✅ 优先级说明

| 优先级 | 说明 | 推荐阶段 |
|--------|------|---------|
| ⭐⭐⭐ | **必须掌握**：没这些进不了面试 | 阶段 1 + 阶段 2 + 阶段 3 |
| ⭐⭐ | **面试必讲**：需要懂原理 | 阶段 1 + 阶段 4 |
| ⭐ | **加分项**：有大厂加分 | 阶段 5 |

---

## 📚 学习资源建议

### 官方文档与教程（T3, T4 来源）
| 资源 | 说明 | 来源 |
|------|------|------|
| [LangChain 官方文档](https://langchain-ai.github.io/langchain/) | Agent 框架官方文档 | hunt-path:4 |
| [LangGraph 官方教程](https://langchain-ai.github.io/langgraph/) | 图编排框架 | hunt-path:5 |
| [DeepLearning.AI 课程](https://www.deeplearning.ai/courses/ai-agents-in-langchain/) | 官方认证课程 | hunt-path:2 |
| [CrewAI GitHub](https://github.com/crewAIInc/crewAI) | 角色协作框架 | hunt-path:3 |
| [LangChain RAG 教程](https://python.langchain.com/docs/tutorials/rag/) | RAG 实战 | hunt-path:1 |
| [吴恩达 - Building RAG Agents](https://www.deeplearning.ai/short-courses/learning/rag/) | RAG 优化 | hunt-path:1 |

### 实践资源（必读）
- GitHub 相关仓库源码实验
- AI 工程师面经技术博客
- 每天至少能跑通一个完整小系统

---

## 🚫 常见误区与死亡问题

### ❌ 常见被问死亡问题（来源：hunt-interview:6）
1. 你为什么选择这个 Agent 框架而不是另一个？
2. 你的项目中最难的部分是什么，怎么解决的？
3. 如果让你重构，你会改什么？
4. 能现场演示你的 Agent 工具调用逻辑吗？

### ✅ 正确回答技巧
- 避免只给名词，要结合**项目实战**
- 强调**问题场景**与**解决思路**
- 展示**性能优化**与**成本控制**意识

---

## 🏆 面试准备清单（最后 1 个月）

### 项目实践
✅ 完成至少 1 个完整的 Agent 项目（如：Research Agent、Customer Support Agent）

### 代码能力
✅ 能手绘 Agent 架构图（Node/Edge/State/Flow）
✅ 能现场演示 Function Calling 调用流程

### 面试准备
✅ 准备 5-8 个面试问题（根据简历中的项目）
✅ 熟悉公司 JD 要求（字节/阿里/腾讯/百度）
✅ 准备自我介绍（2 分钟版本）

---

**祝你好运！Good luck!** 🚀

---

**📝 附录：核心概念速查表**

| 概念 | 说明 |
|------|------|
| **Chain** | LLM 调用 + Tool 调用的顺序 |
| **ReAct** | ReAct 模式：Reason-Act
| **LangGraph** | 图编排框架，支持循环、状态
| **CrewAI** | 角色协作框架
| **Function Calling** | 工具调用机制
| **RAG** | 检索增强生成
| **Vector Store** | 向量数据库
| **MCP** | Model Context Protocol |
| **Token** | 模型计费单位 |