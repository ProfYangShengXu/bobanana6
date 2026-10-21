"""Generate roadmap.md - 2-3 个月 AI Agent 工程师学习路径"""

roadmap_data = """# AI Agent 工程师 · 2-3 个月冲刺学习攻略

## 适用人群
- 有 Python + 后端基础
- 会写 CRUD、有项目经验
- 想在 2-3 个月内具备 AI Agent 投递能力

## 阶段规划

### 阶段 1：基础夯实（1-2 周内）
**学什么**：
- Python 高级特性（Type Hints、数据类、异步）
- LangChain / LangGraph 基础概念与简单项目
- 向量数据库（FAISS / Chroma / Milvus）

**为什么**：这是所有 Agent 项目的 "乐高块"，没有基础，后面的 Tool Calling、RAG 都建立不起来。

**过关标准**：
- 能用 LangChain 做一个"Chat + 知识库"应用
- 能理解 RAG 的核心流程（Embedding → 检索 → Rerank → 生成）

**优先级**：⭐⭐⭐（必须）

**常见坑**：
- 只做表面 Demo，不理解 Vector Store 的内部机制
- 分不清 LangChain 和 LangGraph 的适用场景

### 阶段 2：Agent 核心能力（3-4 周）
**学什么**：
- Function Calling / Tool Calling 原理
- MCP（Model Context Protocol）基础
- LangGraph State & 多 Agent 编排
- 简单的 prompt templating（System/Few-shot/CoT）

**为什么**：Agent 的核心是"自主调用工具"，Tool Calling + 多 Agent 是面试必考。

**过关标准**：
- 能写出一个"主 Agent + 子 Agent（搜索/计算器）"框架
- 理解 MCP 与 LangChain Skill 的区别

**优先级**：⭐⭐⭐⭐（必考）

**常见坑**：
- 只会调 Demo，不会自己 design 一个 custom function
- 分不清 Tool Calling 和 Function Calling 的语义边界

### 阶段 3：RAG + 幻觉治理（3-4 周）
**学什么**：
- 混合检索（关键字 + 向量）
- Rerank 模型
- Prompt 优化（CoT、Self-correction）
- 幻觉缓解（幻觉检测 + 拒绝生成）

**为什么**：RAG 是 Agent+ 知识库的标准配置，幻觉是 Interview 中最常问的软指标。

**过关标准**：
- 能设计一个 70%+准确率的企业 RAG 系统
- 能说出幻觉的 3 种主要原因 +1 条缓解策略

**优先级**：⭐⭐⭐⭐（必考）

**常见坑**：
- 只会调 FAISS，没说清楚为什么选它
- 没讲清楚 Rerank 和重排序的关系

### 阶段 4：项目实战（4-6 周）
**学什么**：
- Cursor / ChatX 辅助编码实践
- 本地 LLM 部署（Ollama / vLLM）
- Docker/K8s 容器化 Agent 服务
- 简单的评估框架（Ragas / TruLens）

**为什么**：真实 JD 越来越看重"可运行、可评估"的工程落地能力，不是 Demo。

**过关标准**：
- 有 1-2 个 GitHub 项目（LangChain+RAG+Tool Calling）
- 能讲清楚"项目为什么用这个工具"

**优先级**：⭐⭐⭐⭐⭐（加分项）

**常见坑**：
- GitHub 项目全是用模板改的，没理解设计原因
- 不会测试 Agent 的实际效果

### 阶段 5：模拟面试（2-4 周）
**学什么**：
- 按题库逐题准备答案（86 题精选）
- 练习"讲清楚项目中的记忆机制"
- 对 AI Coding 经验做诚实的自我介绍

**为什么**：面试是综合应用，不是知识 dump，是表达 + 逻辑。

**过关标准**：
- 能流畅答出 20 道核心题目的结构解法
- 能讲清楚自己项目的记忆系统是如何工作的

**优先级**：⭐⭐⭐⭐⭐（必须）

**常见坑**：
- 背答案 vs 理解机制
- 说 AI 写代码，但解释不清代码结构

---

## 项目建议（1-2 个月完工）

### 项目名称：ChatAgent R1
**技术栈**：LangGraph + LangChain + FAISS + Ollama + 数据库

**核心能力点**：
1. 记忆系统：用 DB 存储对话历史 + 向量压缩
2. Tool Calling：自定义 search/calc 工具
3. Context 管理：用摘要压缩长对话
4. 评估：简单 pass/fail 测试

**面试能讲清的**：
- "我为什么用 LangGraph 而不是 LangChain"
- "我的记忆系统如何防止上下文污染"
- "AI 贡献了多少代码，我如何验证"
"""