# ★ 2026 大厂 AI Agent/LLM 应用岗 学习路线（2-3 个月完成）

> 调研日期：2026-05-02；来源：hunt:3 + hunt:1 + hunt:2

## 阶段划分与优先级

| 阶段 | 主题 | 优先级 | 预计时长 |
|---|---|---|---|
| 阶段 0 | 工程基础补强 | **必须** | 2 周 |
| 阶段 1 | Agent 框架实战 | **必须** | 3 周 |
| 阶段 2 | RAG + 上下文工程 | **必须** | 2 周 |
| 阶段 3 | 推理优化与微调 | **加分** | 2 周 |
| 阶段 4 | 项目实践与面试演练 | **必须** | 2 周 |

---

## 阶段 0：工程基础补强（2 周）

### 学什么
- Python 进阶：异步编程、类型提示、asyncio
- PyTorch 基础：tensor 操作、autoGrad、device
- 基础 ML 概念：embedding、vector search、attention

### 为什么这个顺序
- 后端工程师通常缺 LLM 推理调优底层知识
- Agent 框架底层基于 PyTorch，不理解 tensor 操作会卡壳
- 面试会考"多头注意力"等底层问题

### 做到什么算过关
- 能手写定位编码矩阵（position embeddings）
- 能在 GPU 上运行 PyTorch LLM 推理 demo（HuggingFace transformers）
- 能用 RAG library 完成一次完整的检索问答

### 常见坑
- 只背名词不会调参（batch size、max length 都不理解）
- 不看报错直接复制代码（面试会被问为什么）
- 忽略多进程/异步在 Agent 中的重要性（高并发 Agent 必须支持）

---

## 阶段 1：Agent 框架实战（3 周）

### 学什么
- LangGraph / LangChain 核心概念
- MCP 协议（Multimodal Communication Protocol）
- Function Calling、Tool Spec、Schema
- 多 Agent 协作范式（Master-Worker、Hierarchical）
- A2A（Agent-to-Agent）协议概览

### 为什么这个顺序
- 字节/腾讯面试明确要求会用 LangGraph（hunt:1 lines 58-59）
- MCP 是 2026 年快速普及的新协议（hunt:3 line 547）
- 多 Agent 是面试高频考点（hunt:2 line 86）

### 做到什么算过关
- 用 LangGraph 实现一个 multi-agent workflow
- 集成 MCP server，实现外部工具调用
- 能画出 multi-agent 的 state diagram 并口头讲解
- 能对比 LangGraph vs CrewAI 的优劣

### 常见坑
- 只会 LangChain 基础链，不会 LangGraph 状态机
- 不理解 MCP 与 Function Calling 的区别
- 不会设计 tool schema（json 格式写不对）
- 面试时被问"多 agent 如何分配任务"时卡壳

---

## 阶段 2：RAG + 上下文工程（2 周）

### 学什么
- 混合检索（Vector + BM25）+ rerank
- RRF（Reciprocal Rank Fusion）加权结合
- Context Engineering、Windowing、De-duplication
- Long-term memory design（向量 DB for memory）
- Hallucination 缓解与 Grounding 技术

### 为什么这个顺序
- RAG 是所有 Agent 应用的核心模块
- Context Engineering 是 2026 年下半年趋势（hunt:1 line 41）
- 上下文污染 문제는高频面试题（hunt:3 lines 41-43）
- 幻觉治理是面试必考（hunt:2 line 89）

### 做到什么算过关
- 设计一个混合 RAG 系统（vector + BM25 + rerank）
- 实现 context window compression（summarization 策略）
- 能用向量 DB 存储用户记忆并检索
- 理解什么是 ground truth 的 RAG 验证

### 常见坑
- 以为 RAG = 直接查向量库（没有 rerank/查询改写）
- 不理解 context window 压缩的必要条件
- 把 hallucination == 幻觉，不知 mitigation 策略
- 不会设计 evaluation harness（只调 model 不做评测）

---

## 阶段 3：推理优化与微调（2 周，加分项）

### 学什么
- SFT 微调、QLoRA/LoRA 显存优化
- vLLM / sglang 推理加速
- FlashAttention、PagedAttention
- 模型压缩（quantization）、剪枝
- DeepSeek R1 推理范式（若有余力）

### 为什么这个顺序
- 大厂算法岗非常看重推理部署能力
- QLoRA 是面试高频考点（hunt:2 line 15）
- DeepSeek R1 是字节面试必知（hunt:2 line 89）
- 推理优化是 AI Infra 工程师的核心技能

### 做到什么算过关
- 能用 QLoRA 在 8G 显存上微调基座模型
- 能配置 vLLM 的 key 参数（pre-fill / decode）
- 理解 FlashAttention/FlashInference 原理
- 能解释 Transformer 推理的 Bottleneck

### 常见坑
- 只懂 SFT，不懂 vLLM 调优（面试必死）
- 不理解 QLoRA 的 bit-by-bit 映射逻辑
- 不清楚 Transformer 的 Decode 流程
- 以为量化=提高速度（其实主要是显存占用）

---

## 阶段 4：项目实践与面试演练（2 周）

### 学什么
- 项目选题与实现（多 Agent + RAG + Text2SQL）
- 面试演练（手撕代码 + 架构设计）
- 作品集整理（GitHub + README）
- Answer 常见"坑题"（Agent 设计、Context 处理、RAG 评价）

### 为什么这个顺序
- 面试要求"现场演示"Agent 项目实现细节（hunt:2 line 85）
- 字节/腾讯要求现场展示（not just talk）
- 需要完整作品集撑住面试追问
- 2-3 个月时间集中冲刺

### 做到什么算过关
- 有一个完整可运行的 Agent 项目（GitHub + README）
- 项目包含：多 Agent 协作、MCP 集成、上下文记忆
- 能回答「项目如何记忆用户状态」「如何避免上下文污染」
- 面试前每人至少做 3 次模拟面试

### 常见坑
- 只有 demo，没有 production 设计（面试官会追问）
- 项目太简单（只有 chatbot，无 multi-agent）
- README 里没写 memory 机制、工具调用流程
- 面试被问"你的 Agent 如何记忆用户偏好"时说不出

---

## 项目建议（撑住面试追问）

> 推荐项目：「AI Recruitment Agent」+「Multi-Agent Text2SQL Assistant」

### 项目 A：AI Recruitment Agent
- 支持「岗位搜索 → 简历筛选 → 面试预约」
- 使用 multi-agent architecture：
  - 主 Agent 路由，Worker Agents 做查询/筛选/预约
  - 使用 MCP 调用 Job API、Calendar API
- 状态管理：用 LangGraph State 存储用户职位历史、简历
- 上下文记忆：维护向量记忆库存储用户偏好
- 工具调用：简历筛选 tool、面试查询 tool

### 项目 B：Multi-Agent Text2SQL Assistant
- 使用 LangGraph 实现 Text2SQL 多轮问答
- 混合检索：向量 + BM25 + rerank
- 多 Agent：Research Agent 查库、Validator 校验 SQL、Parser 解析语义
- 上下文工程：limit context window，使用 summarization

### 面试必答问题准备
- 「如何避免 Agent 调用错误导致流程卡死？」
- 「你的多 Agent 如何分工协商？」
- 「你的 Context 如何设计？如何避免污染？」
- 「你的 RAG 如何评估召回率？」
- 「DeepSeek R1 与传统 SFT 训练的核心差异是什么？」
