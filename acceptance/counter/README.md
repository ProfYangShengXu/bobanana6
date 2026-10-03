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

---

## ★★ 本块把它做成了【反证实验】：唯一变量 = **来源数**

上面那一节说的是「`refuse_split` 出口在代码里不存在」，所以这条要验的不是
"那个出口有没有被走到"，而是更前面的一件事：

> **「该不该拆」这个判断，在壳里到底存不存在？**

★ 好消息是**对照题不需要新建**：`party-task.yaml`（3 份局部视图）就是为这件事建的
（`#14` 那轮），它与 `task.yaml` 出自**同一个 `generate.py`**：

| 题 | 来源 | 计划（不接模型，确定性） |
|---|---|---|
| `task.yaml` | 1 份事件日志 | `shape=star`，**1 块**（`T-01`）—— 没有第二个来源就不扇出 |
| `party-task.yaml` | 3 份局部视图 | `shape=pipeline`，**4 块**（3 读 + 1 写）—— ★ **切了** |

★ **逐字对过两版，差三处，只有第一处是变量**：

```
① sources            machine-events → party-a/b/c      ★ 这就是要验的变量
② C-rows 的 target   "*"            → final-state.csv   不是变量：多块装配下
   `target="*"` 必然报「artifact for target '*' not found」（A08 的已知缺口），
   留着它会把「切没切」和「* 看不见合并结果」两件事混在一起 —— 这一处是【规避已知缺口】，
   不是第二个变量
③ task_id / notes / blind 的文案（元数据，不参与判定）
```

共用：同一份 `generate.py` / `ontology.yaml` / 交付物 `final-state.csv` /
`C-prov` 一字不差 / `limits` 一字不差。

这条读数是**确定性**的（切分器不接模型）：把它单独量出来，就不必靠"跑一次看看"。

真模型侧的同窗读数（它切了之后走不走得通、判据拦不拦）见 `.bobanana6/b24/报告.md`。

### 本块复跑（真模型，同窗各 2 次；`.bobanana6/b24/run.log`）

| 跑 | 题（来源数） | 块级 | 装配 | 出口 | 最终答案 |
|---|---|---|---|---|---|
| c1 | `task.yaml`（1） | `T-01` ✓ | passed | passed | 5 行 ✓（这次把状态机推对了） |
| c2 | `task.yaml`（1） | `T-01` ✗（`C-rows` 1<5；`C-prov` 编了 `actor 'WorkerA'`） | ✗ | failed | 1 行（编的） |
| c1 | `party-task.yaml`（3） | `T-01`✓`T-02`✓`T-03`✓ **`T-04` ✗** | ✗ `cut_complete` | failed | 0 行 |
| c2 | `party-task.yaml`（3） | 同上（T-04 逐字：`row 0 of 'final-state.csv' matches no row this block was handed`） | ✗ `cut_complete` | failed | 0 行 |

★ 与上面 `2026-10-03` 那组旧读数的差别只有一处：**单源那版这次有一跑做对了**（5 行）。
它不改变结论 —— 「不扇出」这次救得了单源，但那是**碰巧**；判据仍然是"来源数 < 2 才不切"，
对 `party-task.yaml` 那种"看起来可切"的题一点作用都没有（切了、门禁一条没拦、只有事后 `cut_complete`）。
