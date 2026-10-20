#!/usr/bin/env python3
"""
准备面试题库 - 从各来源收集题目并分类
"""

import json
from collections import defaultdict

# 从 traces 历史中整理出的所有面试题目
# 来源标记：
#   - hunt:1: JD 和框架调研
#   - hunt:2: 字节跳动面经
#   - hunt:3: 学习路线图
#   - hunt:4: 百度面经
#   - hunt:5: 腾讯面经
#   - hunt:6: 华为面经
#   - hunt:7: OpenAI/Anthropic 面经

all_questions = [
    # 来自字节面经 hunt:2
    {"id": 1, "question": "多模态大模型的具体结构", "来源": "hunt:2", "考察点": "LLM 架构理解"},
    {"id": 2, "question": "多模态的用户信息怎么存储和使用", "来源": "hunt:2", "考察点": "Memory 系统设计"},
    {"id": 3, "question": "Agent 项目背景", "来源": "hunt:2", "考察点": "项目理解"},
    {"id": 4, "question": "RAG 系统流程", "来源": "hunt:2", "考察点": "RAG 基础"},
    {"id": 5, "question": "LoRA 的原理和 QLoRA 的原理，QLoRA 怎么优化显存？", "来源": "hunt:2", "考察点": "模型微调技术"},
    {"id": 6, "question": "演示 Agent 项目实现细节", "来源": "hunt:2", "考察点": "工程落地能力"},
    {"id": 7, "question": "AI 辅助开发的实践经验", "来源": "hunt:2", "考察点": "AI Accelerate"},
    {"id": 8, "question": "觉得当前的 Agent 达到预期了吗？对 Agent 的预期是什么", "来源": "hunt:2", "考察点": "业务理解"},
    {"id": 9, "question": "项目中 AI 贡献的代码占比", "来源": "hunt:2", "考察点": "代码贡献评估"},
    {"id": 10, "question": "怎么进行多模态知识检索？", "来源": "hunt:2", "考察点": "多模态检索"},
    {"id": 11, "question": "RAG+MCP 这方面是你做的吗？怎么做的？RAG 怎么构建的", "来源": "hunt:2", "考察点": "MCP 协议 + RAG"},
    {"id": 12, "question": "A2A 与 MCP 的区别", "来源": "hunt:2", "考察点": "工具协议知识"},
    {"id": 13, "question": "项目在研发过程中遇到了哪些困难，怎么解决的", "来源": "hunt:2", "考察点": "问题解决能力"},
    {"id": 14, "question": "较长较多的上下文怎么解决", "来源": "hunt:2", "考察点": "上下文管理"},
    {"id": 15, "question": "项目用的什么架构", "来源": "hunt:2", "考察点": "架构选型"},
    {"id": 16, "question": "Agent 项目开发的框架", "来源": "hunt:2", "考察点": "Agent 框架"},
    
    # 字节二面更多问题
    {"id": 17, "question": "继续项目深挖", "来源": "hunt:2", "考察点": "项目深度"},
    {"id": 18, "question": "多模态大模型的结构细节", "来源": "hunt:2", "考察点": "多模态架构"},
    {"id": 19, "question": "Agent skills 的设计", "来源": "hunt:2", "考察点": "技能系统设计"},
    {"id": 20, "question": "如何加强大模型记忆机制", "来源": "hunt:2", "考察点": "Memory 增强"},
    {"id": 21, "question": "多 Agent 执行策略的智能选择和切换机制设计", "来源": "hunt:2", "考察点": "多 Agent 路由/调度"},
    {"id": 22, "question": "SSE 的局限性", "来源": "hunt:2", "考察点": "通信协议"},
    {"id": 23, "question": "LoRA 效果不佳怎么办？LoRA 的缺点与改进方向", "来源": "hunt:2", "考察点": "微调优化"},
    {"id": 24, "question": "RAG 动态知识更新", "来源": "hunt:2", "考察点": "RAG 更新机制"},
    {"id": 25, "question": "复杂任务执行准确率评估", "来源": "hunt:2", "考察点": "评估体系设计"},
    {"id": 26, "question": "LLM as a search engine 的实现", "来源": "hunt:2", "考察点": "搜索增强"},
    {"id": 27, "question": "Function Calling 的最佳实践", "来源": "hunt:2", "考察点": "工具调用"},
    {"id": 28, "question": "Long-context 处理方案对比", "来源": "hunt:2", "考察点": "长文本优化"},
    {"id": 29, "question": "多 Agent 路由机制", "来源": "hunt:2", "考察点": "Agent 路由"},
    {"id": 30, "question": "向量数据库选型（Milvus/Faiss/Pinecone 等）", "来源": "hunt:2", "考察点": "向量库选型"},
    {"id": 31, "question": "Rerank 模型的影响", "来源": "hunt:2", "考察点": "重排序机制"},
    {"id": 32, "question": "评估指标设计", "来源": "hunt:2", "考察点": "评测体系"},
    {"id": 33, "question": "Prompt Optimization 技巧", "来源": "hunt:2", "考察点": "Prompt Engineering"},
    {"id": 34, "question": "Chain of Thought 变体", "来源": "hunt:2", "考察点": "推理增强"},
    {"id": 35, "question": "Self-correction 机制", "来源": "hunt:2", "考察点": "自我修正"},
    {"id": 36, "question": "上下文压缩技术", "来源": "hunt:2", "考察点": "上下文优化"},
]

# 从 hunt:1 中提取 Agent 架构相关问题
agent_arch_questions = [
    {"id": 37, "question": "多 Agent 协作架构如何设计？主从关系？Workflow 实现？", "来源": "hunt:1", "考察点": "Agent 架构与多 Agent 协作"},
    {"id": 38, "question": "A2A 与 MCP 的区别与联系", "来源": "hunt:2", "考察点": "工具协议"},
    {"id": 39, "question": "Agent framework 选型：LangChain vs LangGraph vs AutoGen vs CrewAI", "来源": "hunt:1", "考察点": "框架对比"},
    {"id": 40, "question": "State 管理在 Agent 中的应用", "来源": "hunt:1", "考察点": "状态管理"},
    {"id": 41, "question": "Workflow/Graph 的可视化与可观测性", "来源": "hunt:1", "考察点": "系统可观测性"},
]

# 从 hunt:3 学习路线图中提炼
roadmap_questions = [
    {"id": 42, "question": "Transformer 架构关键概念（自注意力、残差、FFN 等）", "来源": "hunt:3", "考察点": "LLM 理论基础"},
    {"id": 43, "question": "LLM 训练流程：Pre-training/SFT/RLHF", "来源": "hunt:3", "考察点": "模型训练"},
    {"id": 44, "question": "微调策略对比：LoRA / QLoRA / Full Fine-tuning", "来源": "hunt:3", "考察点": "微调技术"},
    {"id": 45, "question": "HuggingFace 核心工具链使用经验", "来源": "hunt:3", "考察点": "工具链"},
    {"id": 46, "question": "分布式训练与模型量化（4-bit/8-bit 量化）", "来源": "hunt:3", "考察点": "推理优化"},
    {"id": 47, "question": "In-context Learning vs Few-shot vs Zero-shot 实践", "来源": "hunt:3", "考察点": "上下文学习"},
]

# 组合所有题目
all_questions.extend(agent_arch_questions)
all_questions.extend(roadmap_questions)

# 按考察点分类
by_topic = defaultdict(list)
for q in all_questions:
    by_topic[q["考察点"]].append(q)

# 输出整理后的题库数据
output_data = {
    "total_questions": len(all_questions),
    "topics": dict(by_topic),
    "questions": all_questions,
}

print(json.dumps(output_data, ensure_ascii=False, indent=2))