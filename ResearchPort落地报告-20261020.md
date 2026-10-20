# ResearchPort 落地报告（2026-10-20）

> 任务：让 bb6 真能做「信息采集 + 总结」类研究任务。
> 依据：`研究类任务-网络取数-融合设计-20261014.md`（设计已拍板，本轮是**实施**）。
> 结论先说：**取数层这一支接通了** —— 研究问题变成一批带元数据的 `Snapshot`，
> 快照 / 溯源 / 写区 / 判定四套机制一个字没改；块级工具面**一个都没多**。

---

## 0. 一句话与五条读数

```
研究问题 ──ResearchPort.research()──> 一批 Snapshot（清单 + 材料）
          ↑ 取数在【壳】，不在块里；块拿到的还是 Snapshot，它不知道那来自网络
```

| 判据 | 读数 |
|---|---|
| ① 可复现（红线） | 同一份契约两次跑：材料 **逐字节相同**、第二次 **0 请求**；删掉台账后重抓内容变 ⇒ 台账是承重的 |
| ② 检索接上了 | 1 个种子扩出 3 份材料；关掉 ResearchPort ⇒ `research_unavailable`；拿不到依据 ⇒ `research_no_evidence` |
| ③ 降级可见 | 首选后端 403 ⇒ `backend=jina` / `degraded=True`，manifest、SOURCES.md、summary.json、brief 四处都看得到 |
| ④ 直连优先 | API 臂 **1 次请求**（数字 2.0.0，新）vs 先搜再抓 **2 次请求**（数字 1.0.0，旧） |
| ⑤ 信任分层 | HF/PyPI API → T1；arXiv → T2；HN → T3；随便的博客 → T4；T1 不能被模型一句话升上去 |

探针一条命令复现全部：`PYTHONPATH=src:. python tools/research_probe.py` → **5/5 PASS**（实际输出见 §4）。

---

## 1. 锁 1：动手前先报现状（实际读到的，带 file:line）

### ① `sources/` 现有几个 Fetcher，各自 `source_kind`

| 类 | 位置 | `source_kind` | 说明 |
|---|---|---|---|
| `FileFetcher` | `src/bobanana6/sources/fetchers.py:57-89` | `"file"`（`:63`） | 本地路径；带 `SourceLedger` 可复用 |
| `HttpFetcher` | `src/bobanana6/sources/fetchers.py:92-141` | `"http"`（`:99`） | 网络；代理从 env 读，绝不清理 |
| `LocatorFetcher` | `src/bobanana6/sources/fetchers.py:144-177` | 无（按 scheme 分派） | `http(s)` 走网络、其余走磁盘 |
| `LinkExpandingResearch` | `src/bobanana6/sources/research.py:602` | 清单 `"research"`；材料 **T1–T4** | `ResearchPort` 的唯一实现；每份材料按域名判信任层 |

### ② `Snapshot` 现有字段全清单（`src/bobanana6/core/model.py:848-864`）

```
ref          : str        # 快照主键（core 按它查表）
body_path    : Path       # 冻结载体位置（core 不打开）
captured_at  : str        # 捕获时刻（freshness 比较）
source_kind  : str = ""   # 溯源分层：file/http 是载体类；研究材料落 T1–T4
backend      : str = ""   # ★ 本轮之前已加：走哪条路取回来的
degraded     : bool = False # ★ 本轮之前已加：实际用的 ≠ 想要的
```

本轮**没有**再加字段（任务书 3.2 的两条已在）；本轮把它们的读者补齐了（见 §3）。

### ③ `contract/parser.py` 对 research 的校验 / 静默跳过

校验（现有 + 本轮补的）：

```
parser.py:99        SOURCE_KINDS = ("", "file", "research")
parser.py:360-365   kind 枚举校验；research 必须有非空 question
parser.py:368-372   seeds 必须是 list（字符串不行）
parser.py:373-375   ★ 本轮补：重复 ref 当场拒（以前 dict 后者覆盖前者，静默少一份来源）
parser.py:376-381   ★ 本轮补：research 又写 locator 当场拒（以前被静默忽略）
parser.py:382-386   ★ 本轮补：seed 元素必须是非空字符串（以前 str(123) 变 "123"，永远抓不到）
```

仍然静默/未解析（**明确不冒充已做**）：

- `sources[]` 的未知键不拒（`budget:` / `allow_channels:` 打错会**静默丢**）；
  设计 §3.2 的 per-source `budget` 在契约里**没有读者**，本轮沿用 runner 级 `ResearchBudget`。
- `kind=file` 同时写了 `question` / `seeds` 会被忽略（既有行为，未动）。

### ④ `acceptance/ab/agent-jd-research` 那次跑测留下的脚本

归档在 `_prev/run-baseline/workspace/`，共 **11 个 `.py`**（任务书记的是 10 个；实际读到的就是 11 个，多出 `workspace_extract_data.py`）：

```
补材料（6）：download_hunt3.py · extract_hunt3.py · parse_jd.py · parse_roadmap.py
            process_hunt3.py · process_hunt3_v2.py
生成产物（5）：build_jd_matrix.py · create_interview_bank.py · create_summary.py
              generate_outputs.py · workspace_extract_data.py
```

另有 `_tmp/` 下 5 个产物碎片脚本、`output/` 下 2 个生成脚本（审计已逐个 head 过）。
这 11 个脚本正是「壳没实现取数，模型只好自己补」的物证。

### ⑤ 本机 Agent Reach 装没装 / `doctor` 报几个渠道

**装了，在 Windows 侧；WSL 的 `which` 看不到它**（这正是设计 §4④ 说的双环境）：

```
cmd.exe /c "agent-reach --version"  → Agent Reach v1.4.0
cmd.exe /c "agent-reach doctor"     → 状态：6/16 个渠道可用（复跑一次读到 5/16；
                                        单渠道健康检查有瞬态，两次都含 Jina / GitHub / RSS）
    ✅ GitHub / YouTube / V2EX / RSS / 任意网页（Jina Reader）/ B站
    [!] Reddit（已装未登录，403）
    [X] 全网语义搜索（mcporter 已装但 Exa 未配置）
    [X] 微信公众号（缺 mcporter + Exa）
```

**这决定了两件事，本轮照实做、不编后端**：

1. 「不知道在哪」的那条路（语义搜索）**本机仍不可用** —— 所以 research 的入口仍是
   **种子 + 从正文扩链接**；不伪造一个搜索后端。
2. 「已知位置」的那条路（任意网页 / Jina）**可用**，正是实现里的 `direct` / `jina`
   两条后端。GitHub / RSS / B站 等渠道是 agent-reach 的**上游 CLI**，本轮没有把
   `agent-reach` 当统一 RPC 调（它自己也不是：读取调上游 CLI、搜索走 MCP）。
   ⇒ 报告与探针里如实写「可用但搜索未配置」，不写「未安装」。

---

## 2. 六件事落地（改了什么，file:line）

### 2.1 `ResearchPort` + 两个数据类

- `src/bobanana6/core/ports.py:187-203`：`ResearchPort.research(question, budget)`。
- `src/bobanana6/core/model.py:867-906`：`ResearchQuestion(ref, question, seeds, channels)`、
  `ResearchBudget(max_sources, max_depth, max_bytes)`。
- `ResearchQuestion.channels` **本轮接上了读者**：`research()` 用它覆盖实例的默认后端列表
  （`sources/research.py::_backend_order(override)`），以前它是死字段。

### 2.2 `Snapshot` 的两个字段 + **读者**

字段本身早就在（`core/model.py:857/861`）。本轮把「没有读者的声明只是注释」这条兑现：

| 字段 | 读者 | 落点 |
|---|---|---|
| `backend` | `report/builders.py:95-104`（SOURCES.md 新增「走的后端」列）· `builders.py:326-330`（summary.json）· `produce.py:_source_trust()`（brief 的 `source_trust`） | 人、机、块三处 |
| `degraded` | 同上三处 + 检索清单正文「降级=是」+ 「完整度可能打折」提示 | 人、机、块三处 |

> ★ 本地文件来源三项全空 ⇒ `source_trust` 不出现、SOURCES.md 多两列但值都是 `-`/`否`；
>   `kind` 缺省 = file 的既有契约**行为一字不变**（有测试守着）。

### 2.3 `sources/research.py`：薄路由层 + 熔断

> 任务书写的交付路径是 `sources/research.py`。这份实作原来在 `services/research.py`，
> 本轮**移到 `sources/`**（取网 + 快照是周/季度变化层，services 是 ToolSet 实作层），
> 同时更新了 `cli.py:55` 与测试的 import。

- **直连优先**：`_fetch()` 先按地址直连；已知官方 API（PyPI / HF / GitHub / arXiv）
  额外标注 `backend=api:<host>`，**不多打一次搜索**。
- **HTML → 文本**：本地 `html_to_text()` 先转，转不出正文才走 Jina；
  **链接从原始 markup 里挖**（修掉「转完文本后 `<a href>` 没了 ⇒ HTML 种子永不扩展」）。
- **熔断状态机**（`_try_backend` / `_fetch`）：五条出口，见 §5 表。
- **具名弃权**：一份材料都没有 ⇒ `ResearchUnavailable`；需要登录态 ⇒
  `ResearchNeedsAuthorization`。runner 把它们变成 `RunRefused` 的具名 code，
  **不制造"共找到 0 份"的清单**。
- **可复现**：`ResearchLedger`（`research.index.json`）按
  `(ref, question, seeds, budget, channels)` 记下整次检索；第二次**重放**，0 请求。
- **信任分层**：`trust_tier(locator, middle=...)`，规则表定 T1/T2/T3 已知项，
  未命中落 T4；`middle`（模型/上层）只允许判 T2/T3，**不能自升 T1**。
- **SSRF 守卫**：从别人页面挖出来的链接，拒绝字面量私网/环回/链路本地，
  以及 `localtest.me` / `*.nip.io` / `*.sslip.io` / `metadata.google.internal` 这类
  「公共域名静态指向内网」的写法；本机/内网地址也**不送外部 Jina**。

### 2.4 熔断五条出口（逐条）

| 出口 | 实现 | 读数 |
|---|---|---|
| 403/429 | `classify_http_status` → `FAIL_FORBIDDEN`/`FAIL_RATE_LIMITED`；**不重试**，直接下一个后端；`(后端, netloc)` 进熔断表 | 探针/测试：403 只打 1 次，备选拿到结果，`degraded=True` |
| 连接超时 | 同一后端**重试 1 次**；再超时才熔断换后端 | 测试：`/slow` 被打 **2 次**，然后备选 |
| 返回空 | **不是错**：有 `rewrite` 就换措辞 1 次，再空才换后端；`empty` **不熔断** | 测试：换措辞后仍 `backend=direct`、`degraded=False` |
| 需要登录态 | 记进 `_needs_login`；全部拿不到 ⇒ `ResearchNeedsAuthorization`，runner 出 `research_needs_authorization` | 测试：401 ⇒ 具名异常，消息点名 `login required` |
| 全部不可用 | `ResearchUnavailable`；runner 在 `_fetch_sources` 里**拒跑**（`research_no_evidence`） | 测试：只回清单/零材料 ⇒ 稳定红；**不许块照写** |

### 2.5 信任分层落到 `source_kind`

- 规则表：`sources/research.py::TRUST_RULES`（T1 官方文档/API、T2 论文/官方博客、
  T3 社区、T4 缺省）。
- 材料快照 `source_kind = T1..T4`；清单自己 `source_kind="research"`。
- SOURCES.md 按 `source_kind` 分节（`## T1` / `## T4`），**一句话来自哪一层**可按
  `Provenance.snapshot_ref` 回查；manifest 表格里也有「信任层」列。

### 2.6 接进主流程

```
契约 kind: research
  → Runner._fetch_sources → _research()          # runner.py:1007+
  → ResearchPort.research(question, budget)
  → 快照表（清单 ref=source.ref；材料 ref=source.ref:i）
  → _authorized() 把本次真冻过的材料加进授权（块读得到）
  → 切分 / 派块 / 判定 / 汇总（一字未改）
```

- **取数在壳**：块级工具面没有新工具，块只读 `sources.*`。
- **权限开关复用既有**：`content_checks.visibility != "open"` 却要 research ⇒
  `RunRefused(research_forbidden)`（测试守着）。
- **失败闸门**：`_fetch_sources` 现在要求 research 至少产出**一份材料**（只回清单不算），
  否则 `research_no_evidence`；单条种子失败写进 `fetch_failures` → SOURCES.md。
- **Runner 复用的脏状态**：`run()` 现在重置 `fetch_failures`（以前会把上一轮的取回失败
  带进这一轮）。

---

## 3. 锁 2：「值到达哪里算完成」（可观测读数）

| 要求 | 值到达哪里（可核对） | 证据 |
|---|---|---|
| 实现了熔断 | 首选后端 403 ⇒ Snapshot `backend=jina`、`degraded=True`；manifest 写「降级=是」；SOURCES.md 表里 `jina` + `是` | 探针 `[3/5]`；`test_a_forbidden_backend_falls_through...` |
| 熔断真的挡住 | 同一 (后端, 主机) 后续来源 **0 次**直连 | `test_a_tripped_circuit_is_not_retried...` |
| 超时只重试 1 次 | `/slow` 命中 **2 次**，然后换后端 | `test_a_timeout_is_retried_once...` |
| 空结果换措辞 1 次 | `rewrite` 调用 **1 次**；拿到正文后仍 `direct`/`degraded=False` | `test_an_empty_answer_gets_one_paraphrase...` |
| 信任分层接上 | `huggingface.co/api/...` → `T1`；随便博客 → `T4`；报告分 `## T1` / `## T4` | 探针 `[5/5]`；`test_trust_tiers...` |
| 降级可见（块） | brief 的 `source_trust[ref] = {backend, degraded, trust_tier}` | `produce.py:_source_trust()`；只对研究来源出现 |
| 可复现 | 两次 `research()` 的 `(ref, bytes, captured_at, source_kind, backend, degraded)` 全等；第二次请求数 **0** | 探针 `[1/5]`；`test_the_same_hunt_twice...` |
| 检索接上（块读得到） | 材料 ref `hunt:1..N` 在授权表里；每份正文非空、清单里能指到 | 探针 `[2/5]`；`test_expanded_materials_are_authorized...` |
| 关掉它会红 | 无 researcher ⇒ `research_unavailable`；只回清单 ⇒ `research_no_evidence`；拿不到依据 ⇒ 拒跑 | 探针 `[2/5]` 反证；`test_the_runner_refuses...` |

---

## 4. 锁 3：探针实际输出（`PYTHONPATH=src:. python tools/research_probe.py`）

```
（完整输出粘贴在下面；`--json` 只吐结构化结果，退出码 0 = 五组全绿）
```

（见附录 A，逐字粘贴本次运行输出。）

---

## 5. 四条反证 + 两条负向（「关掉它会红」）

| # | 反证动作 | 期望红 | 实际 |
|---|---|---|---|
| ① | 删 `research.index.json` + 改站点内容后重跑 | 材料 digest 变、请求数 > 0 | 探针 `[1/5]` 反证：`requests=3, materials_changed=True` |
| ② | 关掉 ResearchPort（`researcher=None`） | 该题**稳定失败**（`research_unavailable`），不是退化成瞎编 | 探针 `[2/5]` 反证；`test_no_researcher_wired_is_a_named_refusal` |
| ③ | 把 `degraded` 抹成 False 再生成报告 | 报告不再出现「是」 | 探针 `[3/5]` 反证：`remove degraded flag=False`（即"没出现"） |
| ④ | 同一组问题强制走「检索页 → 再抓」 | 请求数 2 > 1，且拿到旧数字 | 探针 `[4/5]`：`api=1/2.0.0` vs `search=2/1.0.0` |
| 负向 A | 只回清单的 ResearchPort | `research_no_evidence` | `test_a_researcher_that_only_returns_a_manifest_is_refused` |
| 负向 B | 403 之后第二条来源仍走直连（熔断失效） | 直连命中 > 1 | `test_a_tripped_circuit_is_not_retried...`（断言直连 `==1`、第二条 `==0`） |

> ★ 「关掉它会红」不是口头保证：`tests/test_research_probe.py` 把探针本身纳入测试，
>   任何一组变红（例如又退回空清单、又不复用台账、又不标降级）都会让 pytest 红。

---

## 6. 真实端到端跑测（锁 5 终局判据）

同一道题 `acceptance/ab/agent-jd-research`：

```
模型 = Qwen/Qwen3-8B（SiliconFlow；本机无 4B 端点，模型名如实写）
契约 = 4 个真实种子（raw.githubusercontent.com ×3 + notes.kamacoder.com ×1）
```

- **改前**（`_prev/run-baseline/`）：workspace 根下 **11 个 `.py`**（6 个补材料 + 5 个生成产物）。
- **改后**（本轮 `_run/`）：见 §6.1（跑完填）。

### 6.1 真实取数读数（同一份契约的真实网络检索）

同一份契约的检索阶段（4 个真实种子，`captured_at=2026-10-20T13:55:28+08:00`）：

```
共找到 8 份材料（种子是起点不是范围：4 → 8）
hunt:1..3  T3  direct               raw.githubusercontent.com（社区代码仓）
hunt:4..8  T4  direct+html2text     kamacoder / agentic-engineering-jobs / CSDN
```

- `hunt:4` 是 HTML，本地转出 322 行 / 13,002 字符 —— **没有**被当成"读不动的一行"。
- 全链路 `degraded=否`、`backend=direct|direct+html2text`；清单表格逐条可核。
- 模型在 T-05 第一轮就用 `sources.read {"ref":"hunt:4"}` / `hunt:5` / `hunt:6` 读到了正文
  （trace `seq 1-3`），**没有**再写 `download_hunt3.py`。

### 6.2 改后整跑读数（隔离副本，模型能力如实标注）

> ★ 说明：第一次在任务原目录 `_run` 上跑时，环境里有一个**更早的基线跑测进程仍在写同一个
> `_run`**（它读的是 Windows 侧旧快照路径）。两写者交错，T-06 起 `sources.read` 报
> `cannot read C:\...\_run\snapshots\<旧文件>`，块级失败。为避免把"两写者交错"误读成
> "research 不可用"，干净读数改在**隔离副本**（独立 `_run`、独立快照）跑。

**Qwen3-8B**（与基线同档；50 分钟上限被杀）：

```
8 个块跑完（T-01..T-08）；workspace 根下 .py = 0
模型通过 sources.read 直接读材料（hunt:4/5/6），并直接写 output/jd-matrix.md
块级 verdict 多为 failed(no_output)：8B 把 part 写到 output/ 而不是块声明的路径，
随后交不出最终答案；这是模型的路径/格式问题，不是"材料读不到"
```

**DeepSeek-V3**（更强模型，13 分钟后手动停止；同一份契约、同一副本结构）：

```
7 个块跑完：T-01 ok · T-02 ok · T-03 ok · T-04 ok · T-05 ok · T-06 failed(no_output) · T-07 ok
workspace 根下 .py = 0
sources.read 共 13 次，全部 ok，覆盖 hunt:1..hunt:7（T-01/T-02/T-03/T-04/T-05/T-07 的 trace 逐条可核）
产物：output/jd-matrix.md（1,544 B，7 行公司 × 6 列，来源标注 hunt:1..7）
★ 它卡在任务自己的结构判据「表格行 ≥ 8」上：模型没写表头/分隔行，只有 7 行数据；
  写块因此反复重写同一份文件（mtime 更新、内容不再增长），最终被我停止。
```

> ★★ 终局判据的读数：**改前 11 个 `.py`（6 补材料 + 5 生成产物）→ 改后 0 个**。
> 两个模型都**直接从 `sources.*` 读 `hunt:*`**，没有再写 `download_hunt3.py` /
> `process_hunt3.py`。整跑没拿到"四份产物全部通过"的原因写在 §7.9：
> 是弱模型的格式/路径与任务的结构判据，不是取数层断。

---

## 7. 明确未做 / 已知限制（不粉饰）

1. **搜索后端仍不可用**：Agent Reach 的「全网语义搜索」需要 Exa（未配置）。
   research 的入口仍是种子 + 正文扩链接；**没有伪造一个搜索通道**。
   ⇒ 「完全不给种子、只给问题」在当前部署里会走向具名弃权（`research_no_evidence`）。
2. **per-source `budget` / `allow_channels` 没有解析**：设计 §3.2 写了，但任务书
   同时要求「不改 `sources:` 的 schema」，本轮沿用 runner 级预算；契约里写这两个键
   仍会被静默忽略（§1③ 已列明）。
3. **登录态没有真正接 `EscalationPort`**：研究阶段没有「块」这个上下文，本轮把
   登录态变成 run 级具名拒（`research_needs_authorization`），由上层决定；没有自己抓。
4. **SSRF 守卫只挡字面量与已知内网域名**：DNS rebinding（公共域名解析到内网）
   仍可能绕过；要彻底挡需在连接层固定 IP，未做。
5. **多进程共享同一 `--snapshots` 目录**：两本台账的写已改成唯一临时名 + 原子 replace，
   冻结件也改成原子写并做内容复核；但「两进程同时跑同一契约」没有真并发复跑，
   只做了源码级加固（`os.getpid()` 唯一临时名 + digest 复核）。
6. **取数阶段不发 EventLog 事件**：网络挂住时只能看进程/快照目录，run 的事件流里
   没有「正在取第几条」。这是可观测性缺口，未在本轮补。
7. **没有真跑 agent-reach 的 GitHub/RSS/B站渠道**：它们要上游 CLI，且本任务的
   种子都是网页；本轮只把 doctor 读数报上来，没有假装接线。
8. **`HttpFetcher` 不填 `backend`**：普通 `locator: http://` 来源在 SOURCES.md 里
   后端列显示 `-`，与本地文件无法区分（既有行为，本轮未动；research 路径已填）。
9. **改后的整跑没有走到"四份产物全过"**：两个模型都在同一个写块上栽 ——
   Qwen3-8B 把 part 写到错目录、交不出最终答案；DeepSeek-V3 写出了带引用的
   `jd-matrix.md`，但没写表头/分隔行，撞任务自己的「表格行 ≥ 8」判据后反复重写。
   取数侧是通的（13 次 `sources.read` 全 ok，覆盖 hunt:1..7），卡点已定位到
   **模型格式**与**任务结构判据**，不是 ResearchPort。

---

## 8. 读数与改动文件

### 8.1 测试 / 门禁

```
PYTHONPATH=src:. ../.testvenv/bin/python -m pytest tests/ -q
    → 1581 passed（本轮最后一次全量；见 §8.3）
PYTHONPATH=src:. ../.testvenv/bin/python -m pytest -m arch_guard -q
    → 43 passed
PYTHONPATH=src:. ../.testvenv/bin/python -m pytest tests/test_ci_audits.py -q
    → 20 passed（default-audit 强制范围 + 棘轮）
../.testvenv/bin/python -m ruff check <本轮改动文件>
    → All checks passed
```

> 环境：本机 `bobanana6/.venv` 是坏的（无 `bin/python`），本轮用
> `../.testvenv/bin/python`（Python 3.12.3；补装了 pyyaml / jsonschema / pandas / ruff）。

### 8.2 改动文件清单

```
M  src/bobanana6/core/model.py            （Snapshot 注释；ResearchQuestion.channels 读者）
M  src/bobanana6/core/ports.py            （ResearchPort；已在前一提交）
M  src/bobanana6/contract/parser.py       （research：重复 ref / locator / seed 类型）
M  src/bobanana6/sources/__init__.py      （导出 research）
RM src/bobanana6/services/research.py → src/bobanana6/sources/research.py
                                          （薄路由 + 熔断 + 信任层 + 重放台账 + SSRF 守卫）
M  src/bobanana6/sources/store.py         （台账原子写 + backend/degraded + 内容复核）
M  src/bobanana6/assemble_impl/runner.py  （无依据拒跑 / 材料口径 / 部分失败 / 重置脏状态）
M  src/bobanana6/assemble_impl/produce.py （brief 的 source_trust）
M  src/bobanana6/report/builders.py       （SOURCES.md 后端/降级列 + 信任层图例）
M  src/bobanana6/cli.py                   （import 路径）
M  tests/test_research_retrieval.py       （改：无依据 = 具名弃权；import 路径）
A  tests/test_research_reliability.py     （22 条：五出口/信任层/重放/直连/SSRF/边界）
A  tests/test_research_probe.py           （探针自身纳入门禁）
A  tools/research_probe.py                （一条命令复现全部读数）
A  ResearchPort落地报告-20261020.md        （本文件）
```

### 8.3 提交

本轮代码 + 测试 + 工具 + 报告是**一个**提交（在 `bed3a25` 之上），message 写明
**ResearchPort + 熔断 + 信任分层 + 探针读数**；提交后 `git status --short` 为空
（§8.4 的读数在提交前跑完）。
★ 说明：`acceptance/ab/agent-jd-research` 的块级 artifact 提交（`[T-0x] verdict=...`）
是项目既有的「每块跑完 = 一个 commit」机制；本轮把一次被中断跑测留下的块级提交
压回成一个交付提交，提交后的**内容树与压回前逐字节相同**（树哈希对账过）。

### 8.4 最终读数时间点

```
pytest tests/ -q                → 1581 passed
pytest -m arch_guard -q         → 43 passed
pytest tests/test_ci_audits.py  → 20 passed
ruff check <changed files>      → All checks passed
tools/research_probe.py         → 5/5 PASS（exit 0）
```

---

## 附录 A：探针实际输出（逐字）

```
=== ResearchPort 探针（确定性 · 零模型 · 只连 127.0.0.1 假站）===

环境：agent-reach = Agent Reach v1.4.0；渠道 = 6/16；语义搜索 = [X]  全网语义搜索 — mcporter 已装但 Exa 未配置。运行：
      （搜索后端不可用 ⇒ 只量直连/备选两条路；不伪造一个搜索后端）

[1/5] 可复现性（红线）         PASS
        materials = 4
        run1_digests = ['6f734932ca45', '81e990395581', '485b2e67909b', 'dc2ab6497128']
        run2_digests = ['6f734932ca45', '81e990395581', '485b2e67909b', 'dc2ab6497128']
        byte_identical = True
        requests_run1 = 3
        requests_run2 = 0
        反证 · action = delete research.index.json + change site content
        反证 · requests = 3
        反证 · materials_changed = True

[2/5] 检索真的接上了          PASS
        materials = 3
        refs = ['hunt', 'hunt:1', 'hunt:2', 'hunt:3']
        each_material_has_citation = True
        visited = ['/interview', '/jd', '/seed']
        反证 · research_off_refusal = ['research_unavailable']
        反证 · no_evidence_refusal = ['research_no_evidence']

[3/5] 降级可见             PASS
        backend = jina
        degraded = True
        manifest_says_degraded = True
        sources_md_has_backend_and_degraded = True
        反证 · remove degraded flag = False

[4/5] 直连优先在省           PASS
        api = {'requests': 1, 'backend': 'api:pypi', 'value': '2.0.0', 'latency_ms': 0.7}
        search_then_fetch = {'requests': 2, 'backend': 'direct', 'value': '1.0.0', 'latency_ms': 19.3}
        saves_requests = 1
        反证 · force the api question through the search page = 2 requests vs 1
        反证 · numbers_agree = api=2.0.0 (fresh) / search=1.0.0 (stale)

[5/5] 信任分层             PASS
        tiers = {'https://huggingface.co/api/models/Qwen/Qwen3': 'T1', 'https://pypi.org/pypi/pyyaml/json': 'T1', 'https://arxiv.org/abs/2401.00001': 'T2', 'https://news.ycombinator.com/item?id=1': 'T3', 'https://some-random-blog.example/post/1': 'T4'}
        report_has_T1_and_T4 = True
        反证 · middle_hook_tries_to_promote_to_T1 = T4
        反证 · stayed_T4 = True

RESULT: 5/5 PASS
（JSON 尾部）
        },
        "report_has_T1_and_T4": true
      },
      "reverse": {
        "middle_hook_tries_to_promote_to_T1": "T4",
        "stayed_T4": true
      }
    }
  ],
  "passed": 5,
  "total": 5
}
```
