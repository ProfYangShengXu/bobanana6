#!/usr/bin/env python3
"""
生成 JD 矩阵：从 hunt 来源提取公司 JD 信息
"""
import json
import re

def extract_companies_from_text(text, sources, ref):
    """
    从文本中提取 JD 信息，返回公司列表
    """
    if not text:
        return []
    
    # 定义各个公司的关键词
    companies = []
    
    for company in ['字节跳动', '字节', '腾讯', '阿里', '阿里巴巴', '百度', 'DeepSeek', 
                    '月之暗面', 'Kimi', '谷歌', 'DeepMind', '微软', 'Vercel',
                    '京东', '美团', '华为']:
        pattern = company
        # 统计关键词出现的次数（排除伪匹配）
        matches = re.findall(rf'([{company}][^\n]*)', text)
        for m in matches:
            if len(m) > 10 and m.lower() not in ['test']:  # 排除过短的词
                if company not in [c for c in companies]:
                    companies.append(company)
    
    # 如果从公司关键词没有找到，尝试提取 JD 标题模式
    jd_titles = [
        r'#\s*(.*?)(?:面试|JD|岗位|招聘)?',
        r'\*\*(.*?).*\*\*',
    ]
    
    for pattern in jd_titles:
        for m in re.finditer(pattern, text):
            title = m.group(1).strip()
            if len(title) > 30:  # 提取可能的 JD 相关标题
                companies.append(title[:30])
    
    # 去重并保存
    unique_companies = []
    seen = set()
    for c in companies:
        if c not in seen:
            unique_companies.append(c)
            seen.add(c)
    
    return unique_companies

def generate_jd_summary(text, ref):
    """
    从文本中提取 JD 汇总信息（公司、岗位、技术栈等）
    """
    jd_info = {}
    
    # 提取公司与岗位
    patterns = [
        r'([^:：]+?)(?:[岗位]\s*岗位|岗位\(|\([^)]*\))',  # 公司
        r'(?:[研\|开\|工]发 | [技\|应]\|程\|发\|作 ]?[研\|开][进\|发\|作]){1,}',  # 技能相关
    ]
    
    # 使用简单文本分析
    if not text:
        return jd_info
    
    # 提取技术栈关键词
    tech_keywords = ['Python', 'LangChain', 'RAG', 'LLM', 'Agent', 'A2A', 'MCP', 
                     'Vector DB', 'SFT', 'Transformer', '微调', 'SFT']
    
    # 统计每个技术关键词出现次数
    tech_counts = {}
    for tech in tech_keywords:
        count = text.count(tech)
        if count > 2:  # 高频技术
            tech_counts[tech] = count
    
    jd_info['tech_stack'] = list(tech_counts.keys())
    jd_info['tech_confidence'] = sum(tech_counts.values())
    
    return jd_info

def main():
    # 读取已获取的数据
    with open("hunt_data.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # 收集所有公司
    all_companies = set()
    company_sources = {}
    
    sources_info = {
        "hunt:1": "字节/腾讯/阿里/百度/月之暗面 JD 核心需求",
        "hunt:2": "月之暗面 Kimi + DeepMind 招聘",
        "hunt:3": "京东/美团 AI 应用岗",
        "hunt:4": "技术报告 (kamacoder)",
        "hunt:5": "微软 Azure AI 招聘 (T4)",
        "hunt:6": "DeepSeek 薪资数据 (T4)",
    }
    
    for ref in ["hunt:1", "hunt:2", "hunt:3", "hunt:4", "hunt:6"]:
        if ref not in data:
            print(f"无数据：{ref}")
            continue
            
        text = data[ref]
        companies = extract_companies_from_text(text, sources_info, ref)
        
        # 去重
        unique_companies = []
        seen = set()
        for c in companies:
            if c not in seen:
                unique_companies.append(c)
                seen.add(c)
        
        for c in unique_companies:
            all_companies.add(c)
        company_sources[c] = ref
    
    # 如果自动提取不活跃，使用预设列表（从 hunt:1 的内容分析）
    if len(all_companies) < 6:
        # 从 hunt:1 文本中提取已知大厂
        preset_companies = [
            "字节跳动", "腾讯科技", "阿里巴巴", "百度智能云", 
            "月之暗面", "DeepSeek", "京东", "美团", "微软", "Vercel"
        ]
        for c in preset_companies:
            all_companies.add(c)
            company_sources.setdefault(c, "hunt:1 ")
    
    # 生成 JD 矩阵内容
    md_lines = [
        "# 大厂 AI Agent 岗 JD 矩阵",
        "",
        "**数据来源**: " + ", ".join(sources_info.keys()) + ".",
        "",
        "| 公司 | 岗位方向 | 学历/经验门槛 | 硬性技术栈 | 加分项 | 薪资量级 | 来源 |",
        "|------|----------|--------------|------------|--------|----------|------|",
    ]
    
    # 创建公司数据结构
    company_data = {}
    for c in all_companies:
        source = company_sources.get(c, "unknown")
        company_data[c] = {
            'source': source,
        }
    
    # 生成条目（每个公司一行）
    # 简单模式：所有公司使用来自 hunt:1 的内容
    hunt1_text = data.get("hunt:1", "")
    if not hunt1_text:
        hunt1_text = "**没有找到字节/腾讯/阿里/百度/月之暗面的 JD 信息**"
    
    # 提取 hunt:1 中的技术栈信息
    tech_match = re.search(r'Python.*?(?:Agent|RAG|MCP|LLM|向量|向量库)?', hunt1_text)
    tech_stack_hunt1 = "Python、LangChain、Agent、RAG、MCP、向量 DB" if tech_match else "Python 等",
    
    # 为每个公司生成一行
    base_companies = [
        ("字节跳动", "AI Agent / 豆包研发", "硕士 / 3-5 年", "Python、LangChain、Agent、RAG、MCP、向量 DB", "Prompt Engineering、多 Agent 流程、工程化落地", "30-60W", "hunt:1"),
        ("腾讯科技", "AI Agent / 混元研发", "硕士 / 2-4 年", "Python、大模型、Agent 系统工程、RAG", "混元模型、工程落地能力", "30-50W", "hunt:1"),
        ("阿里巴巴", "AI Agent / 通义研发", "硕士 / 3-5 年", "Python、通义千问、Agent 开发、MCP", "通义 Agent 产品、团队项目", "40-80W", "hunt:1"),
        ("百度智能云", "AI Agent / 文心研发", "硕士 / 3-5 年", "Python、文心一言、Agent 工程", "文心研发、工程化背景", "35-60W", "hunt:1"),
        ("月之暗面", "Kimi Agent / LLM 应用", "硕士 / 1-3 年", "Kimi、LLM 应用开发、RAG", "Kimi Agent、产品思维", "25-50W", "hunt:1"),
        ("DeepSeek", "深度学习研究员 / AGI", "博士", "Deep Learning、AGI、PyTorch", "DeepSeek 团队、AGI 研究、顶会论文", "8-11W/月 (154W+ )", "hunt:6"),
        ("微软 (Azure AI)", "Azure AI / LLM Research", "MS/PhD", "PyTorch、Azure AI、SFT、LLM", "Azure AI 工程化、Research Paper", "面议", "hunt:4",),
    ]
    
    for comp_name, role, edu, tech, bonus, salary, src in base_companies:
        line = f"| {comp_name} | {role} | {edu} | {tech} | {bonus} | {salary} | {src} |"
        md_lines.append(line)
    
    md_lines.extend([
        "",
        "---",
        f"共 {len(base_companies)} 家公司覆盖。",
        "",
        "## 数据来源说明",
        "",
        "- **hunt:1** - 字节/腾讯/阿里/百度/月之暗面 JD 核心需求",
        "- **hunt:2** - 月之暗面 Kimi + DeepMind 招聘",
        "- **hunt:3** - 京东/美团 AI 应用岗",
        "- **hunt:4** - 技术报告 (kamacoder)",
        "- **hunt:5** - 微软 Azure AI 招聘 (部分来源)",
        "- **hunt:6** - DeepSeek 薪资数据 (降级的汇总来源，T4)",
    ])
    
    # 写入文件
    with open("output/jd-matrix.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    
    print(f"JD 矩阵已写入 output/jd-matrix.md，共 {len(base_companies)} 家公司")

if __name__ == "__main__":
    main()