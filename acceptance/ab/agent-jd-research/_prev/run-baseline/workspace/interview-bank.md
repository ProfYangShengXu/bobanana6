# 2026 大厂 AI Agent/LLM 应用岗 真实面试题库（28 题）

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

