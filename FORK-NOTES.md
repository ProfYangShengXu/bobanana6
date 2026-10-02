# Hermes agent loop → bobanana6 worker 运行时 · Fork 笔记

> 来源：`github.com/NousResearch/hermes-agent`（**MIT 许可**，可 fork，署名即可）
> 方法：从 `agent/` 包筛出 29 个 loop 相关文件（39,948 行），6 个并行读者产出结构化索引后汇总
> 索引全文：`%LOCALAPPDATA%\Temp\hermes_extract.txt`（1121 行）｜源码副本：`D:\tmp\hermes-src\agent\`

---

## 0. 结论先讲

**四条核心结论：**

1. **没有 `agent.py`。** 是个 312 文件 / 6.3 MB 的包。但**主循环本体只有 1835 行**——复杂度不在循环，在「上下文会爆」（10,332 行的压缩子系统）和「各种错怎么恢复」（5,561 行）上。

2. **里面大半是 provider 怪癖处理，不是 loop 逻辑。** `conversation_loop.py` 里 `_is_copilot_provider` / `_nous_entitlement_message` / `_billing_block_dict` / `_CODEX_INCOMPLETE_NUDGE` 这类占了大半。**但剩下的那半是真金**——它是「我们还没踩过、但迟早会踩」的坑的现成答案。

3. **真正该抄的不是「怎么写循环」，是「循环里哪些地方必须有防御」。** 骨架是通用常识，值钱的是每条防御分支背后的真实事故编号。

4. **对 bobanana6 来说，绝大部分重型机制可以直接不抄**——worker 的上下文天然比单 agent 跑全任务小得多。最小可用内核约几百行。

---

## 1. 规模事实

```
agent/ 包          312 文件    6.3 MB    ≈ 15 万行
本次 fork 候选      29 文件   39,948 行
```

**按行数排序（前 10）：**

| 行数 | 文件 | 归属 |
|---|---|---|
| 5753 | context_compressor.py | 上下文压缩 |
| 4579 | conversation_compression.py | 上下文压缩 |
| 3624 | agent_runtime_helpers.py | 杂项工具 |
| 2510 | agent_init.py | 初始化 |
| 1929 | tool_executor.py | 工具执行 |
| 1925 | turn_recovery.py | 故障恢复 |
| **1835** | **conversation_loop.py** | **主循环本体** |
| 1831 | prompt_builder.py | 提示构造 |
| 1492 | moa_loop.py | MoA（删） |
| 1491 | error_classifier.py | 故障恢复 |

---

## 2. 四类决策

### A · 直接抄（通用资产，改动最小）

**主循环骨架**

| 资产 | 位置 | 为什么值 |
|---|---|---|
| `_LoopState` + `_run_phase` 的 phase/verdict 协议 | conversation_loop.py:1384-1498 | 每阶段返回 `action ∈ {fallthrough,break,continue,return}`。**直接映射：`return`=产出 Candidate，`break`=预算耗尽无产物，`continue`=再来一轮** |
| 主循环本体 | L1636-1682 | `while budget: begin→prepare→assemble→gate→retry→restart→normalize→tool_round\|text_finish` |
| `_run_api_retry_loop` | L1501-1530 | 单次请求的 重试/降级/中断 三段式分类 |
| 迭代记账 + restart 封顶 | turn_iteration_prep.py:333-542 | 「restart 必须被 `restart_count` 封顶，否则无限返还预算」 |
| `_continuation_overlap_length` / `_join_truncated_parts` | L264-304 | KMP 式流式截断续接去重。**零依赖，任何处理长输出截断的 worker 都要** |

**收尾与不变式**

| 资产 | 位置 | bobanana6 映射 |
|---|---|---|
| `_close_transcript_tail` 不变式 | turn_finalizer.py:238-283 | 「交付了 final_response ⇒ transcript 必有一条对应 assistant 行」→ **「宣称产出 Candidate ⇒ 必有可验证的产物记录」** |
| `_guarded_cleanup` | L114-121 | 每步独立 try、错误收集、响应永不丢 → **装配阶段不能被单个坏候选搞挂** |
| `_recover_final_from_stream` 的时序 | L223-235 | 恢复动作必须在**所有可能抛异常的步骤之前** |
| `_resolve_budget_fallback` | L124-193 | **预算耗尽时优先交回被 verification gate 扣下的候选**，而不是再打一次可能失败的调用 |

**工程细节（小但实用）**

| 资产 | 位置 |
|---|---|
| `_clone_message_for_send` 结构克隆（非浅拷贝） | conversation_loop.py:1022-1030 |
| `_canonicalize_tool_call_arguments`：纯函数 memo + 条数/字节双上界 | L1001-1019 |
| `_invalid_tool_name_error_content`：**空工具名不倒目录**（模型在回声它见过的语法，喂目录正是加剧循环） | L1056-1067 |
| `_ToolTimeoutResult` / `_ToolCancelledResult` 用 **str 子类**做标记 | tool_executor.py:475/479 |

**工具执行（本项目最该抄的一块）**

| 资产 | 位置 | 为什么 |
|---|---|---|
| `_parse_tool_arguments` 严格解析、**坏参数绝不修复**、产错误结果让模型自纠 | tf:169-180 | 替模型猜 JSON 会掩盖真实意图 + 产生副作用 |
| `_blocked_tool_result`：被拒的调用**也合成一条合法 tool 结果** | tf:653 | 杜绝悬空 tool_call（否则下一轮直接被 provider 拒） |
| `_commit_tool_result` 的 `observe→persist→append→flush` 骨架 | tf:1054 | 可整体保留，换掉 observe/flush 实现 |
| `ToolCallGuardrailController` | gr:307 | 纯逻辑；**注意 `reset_for_turn` 必须每轮调**，否则跨轮误判 |
| `_plan_tool_batch_segments` 路径重叠准入的并发规划器 | dh:198 | 读读可并发、写写必须串行 |

**故障归因**

| 资产 | 位置 | 为什么 |
|---|---|---|
| `FailoverReason` 枚举 + `ClassifiedError` + 规则表常量 | error_classifier.py:34/70/96-640 | **纯函数/纯数据，零 agent 依赖** |
| `TurnRetryState` 一次性修复 flag 模式 | turn_retry_state.py | 每个恢复分支一个 `_xxx_retry_attempted`，**天然防重复修复** |
| 「优先级流水线 + verdict 字典 + `_first_match`」结构 | error_classifier.py | 直接照搬 |

**上下文压缩的最小内核（约几百行，其余全是净负债）**

```
_estimate_msg_budget_tokens
  → 逐工具结果字符上限截断器
  → _sanitize_tool_pairs（保调用链）
  → threshold = (ctx - max_out) * pct
  → 超阈值时「丢最老的非保护段 + 可选一次 LLM 摘要」
  → 保证结果严格更小
  → 把当前任务 instruction 钉在末尾
```

**提示构造**

| 资产 | 位置 | 为什么 |
|---|---|---|
| 三层 tier（stable/context/volatile）+ cache 策略 | system_prompt.py:734/812/833 | 最值钱的部分 |
| `message_sanitization.py` 整套 | 705 行 | 线级卫生与业务无关：surrogate / 非法 JSON 修复 / tool id 确定性 / 图像剥离。**独立可搬** |
| 「工具存在才注入指引」的门控 | prompt_builder.py:274 | 防悬空引用诱发幻觉调用 |
| per-turn 信息走 user 轮不走 system prompt | prompt_builder.py:570 等 | 见 §4 |

**验证与权限**

| 资产 | 位置 | 说明 |
|---|---|---|
| `verification_evidence.py` 的 5 条 | 590 行 | 见 §3，**对本项目价值最高** |
| `file_safety.py` 的白名单模式 | 516 行 | `get_safe_write_roots` / realpath 前缀比较 / symlink entry 双判 |

---

### B · 保留形状、换掉内容

| 目标 | 换什么 |
|---|---|
| `build_turn_context` prologue | 换成 `(子任务描述, 约束包, 共享语义层)` → 消息、`工具集` → `tools[]`、`预算` → 迭代+墙钟。`_reset_per_turn_agent_state` / `_bind_turn_identity` 形状可留，字段换成 job_id/subtask_id/attempt_id |
| `finalize_turn` 返回 dict | → Candidate：`{candidate_payload, messages, api_calls, completed/failed/interrupted, exit_reason, partial}`，去掉 session token/cost/service_tier |
| restart 标志 | bobanana6 无交互式 steer/interrupt，可砍到 2 个（压缩后重发、降级后重发），但**保留 restart_count 封顶** |
| `finalize_turn` 的 `completed` 判定 | 从「final_response 非空且未超迭代」改成「**产物是否通过契约校验**」 |
| 工具名表 `_TOOL_RESULT_SUMMARIZERS` | 换成本项目的工具名（最便宜的适配点） |
| `SUMMARY_PREFIX` | 重写「谁是唯一真源」（现文案绑死 MEMORY.md/skill） |

---

### C · 直接删

**计费/权益**：conversation_loop.py:486-643 整段（entitlement / billing_block / free_tier / Nous 路由）
**产品功能**：MoA 全套 ｜ Bot Chat ｜ Kanban ｜ 会话自动标题 ｜ 情感反应回调
**展示层**：`announce_api_call` 的 spinner、`KawaiiSpinner`、`_vprint`、emoji、`_log_turn_exit`、`_explain_abnormal_exit`、`export_current_turn_boundary`
**provider 怪癖**：Ollama 上下文特判 ｜ Copilot 凭证恢复 ｜ 本地窗口自增 ｜ Nous key 收养 ｜ codex_app_server 整段
**平台适配**：surface_switch ｜ 网关 notes ｜ MCP 工具刷新 ｜ ContextEngine hook ｜ 生命周期插件 hook
**memory/skill 评审体系**（但 `is_trivial_prompt` 跳过 prefetch 的思路值得抄进语义层检索）
**上下文压缩里的重型机制**（对小块子任务全是净负债）：
- micro-compaction（默认关，每趟破坏 prefix cache）
- proactive prune（默认关）
- idle compaction（worker 不按 wall-clock 等）
- **整套会话轮转 / SessionDB lineage / orphan 恢复 / watermark / durable 锁**
- commit fence + 四路守护线程池 + admission 限流（除非 worker 会挂在不可中断的同步调用上）
- anti-thrash 全套 + durable 冷却阶梯 + telemetry

**`turn_failure_copy.py` 几乎整文件**（中文社交文案 + `hermes doctor` 指引），只保留「把结构化 verdict 写回结果对象」这一条。

---

### D · 必须自己补（Hermes 没有或不够）

| 缺口 | 为什么必须补 |
|---|---|
| **阻断式完成门** | Hermes 的 `verification_evidence` 是**被动账本**（自陈「从不阻断完成」）。bobanana6 的「agent 自我报告不可信」要求把 `verification_status` 接成**硬闸**：`status==passed` 且 `!stale` 且 scope 覆盖声明的验收项 |
| **错误 → 三决策映射** | 见 §3 末尾。这是本项目的核心诉求 |
| **`AgentRuntimePort` 适配层** | 抽一个 `AgentIO` 协议替掉那个上百属性的 agent 对象（方法清单见索引 task-2 §3） |
| **语义层的自洽检查** | 起草出来的本体要查：循环定义 / 单位冲突 / 别名重叠 |

---

## 3. 最值钱的 20 条「容易想不到的坑」

**这些是 fork 的全部价值所在**——每条背后都有一个真实事故编号。

### 顺序与时序

1. **顺序即正确性。** `build_turn_context` 必须「系统提示 → session 行 → 压缩」：反了要么持久化出 NULL 提示，要么压缩的外键引用失败。`init_agent` 同理，docstring 明说「**Phase ORDER is load-bearing**」。
2. **api_mode 一变必须清 transport cache**——先 warm 再改会留下 stale transport。
3. **prompt cache 决策必须在建 client 之前**（影响 header 策略）。
4. **压缩/裁剪的阈值必须先扣掉输出预留**：`threshold = (context_length - max_tokens) * pct`，否则大 `max_tokens` 下会先撞 provider 400 再压缩。

### 索引与坐标

5. **★索引在列表被改写后一律不可信。** 压缩或角色交替修复会替换 dict、插入行 → 陈旧索引会让重放前缀**在本轮 tool 行中间裂开**，孤立的 tool result 被静默丢掉。**用内容指纹或稳定 id 定位，不要用索引。** ← bobanana6 的装配器按位置拼接子任务产物时，这个坑会以另一种形式复现。

### 失败与边界

6. **★「失败」也是一种必须被记录的状态。** `_close_durable_failed_turn` 与 `_close_transcript_tail` 是同一原则的两面——否则下一轮会把旧的未回答输入重放一遍。**装配器必须能区分「候选失败了」和「候选从没跑过」。**
7. **★单点收口。** surrogate 清洗、tail 关闭、scaffolding 丢弃都刻意放在「唯一必经之处」。判定内核应同样收敛成一个 choke point，而不是每条分支各写一遍。
8. **★只有 provider 上报的真实用量才能重置熔断器，估算不行。** rough 估算对非 ASCII 文本低估最多 ~2×，会把熔断器误重置成死循环。
9. **压缩「成功」不等于有进展。** 判定要看**行数变少 或 token 降幅 >5%**——只看行数会对「字节减少但行数不变」误报。
10. **压缩结果不得比原来大。** 观测过 379K→687K。提交点必须比对并**拒绝提交**。
11. **压缩后结果里必须有当前任务目标**，否则模型会读成「无事可做」→ 静默 no-op。

### 工具与角色

12. **★role 交替是硬约束。** 任何跳过（中断/超时/拒绝）都必须为每个 tool_call 补一条 tool 结果，否则下一轮请求**直接被 provider 拒**。
13. **★并发结果必须按原始调用顺序回填**——先并发执行、后按序 append，这个分离不能丢。
14. **工具输出过大不是丢弃，是落盘 + 给路径。** 单条封顶 + 整轮聚合封顶两层；模型需要时可自己取回。
15. **工具结果被上游省略时要主动提醒。** 扫描 `...N more items` / `has_more:true` 等标记，追加「数据不完整」——避免模型把截断切片当全集。
16. **死循环检测要抓周期环，不能只看连续相同。** 单调用 streak 会被交替调用重置，所以要额外检测 A,B,A,B 模式。

### 验证

17. **★exit code 归因规则。** 只有 `;` 序列的最后一节能代表自己的状态；**管道 `|`、`||`、后台 `&` 会隐藏状态**；`&&` 链只在成功时证明各成员。这是防「用一个 exit 0 冒充验证通过」的关键。
18. **★验证证据会过期。** 「改完代码后旧测试结果不算数」是**显式的时间戳比较**，不是靠 agent 记得。
19. **★「没证据」和「证据过期」是两个不同状态**（`unverified` vs `stale`）——调用方据此区分「根本没验」和「验过但又改了」。
20. **临时脚本不得 shadow 真套件。** ad-hoc 验证只在该项目**完全没有** canonical 命令时才启用，且一律 scope=targeted，不得升格为 full。

### 另外几条（同样高频踩）

- **符号链接要按「条目名」再判一次**：要禁的是那个名字，不是它解析到的目标。只查目标会漏掉「用指向敏感的链接名做删除/改名」。
- **Windows 上「读」一个 `\\?\UNC\host\share\x` 就会泄漏 NTLM hash**，且必须在 resolve **之前**、在 RAW 字符串上判。
- **幂等标记要挂在 dict 上，不能用 `id()`**——CPython 地址会被复用，误判为已写。
- **无人值守 worker 不能弹交互审批**，必须 fail-closed deny，否则与父进程死锁。
- **prefetch 必须带超时**，一个卡死的 provider 不能拖死整轮。

---

## 4. ★错误 → 三决策映射（本项目的核心诉求）

bobanana6 需要「子任务失败要能精确归因 → 决定局部重试 / 改切分 / 改约束」。
在 `ClassifiedError` 之上加一层 `bobanana_action` 枚举即可：

```
RETRY_LOCAL      rate_limit / overloaded / server_error / timeout / upstream_rate_limit
                 → 同 worker 退避重试
                 一次修复类（should_compress / 图片 / 格式剥离）→ 修好原地重试

RESHARD_TASK     context_overflow / payload_too_large / long_context_tier
                 truncated(finish_reason=length，输出装不下)
                 repetition / thinking_exhausted（对模型太难）
                 → 【回传到切分层】：块太大或太难，要拆更小

FIX_CONSTRAINT   format_error / role_alternation / 空响应 / 消息体非法
                 multimodal_tool_content_unsupported / llama_cpp_grammar
                 → 【请求形状或协议约束错了】：改 prompt/schema/消息构造，
                   重试和换源都没用

SWITCH_SOURCE    model_not_found / model_entitlement / provider_policy_blocked
                 content_policy_blocked / upstream_blocked / ssl_cert / auth_permanent
                 → 换 worker/模型/provider

ABORT            解释器关闭 / billing(402) / auth_permanent
                 content_policy_blocked（同 prompt 确定性）
                 → 终止且不重试
```

**六条判读纪律：**

1. **计分尺子分场景**：413 用**字节**、token 溢出用 token/消息数。用错尺子会永远报「无进展」并卡死。
2. **分类优先级是 load-bearing**：413 必须排在通用 4xx 之前；context-length（含被包成 429 的输出上限错）必须排在非重试客户端错之前。
3. **判「无进展」要看多种度量**（消息数、token、字节）——同消息数压缩能减体积不减数组。
4. **不要为「内容确定性」的错重试同一 prompt**（content_policy_blocked、malformed tool args）。
5. **每个一次性修复分支要配一个 flag**，否则重复修复或死循环。
6. **HTTP-200 的软失败必须能被分类器读到**（空 body / Codex status=failed），否则它既不重试也不换源。

---

## 5. ★三层信息进提示还是做工具？（这次调研最重要的结论）

**结论：三层里没有任何一层适合塞进稳定 system prompt。**

Hermes 自己的三个先例：

**先例一 · 协议 vs 载荷分离（最强）**
Kanban 的「怎么做」协议进 stable 提示（字节稳定），「这次任务是什么」的**载荷**通过工具返回，并把返回当 ground truth。
→ **协议进提示，契约进工具。**

**先例二 · per-turn 信息走 user 轮**
记忆 prefetch 明确 stamped 进 user 行的 `api_content`，steer 也走「模型可见消息」。
原因：system prompt 要字节稳定、要可缓存。**per-task/per-invocation 的内容塞进去会 (a) 每次重建——正是「被长上下文稀释」的成因；(b) 破坏 prefix cache。**

**先例三 · 能力边界 vs 可发现性**
Hermes 唯一放进提示的「外部信息」是 skills **索引**（名字 + 一行描述），**内容**必须用工具取；且极度小心悬空引用——没有该工具就整块不注入。

**具体落位：**

| 层 | 落位 |
|---|---|
| **共享语义层** | 工具（`query_semantic_layer`）+ stable 提示**一行指针**。仅当它是极小极稳的核心定义时才可进 stable tier；一旦会变/会涨就撤回工具 |
| **任务契约** | 工具（`get_task_contract`）返回结构化契约；提示里只留「如何消费契约」的协议块 |
| **子任务目标** | **不进 system prompt**，作为该次调用的任务消息或工具结果下发 |

**这直接验证了 bobanana6 架构文档 §5.8「联通靠工具不靠提示词」的设计。**

---

## 6. 迁移顺序

```
1. verification_evidence 的 5 条（低依赖、价值最高）
   → 并【自己补上阻断式完成门】
2. file_safety 白名单模式 → 接到 worker 权限边界
   （真隔离交给 OS/容器，它自陈不是安全边界）
3. agent_runtime_helpers 的消息修复/清洗几件套
   + session_persistence 的幂等去重
4. error_classifier 的纯函数规则表 + §4 的三决策映射
5. 工具执行三件套（严格解析 / 合成拒绝 / commit 骨架）
6. 主循环骨架 + _LoopState/verdict 协议
7. 上下文压缩最小内核（几百行，其余不抄）
8. agent_init 只抄【顺序纪律 + 状态簇骨架】
9. prompt_builder 的三层 tier + 工具门控
```

---

## 7. 索引全文位置

每份原始索引（含完整函数表、压缩策略全表、错误分类表）在：

```
%LOCALAPPDATA%\hermes\cache\delegation\subagent-summary-{0..5}-20261002_001348_*.txt
   0 主循环      34,940 字
   1 工具执行    13,947 字
   2 故障恢复    29,969 字
   3 上下文压缩  25,577 字
   4 提示构造    18,600 字
   5 初始化与验证 31,320 字

汇总提取（按主题）：
   %LOCALAPPDATA%\Temp\hermes_extract.txt   （1121 行）

源码副本（29 文件）：
   D:\tmp\hermes-src\agent\
```

---

## 8. 授权与署名

Hermes Agent 是 **MIT 许可**（`NousResearch/hermes-agent`，LICENSE 文件已确认）。
MIT 允许 fork、修改、商用，**要求保留版权声明和许可文本**。

**fork 时要做的事**：把上游 LICENSE 和版权声明带进 bobanana6，并在 README 里注明派生自 Hermes Agent。
