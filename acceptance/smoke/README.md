# smoke/ —— 3 个小任务 + 基线容差表（`实现规格.md §14.6`）

**判据**：块级通过率**不低于基线容差**，用**通过数对比容差**，
**不用 reward 二值** —— 那会把「能力波动」误诊成「架构回归」（`架构设计.md §15.3`）。

## `baseline.json` 的形状与来源

```json
{"<task>": {"min_pass_count": <int>, "baseline_run": "<哪一批>"}}
```

**容差怎么定**：拿**同窗**连跑 5 次的通过数分布，**取下界（不是平均）**。
单次定基线会把噪声写进门槛。

## ★ 现在的数值是真模型的档（批次 `real-qwen3.5-4b-5x-20261004`）

真模型 `Qwen/Qwen3.5-4B`（`enable_thinking: false`），3 道题各连跑 5 次，**同窗**：

| 题 | 逐次（通过 ✅ / 失败 ❌） | 通过数=基线（下界） | 每次耗时（秒） |
|---|---|---|---|
| `smoke-panel-one-source` | ❌ ✅ ✅ ❌ ❌ | **2/5** | 44.3 · 19.4 · 76.9 · **211.9** · **221.6** |
| `smoke-panel-tighter-bound` | ❌ ✅ ✅ ✅ ✅ | **4/5** | 11.5 · 22.7 · 8.9 · 9.6 · 10.0 |
| `smoke-panel-two-deliverables` | ✅ ✅ ✅ ❌ ✅ | **4/5** | 18.1 · 7.6 · 7.7 · **208.2** · 25.2 |

（一次跑算「通过」= `assembly_passed and block_pass_rate == 1.0`；逐次真值见
`.bobanana6/b23/smoke-real-readings.json`。加粗的是失败的那几次。）

**为什么取下界**：基线是**门槛**，不是成绩单。拿平均（≈2.7）当门槛，会让
"这次比平均差一点" 与 "能力真的掉了一档" 分不开；取 5 次里**最差的那一档**，
门槛才只在**真的更差**时才响。★ 也**不因为某一次跑得好就抬高** —— 那一批已经跑完，
分布就那一批。

★ **门槛因此比离线档松了（5 → 2/4/4），这是真读数**：离线档的 5/5 是**退化分布**
（确定性替身每次都一样），它验的是门禁算式与加载器，不是模型能力。真模型下：

```
失败两种形态（取自最后一次失败 run 的 summary.json，逐字）：
  ① 端点超时：produce: LLMConnectionError: model call did not reach the server:
     The read operation timed out            ← 三次失败都在 ~210s 那一档
  ② 判定器抓错值：C-prov coverage 1.0000 reaches 0.95, but the citations do not replay;
     row=0 col=amount value '100.0' does not match snapshot 'src-1' row 0: '1200000'
     ← 机制在正常工作（模型写错的值被溯源复核抓住），不是回归
```

冒烟题本体由 `acceptance/smoke/generate.py` 生成（1 源 / 2 源 / 按字段切各一道）。
`tests/test_acceptance.py::test_smoke_tasks_are_small_and_offline_runnable` 仍然用
**同一张表**跑离线档（离线 5/5 ≥ 任何下界），它守的是门禁算式；真模型档由上面那一批定。
