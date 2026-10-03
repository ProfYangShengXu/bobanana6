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

**本块的目标不是让十条都过**，是让每条都能被独立开关、被跑、并**如实**报出三步各自的结果。
`miss` 记录的是被测系统当前的真实状态。跑出来的红（2026-10-03）：

| 攻击 | ① | ② | ③ | 红在哪 |
|---|---|---|---|---|
| A02 polysemy | hit | n/a | **miss** | 口径不一致是在 compare 期被发现的，不是 L-1 拦住的（§14.2 的 `fail_if` 正是这条） |
| A04 missing≠0 | **miss** | n/a | **miss** | 空值与被填平的 0 在判定器眼里是同一个东西 —— 没有判定者 |
| A06 persistent | hit | n/a | **miss** | 停下来了（没有无限重试），但出口是「前置门拒跑」而不是降级/标记不可得 |
| A07 同症异层 | hit | **miss** | n/a | **三条不同根因拿到同一个 blame（tool）** —— 假归因 |
| A08 全局不变量 | **miss** | n/a | **miss** | `target="*"` 在多块装配上只会「看不见产物」，从来没真的看过合并结果 |
| A10 单位 | **miss** | n/a | **miss** | `unit_consistency` 只比**声明**；候选里的值一个都不读 → 「归一」没有判定者 |
| A11 溯源真实性 | **miss** | n/a | n/a | ★ 见下 |

## ★★★ A11：溯源引用的真实性没人校验（本块最重要的新发现）

见仓库根 `溯源缺口-引用真实性未校验-20261003.md`。一句话：

```
验证的：snapshot_ref 这个名字在不在已知快照集合里
不验证：引用的那一行真的存在吗 / 值对得上吗
★ 2026-10-03 真模型实测：4 块全过 · assembly passed · exit=passed · C-prov coverage=1.0
  而答案是 30 行编的数据（真值 7 行），27 行引用的行根本不存在
```

**它不需要 ground truth**（快照就在系统手里），所以它是**系统内可判**的缺口。

**同一个缺口的第二个读数（夜报 #13 报的「纯合并块假 ref」）**：
纯合并块的 `authorized_sources` 为空，`produce.py` 里那条 `unknown_ref` 守卫就**整个不生效** ——
它写什么 `ref` 都收下，还会变成 `candidate.source_id`。只要那个名字在注册表里，
链就报 coverage=1.0；换成没注册的名字才会被抓。
看门狗：`tests/test_attacks.py::test_a_pure_merge_block_can_cite_any_registered_snapshot`
（证明它会红：变体 T10 把守卫改成对空集也生效 = 模拟修好，测试必须转红）。

**验收证据是两条，同一个事实的两个读数**：

```
tests/test_attacks.py::test_a11_citing_rows_that_do_not_exist_is_caught   ← xfail(strict=True)，现在【红】
tests/test_attacks.py::test_a11_the_executor_reports_the_gap_as_a_miss    ← 执行器报 miss
```
★ 它是**活体缺陷样本**：按 `§15.9①`「没有失败样本的验收等于没验收」，
先做成会红的题、再修，才有证据证明修对了。strict 的 xfail 会在修好时 XPASS → 变红 →
**逼着把它升级成正式断言**。

### 修的时候落点在哪（本块只报，未动）

```
★ 这不是「core 忘了实现」，是【分层决定的】：
  Snapshot.body_path   标注 "external:sources —— 固化载体的位置，core 不打开"
  AGENTS.md §1.1       core/ 零 I/O（连 http/url 这些词都禁）
  → core/provenance.py 只做结构性校验是【正确】的，它拿不到快照内容。

修法（AGENTS §1.3「端口定义在稳定侧，实现在外侧」的正用）：
  ① core/ports.py        加 SnapshotReader 端口
  ② core/checkers/       加内容级溯源检查器（判定语义，零 I/O：
                         快照内容由外侧读好、以数据形式注入 CheckContext）
  ③ sources/ 或 adapters/ 实现读快照
判据（能证伪）：引用的 <artifact>:row=<n> 必须在快照里真的存在；值必须对得上。
```

★ **本块不实现**：一是缺口文档自己写了「先做成会红的题，再修」；
二是往冻结的 `core/` 里加 kind / 加 CheckContext 字段是设计变更，不是本块的范围。

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

## ★ 反向验证（`tools/reverse_validation.py`）

全绿树上 **186 变体 / 186 被抓住 / 0 漏网**，每条变体一个见证测试，跑完原样还原。
本块新增的每个测试（执行器结构 / 11 条读数 / A11 两个读数 / 判分器与 harness 纪律）
都各有一条见证变体 —— 没有「写了但从证明不了会红」的测试。
两条纪律是血换的：

- **见证测试必须执行被改的那一行**。T02 一开始指向 `test_every_row_in_the_table_has_an_injector`，
  而它只断言「表里的 id 集 == INJECTORS 的键集」，永远走不到「没有注入器」那条分支 ——
  于是一条**真被覆盖**的变体被报成「未捕获」（假阴性）。
- **harness 被 `kill -9` 时还原钩子不跑**，变异会留在源码树里，下一次全量跑在脏树上量数字
  （2026-10-03 实测发生过一次）。所以开跑之前有 `preflight()`：任何一个锚点不在、或不止一处，
  就**拒绝开跑**并打印是哪个文件，而不是在脏树上继续量。

