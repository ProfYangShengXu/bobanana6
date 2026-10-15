# bobanana6

> **Deterministic judgment kernel for sharded multi-worker task execution.**
> 把复杂任务切成 N 块、用充裕并发分工砸完，判定内核保证**每块合格、语义一致、整体拼得上**。

---

## 这是什么

一个大模型跑不完的复杂任务，通常可以切成多块并行做。真正的难点不在"切"，在**拼**：

- 每块产出合格吗？（不是"看起来像"，是**可判定**）
- 不同块对同一实体的说法一致吗？
- 下游块手上只有上游产物时，判据还成立吗？

bobanana6 把"判定"从"生成"里剥出来，做成一个**确定性内核**：`core/` 里零模型调用，
所有判断都可复算、可追责。模型只负责产出，判定由内核按声明的契约执行。

```
                声明契约（task.yaml）
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
      块 T-01        块 T-02   …    块 T-N          ← 模型在这里干活
        │              │              │
        └──────────────┼──────────────┘
                       ▼
              ┌─────────────────┐
              │   判定内核       │  ← 零模型、确定性、可复算
              │  块级 / 装配 /   │
              │  归因 三层判据    │
              └─────────────────┘
                       ▼
                  可解释的报告
```

---

## 核心设计判断

这六条是本项目真正想说的东西。每条都带**可检查的判据**和**实测反例** ——
不是原则宣言，是被数据逼出来的。

### §0 能力模型：真正的能力 = 瓶颈项 × 传导率

```
真正的能力 = 瓶颈项 × 传导率

  瓶颈项 = min( 模型侧：内含知识量 + 推理能力 ,
                外部侧：知识可用量 )

  传导率 = 壳（harness）= 知识能不能【到达】模型、并【被用上】
```

**为什么是乘法不是加权平均**：本项目 28 轮 A 臂的最大一笔读数 ——
同一模型、同一题，**壳的三处堵点打通前后**：

```
阶段一  传导率 ≈ 0（散文信封 + 写区误伤 + 逐字匹配）  ⇒ 块级 1/8
        ★ 期间反复"给模型加东西"（题面全文内联、装 pandas）—— 读数【一动不动】

阶段三  三处堵点先后打通                              ⇒ 块级 4/8
        ★ 同样的知识量、同样的模型，产出翻四倍
```

**⇒ 变量是传导率。加权平均解释不了"含量不变、能力变四倍"。**
**⇒ 推论：传导率低的时候，提升模型侧或知识侧的收益恒为零。**

### 六条硬原则

| 原则 | 一句话 | 反例代价 |
|---|---|---|
| **一 · 先问瓶颈** | 出问题先定位：换强模型能修好吗？信息内联 vs 工具背后有差别吗？ | 前 20 轮反复调 prompt，读数一次没动 |
| **二 · 壳级失败优先** | 存在壳级失败时，该轮读数对"模型强弱"**没有判别力**，不得拿它比模型 | 5/8 块挂在散文信封上，拿去比模型结论必错 |
| **三 · 守卫落在确定性一侧** | 抓"确定的坏事"的留下；猜"对错"的退休 | 逐字匹配守卫：真编造拦不住，合法加工一定误伤 |
| **四 · 高精度低召回** | 抓错一次的代价大于漏十次。新守卫一律从"记录"起步 | 前置门一处没过，八个块只跑过一个 |
| **五 · 下游块手上只有上游产物** | 加规则前先问：这条是照"第一道工序"写的还是"第二道工序"？ | 聚合块被按"读来源"的规则判死 |
| **六 · 强模型是判别器不是裁判** | 它判不出来的地方最值钱 —— 那是**规格缺口**，不是要它拍板 | 分歧只记录、不自动补约束 |

**完整版（含逐条判据与十个真实反例）见 [`设计原则.md`](设计原则.md)。**

---

## 架构

```
adapters/ ─┐
agentloop/ ─┼─→ core/        ✅ 依赖箭头全部指向稳定侧
sources/  ─┘

core/ ─→ adapters/           ❌ 绝对不行
```

**没有一条箭头从 `core/` 指出去** —— 这条由测试守着（`test_no_outbound_edges_from_core`）。

```
src/bobanana6/
  core/            5,240 行   ★ 零模型：模型、端口、判据、预算。稳定侧
  ontology/          205 行   YAML 本体 + JSON Schema
  contract/          742 行   把 task.yaml 解析成可执行的契约
  decompose/       1,218 行   切分策略与预算分配
  sources/           609 行   来源冻结（内容寻址 + 台账）
  adapters/        1,121 行   OpenAI 兼容客户端、离线路径、限速
  services/        5,835 行   工具面、检查器、工作区、求助出口
  assemble_impl/   5,498 行   装配：读块 / 写块 / 归因 / 报告
  report/            425 行   报告渲染
  agentloop/       4,259 行   块内循环（fork 自 Hermes，非依赖）
```

**关键边界**：端口定义在稳定侧、实现在外侧；`core/` 不认识"桥 / agent / 回环"这些词。

---

## 快速开始

```bash
pip install -e .

# 跑一份任务契约（离线路径，不调模型）
bobanana6 run examples/task.yaml --offline
```

**本体的发现顺序**（优先级从高到低）：

```
① --ontology <file>              显式指定
② <任务目录>/ontology.yaml        与任务同名同目录，自动使用
③ 由模型起草                     前两者都没有时
```

> 第 ② 条是默认约定 —— 把本体放在任务文件旁边，就不用在命令行里再指一遍。

接真模型（任何 **OpenAI 兼容**端点）：

```bash
export BOBANANA6_BASE_URL="https://api.example.com/v1"
export BOBANANA6_MODEL="your-model"
export BOBANANA6_API_KEY="sk-..."

bobanana6 run your-task.yaml
```

> ★ 代码里**没有 provider 分支、不硬编码模型名** —— 换一家只改环境变量。
> 限额也从 env 读（`BOBANANA6_TPM_LIMIT` / `RPM_LIMIT` / `TIMEOUT_S`）。

任务契约（`task.yaml`）声明三件事：

```yaml
deliverables:        # 要交什么、怎么被消费
sources:             # 从哪取数（本地文件 / 研究类取数）
constraints:         # 判据是什么（硬约束 + 内容检查）
```

---

## 验收集

`acceptance/` 是**系统级验收**（可以接真模型，但要可控），与 `tests/` 的确定性单测分开。

```
acceptance/
  judge.py      判分器：三口径 + 强制覆盖声明
  harness.py    把一道题跑成一份 AcceptanceReport
  smoke/        3 个小任务 + baseline.json（门禁容差表）
  main/         同形状 × 3 域（金融 / 文献 / 日志）
  stress/       input/ 与 truth/ 【物理隔离】+ attacks.yaml
  counter/      反例集：应当【拒绝切分】的任务
```

### 三条不可破的口径

```
① ★ 禁合成单一总分 —— 块级 / 装配 / 归因朝不同方向动，一个总分把它们掩盖掉
② ★ 没走到的出口必须声明 —— 没被走到的出口 = 未验证的代码
③ ★ 冒烟用【通过数对比容差】—— 不用 reward 二值（会把能力波动误诊成架构回归）
```

三条都**落成了测试**，不是写在文档里。

### ground truth 物理隔离

`stress/input/` 给系统，`stress/truth/` 只在评测时挂 —— **不在同一个容器里同时挂**。
不隔离时，「模型没答对」和「模型偷看了」分不开。

---

## 官方入口：contactor

**bobanana6 设计上的入口是一个 agent 桥 —— [`contactor`](https://github.com/ProfYangShengXu/contactor)。**
这不是附件，是设计的一部分：块跑不动时往外出、任务成批往里进，都走这一条路。

```
                    ┌──────────────────────────┐
   派任务进来  ───→  │                          │
                    │   contactor  (JSON-RPC)   │  ← 本机 agent 桥，默认 :8791
   求助出去    ←──→  │   message/send            │
                    │   tasks/get / tasks/answer │
                    └──────────────────────────┘
```

### 为什么是桥，不是函数调用

同进程能包在事务里的事，跨进程做不到：网络分区、超时、限流、远端崩溃……
**这些失败全部要在接口上表达出来**，不能靠"调用没抛异常"来假定成功。桥把这些变成协议事实：

| 协议事实 | 为什么必须有 |
|---|---|
| `messageId` 幂等 | 网络抖一下重试，有副作用的活就会执行两遍 |
| `input-required` 是**常态**不是异常 | 对面要放行/拍板时必须能停下来，而不是挂到超时 |
| `state: completed` ≠ 结果正确 | 桥只保证"执行完毕"，`contentVerified` 恒为 false |
| `taskId` 是 16 位十六进制 | 小整数是 JSON-RPC 的 `id` —— 那是**请求报错了**，任务没发出去 |

### 求助出口：块反复做不成时

```
EscalationPort → services/escalation.py → services/bridge_call.py → contactor
```

**两条设计约束**：

```
★ 不设答复超时 —— 对面是只会被动应答的一方，一超时就等于把答复扔了
★ 连不上 ⇒ 立刻返回"没拿到" —— 那不是"等一等就好"，那是"这条路今天不通"
```

**★ 内容验证的边界**：`contentVerified` 恒为 false，所以拿回来的答复**不当成结论用** ——
它是**一条交回给那个块的消息**，块据此再试一次，成败仍由确定性内核判。桥不替我们判。

### 怎么给 bb6 派任务（给别的 agent 看的）

**一句话**：往桥上的 `bobanana6` 发一条消息，消息里**含一个 `task.yaml` 的绝对路径**。

```bash
contactor send bobanana6 "跑这道题 /abs/path/to/task.yaml"
```

或在 `~/.bashrc` 里放个别名：

```bash
alias vrs='contactor send bobanana6'
vrs "跑 /abs/path/to/task.yaml"
```

**★ 派的是契约，不是自然语言。** 契约（`task.yaml`）本身就是这道题的规格 ——
`deliverables` 要交什么、`sources` 从哪取数、`constraints` 判据是什么。
让 bb6 现场把自然语言猜成契约，等于把"要什么"重新变成一次赌博。
**写契约的是调用方；bb6 只负责按契约把活干完、并如实报告干成了没有。**

契约长这样（`examples/task.yaml` 是最小示例）：

```yaml
task_id: example-panel
goal: 把债务人敞口面板整理成一份可复核的 CSV
deliverables:
  - {name: out.csv, schema: {obligor: str, pd: float, amount: float}}
sources:
  - {ref: panel-2026q1, locator: panel.csv}    # ★ 相对【任务目录】，不是仓库根
constraints:
  hard:
    - {id: C-rows, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "…"}
```

> ⚠️ `locator` 的相对基准是 **task.yaml 所在目录**（跑题时会 chdir 过去）。
> 写成 `examples/panel.csv` 换个入口就找不到，报出来是「在 sources 门被拒」——
> 看着完全不像路径问题。

**返回什么**（一行 JSON）：

```json
{"ok": true, "task_file": "/abs/path/task.yaml", "exit_code": 0, "duration_s": 14.9,
 "reports": ["CONFLICTS.md", "ONTOLOGY-MAP.md", "SOURCES.md", "summary.json"],
 "summary": {"task_id": "example-panel", "exit_reason": "passed",
             "blocks": [{"subtask": "T-01", "ok": true, "failed": []}],
             "assembly": {"passed": true, "failed_blocks": [], "coverage": {"blocks": "1/1"}}},
 "detail": "passed"}
```

**怎么读它**（★ 三种结论要分清，外面的下一步完全不同）：

| `detail` | 含义 | 下一步 |
|---|---|---|
| `passed` | 每块合格、装配过 | 可以消费产物 |
| `failed: <codes>` | **开工了没做成** —— 某块没过判据 | 看 `_run/reports/summary.json` 的逐块证据 |
| `refused before any worker started: …` | **根本没开工** —— 源取不到 / 契约不合法 | 改的是题或环境，**不是实现** |

**产物在哪**：`<task.yaml 所在目录>/_run/reports/`（`summary.json` 是完整读数，
上面返回的 `summary` 只是关键字段的摘要 —— 细节按路径去读，别让传输层当搬运工）。

**注册**（一次性，在 `contactor` 的 `config.yaml`）：

```yaml
  bobanana6:
    kind: subprocess_cli
    description: "…它吃的是【任务契约】不是自然语言…"
    skills:
      - id: run-contract
        name: 按契约跑一道题
        description: 给一份 task.yaml 的绝对路径，它按契约切块并行做完，返回每块成没成
        tags: [orchestration, acceptance]
    command: ["wsl", "-d", "Ubuntu", "-u", "root", "--",
              "/mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6/tools/bb6_bridge.sh"]
    prompt_via: arg
    cwd: "C:\\Users\\45140"
    timeout_s: 3600
```

> ⚠️ `command` **不要**套 `bash -lc "… \"$@\""` —— 实测 `bash -c`/`bash -lc` 都会把
> 位置参数吃掉（`argc=0`），prompt 传不进来，而表现是"脚本跑了但说没找到 task.yaml"。
> 直接 exec 脚本即可。

**端点从哪来**：`subprocess_cli` **没有 `env` 字段**，而 `wsl.exe` 也**不会**把 Windows 侧
的环境变量带进 WSL（实测：WSL 里读到空）。所以模型端点走一个 WSL 侧的文件，
由 `tools/bb6_bridge.sh` 读取：

```bash
# /root/.bb6_bridge.env   （chmod 600，别提交）
BOBANANA6_BASE_URL="https://api.example.com/v1"
BOBANANA6_MODEL="your-model"
BOBANANA6_API_KEY="sk-..."
```

### 配置：两件事分开

送达方式与机器拓扑无关 ——

```
桥在哪         BB6_BRIDGE_ENDPOINT     默认 http://127.0.0.1:8791/
用哪个解释器    BB6_BRIDGE_PYTHON       默认 = 本进程解释器
```

同侧部署直接用本进程解释器；双栈机器（进程在 WSL、桥在 Windows）指过去即可 ——
**这是默认拓扑下的落地点，不是接口定义**。`bridge_call.py` 只做一件事：往端点发 HTTP。

> 完整协议用法（发现 agent、读名片、放行、轮询、幂等重发）见
> [`contactor` 项目主页](https://github.com/ProfYangShengXu/contactor)。

---

## 文档

| 文件 | 内容 |
|---|---|
| [`设计原则.md`](设计原则.md) | ★ 六条硬原则 + 判据 + 十个真实反例 |
| [`架构设计.md`](架构设计.md) | 分层、依赖方向、端口与实现的分界 |
| [`实现规格.md`](实现规格.md) | 逐步实现规格 |
| [`AGENTS.md`](AGENTS.md) | 给在本仓库工作的 agent 的开发指引 |
| [`FORK-NOTES.md`](FORK-NOTES.md) | `agentloop/` 从 Hermes fork 的边界与理由 |

根目录另有 50+ 篇过程记录（每次改动一份，含**改前/改后对照读数**与**反证**）。

---

## 已知边界（诚实标注）

```
· `ArbiterPort` 只有定义、零实现 —— 分歧被分类、合并、进报告，然后就断在那里
· `join_truncated_parts` / `continuation_overlap_length` 无调用方（本项目 loop 没有流式文本路径）
· `PROGRESS_RULER` 无读者（没有跨 turn 的"无进展检测"）
· `agentloop/` 是 fork 不是依赖，两侧会漂移
```

这些不是残留，是**明确报告的未接线项** —— 每一条都写在对应模块的 README 里。

---

## 许可证

本项目以 **MIT** 许可发布，见 [LICENSE](LICENSE)。

### 派生声明

`src/bobanana6/agentloop/` 派生自 **Hermes Agent**
（[`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent)，MIT 许可）。

该子目录保留上游的版权与许可文本（[`src/bobanana6/agentloop/LICENSE.hermes`](src/bobanana6/agentloop/LICENSE.hermes)）。
fork 的**边界与理由**（抄了什么、没抄什么、为什么不直接依赖）见 [`FORK-NOTES.md`](FORK-NOTES.md)。

> 除此之外，本仓库其余部分为原创实现。
