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

---

## ★ 观察结果（真模型 `Qwen/Qwen3.5-4B`，2026-10-03，按 `§14.4` 现状标注执行）

「落地前做观察而不是断言」—— 两道题各真跑一遍，如实记录系统怎么处理它们：

```
machine-events（1 源）     plan = star  ['T-01']            1 块
                          blocks  : T-01 ✗
                          assembly: ✗ ('cut_complete', 'global_invariant')
                          exit    : failed

party-views（3 源）        plan = pipeline ['T-01','T-02','T-03','T-04']
                          blocks  : T-01 ✓  T-02 ✓  T-03 ✓  T-04 ✗（合并块）
                          assembly: ✗ ('cut_complete' —— panel.csv 没产出)
                          exit    : failed
```

**这几行就是决定「出口补成什么形态」的依据：**

1. **它真的切了** —— 三方局部视图被切成 3 读 + 1 合并，**门禁一条都没拦**。
   三份「看起来独立」的来源正好绕过 11 条合法性检查。
2. **★ 三个读块各自都过** —— 每块只回自己那两三步，单独看完全合格；
   而任务要的是「顺着同一份状态机一步步推」。**块级全绿 + 整体无意义**，
   这正是 §15.8 说的 `fake parallelism` 的代价，也是「块级通过率」这一维
   单独看会骗人的地方。
3. **失败只出现在合并块与装配关**（`cut_complete`），
   也就是**事后**才发现 —— 与文档标注的「两层脱节」完全一致：
   系统能事后降级，不能事前拒绝。
4. 单源那道题没扇出（`star`，1 块），但它连**单块**也没做出来
   （状态机推理对 4B 太难）—— 所以「不扇出」在这一道上救不了它，
   反例集的判据仍然应该是**事前拒绝**，而不是「碰巧没切」。

**给出口形态的提示（不是结论）**：判定「三方共享同一份状态」需要的是
**块之间有没有状态依赖**这个信息，而今天的 `Subtask` 只有 `inputs`（数据依赖）
与 `partition`（同级写互斥）—— 描述不了「共享可变状态」。
先有那个字段/判据，才谈得上 `refuse_split`；否则补出来的出口只会是摆设。
