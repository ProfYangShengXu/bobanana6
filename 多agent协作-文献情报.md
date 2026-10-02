# 大规模多 Agent 协作 —— 文献与社区情报（v2 · 全量）

> 采集：2026-10-02 · 来源：arXiv 深挖 9 篇 + forage 技术博客库两轮
> 用途：bobanana6 的外部证据面 —— **哪些支持设计、哪些是反证、哪些要改**

---

## 0. 怎么读

```
① 实证（有数字、有对照）    → 可直接进设计理由
② 设计原则（作者提炼）      → 当判据用，不当证据
③ 社区观察（博客）          → 当线索用，必须回原文核
```

**§5 是要紧的部分**：四条反证打在本项目核心假设上。
**§6 是个战略判断**：有一块别人已经做完且做得更细。

---

## 1. 架构与缩放

### ① Towards a Science of Scaling Agent Systems — `arXiv:2512.08296`
**Google Research 等，2025-12 / 2026-04 v3**

**规模**：260 组配置 × 6 个 agentic benchmark × **5 种架构**
（Single / Independent / Centralized / Decentralized / Hybrid）× 3 个模型族。
**标准化了工具、prompt、算力**以隔离架构效应。

```
★ 相对单 agent 基线：+80.8%（可分解金融推理） ～ −70.0%（顺序规划）
★ 无中心化【验证】的架构传播错误更多
★ 独立多 agent（并行但不通信）错误放大 17.2×
★ 能力饱和：单 agent 基线一旦超过某水平，协调收益递减
★ 工具密集任务上多 agent 有明显开销
```
模型对 87% 留出配置预测出最优架构（R²≈0.373–0.413）。

### ② Scaling LLM-based Multi-Agent Collaboration — `arXiv:2406.07155`（ICLR 2025）
**OpenBMB / 清华，MACNet**

- DAG 组织协作网络；**支持 >1000 agents**
- ★ **协作缩放律：性能随 agent 数呈 logistic 增长**
- ★ **不规则拓扑优于规则拓扑**
- 协作涌现早于神经涌现

⚠️ 它的"协作"是**多轮互相反思**，不是本项目的"独立分工 + 判定"。**regime 不同，别直引数字。**

### ③ Agensh: Scaling Organizational Intelligence to 1,024 Agents — `arXiv:2609.26781`
**2026-09**

- ★ **无中心 orchestrator** 的自组织 harness（动机：中心化调度的协调容量是瓶颈）
- 三组件：共享工作区 + 消息接口 + 共享上下文
- 1→128 agents：19.31% → 28.78%（相对 +49%）
- **pandoc 1→1,024 agents：33.89% → 55.06%**
- 大规模组织**更早**达到同等通过率；自组织形态随规模涌现并标准化

### ④ Scaling LLM-Driven Multi-Agent Systems: Design Principles — `arXiv:2607.27942`

四条设计原则：**simplicity / elastic feedback / sequential workflows with optional loops / summary-based communication**

```
★ 准确率提升 + 成本约【线性】增长 —— 但【仅当底层 LLM 超过最低能力门槛】
★ 性能在【中间复杂度】达峰，之后因超时与评测限制下降
★ persistent consistency issues 是所有 scaling 层级的核心挑战
```

### ⑤ AdaptOrch — `arXiv:2602.16873`

- ★ 四种规范拓扑：**parallel / sequential / hierarchical / hybrid**
- 按任务依赖图动态选，O(|V|+|E|)
- ★ 拓扑感知编排比静态单拓扑 **+12–23%（同一底层模型）**
- 主张：**编排拓扑已主导系统级性能，超过模型能力**
- 附带"自适应综合协议"：**终止性保证** + 并行输出的**一致性打分**

---

## 2. 成本与角色 ★ 新增

### ⑥ Specialize Roles, Mix Deployments — `arXiv:2606.20629`（AgentCARD）

**这是与"N 该开多大"最对口的一篇。**

产出物：**AgentCARD** —— 角色感知 benchmark，四件套：
**角色分解评估 harness + 统一 API/自托管成本模型 + Pareto 前沿分析 + 基于 Shapley 值的角色瓶颈诊断**。

```
★ 异构团队【持续占据】成本-准确率前沿
★ 比【成本等价】的同构团队准确率高至多  +44%
★ 或用混合部署，以至多 1/12 的单任务成本 追平最强同构团队
★ 最佳角色分配是【领域相关】的：
    有的领域是 planner 瓶颈，有的是 executor 瓶颈
★ 可扩展到 verification 等额外角色
```

**对 bobanana6 的三个直接用处：**

```
① "异构团队占前沿" = 支持 §5.7「runtime 可混用、简单块配便宜模型」
② Shapley 诊断角色瓶颈 = 一个现成的"归因"方法论 ——
   我方的 attribution.py 在找【哪个角色】是瓶颈时，可以直接借这个口径
③ "最佳角色分配领域相关" = 又一条支持 L-1 语义层按领域起草的理由
```

### 补充：成本-准确率参照表

| 论文 | 说法 |
|---|---|
| 2606.20629 | 异构 vs 成本等价同构：**+44%** 准确率；或 **1/12** 成本 |
| 2607.27942 | 成本约**线性**增长 |
| 2512.08296 | 工具密集任务有**额外开销** |
| §9（前序） | 多 agent ≈ **4×** 单 agent ≈ 15× 普通对话 |

---

## 3. 并行分解 ★ 新增

### ⑦ APWA: A Distributed Architecture for Parallelizable Agentic Workflows — `arXiv:2605.15132`

- ★ 把 workflow 分解成 **non-interfering subproblems**，
  用**独立资源**处理且 **without cross-communication**
- 支持异构数据与并行处理模式
- **在先前系统完全失败的大任务上能扩展**

**⚠️ 这条要小心读**：APWA 的 "without cross-communication" 与 §1① 的
"独立并行不通信 = 错误放大 17.2×" **表面矛盾**。

**解释**：APWA 说的是**子问题之间**不通信（横向），
但它们**共享同一个 workflow 定义与判定**（纵向）。
**这与 bobanana6 的"星形耦合"是同一个形状** —— N 块各自对契约，不两两互调。

### ⑧ MegaAgent — ACL 2025 Findings
**NUS / SJTU / HKUST 等**

- 按**任务复杂度生成** agent；动态分解、并行执行、高效通信、系统级监控
- 数字：**800 秒内开发出一个 Go 程序**；政策模拟**扩展到 590 agents**
- 显著优于 MetaGPT（完成效率与可扩展性）
- ⚠️ 明确点出：**幻觉会传播并危及整个系统**，所以需要 robust 的可靠性机制

**它的两层结构值得抄：**

```
【分层任务管理】
  ① Boss Agent Level：任务分解 → 分给 admin agents
  ② Dynamic Hierarchical Group Formation：子任务超容量时 admin 递归招人组队
  ③ System-Level Coordination：并行执行 + 跨系统交互（function call）

【分层监控】（对应"无预设 SOP 也要保证正确"）
  ① Agent 级：每个 agent 用 checklist 自查，过关才前进
  ② Group 级：admin 监督自己组的 agent
  ③ System 级：Boss 审查【所有组的输出】保证准确与一致
```

**★ 与 bobanana6 的对照**：
它的"System 级审查所有组输出保证一致" ≈ 我方的**装配检查**；
它的"Agent 级 checklist 自查" ≈ 我方的 `check.local()`。
**但它是纯模型驱动（Boss 也是 LLM），我方是零模型判定内核** —— 我方更硬。

---

## 4. 社区侧（forage）

### ⑨ DeepSeek Harness 深读 ★★★ 本轮最有价值的一篇
`rowkey.cn/blog/2026/08/21/deepseek-harness-vs-agentscope-java/`

**读的是源码（DeepSeek Harness `141eb6f` / AgentScope Java `643905e`），不是 README。**

#### 设计要点（逐条都对得上本项目的某个缺口）

```
【事件日志才是真相】
  会话 = 【只追加的事件日志】；turn/start、step/start、消息、chunk、
  工具调用与结果全部进日志；模型上下文由 deriveMessages() 从日志【投影】出来。
  原则原话：★ "模型可见即已记录。"
  → 解决的问题：很多系统同时维护三份相似状态（给模型的上下文 / 给用户的
    聊天记录 / 用于恢复的快照），一旦漂移就出现最难查的 bug。
  → 对本项目：我写的 events.py 是"给归因用"；他们的口径更强 ——
    ★ 事件日志是【唯一真相源】，上下文是它的投影。建议照此加强。

【Crash Recovery 的具体做法】（★ 直接解决我方的"失败 turn 落盘"）
  载入时若发现一个 turn/start 没有对应的 turn/end，
  ★ 不粗暴截断已持久化的长任务，而是【补一个 interrupted 结束事件】，
    保留中断前已经发生的事实。
  → 与我方"失败也是一种必须被记录的状态"同源，但给出了【具体动作】。

【工具执行：调用级分类，不是工具级】
  每次待执行调用查询 executionMode：
    parallel  —— 可与相邻安全调用重叠
    exclusive —— 必须独占，形成顺序屏障
  用【滚动并发池】执行，但★结果仍按模型调用顺序写回。
  ★ 分类发生在【调用级】：同一个工具可以根据本次参数判断是只读还是独占写。
  → 正是我方 gap #8「并行与依赖判定」。他们做得更细。

【工具流水线】
  pre-execute → 单调守卫 → execute → post-execute → finalize → result
  审批、沙箱、超时、重试、指标、结果改写都挂在这条线上，工具本身不依赖策略。

【Code Mode —— 大输出问题的更彻底解法】
  只向模型暴露一个 run_code 入口 + 自动生成的 SDK；
  中间工具结果留在本次执行的局部，★只有 return / console.log 的内容回上下文。
  → 与我方"大输出落盘"同源，但他们省掉的不只是存储，是【token】。
  代价（作者点出）：每次全新状态、不是持久 REPL、执行局部无统一字节上限。

【沙箱 fail closed】
  ★ 无法落实策略时【fail closed】，而不是悄悄降级为无限制执行。

【Subagent 的两类】
  ★ 区分【一次性子 Agent】和【可继续对话的子 Agent】；
  后者有持久 Session、可冷恢复的 Activation、唯一 inbox、明确的父子授权关系。
  → 我方的 AgentRuntimePort 只有一种形态，建议按此分两类。
```

#### 他们的状态分层（AgentScope 侧，可直接借鉴）

```
RuntimeContext   当前调用的 userId/sessionId + 临时属性，【不持久化】
AgentState       对话上下文、权限、Plan Mode、工具状态，跨调用恢复
Session JSONL    长期保留的对话流水账
Workspace Memory 每日只追加记忆 + 聚合的 MEMORY.md
Sandbox Snapshot 容器文件、依赖、运行环境快照

★ 同一 Session 的调用【串行化】，不同 Session 可并行。
→ 直接对应我方的"会话隔离"，而且给出了分层口径。
```

#### 两条能力接缝的做法（对应我方 AgentRuntimePort）

```
模型 / 文件系统 / Shell / PTY / LSP / Session Persistence / Subagent
★ 全部是 provider —— 换 E2B provider 后，上层 Shell、文件工具继续复用同一套语义。
★ 他们强调"能力接缝"（capability seam），而不是在每个工具内部判断
  当前跑在本机、容器还是远端。
```

### ⑩ 并行 agent 的三个要求 —— PingCAP `ai-coding-agent-files`
> "Agents **fail, retry, and explore in parallel**. That creates three requirements:
> **isolated workspaces** so one attempt cannot corrupt another, **concurrency controls**..."

**★★ 这一句直接命中我方的"并发与调度层"缺口** —— 而且把顺序说清了：
**先隔离，再并发控制，再回滚。**

### ⑪ 准确率是语义层问题 —— SingleStore
> "Accuracy in general terms of any agentic system is usually a **context problem**.
> Accuracy depends on a **semantic layer** that somebody maintains by hand."

**★ 直接支持 §1.1「必须有共享语义」** —— 而且点明它是**需要人维护的**（对应我方"可手改、下次复用"）。

### ⑫ 多 agent 状态栈 —— PingCAP `ai-agent-state`
> "两个 agent 碰同一条记录，需要一种办法不互相踩"；
> **checkpoint 必须原子**（要么全完成要么不发生）；
> **branching / copy-on-write** 做并行隔离。

### ⑬ 其他（当线索用）

```
失败处理三分 —— AWS bedrock-agentcore 博文
  "Three failure strategies, matched to component criticality:
   per-agent retry with backoff / degradation / isolation"
  → 与 §7.9 的三级出口同构，而且是按【组件关键性】匹配策略。

Verification for autonomy —— eugeneyan.com/writing/working-with-ai
  "pair programmer can watch for execution drift — is the model doing the task right?
   This is local and tactical..."
  → 与"背景 reviewer"同源。

Cognition 为何不用多 agent —— jxnl.co（Walden Yan）
  失败于【上下文丢失】与【冲突决策】；agent 之间"telephone game"效应。

Harness 的八个组件 —— xargin.com/harness-or-not-harness-it-is-a-question
  context engineering = 【context injection 策略的设计】（何时、以何方式注入）。

In-Context Reward Hacking —— lilianweng.github.io
  迭代自改进里评测模型 == 生成模型时的 reward hacking（= LLM 审 LLM 是建议性的）。
```

---

## 5. 对 bobanana6 的直接影响

### 5.1 支持的

```
✅ 中心化验证优于无验证（2512.08296）        → 判定内核 + gate 这条主线
✅ 独立并行不通信 = 错误放大 17.2×            → 正是要防的
✅ 一致性是跨所有 scaling 层级的核心挑战       → §5.6「不让各立一套」
✅ 编排拓扑主导性能，超过模型能力              → "卖点是编排不是模型"
✅ 准确率是语义层问题，且需人工维护（SingleStore）→ §1.1 共享语义
✅ 异构团队占成本-准确率前沿（2606.20629）     → §5.7「runtime 可混用」
✅ 子问题不通信但共享契约（APWA + 星形耦合）   → §4.3 形状选对了
✅ 成本约线性增长（2607.27942）               → 成本参照
```

### 5.2 反证 / 警告（**必须正面处理**）

```
🔴 【能力饱和】单 agent 基线超过某水平后，协调收益递减
   → 打在本项目的【任务选择】上。任务单 agent 能做，分工是负收益。

🔴 【顺序规划 −70.0%】
   → 某些任务形状下是【结构性负收益】，不是概率问题。
   → 建议：反例集加"顺序规划型"，判据硬化为"识别为顺序依赖型"。

🔴 【性能在中间复杂度达峰，之后下降】（2607.27942）
   → 打在"N 越大越好"的直觉上。
   → 建议：ClusterPolicy 补一条【复杂度上限】。

🔴 【只有模型过最低能力门槛，scaling 才有效】（2607.27942）
   → ★★ 最尖锐：本项目计划用弱模型/免费小模型当 worker。
   → 必须先回答：Qwen3.5-4B 这个量级过没过门槛？
     建议做成前置实验（同一子任务，能否稳定产出可判定产物）。
     不过门槛 ⇒ "用充裕并发砸任务"变成只增数量不增能力。
```

### 5.3 需要改设计的（含本轮新增）

```
① ClusterPolicy 取值域 → 借 AdaptOrch 四拓扑
   parallel / sequential / hierarchical / hybrid（有外部定义可引用）

② 补"复杂度上限"判据（2607.27942 的峰值现象）

③ 反例集加"顺序规划型任务"

④ 前置实验：worker 模型的最低能力门槛

⑤ ★ 事件日志升级为【唯一真相源】
   我方原设计：events.py 供归因用。
   DeepSeek Harness 的做法更强：会话 = append-only 日志，
   ★ 模型上下文由日志【投影】出来（deriveMessages）。
   好处：从结构上消灭"三份状态漂移"这类最难查的 bug。
   → 建议采纳其口径。

⑥ ★ Crash Recovery 的具体动作
   发现 turn/start 无 turn/end 时，★ 补 interrupted 结束事件，
   不粗暴截断。直接写进 state.py 的实现要求。

⑦ ★ 工具并发改【调用级】分类
   不是"这个工具能不能并行"，而是"这一次调用能不能并行"
   （同一工具，只读参数可并行、写参数必须独占）。

⑧ ★ Subagent 分两类：一次性 / 可继续对话（后者要持久 Session + 冷恢复 + inbox）

⑨ ★ 沙箱 fail closed，不悄悄降级

⑩ ★ 角色瓶颈诊断可直接借 Shapley 口径（2606.20629）
```

---

## 6. 一个战略判断（读完 DeepSeek Harness 之后）

**本项目原计划：fork Hermes 的 loop 当 `agentloop/`。**

但读完之后要说一句实话：

```
Hermes 的 loop 是【单 agent 的 loop】—— 它解决的问题是
  "上下文会爆" 和 "各种错怎么恢复"（见 FORK-NOTES）。
  它对【多 agent 运行时】几乎没有设计。

而 DeepSeek Harness 解决的正是【多 agent 运行时】：
  子 agent 的能力接缝 / 一次性 vs 可继续 / 调用级并发分类 /
  事件日志唯一真相 / crash recovery / 沙箱 fail closed。

★ 这两件事的交集比想象中小。
```

**bobanana6 的 `agentloop/` 缺口清单（14 条）里，至少 8 条 DeepSeek Harness 已经有现成答案**，
而且比我原方案更细（见 §5.3 的 ⑤⑥⑦⑧⑨）。

**建议**（不是换掉，是分工）：

```
Hermes 的 loop       → 提供【单 worker 内部】的循环、压缩、错误恢复
DeepSeek Harness     → 提供【多 worker 之间】的运行时语义
（两者都 MIT/Apache 类开源，路径可行）
```

**⚠️ 但要注意**：DeepSeek Harness 是 **`0.1.0-rc.8` Developer Preview**，
Session 格式是**预发布 v0 且没有内置迁移链**，官方明确提示会有破坏性变化。
**→ 所以是"读它的设计、抄它的形状"，不是"依赖它"。**

---

## 7. 文献坐标表（写文档直接引）

| 主题 | 引用 | 关键数字 / 结论 |
|---|---|---|
| 架构-任务对齐决定成败 | 2512.08296 | +80.8% ～ −70.0% |
| 无中心验证传播错误 | 2512.08296 | 有对照实验 |
| 独立并行不通信 | 2512.08296 | 错误放大 **17.2×** |
| 能力饱和 | 2512.08296 | 单 agent 够强时协调负收益 |
| 协作缩放律 | 2406.07155 | logistic；>1000 agents；不规则拓扑更优 |
| 大规模自组织 | 2609.26781 | 1→1024：33.89%→55.06% |
| MAS 四设计原则 | 2607.27942 | simplicity/elastic feedback/seq+loops/summary comm |
| 复杂度峰值 | 2607.27942 | 中间最优，之后下降 |
| 模型能力门槛 | 2607.27942 | ★ 不过门槛则 scaling 无效 |
| 一致性是核心挑战 | 2607.27942 | 跨所有 scaling 层级 |
| 四拓扑 + 路由 | 2602.16873 | parallel/sequential/hierarchical/hybrid |
| 拓扑收益 | 2602.16873 | +12–23%（同模型） |
| **异构团队占前沿** | **2606.20629** | **+44% 准确率 或 1/12 成本** |
| **角色瓶颈诊断** | **2606.20629** | **Shapley 值** |
| **非干扰子问题** | **2605.15132** | **without cross-communication** |
| **590 agents 政策模拟** | **MegaAgent (ACL25)** | **800s 出 Go 程序** |
| **会话=append-only 事件日志** | **DeepSeek Harness** | **"模型可见即已记录"** |
| **调用级并发分类** | **DeepSeek Harness** | **parallel / exclusive** |
| 并行 agent 三要求 | PingCAP | 隔离 → 并发控制 → 回滚 |
| 准确率是语义层问题 | SingleStore | 需人工维护的语义层 |

---

## 8. 还没查的

```
□ AgentCARD 代码库 —— github.com/Auto-CAP/AgentCAP（Pareto + Shapley 实现可直接用）
□ APWA 全文 —— 它与"星形耦合"的异同值得细看
□ MegaAgent 中段（本次只读了头尾）—— 分层监控的具体机制
□ DeepSeek Harness 的 docs/ 四份原文（architecture / agent-lifecycle /
   tool-execution-pipeline）—— 本轮只读了二手解读
□ forage 补查：「事件溯源 agent」「turn 边界」「角色瓶颈 归因」
```

---

## 附：采集命令（可复现）

```bash
# 网络
web_search  6 组关键词
web_extract arxiv.org/abs/{2512.08296, 2406.07155, 2609.26781, 2607.27942,
             2602.16873, 2606.20629, 2605.15132}
web_extract aclanthology.org/2025.findings-acl.259.pdf
web_extract rowkey.cn/blog/2026/08/21/deepseek-harness-vs-agentscope-java/

# forage（WSL，必须 -u root，默认 user 无权限）
wsl -u root bash -c "cd /root/code/forage && \
  /root/code/memory-bridge/.venv/bin/python -m forage search '<query>' -k 5"
```
