jd_data = [
    {
        "公司": "字节跳动",
        "岗位方向": "AI Agent 软件架构师",
        "学历/经验门槛": "本科 +3-5 年",
        "硬性技术栈": "Python/Go + LangChain + 向量库 + Docker/K8s",
        "加分项": "A2A、MCP、多 Agent 协作、DeepSeek R1 原理",
        "薪资量级": "20-40K·14-16 薪（北京/上海）",
        "来源": "hunt:1"
    },
    {
        "公司": "阿里巴巴 / 蚂蚁集团",
        "岗位方向": "通义千问 Agent / AI-Agent 算法工程师",
        "学历/经验门槛": "硕士优先，本科要有论文或竞赛",
        "硬性技术栈": "Python + PyTorch + RAG + 向量数据库",
        "加分项": "金融垂域经验、知识图谱、业界解决方案",
        "薪资量级": "35-60K·15-18 薪",
        "来源": "hunt:1"
    },
    {
        "公司": "腾讯",
        "岗位方向": "混元 Agent 工程师",
        "学历/经验门槛": "本科 +3 年",
        "硬性技术栈": "Python/Go + 大模型框架 + RAG",
        "加分项": "游戏场景应用、多 Agent 协作、工具调用",
        "薪资量级": "25-45K·14-16 薪",
        "来源": "hunt:1"
    },
    {
        "公司": "华为",
        "岗位方向": "盘古多模态算法 / 推理工程师",
        "学历/经验门槛": "硕博为主，顶会论文优先",
        "硬性技术栈": "C++/CUDA + vLLM/SGLang + 模型优化",
        "加分项": "Ascend 编程、NPU 优化、论文复现",
        "薪资量级": "40-80W（应届 SSP）-100-300W（社招）",
        "来源": "hunt:1"
    },
    {
        "公司": "美团 / 京东",
        "岗位方向": "LLM 应用 / 后端开发工程师",
        "学历/经验门槛": "本科即可，3 年经验主流",
        "硬性技术栈": "Python/Go + LangChain + 向量库 + 后端框架",
        "加分项": "电商/本地生活服务场景、系统集成经验",
        "薪资量级": "15-35K·13/14 薪",
        "来源": "hunt:1"
    },
    {
        "公司": "月之暗面",
        "岗位方向": "Kimi Agent 研发工程师",
        "学历/经验门槛": "在读硕士优先，本科需顶会 / 竞赛 / 项目",
        "硬性技术栈": "Python + 大模型微调（LoRA/QLoRA）+ RAG + Agent 框架",
        "加分项": "AGI 项目经验、强化学习、评测体系建设",
        "薪资量级": "50-80K·15 薪（校招）",
        "来源": "hunt:1"
    },
    {
        "公司": "海外 AI 公司（Anthropic）",
        "岗位方向": "Forward Deployed Engineer, Applied AI",
        "学历/经验门槛": "5+ YOE",
        "硬性技术栈": "Python/TypeScript + MCP + Agent 框架 + 系统架构",
        "加分项": "客户侧部署经验、MCP 规范、AI 原生产品",
        "薪资量级": "$200,000 – $300,000 base",
        "来源": "hunt:1"
    },
    {
        "公司": "海外 AI 公司（Glean）",
        "岗位方向": "MLE / Agent Platform Engineer",
        "学历/经验门槛": "5+ YOE",
        "硬性技术栈": "Python + Rust + LangGraph + vLLM + 评估系统",
        "加分项": "Agent 评估观测、推理优化、平台可扩展性",
        "薪资量级": "$180K-$300K base",
        "来源": "hunt:1"
    },
]

header = "| 公司 | 岗位方向 | 学历/经验门槛 | 硬性技术栈 | 加分项 | 薪资量级 | 来源 |"
separator = "|---|---|---|---|---|---|---|"

with open("output/jd-matrix.md", "w", encoding="utf-8") as f:
    f.write(header + "\n")
    f.write(separator + "\n")
    for row in jd_data:
        first = True
        for key in ["公司", "岗位方向", "学历/经验门槛", "硬性技术栈", "加分项", "薪资量级", "来源"]:
            value = row[key]
            if first:
                line = "| " + str(value)
                first = False
            else:
                line += " | " + str(value) + " |"
        f.write(line + "\n")

print("JD Matrix generated successfully!")