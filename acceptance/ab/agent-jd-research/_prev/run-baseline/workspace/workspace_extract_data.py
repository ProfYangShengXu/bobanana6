#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 hunt:* 系列来源中提取 JD 矩阵和面试题库数据
"""
import re
import json

def extract_jd_matrix():
    """从来源中提取 JD 矩阵数据"""
    jd_data = []
    
    # 来自 hunt:1 的 JD 数据摘录
    jd_sources = [
        ("字节跳动", "AI Agent / LLM 应用开发", "211/985 本科，2-3 年经验", "Python, LangChain, RAG, Tool Calling, 向量库", "Agent 项目主导经验", "35-60K*15"),
        ("阿里通义实验室", "LLM 应用工程师", "985/211 硕士优先，3-5 年经验", "PyTorch, Transformer, RAG, MCP", "生产级 Agent 部署经验", "50-80K*15"),
        ("腾讯混元", "AI Agent 开发工程师", "本科以上，2-4 年经验", "Python, LangChain, Embedding, 向量检索", "微服务部署经验", "40-65K*14"),
        ("百度", "智能体研发工程师", "211/985 本科，2-3 年经验", "Python, HuggingFace, RAG, Function Calling", "全生命周期 Agent 经验", "35-55K*14"),
        ("美团", "AI 应用工程师", "本科以上，2-3 年经验", "Python, LangGraph, RAG, Embedding", "工程化落地经验", "30-50K*14"),
        ("小红书", "AI 内容创作 Agent", "211 本科优先，1-3 年经验", "Python, LLM API, RAG, 多模态", "垂直领域应用经验", "40-60K*14"),
        ("华为", "AI 应用开发工程师", "硕士优先，3-5 年经验", "PyTorch, LangChain, 模型部署", "服务器端性能优化经验", "50-80K*14"),
        ("OpenAI", "Applied AI Engineer", "硕士优先，3-5 年经验", "Python, LangChain, MCP", "生产级 Agent 架构经验", "$250K+$ + equity"),
        ("Anthropic", "Applied AI Engineer", "硕士优先，4+ 年经验", "Python, Anthropic SDK", "安全对齐经验", "£225K-£240K + equity"),
        ("Hugging Face", "Model & AI Engineer", "硕士优先", "PyTorch, Transformers", "模型优化经验", "$200K+ + equity")
    ]
    
    return jd_data

def extract_interview_questions():
    """从来源中提取面试问题"""
    questions = []
    
    # 来自 hunt:4 的面试问题
    hunt4_questions = [
        ("多模态大模型的用户信息怎么存储和使用", "考察多模态系统的数据流和记忆设计", "hunt:4:线11"),
        ("Agent 项目背景深挖", "考察候选人技术实现深度", "hunt:4:线12"),
        ("RAG 系统流程详解", "考察索引、切片、检索全流程理解", "hunt:4:线12"),
        ("LoRA 的原理和 QLoRA", "考察微调知识的对比理解", "hunt:2:线15"),
        ("演示 Agent 项目实现细节", "考察动手能力和技术深度", "hunt:2:线16"),
        ("Embedding 的作用和检索策略", "考察向量搜索引擎原理", "hunt:4:线166"),
        ("混合检索 + RRF 融合", "考察检索工程优化能力", "hunt:4:线170"),
        ("HyDE 检索增强技术", "考察检索优化的高级理解", "hunt:4:线172"),
        ("Rerank 的精排作用", "考察检索精度优化", "hunt:4:线173"),
        ("Agent 的思维能力链 + Function Calling", "考察 Agent 核心架构理解", "hunt:4:线176"),
        ("LangGraph 的循环和 Human-in-the-loop", "考察 Agent 编排框架", "hunt:3:线 501"),
        ("Checkpointing 状态持久化", "考察长对话记忆设计", "hunt:3:线 503"),
        ("Subgraph 子图支持", "考察子 Agent 编排能力", "hunt:3:线 505"),
        ("ReAct Agent 实现", "考察基础 Agent 模式", "hunt:3:线 506"),
        ("Supervisor 多 Agent 合作", "考察多 Agent 协作", "hunt:3:线 508"),
        ("Plan-and-Execute 模式", "考察任务规划能力", "hunt:3:线 509"),
        ("模型量化（PTQ/GPTQ）", "考察推理优化经验", "hunt:3:线 749"),
        ("DeepSeek R1 的 GRPO", "考察最新对齐方法", "hunt:3:线 696"),
        ("RLAIF 与 KTO 对比", "考察对齐技术理解", "hunt:3:线 699"),
        ("LangChain vs LangGraph 区别", "考察框架选型", "hunt:3:线 506"),
        ("向量数据库 HNSW vs IVF", "考察索引优化", "hunt:3:线 1100"),
        ("Lost in the Middle 问题", "考察上下文工程", "hunt:3:线 1096"),
        ("GraphRAG vs 向量 RAG", "考察最新检索进展", "hunt:3:线 1098"),
        ("向量量化与模型量化", "考察推理优化", "hunt:3:线 745"),
        ("TensorRT-LLM", "考察生产部署", "hunt:3:线 737"),
        ("SGLang、Ollama", "考察推理引擎对比", "hunt:3:线 738"),
        ("Flash Attention", "考察加速技术", "hunt:3:线 996"),
        ("SFT 微调实践", "考察模型定制能力", "hunt:3:线 620"),
        ("QLoRA 显存优化", "考察资源优化", "hunt:2:线15"),
        ("Prompt Engineering 最佳实践", "考察 Prompt 优化技巧", "hunt:3:线 980"),
        ("上下文压缩管理", "考察长对话处理", "hunt:3:线 960"),
        ("缓存与上下文窗口优化", "考察工程实践", "hunt:3:线 950"),
        ("评测与评估体系", "考察效果评估方法", "hunt:3:线 940"),
        ("幻觉治理", "考察可靠性设计", "hunt:3:线 930"),
        ("检索改写与 Multi-Query", "考察 RAG 优化", "hunt:3:线 914"),
        ("BM25 与稠密检索", "考察混合检索", "hunt:3:线 911"),
        ("Embedding 模型微调", "考察 Embedding 优化", "hunt:3:线 1099"),
        ("查询改写与 HyDE", "考察检索增强", "hunt:3:线 914"),
        ("工具调用与 Function Calling", "考察 Agent 执行", "hunt:4:线 161"),
        ("MCP 协议对比", "考察协议知识", "hunt:4:线 162")
    ]
    
    return questions

def main():
    jds = extract_jd_matrix()
    questions = extract_interview_questions()
    
    # 输出 JD 矩阵
    jd_content = "| 公司 | 岗位方向 | 学历/经验门槛 | 硬性技术栈 | 加分项 | 薪资量级 |\n"
    jd_content += "|------|---------|--------------|------------|--------|---------|\n"
    
    for i, jd in enumerate(jds):
        jd_content += f"| {jd[0]} | {jd[1]} | {jd[2]} | {jd[3]} | {jd[4]} | {jd[5]} |\n"
    
    print(jd_content)
    
    # 统计 JD 条目
    print(f"JD 总数：{len(jds)}")

    # 输出面试问题
    print(f"\n面试问题总数：{len(questions)}")
    
    # 导出为文件
    with open("output/jd-matrix.md", "w", encoding="utf-8") as f:
        f.write(jd_content)
    
    with open("output/interview-bank.md", "w", encoding="utf-8") as f:
        f.write("# 大厂 AI Agent 岗 面试题库\n" +
                "# 共 {} 道题\n\n".format(len(questions)) +
                "| 主题 | 题干 | 考察点 | 来源 |\n")
        
        # 按主题分组
        topics = {
            "Agent 架构与多 Agent 协作": [],
            "工具与协议": [],
            "上下文工程与记忆": [],
            "RAG 检索与优化": [],
            "模型层与微调": [],
            "工程与 AI Coding": []
        }
        
        for q in questions:
            title = q[0]
            # 判断主题
            topic = "Agent 架构与多 Agent 协作"
            if "Agent" in title or "Multi-Agent" in title or "Supervisor" in title:
                topic = "Agent 架构与多 Agent 协作"
            elif "Function Calling" in title or "MCP" in title or "Tool" in title:
                topic = "工具与协议"
            elif "记忆" in title or "记忆" in title or "持久化" in title or "Checkpo" in title:
                topic = "上下文工程与记忆"
            elif "RAG" in title or "Embedding" in title or "检索" in title:
                topic = "RAG 检索与优化"
            elif "SFT" in title or "QLoRA" in title or "微调" in title:
                topic = "模型层与微调"
            else:
                topic = "Agent 架构与多 Agent 协作"
            
            topics[topic].append(q)
        
        # 写入文件
        for topic_name in ["Agent 架构与多 Agent 协作", "工具与协议", "上下文工程与记忆", "RAG 检索与优化", "模型层与微调", "工程与 AI Coding"]:
            if topics[topic_name]:
                f.write(f"\n## {topic_name}\n\n")
                for q in topics[topic_name]:
                    f.write(f"| {topic_name} | {q[0]} | {q[1]} | {q[2]} |\n")
    
    print(f"文件已生成：output/jd-matrix.md, output/interview-bank.md")

if __name__ == "__main__":
    main()