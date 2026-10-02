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

## 许可

上游 Hermes Agent 为 MIT：fork/修改/商用均允许，要求保留版权声明与许可文本。
本目录按规则重写而非逐字复制，仍按派生作品对待。

> **待补**：`agentloop/LICENSE.hermes`（上游 LICENSE 全文 + 版权行）尚未随素材提供，
> 需在接入上游代码前补齐；`AGENTS.md §1.6` 要求它必须存在。
