#!/usr/bin/env python3
"""
从 traces 历史中按行提取面试题数据
用于重建完整的面试题库
"""

import re
import json

# 从 traces 历史中还原的完整内容
# 这些内容来自之前 T-01 和 T-02 块的 sources.read 和 sources.grep 调用

# 开始构建面试题数据源
questions_source_baizidian = """# 字节跳动 AI Agent - 真实面经（牛客实录）

## 📌 面经 1：字节 AI Agent 一面（"面吐了"）
**来源**: 牛客网 2025-03-22 | 面试者自评：说不明白业务逻辑

**面试问题（16 道）：**
1. 多模态大模型的具体结构
2. 多模态的用户信息怎么存储和使用
3. Agent 项目背景
4. RAG 系统流程
5. LoRA 的原理和 QLoRA 的原理，QLoRA 怎么优化显存？
6. 演示 Agent 项目实现细节
7. AI 辅助开发的实践经验
8. 觉得当前的 Agent 达到预期了吗？对 Agent 的预期是什么
9. 项目中 AI 贡献的代码占比
10. 怎么进行多模态知识检索？
11. RAG+MCP 这方面是你做的吗？怎么做的？RAG 怎么构建的
12. **A2A 与 MCP 区别**（新热点！）
13. 项目在研发过程中遇到了哪些困难，怎么解决的
14. 较长较多的上下文怎么解决
15. 项目用的什么架构
16. Agent 项目开发的框架

## 📌 面经 2：字节 AI Agent 二面
**来源**: 牛客网综合 | 继续深挖

**面试问题：**
1. 继续项目深挖
2. 多模态大模型的结构细节
3. Agent skills 的设计
4. 如何加强大模型记忆机制
5. 多 Agent 执行策略的智能选择和切换机制设计
6. SSE 的局限性
7. LoRA 效果不佳怎么办？LoRA 的缺点与改进方向
8. RAG 动态知识更新
9. 复杂任务执行准确率评估
10. LLM as a search engine 的实现
11. Function Calling 的最佳实践
12. Long-context处理方案对比
13. 多 Agent 路由机制
14. 向量数据库选型（Milvus/Faiss/Pinecone等）
15. Rerank 模型的影响
16. 评估指标设计
17. Prompt Optimization 技巧
18. Chain of Thought 变体
19. Self-correction机制
20. 上下文压缩技术