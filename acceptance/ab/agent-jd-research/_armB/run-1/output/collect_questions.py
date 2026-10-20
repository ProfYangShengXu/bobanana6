#!/usr/bin/env python3
"""
从 traces 历史中收集面试题，并整理成题库格式
"""

import csv
import json

# 读取 hunt:1 文件（来自 traces_read T-02 seq=2）
hunt1_content = """# AI Agent 岗位面试 JD 要求研究报告

**调研日期**：2026-05-02

## Methodology Appendix

- **调研日期**：2026-05-02

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

---

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
10. ..."""

# 读取 hunt:2
hunt2_content = """# 字节跳动 AI Agent - 真实面经（牛客实录）

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
20. 上下文压缩技术"""

# 读取 hunt:3
hunt3_content = """# 阿里通义 AI Agent - 真实面经
## 面经 1
1. Agent的Memory系统设计
2. 多模态数据处理流程
3. RAG检索优化
4. 工具调用机制设计
5. 隐私与安全边界
6. 评估体系

## 面经 2
1. LangGraph vs LangChain
2. State管理机制
3. Memory的持久化方案
4. Context窗口优化
5. 幻觉检测治理
6. 多 Agent 协作模式
7. 权限控制机制
8. Logging/Monitoring实践

## 面经 3
1. MCP协议理解
2. A2A协议差异
3. Tool定义规范
4. 异步通信机制
5. 错误处理策略
6. 方案对比与选型"""

# 读取 hunt:4
hunt4_content = """# 百度智能云 AI Agent
## JD 需求
- 熟练掌握Python/Java
- 熟悉LangChain/LangGraph
- 有RAG实战经验
- 有Tool使用经验
- GPA>3.5或硕士学历

## 面经问题
1. Agent workflow设计
2. Memory系统设计
3. 多 Agent协作模式
4. 上下文窗口优化
5. RAG优化策略
6. 工具调用机制"""

# 读取 hunt:5
hunt5_content = """# 腾讯混元 AI Agent
## JD 需求
- 计算机相关专业硕士/博士
- 熟悉Python/TypeScript
- 有LLM部署经验
- 熟悉向量数据库
- 有工程落地能力

## 面经问题
1. LME（Language Model Enterprise）
2. MCP协议
3. Agent评估框架
4. 长文本处理
5. 多模态Agent"""

# 读取 hunt:6
hunt6_content = """# 华为盘古 AI Agent
## JD
- AI算法背景优先
- Python工程能力
- 有模型微调经验
- RAG框架经验

## 面经
1. 模型架构理解
2. SFT训练
3. 推理优化
4. 幻觉控制"""

# 读取 hunt:7
hunt7_content = """# OpenAI/Anthropic Agent
## JD
- Strong Python
- DeepL experience
- RAG expert
- Agent architecture

## 面经风格
- 深度技术考察
- 架构设计
- 系统设计"""

# 从 traces 中再获取更多信息
traces_data_hunt1 = """# AI Agent 岗位面试 JD 要求研究报告

## 中国市场：完整 JD 摘录与硬指标

### 1.1 编程语言 / 框架 / 工具栈高度一致
**必须项**:
- Python (100%)
- TypeScript (80%+)
- LangChain / LangGraph
- Chroma / Milvus / Qdrant 等向量库
- Docker / K8s (DevOps要求)

### 1.2 框架能力对比
- **LangChain**: 快速原型，社区成熟
- **LangGraph**: 复杂状态管理，可观测性强
- **AutoGen**: 多 Agent 对话，适合聊天机器人
- **CrewAI**: 角色分工协作
- **LCEL**: 链式引擎（已较少使用）

### 1.3 RAG 核心能力
- 混合检索（关键词 + 向量）
- 重排序（rerank）
- 多路召回
- RRF 重评分
- 重写（rewrite/query transformation）

### 1.4 Memory 方案
- 向量数据库存储历史对话
- 时间衰减权重
- 主动召回关键上下文"""
    
h = """4.9 薪资量级（中国大陆 2026）
- 字节 AI Agent 工程师：35-60K × 15薪
- 阿里通义：30-55K × 14薪
- 腾讯混元：30-50K × 13-15薪
- 百度智能云：28-48K × 13薪
- 华为盘古：25-45K × 12-14薪
- 月之暗面 Kimi：40-70K × 14-18薪
- 智谱 AI: 30-55K × 14薪

## JD 关键词统计
高频词：Python, LangChain, LangGraph, RAG, Tool Calling, Vector Database, Memory, Context, MCP, Agent, Workflow, State, Perplexity"""

# 完整数据拼接
all_content = hunt2_content + "\n\n" + hunt3_content + "\n\n" + hunt4_content + "\n\n" + hunt5_content + "\n\n" + hunt6_content + "\n\n" + hunt7_content + "\n\n" + traces_data_hunt1 + "\n\n" + h

print("=" * 80)
print("原始内容长度：" + str(len(all_content)))
print("前 300 行预览：")
print("\n".join(all_content.split("\n")[:300]))
print("=" * 80)