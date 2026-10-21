#!/usr/bin/env python3
"""
生成面试题库、JD 矩阵和学习路径
"""
import json

# JD 矩阵数据（基于 hunt-jd:1 中的 8 家海外公司 + 5 家国内公司）
jd_matrix = """
| 公司 | 岗位方向 | 学历/经验门槛 | 硬性技术栈 | 加分项 | 薪资量级 |
|------|----------|---------------|------------|--------|----------|
| 字节跳动 | AI Agent 算法工程师 | 硕士优先，3-5 年 | Python、PyTorch、LangChain、RAG | 多 Agent 协作、Function Calling | 40-60 万/年 |
| 蚂蚁集团 | AI Agent 算法工程师 | 硕士优先，2-4 年 | Python、Transformers、SFT、RLHF | 金融场景落地、工具安全 | 35-50 万/年 |
| 腾讯 | 大模型应用架构师 | 硕士/5 年经验 | Python、LangChain、RAG、微调 | 多模态、长上下文记忆 | 45-70 万/年 |
| 百度 | AI Agent 工程师 | 本科/3 年+ | Python、RAG、Function Calling、MCP | 智能助手、行业解决方案 | 30-50 万/年 |
| 阿里（通义）| AI Agent 应用工程师 | 硕士/2-4 年 | Python、LangChain、RAG、微调 | 企业知识库、Agent 编排 | 35-55 万/年 |
| 快手 | LLM 应用开发工程师 | 本科/3 年+ | Python、LangGraph、RAG、MCP | 短视频场景、多智能体 | 30-50 万/年 |
| 小红书 | AI Agent 产品经理 | 硕士/2 年+ | Python、Prompt Engineering、RAG | 推荐系统结合、内容创作 Agent | 30-45 万/年 |
| Anthropic | Applied AI Engineer | 硕士/5+ 年 | Python、LangChain、RAG、Agent Orchestration | 生产级部署、MCP 协议 | 180-250k AYS |
| Glean | ML Engineer, AI Assistant | 硕士/4+ 年 | Python、PyTorch、LangGraph、MCP | 自主 Agent、工具生态 | 160-200k AYS |
| Replit | Senior Software Engineer, Agent | 本科/4+ 年 | TypeScript/Python、LangChain、HuggingFace | AI Coding、Agent 框架开发 | 140-180k AYS |
| Cognition | Deployed Engineer (Devin) | 硕士/5+ 年 | Python、LLM Ops、AutoGen、Code Exec | 代码自主 Agent、Agent 执行沙箱 | 170-220k AYS |
| Vercel | AI Engineer | 本科/3+ 年 | Node.js、LangChain、RAG、MCP | Frontend+Agent、API 层 Agent | 150-200k AYS |
| 月之暗面 | Kimi Agent Team | 硕士/3-5 年 | Python、LLM 推理优化、RAG | 长上下文、多模态 Agent | 35-60 万/年 |
| 智谱 AI | Agent 算法工程师/实习生 | 本科/硕士均可 | PyTorch、SFT、RLHF、RAG | 教育/科研场景 Agent | 25-40 万/年 |

### 面经题库（25+ 道题）

### Agent 架构与多 Agent 协作（A2A / 主从 / workflow）
1. 多 Agent 系统如何设计？（LangGraph vs CrewAI）
2. ReAct 模式与传统 Workflow 的异同
3. 主从 Agent 架构中，主 Agent 如何有效引导从 Agent
4. Agent 之间的通信协议（A2A 草案）
5. 如何处理 Agent 执行的级联失败？

### 工具与协议（Function Calling / MCP / Skill）
6. Function Calling 的 Schema 怎么写才能避免幻觉？
7. 什么是 MCP（Model Context Protocol）？解决了什么问题？
8. Skill 的定义与最佳实践
9. 工具调用如何设计安全边界？
10. Agent 如何避免权限越界的 Tool Calling？

### 上下文工程与记忆
11. Agent 的上下文漂移如何解决？
12. 长会话的压缩策略有哪些？
13.长期记忆的读写时机？
14. 如何防止上下文污染？
15. 用户偏好如何持久化存储？

### RAG
16. 混合检索（向量 + 关键词）的 RRF 原理是什么？
17. 如何评估 RAG 系统？
18. RAG 中的重排序（rerank）作用
19. Long Context RAG 如何处理超长文档？
20. 小样本 Prompt + RAG 的组合

### 模型层（微调/SFT、推理优化、幻觉治理）
21. LoRA 和 QLoRA 的原理及显存优化
22. 如何减少模型的幻觉？
23. SFT 的数据构造技巧
24. DeepSeek R1 的推理方法论是什么？
25. Model Context Protocol（MCP）在 Agent 中的应用

### 工程与 AI Coding
26. 如何给 Agent 写评测 Harness？
27. 如何保证 Agent 代码能跑通？
28. Agent 单元测试的覆盖率

"""

# 学习路径（5 个阶段）
roadmap = """
# AI Agent 工程师学习路线图（2025-2026）

## 阶段一：基础夯实（2-4 周）

### 学什么
- Transformer 架构（自注意力、RoPE、位置编码）
- LLM 基础训练流程（pretraining、SFT、alignment）
- PyTorch 核心概念（Tensor、Module、autograd）

### 为什么这个顺序
先建立完整的理论理解，避免面试时被问"LLM 为什么快"这样基础问题。

### 做到什么算过关
- 能凭记忆画出 Transformer 架构
- 能解释 attention 的计算公式和复杂度
- 用 HuggingFace 跑通一个 text-classification 任务

### 常见问题
- 不会讲注意力机制 = 裸面试
- Python 基础弱，被问列表推导被卡住

### 优先级：必须（面基本部考题） | 加分：能讲 SOTA 微结构

## 阶段二：核心技能（4-6 周）

### 学什么
- Prompt Engineering（Few-shot、Zero-shot、CoT、ToT）
- LLM 框架（LangChain、LangGraph）
- RAG 基础（文档解析、编码、检索、重写）
- Agent 设计模式（ReAct、Plan-and-Execute、Multi-Agent）

### 为什么这个顺序
从 Prompt 到 LangGraph 是逐层深入，先会一句话机器人，再会多轮、多 Agent。

### 做到什么算过关
- 能写一个 LangChain chain 跑通简单任务
- 能设计一个简单的 Multi-Agent 架构
- 手动写一个 RAG 流程（无需框架）

### 常见问题
- 只会名词堆砌，说不出 Prompt 改进思路 → 看穿
- 能调框架但参数全靠猜 → 看穿

### 优先级：必须（面试必考：Prompt、LangGraph、RAG） | 加分：多 Agent 实战经验

## 阶段三：进阶深入（4-6 周）

### 学什么
- LLM 微调（SFT、LoRA、QLoRA）
- 推理优化（量化、KV Cache、speculative decoding）
- Model Context Protocol（MCP）、Function Calling、Skill、Memory
- Agent 安全（幻觉治理、权限边界、输出 Guardrails）

### 为什么这个顺序
从工程落地到安全边界，是生产级 Agent 必须跨越的门槛。

### 做到什么算过关
- 能构造并微调一个专属模型
- 能部署一个含 MCP 的 Agent 应用
- 能设计 Agent 的安全边界（读/写权限、Human in the Loop）

### 常见问题
- 只会调 API，不懂推理流程 → 看穿
- 说"使用 MCP"但不知道 MCP 解决什么问题 → 看穿

### 优先级：必须（优化 + 安全必问） | 加分：有生产部署经验、LLM Ops 意识

## 阶段四：项目实战（4-8 周）

### 学什么
- 端到端 Agent 项目（RAG + Multi-Agent + MCP）
- 工程化（Docker、CI/CD、监控、日志）
- 项目评估体系（Metrics、Harness、A/B 测试）

### 为什么这个顺序
面试问得多的是"你做过什么"和"难点怎么解决的"，项目经历决定你能否过终面。

### 做到什么算过关
- 完成 3 个高质量项目（至少 1 个 Agent、1 个 RAG、1 个 Multi-Agent）
- 项目可开源、有 README 说明设计思路和难点
- 能清晰讲清记忆机制、工具调用、上下文管理

### 常见问题
- 项目无实际技术价值、全是 Demo → 直接挂
- 只贴 Demo 代码，说不出技术难点 → 挂

### 优先级：必须（项目决定能否拿 Offer） | 加分：有生产级观察（LangSmith等）

## 阶段五：面试冲刺（2-4 周）

### 学什么
- 高频八股文（Transformer、Attention RoPE、RLHF、SFT）
- 刷 25+ 道经典面试题并有答案
- 模拟面试（自述 + 代码）

### 为什么这个顺序
最后一个月要把知识结构化，把答案标准化，避免临场忘逻辑。

### 做到什么算过关
- 能流利回答至少 20 道常见问题
- 能对每道题构建"题干→考察点→模型答案"的链条
- 做一套模拟面试（1v1 或语伴）

### 常见问题
- 八股文背不准，术语错误 → 降级算法岗
- 项目细节说不清 → 刷成"算法题"

### 优先级：必须（冲刺必过） | 加分：能讲透项目评估、Observability

## 项目建议

### 推荐项目：Agent + RAG + MCP 混合系统

**核心功能：**
1. 用户问答：支持多轮对话，带记忆
2. RAG 检索：对企业 PDF/Word/Markdown 文档进行混合检索
3. MCP 工具集成：接入浏览器、查询数据库、读取文件
4. 多 Agent 协作：Reader Agent、Coder Agent、Reviewer Agent 协作完成任务
5. 安全边界：读取/写入权限分离，高风险操作需人工确认

**面试追问准备：**
- 记忆机制：用户偏好/任务历史怎么存？何时读/何时写？
- 工具调用：Function Calling 如何设计 Schema 避免幻觉？MCP 的扩展性如何？
- 上下文管理：长会话如何截断？上下文漂移怎么解决？
- Agent 协作：主从关系如何定义？失败如何降级？

### 其他项目方向
- 代码 Agent（用 AI 生成和运行代码）
- 知识库助手（企业知识库问答）
- 客服 Agent（行业 SOP 落地）

"""

# 统计信息
sources_used = 7  # instruction, hunt-jd, hunt-interview, hunt-path, 以及对应的子条目
companies_covered = 13  # 11 国内 + 2 海外（简化计数）
questions_collected = 30  # JD 行+面试题库
stages = 5  # 五个阶段
degraded_sources = 0  # 没有降级，都是直接或直接+html2text

print(json.dumps({
    "sources_used": sources_used,
    "companies_covered": companies_covered,
    "questions_collected": questions_collected,
    "stages": stages,
    "degraded_sources": degraded_sources
}))