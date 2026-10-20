# 大厂 AI Agent 岗 JD 矩阵

## 数据来源总结

本文档根据 **hunt** 调研数据整理，覆盖 16+ 公司招聘要求对比。

> **数据来源说明**：
> - **hunt:1** lines 398-423：40+ 招聘渠道汇总（字节、Google、Replit、Vercel 等）
> - **hunt:2**：字节跳动牛客网真实面经
> - **hunt:3**：AI Agent 工程师学习路线图（1684 行）

---

## JD 概览（按公司分类）

| 公司 | 核心考察 | 技术重点 | 参考来源 |
|------|---------|----------|----------|
| 字节跳动 | Agent 项目演示、MCP 协议、A2A 多 Agent 通信 | LangGraph 工作流编排 | hunt:2（字节面经） |
| 阿里巴巴 | RAG 系统、多模态嵌入（BGE/GTE） | Qwen/通义千问集成 | hunt:3（目标公司） |
| 腾讯 | 多模态大模型存储机制 | 腾讯混元模型 | hunt:1 来源标记 |
| 百度 | 文心一言集成、RAG 优化 | 飞桨 Agent | hunt:3 目标公司 |
| 美团 | Agent 在招聘、外卖场景应用 | 自研场景 | hunt:1 C 级来源 |
| 京东 | 智能客服 Agent、供应链优化 | TGT/JD 自研 | hunt:1 C 级来源 |
| 小红书 | 内容生成 Agent、社区互动 | 低代码平台集成 | hunt:3 目标公司 |
| 蚂蚁集团 | 金融场景 Agent、风控应用 | 阿里云大模型 | hunt:3 目标公司 |
| 华为 | 盘古大模型集成、企业级 Agent | 盘古模型集成 | hunt:1 来源标记 |
| 快手 | 短视频领域 Agent、内容推荐优化 | 自研 | hunt:3 目标公司 |
| OpenAI/月之暗月 | GPT-4 集成、高级推理能力 | Copilot+LLM | hunt:1 来源 |
| 商汤 | 计算机视觉 Agent、多模态理解 | 自研视觉模型 | hunt:3 目标公司 |
| 智谱 AI | LLM 微调、RLHF、SFT | 模型研发 + 微调 | hunt:3 目标公司 |
| MiniMax | 对话 Agent、角色扮演 | 深度对话场景 | hunt:3 目标公司 |
| 百川智能 | 开源模型集成、定制化场景 | LangChain/AutoGen | hunt:3 目标公司 |

---

## 能力要求对比表

| 维度 | 字节跳动 | 阿里巴巴 | 腾讯 | 百度 | 美团 | 
|------|---------|---------|------|------|------|
| 核心重点 | Agent 演示、A2A | RAG/多模态 | AIGC 应用 | 文心集成 | 场景应用 |
| 必备框架 | LangGraph | LangChain/BACE | 自研 | 飞桨 | 自研 |
| 协议要求 | MCP/A2A | - | - | - | - |
| 模型方向 | 自研 + 通用 LLM | Qwen/通义 | 混元 | 文心 | 自研 |
| 薪资量级 | 高级 | Mid/高级 | Mid-Senior | Senior | Senior |

---

## 招聘渠道来源

hunt:1 包含 40+ 招聘渠道，主要包括：
1. **国内平台**：BOSS 直聘、拉勾、51Job、智联、牛客、猎聘、V2EX、知乎、CSDN
2. **大公司官网**：字节、阿里、腾讯、百度、京东、美团、快手、小红书、华为
3. **海外平台**：Greenhouse、Ashby、Glassdoor、Levels.fyi
4. **生态分析**：LangChain ecosystem analysis, HN"Who is Hiring?"

---

## 证据等级参考

hunt:1 第 420-422 行明确标记证据等级：
- **A 级（一手原文）**：Anthropic Greenhouse、腾讯混元、微软、Replit、Vercel
- **B 级（缓存摘要）**：BOSS 直聘、智联、阿里通义、百度文心、华为盘古
- **C 级（媒体报道）**：京东 TGT、美团北斗、字节招培
- **D 级（聚合统计）**：LangChain ecosystem analysis

---

*本文档由 **hunt** 数据驱动生成，最后更新于 2026-05-02*
