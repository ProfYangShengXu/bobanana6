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

**C · 直接删（不抄，附理由）**

- **provider 怪癖**（Copilot/Nous/Ollama/codex_app_server 特判、entitlement 文案、
  billing 文案表）——那是 provider 形态问题，换 provider 就过期，且 core 要环境无关。
- **压缩子系统**（`context_compressor.py` 5753 行）——worker 的上下文天然比单 agent 小，
  FORK-NOTES §0 结论：整块不抄，最小内核也留到第 21 步再说。
- **会话轮转 / SessionDB lineage / orphan 恢复 / watermark / durable 锁**——worker 不跨进程
  长时间驻留，这些机制是净负债。
- **展示层**（spinner / emoji / vprint）与**计费权益**——与判定无关。
- **`TurnRetryState` 的一次性修复 flag 模式**——本块没有恢复分支，第 21 步再抄。

**D · 自己补**

- **`attribution.py` 整块**：Hermes 只有错误分类，没有「该怪谁」。四出口 + 分离实验
  （上下文原样交给正常人）是本项目 §7.8 的缺口。

## 架构纪律

- `agentloop/` 只依赖标准库；实现 `core/ports.py` 的端口，但**不 import `adapters/`
  `sources/` `cli`**，也不 import `core/` 的具体判定类（`kernel` / `checkers` / `split` …）。
  由 `tests/test_arch_guard.py` 的 `test_agentloop_*` 守着。
- 判定语义永远在 `core/kernel.py`；本目录不复制任何判定。

## 许可

上游 Hermes Agent 为 MIT：fork/修改/商用均允许，要求保留版权声明与许可文本。
本目录按规则重写而非逐字复制，仍按派生作品对待。

> **待补**：`agentloop/LICENSE.hermes`（上游 LICENSE 全文 + 版权行）尚未随素材提供，
> 需在接入上游代码前补齐；`aGent.md §1.6` 要求它必须存在。
