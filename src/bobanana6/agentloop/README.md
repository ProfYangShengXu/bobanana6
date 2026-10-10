# agentloop/ —— 派生自 Hermes 的 worker 运行时

> **派生来源**：`github.com/NousResearch/hermes-agent`（**MIT 许可**）。
> 本目录是**按规则重写**（re-implementation of shape/rules），不是逐字复制：
> 只保留 bobanana6 需要的判定与防御，provider 专属分支一律不抄。
> 依据：`FORK-NOTES.md` 的四类决策 + `实现规格.md §10` 第 15–18 步。

## 这一块包含什么

| 文件 | 对应 Hermes | 步 |
|---|---|---|
| `errors.py` | `agent/error_classifier.py` | 15 |
| `events.py` | `conversation_loop.py` 的 turn/事件记录 | 16 |
| `metrics.py` | 散落在 `agent_runtime_helpers.py` / 观测钩子里的计数 | 16 |
| `state.py` | `session_persistence.py` 的幂等落盘 + turn 边界 | 17 |
| `attribution.py` | **Hermes 没有**，本项目自补（FORK-NOTES §D） | 18 |
| `concurrency.py` | `tool_dispatch_helpers.py` 的并发规划 + 端点限流 | 19 |
| `sandbox.py` | `file_safety.py` 的边界思路（**执行级**隔离是本项目改的） | 19 |
| `gate.py` | `verification_evidence.py` 的五条 | 20 |
| `tools.py` | `tool_executor.py`（严格解析/合成拒绝）+ `tool_guardrails.py` + `_plan_tool_batch_segments` | 21 |
| `compress.py` | `context_compressor.py` 的**最小内核**（其余整块不抄） | 21 |
| `recovery.py` | `turn_recovery.py` 的三级出口 + taint | 21 |
| `loop.py` | `conversation_loop.py` / `turn_finalizer.py` 的 phase/verdict + 收尾不变式 | 21 |
| `text_tool_calls.py` | **Hermes 没有**，本项目自补：识别"工具调用写成了正文文本"的方言（只识别、不执行） | — |

## 四类决策（对着 `FORK-NOTES.md §2/§3/§4`）

**A · 直接抄（保留形状）**

- `error_classifier` 的**优先级流水线**结构：有序 stage，`_first_match` 首个命中即返回。
- 两条判读纪律原样落地：
  1. **计分尺子分场景** —— 413 用字节，token 溢出用 token/消息数，两者不许合并
     （`errors.PROGRESS_RULER`）。
  2. **分类优先级 load-bearing** —— 413 排在通用 4xx 之前；context-length（含被包成
     429/5xx 的输出上限错）排在前；`errors._by_status` 的分支顺序就是这条纪律。
- 事件流的「结构化字段不是散文」口径（`events.py`、`metrics.py`）。
- `_close_transcript_tail` 那条不变式 → 「失败 turn 也必须落盘成合法状态」（`state.save_turn`）。
- 「内容指纹/稳定 id 定位，不用索引」→ `state.messages_digest`。

**B · 换内容**

- `FailoverReason` 收窄为 `实现规格 §5.8` 表里的 22 个 reason，映射到 `BobananaAction`。
- Hermes 的恢复提示（`should_rotate_credential` / `should_fallback` / tier 路由）换成
  `retryable` / `should_compress` 两个本项目决策层真正会读的字段。
- `Checkpoint` 换成 `task_id / worker_id / turn / messages_digest / done_actions / status`。
- **`verification_evidence` 从「被动账本」接成硬闸**（`gate.py`）：Hermes 自陈「从不阻断完成」，
  本项目要求 `passed` 且 scope 覆盖才能完成 —— 这是 §7.3「自我报告不可信」的落地。
- 压缩只保留**最小内核**并加了「不缩小就拒绝提交 + 可恢复」两条本项目要求。

**C · 直接删（不抄，附理由）**

- **provider 怪癖**（Copilot/Nous/Ollama/codex_app_server 特判、entitlement 文案、
  billing 文案表）——那是 provider 形态问题，换 provider 就过期，且 core 要环境无关。
- **压缩子系统里的重型件**（`context_compressor.py` 5753 行）：micro-compaction /
  proactive prune / idle compaction / anti-thrash / telemetry / 会话轮转 /
  SessionDB lineage / orphan 恢复 / watermark / durable 锁 / commit fence +
  四路守护线程池 / admission 限流 —— worker 的上下文天然比单 agent 小，整块净负债。
- **展示层**（spinner / emoji / vprint）与**计费权益**——与判定无关。
- **`TurnRetryState` 的一次性修复 flag 模式**——本项目的恢复走 `recovery.py` 的
  重试/反思/降级三段，不需要每个分支配一个 flag。

**D · 自己补**

- **`attribution.py` 整块**：Hermes 只有错误分类，没有「该怪谁」。四出口 + 分离实验
  （上下文原样交给正常人）是本项目 §7.8 的缺口。
- **`gate.py` 的 `declined` 第三出口**：Hermes 的 gate 是二分，本项目把它改成三分，
  且拒答不触发重试（`should_retry` 对 declined 返回 False）。
- **`sandbox.py` 的执行级隔离 + fail-closed**：Hermes 的 `file_safety` 是路径级边界；
  并发 worker 互相踩工作目录是另一回事，且隔离落不下来时**拒绝执行**而不是静默放开。

## 架构纪律

- `agentloop/` 只依赖标准库；实现 `core/ports.py` 的端口，但**不 import `adapters/`
  `sources/` `cli`**，也不 import `core/` 的具体判定类（`kernel` / `checkers` / `split` …）。
  由 `tests/test_arch_guard.py` 的 `test_agentloop_*` 守着。
- 判定语义永远在 `core/kernel.py`；本目录不复制任何判定。

## ★ 接线点清单（步骤 13 的勾选结果）

> 来源：步骤 21 交付报告里的「哪些出口没走到」，把其中 **A 类（等装配点）** 显式落盘。
> 为什么写进仓库：`架构设计 §6.9` ——「声明了但没人读」**永远不报错**，只把归因引向错误方向。
> 散在聊天报告里，步骤 13 一定会漏；写在这里才能逐条勾掉。
> 格式：`[x] <缺什么> —— 接线点：<文件/函数>` / `[ ] <缺什么> —— 不接的理由：<一句话>`

- [x] `WorkerLoop` 不是 `AgentRuntimePort` 的实现 —— 接线点：`assemble_impl/runtime.py::BlockRuntime`。
  它把 `(subtask, bundle, ontology, tools, budget)` 映射成 loop 的 `run(task_id, worker_id, ...)`，
  并把「没有候选」拆成两种具名结果：`BlockDeclined`（第三种合法出口）与 `BlockRuntimeError`。
  由 `tests/test_runner.py::test_block_runtime_satisfies_the_agent_runtime_port` 守着。
- [x] gate 的证据不由 loop 产生 —— 接线点：`services/checks.py::shell_command_check` + `BlockJudge.verify`。
  `custom` 约束提供命令（**契约是人写的，不是模型生成的代码**），执行后走
  `gate.record_evidence` → `gate.evaluate`，`attribute_exit` 保证管道/后台命令的退出码不算数。
  **边界**：只有声明了 `custom` 检查的块才有命令证据；其余块的闸门是 `core/kernel.admit`
  （它同样不看 worker 的自我报告，这正是 §7.3 要的独立性）。
- [x] `gate` 的 `declined` 出口没有真实生产者 —— 接线点：`assemble_impl/produce.py::_declined`。
  模型回答 `{"declined": true, "reason": "..."}` 时，产出 `TurnProposal(declined=True)`，
  loop 据此构造 `GateVerdict(status="declined")`；`tests/test_runner.py::test_a_declined_block_is_reported_as_declined_not_failed`
  证明它一路走到报告里是「declined」而不是「failed」。
- [x] `compress` 的 LLM 摘要回调从未被 loop 传入 —— 接线点：`WorkerLoop(summarize=...)`
  （`loop._prepare` 把它交给 `compact`）+ `assemble_impl/runtime.py::llm_summarizer`，
  由 `cli.py::build_app` 注入。摘要调用是第二条打模型的路径，因此它也走 `RuntimeGate`。
- [x] `ToolCall.effect/targets` 是调用方自报、没有核对机制 —— 接线点：`services/registry.py`。
  `RegisteredTool` 声明 effect 与 target 参数，`ToolRegistry.calls` 负责填；
  `tests/test_services.py::test_write_calls_become_a_barrier_because_the_registry_says_so`
  证明模型把写谎报成读也拦不住屏障。
- [ ] `join_truncated_parts` / `continuation_overlap_length` 无调用方 —— **不接的理由：**
  loop 里根本不存在流式文本路径（`TurnProposal.text` 是一次到齐的整段），
  接上它等于先实现流式组装，那是**新机制**不是接线；本块范围外。
  ★ `#41` 复核并加强：截断现在会被**分类**（`truncated` = 撞到我们自己的输出上限 /
  `repetition` = 模型在转圈，见 `produce.py::repetition_dominated`），而这**两条都不该"接着写"**：
  重复主导时接着续只会继续重复；真·额度不够时 `REASON_ACTION` 给的出口是 `reshard_task`
  （块交回切分器），不是同一个块续写。要让某个块"接着写"，得先把出口语义改成续写优先 ——
  那是判定语义的改动，不是接线。
- [ ] `PROGRESS_RULER` 无读者 —— **不接的理由：** 需要「无进展检测」（跨 turn 比较候选指纹），
  属于新机制。当前 loop 用 `max_turns` / `max_restarts` / `max_retries` 封顶，不会空转到预算耗尽；
  但确实没有用正确的尺子判「有没有进展」（`AGENTS §7⑤`）。这是**已知缺口**。
  ★ `#41` 复核：`truncated` / `repetition` 现在**一分类就终止本块**（`reshard_task` 出口），
  不存在"同一块反复重试到耗尽"的场景，尺子在这两条上没有用武之地；其余失败仍靠 `max_*` 封顶，
  缺口照旧记着。另外 `REPETITION` **故意不进表** —— 它说的是"模型在转圈"，与尺寸无关，
  `progress_ruler(REPETITION)` 返回 `"none"` 正是对的。
- [x] `attribution.BLAME_ACTIONS` 无读者 —— 接线点：`assemble_impl/runner.py::Runner._attribute`。
  失败时产出 `Attribution`，再用 `actions_for(blame)` 把结论转成动作，写进
  `summary.json` 与 `CONFLICTS.md`；归不出来就写 `blame=null, confidence=unknown`。
- [x] `RuntimeGate` 的 `acquire` / `single_flight` / `route` —— 现在两条模型调用路径都走它：
  loop 的 `_call_model`（块执行）与 `llm_summarizer`（压缩摘要）。
- [x] `done_actions` 已接：`loop._persist` 传本 turn 已完成动作的稳定 id（`tool:<call_id>`），
  `state.resume` 的「不重放已完成动作」因此在 loop 侧成立。

**设计决定（不是待办）**：`sandbox.py` 提供的是**执行级目录隔离**，不是 OS 级隔离（无 namespace/seccomp/权限位）；真隔离交给容器/OS。同 UID 的 worker 仍能读对方目录 —— 这是有意为之，不要当成待办去补。

**接线时新发现的三处**（都不是 agentloop 的问题，是装配口径问题）：

1. `CheckpointStore` 只按 `task_id` 索引，一个任务的多个块会互相覆盖 —— 装配侧用
   `assemble_impl/runtime.py::PerWorkerCheckpointStore` 按 `(task_id, worker_id)` 收口。
2. `kernel.admit` 是「一个候选对一个包」的判定；把整份 plan 的包递给单块会拿邻块的
   `AUTO-*` 约束去判它 —— 装配侧用 `assemble_impl/runtime.py::block_bundle` 先切片。
3. `agentloop/metrics.py::tool_metrics` 按 `task_id` 归并「任务成功率」，一个任务的多个块
   只算一个样本 —— 这是运行时既有的口径，装配侧不改它，只在报告里注明。

## 许可

上游 Hermes Agent 为 MIT：fork/修改/商用均允许，要求保留版权声明与许可文本。
本目录按规则重写而非逐字复制，仍按派生作品对待。

`LICENSE.hermes` 已随附（上游 `NousResearch/hermes-agent` 的 MIT 全文，版权行
`Copyright (c) 2025 Nous Research`，原样照搬），满足 `AGENTS.md §1.6`。
