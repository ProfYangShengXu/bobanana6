# inject/ —— 注入执行器（`实现规格.md §14.2` / `§14.5`）

`attacks.yaml` 现在**是执行器读的表**：`acceptance/inject/attacks.py` 按行注入、按行报结果；
每条攻击的注入与读数在 `acceptance/inject/injections.py`。跑：

```
PYTHONPATH=src .venv/bin/python -c "
from acceptance.inject.attacks import run_attacks, render_table
print(render_table(run_attacks()))"
# 只跑某几条（逐行开关）：
run_attacks(only=["A11-provenance-content"])
# 逐行关掉：把 attacks.yaml 里那一行改成 enabled: false
```

## ★★ 三步分开报，不合成一个「整体通过」

```
① 发现了吗     found   —— 系统注意到注入的缺陷了吗
② 归因对不对   blame   —— attribute() 指到的层对不对
③ 出口对不对   action  —— 走的是不是对症的那条出口
```

§14.5 的原话：合成一个「整体通过」会掩盖「发现对了但归因错」与「归因对了但动作错」
—— 而这两种失败的处置完全相反。所以表里三列各报一行，`n/a` 是**显式声明**「这条攻击
不适用这一步」，不是默认通过。

## ★ 红了就是产出

`miss` 记录的是被测系统当前的真实状态，**不是为了让十条都过**。`#15` 先把每条注入做成
可独立开关、可跑、三步分开报的读数；`#16` 据此把其中四条【判定缺口】补上。
下面第一张表是 `#16` 的修前 / 修后对照，第二张是仍然红的（性质不同，本块不做）。

### #16 收口的四个（修前 / 修后对照读数）

| 缺口 | 修前读数 | 修后读数 | 证据 |
|---|---|---|---|
| **A04** 缺失 vs 被填平的 0 | ① miss ② n/a ③ miss —— 空值与 `0` 产出**完全相同**的 outcome 元组 | ① **hit** ② n/a ③ **hit** —— 缺失格报到 `row=0 col=amount holds no value`，`0` 是一个值、照样通过 | 缺失现在是**一等状态** `core.model.MISSING`；读取端把空单元格映射成它（`0` 不映射），`schema` 在 `nullable: false` 上**逐格**判 |
| **A07** 假归因 | ① hit ② **miss** ③ n/a —— 三条不同根因全归成 `tool` | ① hit ② **hit** ③ n/a —— `tool` / `context` / `constraint` 三条各不相同 | 归因层先读**自带病因**（工具标 `deterministic` / 判定器标 `constraint_rejected` / 分离实验判上下文）；两层互相指认则 `unknown`，不猜 |
| **A10** 单位只比声明 | ① miss ② n/a ③ miss —— 值写 `1 千元` 与写 `1 CNY` 得到同一个判决 | ① **hit** ② n/a ③ **hit** —— `1 千元` 在 `row=0 col=amount` 被点名，`1 CNY` 通过 | `unit_consistency` 现在**读值里的单位**（`实现规格 §5.0.1`② 的 Kubernetes `Quantity` 形态）；值里不带单位仍然判不了，如实写在证据里 |
| **A11** 溯源引用真实性 | ① **miss** ② n/a ③ n/a —— 3 行快照 / 30 行引用，`coverage=1.00 breaks=0` 放行 | ① **hit** ② n/a ③ n/a —— `coverage=0.10 breaks=27`，点到 `row=3` 不存在 | `SnapshotReader` 端口 + 内容级校验；见下 |

### 仍然红的（本块不做，性质不同）

| 攻击 | ① | ② | ③ | 红在哪 |
|---|---|---|---|---|
| A02 polysemy | hit | n/a | **miss** | 口径不一致是在 compare 期被发现的，不是 L-1 拦住的（**时序/效率**缺口） |
| A06 persistent | hit | n/a | **miss** | 停下来了（没有无限重试），但出口是「前置门拒跑」而不是降级/标记不可得（**出口选择**缺口） |
| A08 全局不变量 | **miss** | n/a | **miss** | `target="*"` 在多块装配上只会「看不见产物」，从来没真的看过合并结果（留给下一块） |

## ★★★ A11：溯源引用的真实性（#16 已收口）

见仓库根 `溯源缺口-引用真实性未校验-20261003.md`。一句话：

```
验证的：snapshot_ref 这个名字在不在已知快照集合里
不验证：引用的那一行真的存在吗 / 值对得上吗
★ 2026-10-03 真模型实测：4 块全过 · assembly passed · exit=passed · C-prov coverage=1.0
  而答案是 30 行编的数据（真值 7 行），27 行引用的行根本不存在
```

**它不需要 ground truth**（快照就在系统手里），所以它是**系统内可判**的缺口。

**同一个缺口的第二个读数（夜报 #13 报的「纯合并块假 ref」）**：纯合并块的 `authorized_sources` 为空时，
`produce.py` 里那条 `unknown_ref` 守卫**整个不生效** —— 它写什么 `ref` 都收下，还会变成
`candidate.source_id`；只要那个名字在注册表里，链就报 coverage=1.0。

### #16 怎么修的（落点与证据）

```
① core/ports.py::SnapshotReader     端口定义在稳定侧（core 零 I/O，打不开载体）
② core/model.py::SnapshotBody       快照内容以【数据】形态注入，core 只判定
   core/checkers/base.py            CheckContext.snapshot_bodies
③ sources/reader.py::SnapshotFileReader   外侧读载体、解析成行
   cli.py 装配 · runner 读一次、注入 CheckContext

判定（core，零 I/O）：
  core/provenance.py::content_break   引用的行必须真的存在（按 source_row）
  checkers/provenance.py::value_mismatches  引用的值必须与快照那一行一致
  → 名字可达不再是「引用为真」；coverage 也会因内容不到位而下降
```

**生产端的两件事（同一个事实的两个读数一起修）**：

```
· 块声明了输入就只能引用它/它消费的部件真正读过的快照（守卫不再对空集失效）
· 合并块自己没读过快照 → 引用【继承】自它拿到的输入行；编出来的一行
  与任何输入行都不相同 → 当场拒收（`untraceable_row`），不是发一个撑不住的引用
```

**验收证据（全部是【正式断言】，没有恒真的 xfail）**：

```
tests/test_attacks.py::test_a11_citing_rows_that_do_not_exist_is_caught     ← 3 行快照 / 30 行引用 → 抓住 row=3
tests/test_attacks.py::test_a11_a_citation_whose_value_does_not_match_is_caught
tests/test_attacks.py::test_a11_the_executor_reports_the_gap_as_found       ← 执行器报 hit
tests/test_attacks.py::test_a_merge_block_may_not_cite_a_source_it_never_read
tests/test_attacks.py::test_a_merge_row_inherits_the_citation_of_the_row_it_was_handed
tests/test_acceptance.py::test_a_fabricated_answer_is_only_caught_once_the_chain_reads_the_snapshot
```

★ 原来那条 `xfail(strict=True)` 在修好后 XPASS（strict 会让它变红）→ 已按 §15.9① **升级成正式断言**。

**离线端到端对照**（`tests/test_acceptance.py`，同一道题、同一个脚本化客户端，只有尺子不同）：

```
读块编 30 行（真值只有 3 行），每行都引用自己的快照
  修前（链只查名字）→ block_pass_rate=1.0 · assembly passed · exit passed
  修后（链逐点重放）→ 合并块被 C-prov 拦下：'panel.csv:row=10' cites row 10 of
                      'panel-a', which holds 3 row(s)；值也对不上
```

**★ 残留（如实报，不假装）**：合并块若**变换**了值（例如把千元换算成元），
与输入行不再完全相同 → 现在会被判 `untraceable_row` 拒收。也就是说：
**「引用 + 变换」今天无法表达** —— 需要答案里能声明每行来自哪个输入行（或声明变换），
那是答案形状的变更，本块没做（属于下一块的范围）。

## ★ 本次哪些出口没被走到（§4.2⑤）

`acceptance/judge.py::EXIT_KINDS` 九条出口里，本块 11 条注入**碰到三条、没碰六条**：

| 出口 | 本次读数 |
|---|---|
| `retry_local` | A05：2 次超时后第 3 次成功（`produce calls=3`，run 级状态 `passed`） |
| `decline` | A09：`exit=declined`，candidate 为空 —— 拒答被当成第三种合法出口 |
| `deliver` | A01/A03/A05 产出了候选（路径走过），但**没有哪一行的判据是它** → 本次不计分 |

**没走到的六条**：`refuse_split` · `single_agent` · `degrade_single` · `reshard` · `taint` · `abort`
—— 在被注入碰到之前，这六条路径在本块口径下**未被验证**。
两条容易混淆的：A06 走的是「前置门拒跑」（`RunRefused('sources')`），**不是** `degrade_single`；
A08 期望的「装配拒绝」不在 `EXIT_KINDS` 里（它是装配口径，不是运行出口口径）。

★ 出口的全景巡检是验收集（§14.3）的职责，不是注入执行器的：执行器只回答
「这一条注入被系统怎么处置了」。上面这张表是【本块的】覆盖声明，不是全局的。

★ `#16` 的补充：四条修的都是**判定**，不新增出口 —— 上面「没走到的六条」在 `#16` 之后
仍然没被走到（`#16` 的离线端到端对照走的是 `deliver` + 块级 `C-prov` 拒收）。

## ★★★ #17 收掉 #16 暴露的两个真问题

### ① 合并块的「引用 + 变换」表达不了 → 改成【读取时归一】

**问题**：合并块若把值 ×1000（千元→元），输出行就不再精确匹配它收到的行 →
被 `untraceable_row` 拒收 —— **正确的答案被判死**。

**判断（三条依据，都是项目自己的）**：
```
· 合并块的语义就是【搬运】（代码注释自己写着 "a merge may only carry values it received"）
· 读取块的语义是【读取 + 理解】—— 它读某个源的时候就知道那个源用什么单位
· `实现规格 §5.0.1` 判据①：单位必须声明在【字段自己的定义处】；读取时正是那个地方
```
→ **归一放在读取块**；合并块不改，`untraceable_row` 不放宽。

**修前 / 修后（同一道题、同一个脚本化客户端，离线确定性）**：

| 读数 | 结果 |
|---|---|
| 修前 | `block_pass_rate=0.75 assembly=False`；T-04 `rejected_by=C-prov`：`coverage 1.0000 reaches 0.95, but the citations do not replay; row=4 col=amount value 250000.0 does not match snapshot 'panel-c' row 0: '250 千元'` |
| 修后 | `block_pass_rate=1.0 assembly=True`；面板金额全是元（`C-002=3000000.0`、`B-002=250000.0`），`C-prov coverage 1.0000 >= required 0.95` |

**做法**：
```
· 载体把单位写在值里（`250 千元`）—— §5.0.1② 的 Kubernetes `Quantity` 形态
· 本体把换算声明出来（`amount_千元: {concept_id: amount, symbol: 千元, scale: 1000, base: CNY}`）
· 读取块归一（脚本化客户端 =「读取 + 理解」那一步）
· A11 的引用校验改成【比量】：值里带单位就换算到基准单位再比
  → 换过单位的同一个量算「引用为真」；**把单位丢掉没换算（3000 而不是 3000000）仍然算假**
```

**边界（不许放宽）**：合并块自己改值仍然拒收 —— 见
`tests/test_acceptance.py::test_a_merge_that_transforms_is_still_refused`。

**★ 残留**：源里既不带单位、也没有别处声明 → 仍然无从归一（A10 攻击面最难的那一半）。
值里带单位这条走通了，没标的还是判不了，如实留着。

### ② A07 的声明通道：补上生产者

**问题**：`_trace_declarations` 读 `detail["cause"]` / `deterministic` / `constraint_rejected`，
**全仓没有任何地方写它们** → 真实 run 里 `declared` 为空 → 假归因照旧。

**做法（§7⑫ 的手法从【错误】推广到【失败事件】）**：

| 层 | 谁声明 | 怎么声明 |
|---|---|---|
| tool | 工具自己 | `SourceError.blame = "tool"`（与它的 `reason="switch_source"` 同层）；registry 抛出时读它写进 `ToolResult.cause` |
| model | 调用方 | 不存在的工具名 / 不可用的参数 → `ToolResult.cause="model"`（工具没跑，是调用方给的） |
| context | 委派层 | `task.upstream` 的载荷说 `missing` → `produce.execute` 在事件上标 `cause="context"` |
| constraint | 判定器 | 被具名约束拒了 → runner 的 `gate_check` 标 `constraint_rejected=True` |

**真实 run 的对照读数**（测试一行 `cause` 都没写，全是生产者发的）：

| 场景 | 修前归因 | 修后归因 |
|---|---|---|
| 读块拒答 → 合并块的声明输入从没被生产出来 | `None`（没声明可读） | **`context`**（`attribution_accuracy=1.0`） |
| 候选被 `C-rows/C-prov` 拒 | `None` | **`constraint`** |
| 模型叫了不存在的工具 | `tool`（旧启发式，动作还是 `repair_tool`） | **`model`**（动作 `switch_worker`） |
| 载体读坏（handler 抛 `SourceError`） | `tool`（靠启发式） | `tool`（**声明的**，走真 registry + 真 `LoopIOImpl.execute`） |

**★ 判断（「模型自身问题该由谁标？」）**：
```
· 结构性的模型错误（叫了不存在的工具、参数不是 JSON）→ 由【调用/工具层】标：
  那两件事是工具表与参数解析当场就能判定的，不需要分离实验。
· 语义性的模型错误（上下文够但模型答错）→ 只能由【分离实验】标
  （`Experiment.HUMAN_CAN_ANSWER/DIFFERS`）—— 在进程内无法与「上下文不够」区分，
  归因层不许硬猜。本块没有为此新增任何启发式。
```

**★ 真实 run 里到不了的第四格**：工具坏（载体读不回来）在**完整 run** 里到不了块级工具路径 ——
`#16` 把快照内容读在**块开始之前**（`Runner._snapshot_bodies`，读不回来就整个 run 失败，
这是有意的：判定器不许退化成只查名字）。所以那条声明由
`tests/test_agentloop_attribution.py::test_a_broken_carrier_declares_the_tool_layer_on_the_real_event_stream`
用**真 registry + 真 `LoopIOImpl.execute`** 验证（事件是生产者写的，不是测试手写的 `cause`）。

## ★★★ #18 把 #17 报上来的三个口子补严

### ① 单位异构的另一半：不标单位的那一段 → **(c) 说出来 + (b) 可要求**

**先回答那个问题：源里不标单位，单位到底是什么？**
```
· 值里标了        → 就是它（#17 已做：换算到基准单位）
· 本体声明了      → 这是【声明】，不是假设 —— `实现规格 §5.0.1` 判据①
                    说的"字段自己的定义处"就包含概念在本体里的定义
· 两者都没有      → 单位**没有确立**：判不了，且不许默认成基准单位
```
★ 所以「不标单位」本身不等于「判不了」（本体可以替它说）；真正缺的是
**「这一段的值靠的是假设」这句话没人说**。本块的判断：

```
默认   (c) 把假设【说出来】：判定事实里写 "assumption, not a declaration"，
          并且列上不带单位（`Column.unit == ""` = 没确立），不静默当基准单位
严格   (b) 任务要求单位必须显式（`unit_consistency.spec.unit_source: "explicit"`）
          → 这一段【拒】：拿不到就说拿不到，别编
```
两者都不猜：**没有任何量级启发式**（1000 倍也可能真是业务差异）。

**端到端对照**（三个源里 panel-c 的值自带单位、panel-a/b 是光秃秃的数）：

| 读数 | 结果 |
|---|---|
| 修前（假设无人说） | 判定事实只有 `unit 'CNY' consistent for 'amount'` —— 那一段靠假设，但**一个字都没说**；`unit_source: explicit` 也**被忽略**（照样过） |
| 修后 默认 | `C-units: 7 value(s) in 'amount' carry no unit; the ontology declaration 'CNY' applies to them (assumption, not a declaration)`，块仍然通过 |
| 修后 严格 | 同一个块**被拒**：`7 value(s) in 'amount' carry no unit and the column declares none; this constraint requires the unit to be explicit` |

**★ 残留（在原理上就判不了的）**：源不标、本体/任务也没声明差异，而且没有别的源
覆盖同一实体 → **没有任何系统能看出它其实是千元**。本块做的是「不再静默假设 + 可要求显式」，
不是「猜对」。另一条**能**发现它的路是跨源比对（同一实体两个源给出不同值 → 真冲突），
而跨块的候选今天**不互相比较**（`classify` 只在同一块的收敛里比）—— 那是另一台机制，本块不动它。

### ② 载体读不回来 → 报【具名门 sources】

| 读数 | 结果 |
|---|---|
| 修前 | `SourceError` 直接穿出 `Runner.run()`：验收 harness 报错，CLI 兜底成 `failed: …` + exit 1（现象层） |
| 修后 | `refused = "sources"`，note：`the run was refused at the 'sources' gate before any worker started`，**一个块都没跑** |

换的只是**报错的形式**，不是判定的语义：读不回来仍然是失败，判定器**不许**退化成只查名字
（A11 的洞不重开）。与 A07 的声明通道不打架：**「哪个门挡的」与「谁的责任」是两个层的问题**。

### ③ fork 边的静默陷阱：回退把 Mapping 喂给 `json.loads`

| 读数 | 结果 |
|---|---|
| 修前 | 只给 `arguments`（合法 JSON 对象）→ `ok=False error_type=invalid_tool_arguments`，内容 `tool arguments are not valid JSON` |
| 修后 | 同一个调用 `ok=True`，handler 收到 `{'path': 'a'}`；而**真正的坏 JSON 字符串仍然被拒**（fork 的严格解析契约没动） |

修的是**回退与解析器的类型不匹配**（`raw_arguments or arguments` 把 Mapping 交给只吃字符串的
解析器），不是给它加特判：现在回退会把自己已经有的 mapping 序列化再交给解析器。
没有改 `agentloop/`，所以不涉及上游同步；这是本项目自己的回退逻辑写错了类型。

## ★ 反向验证（`tools/reverse_validation.py`）

全绿树上 **197 变体 / 197 被抓住 / 0 漏网**，每条变体一个见证测试，跑完原样还原。
本块新增的每个测试（A04 的两半 / A10 的四种读数 / A07 的三条声明规则 /
A11 的五条 + 离线端到端对照）都各有一条见证变体 —— 没有「写了但证明不了会红」的测试。
两条纪律是血换的：

- **见证测试必须执行被改的那一行**。T02 一开始指向 `test_every_row_in_the_table_has_an_injector`，
  而它只断言「表里的 id 集 == INJECTORS 的键集」，永远走不到「没有注入器」那条分支 ——
  于是一条**真被覆盖**的变体被报成「未捕获」（假阴性）。
- **harness 被 `kill -9` 时还原钩子不跑**，变异会留在源码树里，下一次全量跑在脏树上量数字
  （2026-10-03 实测发生过一次）。所以开跑之前有 `preflight()`：任何一个锚点不在、或不止一处，
  就**拒绝开跑**并打印是哪个文件，而不是在脏树上继续量。

