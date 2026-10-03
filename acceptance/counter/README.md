# counter/ —— 反例集：**不该拆的任务必须被拒绝**（`架构设计.md §15.8`）

构造：三方在**同一份状态机**上做增量式互相依赖的推理 —— 每一步依赖上一步的结果，
三方频繁共享中间状态。这类任务拆开就是 `fake parallelism`。

**★ 判据刻意不看输出质量**：切了也能做对只能说明这次运气好。

## ★★ 先报一件事：`refuse_split` / `single_agent` 这两个出口**在代码里不存在**

`实现规格.md §14.4` 的判据写的是：

```python
plan = plan_for("counter/state-machine-task")
assert plan.decision == "refuse_split"
assert plan.fallback == "single_agent"
```

我逐个查过：

```
decompose/strategies.py    只有 source / field / single 三种策略；唯一「不扇出」的
                           情形是来源数 <2 时回落到 split_single —— 那是【数量不足】，
                           不是【判定不该切】
core/split.py              11 条合法性检查全是「这个计划合不合法」，没有「该不该切」
                           （SplitReport 只有 violations / checks_run）
core/model.py::SplitPlan   没有 decision / fallback 字段（只有 shape / round / version）
core/model.py::ConvergeDecision  五个出口里没有 refuse_split
agentloop/recovery.py      RecoveryAction.DEGRADE_SINGLE_AGENT 存在，但那是【运行时】
                           因为「块不可独立验证」而降级，不是切分器的拒绝决策
```

**按「不在就报上来、别自己发明出口」的要求，我没有加这个出口。**
于是本目录的测试写成了今天能真断言的两条：

| 测试 | 断言什么 | 为什么这样写 |
|---|---|---|
| `test_counter_task_is_not_fanned_out` | 状态机只有**一份**来源时，切分器**不扇出**（1 块） | 这是今天真实存在的「不做 fake parallelism」的机制（回落 `split_single`） |
| `test_counter_set_gap_refusal_exit_does_not_exist_yet` | 三份「看起来可切」的局部视图**会被切开、而且门禁放行** | ★ 这是**缺口的看门狗**：把「今天拦不住」钉进测试，等 `refuse_split` 落地时它会变红，逼着把它升级成 §14.4 的正式判据 |

**要落这个出口，需要一次设计决定**（谁来判断「共享状态」？`Subtask.partition` 只描述
同级写的互斥，描述不了跨块的状态依赖）—— 这超出「建验收集」的范围，报给用户定。
