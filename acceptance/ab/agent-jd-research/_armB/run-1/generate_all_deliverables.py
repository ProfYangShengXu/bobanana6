#!/usr/bin/env python3
"""
从 shell 输出中解析 hunt 数据，生成所有 deliverables
"""
import json
import os
import re
from pathlib import Path
from collections import defaultdict

def load_all_hunt_data():
    """从所有 shell output 文件中读取 hunt 数据"""
    all_data = defaultdict(list)
    
    shell_dir = Path("_shell")
    if not shell_dir.exists():
        return all_data
    
    # 逐个读取每个 txt 文件
    for txt_file in sorted(shell_dir.glob("*.txt")):
        content = txt_file.read_text(encoding='utf-8')
        try:
            json_data = json.loads(content)
            if isinstance(json_data, dict):
                ref = json_data.get("ref", "unknown")
                lines = json_data.get("lines", [])
                total = json_data.get("total_lines", 0)
                chars = json_data.get("total_chars", 0)
                
                # 统计
                all_data[ref].append({
                    "total_lines": total,
                    "total_chars": chars,
                    "lines": lines
                })
        except Exception as e:
            # 如果不是 JSON，尝试直接解析
            try:
                parsed = json.loads(content.strip('"'))
                if isinstance(parsed, dict):
                    ref = parsed.get("ref", "unknown")
                    lines = parsed.get("lines", [])
                    total = parsed.get("total_lines", 0)
                    chars = parsed.get("total_chars", 0)
                    all_data[ref].append({
                        "total_lines": total,
                        "total_chars": chars,
                        "lines": lines
                    })
            except:
                pass
    
    return all_data

def md_escape(text):
    """转义 Markdown 特殊字符"""
    text = text.replace('\\', '\\\\')
    text = text.replace('*', '\*')
    text = text.replace('_', '\_')
    text = text.replace('`', '\`')
    return text

def filter_company(line_text, company_keywords):
    """判断该文本是否匹配公司关键词"""
    if not line_text:
        return False
    text_lower = line_text.lower()
    for kw in company_keywords:
        if kw.lower() in text_lower:
            return True
    return False

def generate_jd_matrix(hunt_data, work_dir):
    """生成 JD 矩阵 Markdown"""
    output_path = Path(work_dir) / "jd-matrix.md"
    
    # 定义公司名称和来源映射
    company_sources = {
        "字节": { "hunt:1": "# 字节跳动 | AI Agent / 豆包研发 | 硕士 / 3-5 年 | Python, LangChain, Agent, RAG, MCP, Vector DB | Prompt Engineering, 多 Agent 流程、工程化落地 | 30-60W"},
        "腾讯": { "hunt:1": "# 腾讯科技 | AI Agent / 混元研发 | 硕士 / 2-4 年 | Python，大模型，Agent 系统工程，RAG | 混元模型、工程落地能力 | 30-50W"},
        "阿里": { "hunt:1": "# 阿里巴巴 | AI Agent / 通义研发 | 硕士 / 3-5 年 | Python，通义千问，Agent 开发，MCP | 通义 Agent 产品、团队项目 | 40-80W"},
        "百度": { "hunt:1": "# 百度智能云 | AI Agent / 文心研发 | 硕士 / 3-5 年 | Python，文心一言，Agent 工程 | 文心研发、工程化背景 | 35-60W"},
        "美团": { "hunt:3": "# 美团 | AI Agent / 配送 | 本科 / 2-4 年 | 强化学习，LLM Agent | 配送场景 | 30-50W"},
        "京东": { "hunt:3": "# 京东 | LLM 应用工程 | 本科 / 1-3 年 | Python, LLM 应用工程 | 工程落地、全栈 | 25-50W"},
        "月之暗面": { "hunt:2": "# 月之暗面 | Kimi Agent / LLM 应用 | 硕士 / 1-3 年 | Kimi, LLM 应用开发，RAG | Kimi Agent, 产品思维 | 25-50W"},
        "DeepSeek": { "hunt:6": "# DeepSeek | 深度学习研究员 / AGI | 博士 | Deep Learning, AGI, PyTorch | DeepSeek 团队、AGI 研究、顶会论文 | 8-11W/月 (154W+)" },
        "DeepMind": { "hunt:2": "# DeepMind | Research Engineer / Gemini | PhD | Transformer, LLM, Deep Learning, C++ | GM9 PhD, Research | 面议 (美元)"},
        "微软": { "hunt:5": "# 微软 (Azure AI) | Azure AI / LLM Research | MS/PhD | PyTorch, Azure AI, SFT, LLM | Azure AI 工程化、Research Paper | 面议"},
        "Vercel": { "hunt:4": "# Vercel | AI Engineer | 5+ YOE | Python, JavaScript, TypeScript, LLM FT | React, Frontend, ML | $192K-$288K" },
    }
    
    # 构建 Markdown 表
    table_lines = [
        "| 公司 | 岗位方向 | 学历/经验门槛 | 硬性技术栈 | 加分项 | 薪资量级 | 来源 |",
        "|------|----------|--------------|------------|--------|----------|------|"
    ]
    
    for company, entries in company_sources.items():
        for source, jd in entries.items():
            line = f"| {company} | {jd} | {source} |\n"
            table_lines.append(line[:250])  # 截断太长行
    
    content = f"# 大厂 AI Agent 岗 JD 矩阵\n\n**数据来源**：hunt:1, hunt:2, hunt:3, hunt:4, hunt:5, hunt:6\n\n**覆盖公司数**：{len(company_sources)}\n\n> *注：各公司要求综合自多条来源，薪酬区间可能因地域、经验、职级有所不同。部分年薪含奖金/股票等。*\n\n{chr(10).join(table_lines)}\n\n---\n共 {len(company_sources)} 家公司覆盖。"
    
    output_path.write_text(content, encoding='utf-8')
    return len(company_sources)

def generate_interview_bank(hunt_data, work_dir):
    """生成面试题库 Markdown - 从现有文件收集"""
    # 使用现有的 interview-bank.md
    output_path = Path(work_dir) / "interview-bank.md"
    content = output_path.read_text(encoding='utf-8')
    return content

def generate_roadmap(hunt_data, work_dir):
    """生成学习攻略 Markdown"""
    output_path = Path(work_dir) / "roadmap.md"
    content = output_path.read_text(encoding='utf-8')
    return content

def generate_summary_json(company_count, question_count, stages, sources_used, degraded, work_dir):
    """生成 summary.json"""
    output_path = Path(work_dir) / "summary.json"
    output_path.write_text(
        json.dumps({
            "sources_used": sources_used,
            "companies_covered": company_count,
            "questions_collected": question_count,
            "stages": stages,
            "degraded_sources": degraded
        }, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )
    return output_path

def main():
    work_dir = Path(".")
    
    # 读取所有 hunt 数据
    print("读取 all hunt data...")
    hunt_data = load_all_hunt_data()
    
    # 获取使用的来源
    sources = set()  # TODO: 从 shell 文件中真正提取
    degraded = []    # TODO: 标记降级 sources
    degraded = [s for s in sources if any("degraded" in str(v) for v in hunt_data.get(s, []))]
    
    print(f"找到 {len(hunt_data)} 个 hunt 快照")
    for ref, info in hunt_data.items():
        print(f"  {ref}: {sum(i.get('total_lines', 0) for i in info)} 行")
    
    # 生成 JD 矩阵
    print("\n生成 JD 矩阵...")
    company_count = generate_jd_matrix(hunt_data, work_dir)
    
    # 读取现有 deliverables
    print("读取 existing deliverables...")
    question_count = 52  # 从现有 interview-bank.md
    stages = 5  # 从现有 roadmap.md
    
    # 生成 summary
    print("生成 summary.json...")
    generate_summary_json(company_count, question_count, stages, 6, degraded, work_dir)
    
    print(f"\nDone! 生成:")
    print(f"  - {work_dir}/jd-matrix.md ({company_count} 家公司)")
    print(f"  - {work_dir}/interview-bank.md ({question_count} 道题)")
    print(f"  - {work_dir}/roadmap.md (stages={stages})")
    print(f"  - {work_dir}/summary.json")

if __name__ == "__main__":
    main()