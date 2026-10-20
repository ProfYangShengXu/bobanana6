#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""处理 hunt:3 学习路线图文档，提取关键信息生成 roadmap.md"""

import os
import json

# 从之前读取的 lines 中提取信息
# 从 sources_read 返回的内容中，我已经有完整的 lines 列表

# 从 output 中提取关键信息
stages_data = {
    "阶段一：基础夯实（8-10 周）": {
        "目标": "建立大模型理论基础，掌握基本工具链",
        "技术栈": "Python, 基础 LLM 架构 (Transformer), HuggingFace, LangChain",
        "优先级": "必",
        "备注": "这是地基，面试必考"
    },
    "阶段二：Agent 核心能力（8-10 周）": {
        "目标": "掌握 Agent 主流框架与核心能力",
        "技术栈": "LangChain/LangGraph, CrewAI, Function Calling, 工具调用、RAG",
        "优先级": "必",
        "备注": "Agent 框架是核心，必须动手写代码"
    },
    "阶段三：进阶深入（4-6 周）": {
        "目标": "掌握模型微调、对齐技术、推理优化等进阶技能",
        "技术栈": "SFT, LoRA, 数据工程，评测体系",
        "优先级": "必",
        "备注": "技术深度是加分项"
    },
    "阶段四：工程与实战（4-6 周）": {
        "目标": "完成端到端项目，准备面试",
        "技术栈": "Docker/K8s, 推理优化，部署，系统集成",
        "优先级": "必",
        "备注": "必须有一个能讲清的项目"
    }
}

# 写 roadmap.md
md_content = """# 🗺️ AI Agent 工程师学习路线图（2025-2026）

> 本路线图面向**国内互联网大厂跳槽**场景，系统梳理从零到拿到 Offer 的完整学习路径。
> 适用于有一定编程基础（Python 熟练）、想转型或深入 AI Agent 方向的工程师。

---

## 目标定位

| 维度 | 说明 |
|------|------|
| **目标岗位** | 大模型算法工程师 / AI Agent 工程师 / AI 应用开发工程师 / LLM 应用架构师 |
| **目标公司** | 字节跳动、阿里巴巴、腾讯、百度、小红书、美团、蚂蚁集团、华为、京东、快手、商汤、月之暗面、智谱 AI、MiniMax、百川智能 |
| **薪资区间** | 25-60K·14/15 薪（3-5 年经验） |

---

"""

for stage_name, data in stages_data.items():
    md_content += f"## {stage_name}\n\n"
    md_content += f"> **{data['目标']}**\n\n"
    md_content += f"**技术栈：**{data['技术栈']}**\n"
    md_content += f"**优先级：**{data['优先级']}**\n"
    md_content += f"**备注：**{data['备注']}**\n\n"
    md_content += "---\n\n"

# 写项目建议
md_content += """## 项目建议

做能撑住追问的 Agent 项目，要能讲清：

1. **记忆机制**：用 MinHash+LSH+HyDE 做检索增强的短期记忆，定期持久化到向量库做长期记忆
2. **工具调用**：设计工具注册入口、调用超时控制、错误重试、权限边界
3. **上下文管理**：展示 prompt 压缩策略（重要性排序 + 摘要）、窗口滑动、历史摘要

推荐技术选型：
- 框架：LangGraph（强控流比 LangChain 更适合复杂 Agent）
- 记忆：min-hash+LSH+向量库持久化
- 工具：pydantic 校验 + 超时控制 + 幂等 ID
- 上下文：按重要性排序 + 摘要压缩 + 窗口滑动

"""

md_content += """## 常见陷阱

1. **只会名词不会写代码**：Agent 是工程问题，会写才是核心
2. **项目泛泛不能讲清楚**：能讲清记忆机制、工具调用、上下文管理才能过面试
3. **忽视工程落地**：RAG 召回率 100% 但延迟 10 秒也是失败

"""

md_content += """## 学习产出清单

- [ ] 用 LangGraph 实现一个完整的多 Agent 系统
- [ ] 用 Dify 搭建一个 RAG 应用并部署
- [ ] 对比 LangGraph 和 CrewAI 的代码实现差异

---

## 持续学习

1. **动手 > 看书**：代码实现 > 理论学习的比例应该 6:4
2. **项目驱动**：所有学习最终要落地到项目
3. **持续更新**：关注最新论文和技术，每周至少读 2-3 篇
4. **面试是双向选择**：不要害怕被拒，每次面试都是学习机会
5. **技术深度 > 广度**：在核心方向（如 RAG 或 Agent）做到深入
6. **保持健康**：学习是持久战，注意休息和运动
"""

# 写入文件
os.makedirs("output", exist_ok=True)
with open("output/roadmap.md", "w", encoding="utf-8") as f:
    f.write(md_content)

print("✅ output/roadmap.md 已生成")
print(f"总行数：{len(md_content.split(chr(10)))}")