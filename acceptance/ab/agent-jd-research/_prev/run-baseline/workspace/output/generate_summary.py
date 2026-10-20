import json

# 三个来源的关键引用点
data = {
    "ref": "hunt:4:hunt:5:hunt:6",
    "columns": ["source", "line_num", "text"],
    "rows": [
        {"source": "hunt:4", "line_num": "203", "text": "MCP 解决的是工具对接的标准化问题。没有 MCP 的时候，每接一个新工具就要写一套对接代码，换个模型又得重写，就像以前每个手机品牌一个充电口。"},
        {"source": "hunt:4", "line_num": "206", "text": "MCP 是工具层的标准化，Skill 是能力层的复用，两者配合才能让 Agent 系统真正规模化。"},
        {"source": "hunt:4", "line_num": "213", "text": "面试里如果被追问什么场景必须微调，可以举这个例子：你希望模型输出某种特定格式和推理链路，而且这个模式要在各种输入下都稳定。"},
        {"source": "hunt:4", "line_num": "215", "text": "SFT(监督微调) 更适合快速迭代，因为流程简单——准备好问答对，直接训就行，几个小时到一天就能出结果。"},
        {"source": "hunt:4", "line_num": "216", "text": "RLHF 需要先训 Reward Model 再做 PPO，链路长、工程复杂、稳定性也差，一个迭代周期可能是 SFT 的好几倍。"},
        {"source": "hunt:5", "line_num": "1", "text": "LangChain Job Market 2026: 34% Share, $80k Salary Gap", "note": "market_share"},
        {"source": "hunt:5", "line_num": "184", "text": "AI Agent Frameworks 2026", "note": "frameworks"},
        {"source": "hunt:5", "line_num": "185", "text": "LangChain Job Market 2026", "note": "frameworks"},
        {"source": "hunt:5", "line_num": "186", "text": "CrewAI Job Market 2026", "note": "frameworks"},
        {"source": "hunt:6", "line_num": "1", "text": "北京/深圳内推 | 腾讯混元大模型团队招聘 AIGC 算法研究员/实习生", "note": "job_posting"},
        {"source": "hunt:6", "line_num": "18", "text": "北京/深圳内推 | 腾讯混元大模型团队招聘 AIGC 算法研究员/实习生", "note": "job_posting"},
        {"source": "hunt:6", "line_num": "145", "text": "北京/杭州内推 | 阿里通义实验室招聘大模型算法专家/研究型实习生", "note": "job_posting"},
        {"source": "hunt:6", "line_num": "181", "text": "Kubernetes", "note": "tech"},
        {"source": "hunt:6", "line_num": "182", "text": "Go", "note": "tech"},
    ],
    "files": {}
}

with open("output/summary_ref.json", "w") as f:
    json.dump(data, f, indent=2)