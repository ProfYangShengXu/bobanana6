import json

# Data extracted from sources -.jd-matrix
jd_matrix_data = """
| 公司 | 岗位方向 | 学历/经验门槛 | 硬性技术栈 | 加分项 | 薪资量级 | 来源 |
| --- | --- | --- | --- | --- | --- | --- |
| 字节跳动 | Agent 软件架构师 - 移动 OS | 本科+3-5 年 | Python、LangGraph/Grap  | 多 Agent 协同框架、A2A 协议 | 70-100K×15 薪 | 猎聘/hunt-jd:1:l42|
| 字节跳动 | AI Agent 工程师（应届） | 应届生 + SSP | Python + LangChain + RAG + DB | MCP、模型微调经验 | 33-36K×15 +5W 签字 +10W 股票≈55W+ | CSDN/hunt-jd:1:l90|
| 百度 | 大模型算法（卓越） | 应届 SSP | PyTorch、SFT、RLHF | 顶会论文、会议 | 40K+×16+10W 股票≈70W+ | hunt-jd:1:l91|
| 腾讯 | 混元大模型 AIGC 算法 | 应届 SP | DeepLearning、CV/NLP | 顶会一作 | 30-32K×15+3W 签字≈48-52W | hunt-jd:1:l93|
| 腾讯 | 混元 Agent | 本科+3 年 | LangChain/Gram、RAG、DB | 多 Agent 框架、MCP | 同上 | hunt-jd:1:l74|
| 美团 | 后端开发（AI Agent） | 本科+3 年 | Python、后端架构、RAG | 本地 LLM 部署 | 30K-35K×15+ 股票 | hunt-jd:1:l126|
| 美团 | 算法（AI） | 本科+3 年 | 推荐系统 +LLM | 薪资溢价 | 26-32K×15+ 股票可达 60W+ | hunt-jd:1:l133|
| OpenAI | Agent Engineer | 本科 +3-5 年 年 | Pyton / LangChain、RAG/GMC | MCP、Claude SDK | $180K-$260K+equity+bonus | hunt-jd:1:l177|
| Anthropic | Claude Code Team | 本科 +3 年 | Python / MCP SDK、Tools | Enterprise 部署 | ~200-400 万人民币 | hunt-jd:1:l306|
"""

# Writing jd-matrix.md