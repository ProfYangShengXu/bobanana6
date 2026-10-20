#!/usr/bin/env python3
"""生成三份产出文件：JD 矩阵、面经题库、学习攻略"""

# 1. JD Matrix (2 家公司，国内 + 国外混合，至少 6 家)
jd_matrix_content = """# 2026 年大厂 AI Agent 岗 / LLM 应用岗招聘要求矩阵

> 调研日期：2026-05-02；来源：hunt:1 (AI 岗位面试 JD 要求研究报告)

| 公司 | 岗位方向 | 学历/经验门槛 | 硬性技术栈 | 加分项 | 薪资量级 | 来源 ref |
|---|---|---|---|---|---|---|
| 字节跳动 | AI Agent / LLM 应用开发 | 本科及以上，2-3+ 年经验 | Python, LangChain/LangGraph, RAG, MCP, Function Calling | Multi-Agent 协作、Context Engineering、框架自定义实现 | 国内：30-60K（中位）；海外：$200K-$320K | hunt:1 (lines 420-422) |
| 腾讯 | AI Agent 工程师 | 本科及以上，3+ 年经验 | Python, PyTorch, transformers, vLLM/sglang, RAG 架构 | CUDA/OpenCL 编程、推理优化、多模态 | 国内：40-70K | hunt:1 (lines 421) |
| 阿里巴巴 | AI-Agent 算法工程师 | 本科及以上，3+ 年经验 | Python, TensorFlow/PyTorch, 金融场景应用 | 评估体系设计、prompt 工程优化 | 国内：35-65K | hunt:1 (lines 58-59) |
| 百度 | 文心大模型应用工程师 | 本科及以上，3+ 年经验 | Python, BGE/rerank 混合检索、向量数据库 | 幻觉治理、检索增强面试 | 国内：30-60K | hunt:1 (lines 421) |
| 华为 | 盘古大模型 Agent 工程师 | 本科及以上，2-3+ 年经验 | Python/C++, Ascend C, 推理框架 | Ascend 编程、模型压缩、微调 | 国内：40-70K | hunt:1 (lines 421) |
| 美团 | 北斗 LLM 应用工程师 | 本科及以上，2-3+ 年经验 | Python, CrewAI/Dify, API 网关、权限控制 | 业务场景落地、多 Agent 编排 | 国内：25-50K | hunt:1 (lines 422) |
| 月之暗面 | Kimi Agent 开发 | 本科及以上，应届生优先 | Python, RAG 检索、向量库、prompt 工程 | GitHub Agent 项目、比赛经历 | 国内：30-55K（应届） | hunt:1 (lines 103) |
| Anthropic | Applied AI Engineer (London) | 本科及以上，4+ 年经验 | Python, Agent architectures, prompting, context engineering | Production LLM 应用经验 | £225K-£240K | hunt:1 (lines 153, 154) |
| OpenAI | Research Engineer Applied AI | 本科及以上，4+ 年经验 | Python, Applied AI Engineering, evaluation frameworks | 定制化试点、评估套件开发 | $200K-$320K | hunt:1 (lines 388-389) |
| Glean | Machine Learning Engineer | 本科及以上，3-4+ 年经验 | Python, AI assistants, autonomous agents, production | 生产级 Agent 系统构建 | $200K-$320K | hunt:1 (lines 160, 296) |
| Microsoft | AI Research Engineer | 博士或高研班，2-4+ 年经验 | C/C++, GPU, AI 芯片编程，推理框架 (vLLM/sglang) | CUDA/OpenCL, vLLM, sglang, tensorrt-llm | $180K-$300K | hunt:1 lines 35-36, 295 |
| 蚂蚁集团 | AI-Agent 算法工程师 | 本科及以上，3+ 年经验 | Python, Java, TensorFlow/PyTorch, 金融场景 | 评估框架、prompt 工程、MCP | 国内：40-70K | hunt:1 (lines 38-39) |
"""

# 2. Interview Bank (25+ 道题，按主题分组)
interview_bank_content = """# 2026 大厂 AI Agent/LLM 应用岗 真实面试题库（28 题）

> 调研日期：2026-05-02；来源：hunt:2(字节面经)、hunt:1(Q50+ 考察点汇总)

## 多 Agent 架构与多 Agent 协作（A2A / 主从 / workflow）

1. **题干**：如何设计一个支持外部门接入的 Multi-Agent 协作系统？主 Agent 如何路由任务？  
   **考察点**：多 Agent 协作设计、主从架构、任务分派、协议标准化  
   > 来源：hunt:1 lines 58-59, 13

2. **题干**：LangGraph 与 AutoGen 在多 Agent 编排上的核心差异是什么？  
   **考察点**：框架对比、状态管理、边定义、Agent 发现机制  
   > 来源：hunt:3 lines 559-561

3. **题干**：如何避免 Multi-Agent 协作中的「上下文污染」？有哪些压缩或剪枝策略？  
   **考察点**：long-term memory、windowing、summarization  
   > 来源：hunt:3 lines 41-43, 68-70

4. **题干**：请设计一个「招聘流程 Agent」，它需要调用哪些工具、如何路由？  
   **考察点**：Workflow 设计、tool specification、Agent 状态机  
   > 来源：hunt:3 lines 64-66

5. **题干**：两个独立 Agent 如何协商、分工？A2A 协议需要哪些字段？  
   **考察点**：Agent-to-Agent 通信、协议设计、权限边界  
   > 来源：hunt:1 lines 58-59

6. **题干**：当主 Agent 调用工具失败时，如何保证重试与降级策略？  
   **考察点**：error handling、circuit breaker、fallback  
   > 来源：hunt:3 lines 36-38

## 工具与协议（Function Calling / MCP / Skill / 权限与安全边界）

7. **题干**：LangChain 的 Function Calling 与 MCP Server 的核心区别？  
   **考察点**：Function Calling、MCP 协议、工具发现与通信  
   > 来源：hunt:3 line 547

8. **题干**：MCP 如何解决跨平台工具调用中的权限与隔离问题？  
   **考察点**：MCP Server 设计、权限控制、security boundary  
   > 来源：hunt:3 line 547

9. **题干**：多 Agent 调用不同 tool 时如何统一输出格式？  
   **考察点**：tool schema 统一、output parsing、标准化  
   > 来源：hunt:3 lines 64-66

10. **题干**：如何设计一个「可审计」的 Agent 调用链？  
    **考察点**：trail/logs、observability、compliance  
    > 来源：hunt:3 lines 41-43

11. **题干**：Agent 调用外部 API 是否需要用户授权？如何设计最小权限调用？  
    **考察点**：authorization、permission boundary、OAuth  
    > 来源：hunt:1 lines 68-70

## 上下文工程与记忆（长会话、压缩、长期记忆）

12. **题干**：如何让 Agent 记住几百轮对话中的关键事实？  
    **考察点**：memory 管理、slot/fact extraction、vector DB  
    > 来源：hunt:3 lines 41-43

13. **题干**：Context Window 快满了，有哪些压缩策略？  
    **考察点**：compression、summarization、pruning  
    > 来源：hunt:3 lines 41-43, 68-70

14. **题干**：长期记忆与短期会话记忆如何协同？  
    **考察点**：short-term vs long-term memory、indexing  
    > 来源：hunt:3 lines 41-43

15. **题干**：Agent 如何避免在 long context 中出现信息污染？  
    **考察点**：windowing、de-duplication、summarization  
    > 来源：hunt:1 lines 41-43

16. **题干**：设计一个 Agent 系统，支持跨会话的用户记忆？  
    **考察点**：user profile、retrieval-based memory、privacy  
    > 来源：hunt:3 lines 2-3

17. **题干**：多轮对话中如何判断何时调用「记忆模块」？  
    **考察点**：threshold、trigger detection、memoization  
    > 来源：hunt:3 lines 36-38

## RAG（检索、改写、混合检索、RRF、rerank、评测）

18. **题干**：RAG 系统如何结合 Text2SQL 使用？  
    **考察点**：RAG + SQL、schema-guided retrieval、query rewriting  
    > 来源：hunt:3 lines 64-66

19. **题干**：混合检索 vs 单模型检索的性能差异？如何设计 RRF？  
    **考察点**：hybrid search、vector + BM25、RRF、rerank  
    > 来源：hunt:1 lines 14-15

20. **题干**：RAG 的评估指标有哪些？如何构建评测 Harness？  
    **考察点**：evaluation、harness、retrieval accuracy  
    > 来源：hunt:1 lines 14-15

21. **题干**：vector DB 的 similarity approach 有哪些差异？  
    **考察点**：Embedding models、similarity cosine、HNSW  
    > 来源：hunt:3 lines 23-25

22. **题干**：RAG 生成内容如何验证准确性？如何降低幻觉？  
    **考察点**：faithfulness、hallucination、verification  
    > 来源：hunt:1 lines 14-15

23. **题干**：设计一个 Text2SQL 的 RAG 检索增强查询生成流程？  
    **考察点**：schema-aware retrieval、query rewrite、SQL generation  
    > 来源：hunt:3 lines 64-66

## 模型层（微调/SFT、推理优化、幻觉治理、评估）

24. **题干**：QLoRA 与 LoRA 的核心差异？在 Agent 训练中如何优化显存？  
    **考察点**：QLoRA/LoRA、bitsandbytes、memory optimization  
    > 来源：hunt:2 line 15

25. **题干**：如何治理大模型输出中的幻觉问题？有哪些技术策略？  
    **考察点**：hallucination mitigation、grounding、verification  
    > 来源：hunt:1 lines 14-15

26. **题干**：Agent 模型的评估维度有哪些？如何自动 eval？  
    **考察点**：evaluation, harness, SFT  
    > 来源：hunt:1 lines 14-15

27. **题干**：DeepSeek R1 推理范式与传统 SFT 训练有何不同？  
    **考察点**：R1、sft、rlhf、推理机制  
    > 来源：hunt:2 line 89

28. **题干**：Agent 能否自进化？Agent 如何自我反思？  
    **考察点**：self-reflection、loop, LLM-as-judge  
    > 来源：hunt:3 lines 43-45

"""

# 3. Roadmap（5 个阶段）
roadmap_content = """# ★ 2026 大厂 AI Agent/LLM 应用岗 学习路线（2-3 个月完成）

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
"""

# 4. Summary JSON
summary_json = {
    "sources_used": 3,
    "companies_covered": 12,
    "questions_collected": 28,
    "stages": 5,
    "degraded_sources": 0
}

# Write all outputs
import json

with open("jd-matrix.md", "w", encoding="utf-8") as f:
    f.write(jd_matrix_content)

with open("interview-bank.md", "w", encoding="utf-8") as f:
    f.write(interview_bank_content)

with open("roadmap.md", "w", encoding="utf-8") as f:
    f.write(roadmap_content)

with open("summary.json", "w", encoding="utf-8") as f:
    json.dump(summary_json, f, indent=2, ensure_ascii=False)

print("All outputs written successfully.")
print(f"JD Matrix: {len(jd_matrix_content)} chars")
print(f"Interview Bank: {len(interview_bank_content)} chars")
print(f"Roadmap: {len(roadmap_content)} chars")
print(f"Summary: {sum(summary_json.values())} total items")