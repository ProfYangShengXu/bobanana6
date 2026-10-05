# stress/ —— 压力任务 A（信息类，`架构设计.md §15.4`）+ 注入表

```
attacks.yaml        注入表（A01–A11，由 acceptance/inject/ 的执行器读；读数见 acceptance/inject/README.md）
task.yaml           压力任务 A 的契约（★ 由 generate.py 产出，不许手改）
ontology.yaml       压力任务 A 的共享语义（同上）
generate.py         24 个异构源的生成器 + 真值生成器
input/              运行期【唯一】挂载的东西（23 个源；s24 故意缺席）
truth/              真值（只给判分器读；与 input/ 物理隔离）
```

## ★★★ #20 压力任务 A：24 个异构源 → 实体 × 时间面板

```
形状    24 个源（23 个活 + 1 个故意下线）→ panel.csv + CONFLICTS.md / SOURCES.md / ONTOLOGY-MAP.md
跑法    run_task(acceptance/stress, concurrency=32)      # 24 块，部署必须声明得下
真值    input/ 与 truth/ 物理分开；运行期只挂 input/（tests/test_stress_task.py 有测试守着）
```

### 五条异构：在数据里，不在文档里

| 维度 | 形态 | 证据 |
|---|---|---|
| 格式 | CSV · HTML 表格 · API JSON · 纯文本年报 · 扫描件 PDF 的文本模拟（OCR 式噪声：`1`→`l`、`0`→`O`、`-`→`—`） | `input/` 里五种后缀都在 |
| 语言 | 中/英/日 —— 字段名（`营业收入`/`revenue`/`売上高`）与实体名（`主体01`/`Entity-01`/`取引01`）写法都不同；本体用 aliases 收 | `test_the_five_heterogeneity_dimensions_are_really_in_the_data` |
| 时间 | 实体做行 vs 年度做列（宽表）；两个修订版本（s21 重述前 / s22 重述后） | `_csv_wide` + s21/s22 |
| 单位 | 元/千元/百万元混用，**有的源标、有的源不标**（标 = 值里带单位，§5.0.1② 形态） | s02/s07/s13 标，s01/s05/s10 不标 |
| 结构 | 长表 vs 宽表；缺失四种写法 `空`/`0`/`-`/`N/A` 都在数据里 | `MISSING_TOKENS` + 宽表源 |

### 九条攻击面：造了什么 / 判据 / 真模型读数（`Qwen3.5-4B`，4 次真跑）

| # | 造了什么 | 判据（谁判） | 离线 | 真模型 |
|---|---|---|---|---|
| ① | 网状依赖：s13..s18 用内部码，只有 s19 能换回 `ENT-xx` | split 的 `iface_match`（跨块依赖被声明）+ 面板必须是规范主体号 | ✅ 24 块 pipeline、合并块声明了全部输入 | 部分：读块读得到，合并块几乎从不交出面板 |
| ② | 多义概念：`revenue` 四个口径版本，交付物钉 `v1-group-consolidated` | `definition_pin` | ✅ | ✅ 判据真的在看：批次 1 报了 `pinned to 'v-excl-tax', candidate declares 'v-group-consolidated'` —— **压力题抓到的真缺陷**（见下） |
| ③ | 真冲突：s20 与 s02 对 ENT-03/2022 给出不同值，都不是错的 | 期望 CONFLICTS.md 双方证据 | ⚠️ 跨块候选不互相比较（#63） | ❌ **没判**：`CONFLICTS.md` 里没有这一对（#63 的实测确认） |
| ④ | 缺失陷阱：四种写法 + 真值三个缺格 | `schema`（`exposure` 不许空）+ A11 内容级校验 | ✅ | 未走到（面板没出来） |
| ⑤ | 瞬时故障 | ★ 压力题没有假故障通道 | —— | —— **造得出、判不出**：由注入集 A05 覆盖，如实标注 |
| ⑥ | 持久故障：s24 的输入文件故意不生成 | A06 的降级 + 标记 | ✅ `refused=None`、`sources unavailable: s24 (1 of 24)` | ✅ **4/4 都报这条**（降级而不拒跑） |
| ⑦ | 同症异层 | 归因轴 | —— | 4/4 拿到 `constraint`；另三种根因没走到 |
| ⑧ | 全局不变量：整列同一单位/同一口径 | `unit_consistency` + `definition_pin`（**点名交付物**表达；`target="*"` 多块必 not found，见 A08） | ✅ 判据在 | 未走到（面板没出来） |
| ⑨ | 不可能块：ENT-12 的 revenue 只在 s24 | 真值 `invented` | ✅ | ✅ 4/4 `invented 0`（宁可空也不编） |

### 三口径 + 成本（真模型 4 次）

| 口径 | 读数 |
|---|---|
| 块级通过率 | 0.917 / 0.958 / 0.958（批次 1）· 0.917（命名修完那轮）—— 24 块里 1–2 块失败 |
| 装配 | ❌ 4/4 `cut_complete`（合并块没交出 piece） |
| 归因 | 4/4 `constraint`（判据拒了块），没有第二种 blame |
| 第四口径（真值） | 4/4 `rows 0/44`、`invented 0` —— 面板没出来，但也没有编 |
| 成本 | 52–55 次调用/轮；≈ 75–108k prompt + 12–16k completion tokens；单轮 9–22 分钟；`rate_limited=0` `billing=0` |

### ★ 压力题抓到的真缺陷（报告在案；本阶段不做调优）

1. **多版本口径的 pin 靠字典序**：`pinned_version` 在多版本时取**版本号字典序最小**的那个，
   于是 `definition_pin` 把「按任务要求声明 `v-group-consolidated`」的候选判成错的
   （实测：`pinned to 'v-excl-tax', candidate declares 'v-group-consolidated'`）。
   处置：把规范口径的版本号排到最前（`v1-…`）—— **题侧命名，不是判定语义改动**；
   「本体里没有显式 pin、只有字典序兜底」这条缺口报上来。
2. **合并块几乎从不交出面板**：4/4 轮里合并块的答案要么 0 行、要么列名不对，
   要么与它收到的输入不匹配（#17 继承守卫点名 `matches no row this block was handed`）。
   机制在拦（没有编造值流出去），但**这道题今天在真模型下过不去**。
3. **读块会幻觉 ref**：批次 1 有一个读块报 `'ref' names 'snapshots/local'` → 被判据拦下
   （`unknown_ref`）✓，但说明 24 个授权源下模型容易乱指（读块的授权是**全部**源，不是它自己那一个）。
4. **24 块的计划需要部署声明并发**：默认 `--concurrency 8` 时装配报 `capability_short`；
   声明 32 后消失 —— 能力匹配在正常工作，压力题必须按部署真实能力跑。

## 压力任务 B（代码类，`架构设计.md §15.5`）—— ★ `#33` 已建成

**题在 `acceptance/stress/code-convergence/`**（自己的 `README.md` 写了形状/硬点/判据/怎么跑；
读数在 `.bobanana6/b33/报告.md`）。

> 下面保留 `#21` 当年"建不起来"的记录 —— 那是**当时**的事实（三条壳能力都缺）。

<details>
<summary>历史记录（`#21`，2026-10-03）：当时建不起来，缺口逐条定位</summary>

指令要求"把压力集推出来"（第二道压力题：多模块仓库 → 归并重复实现为一个共享抽象）。
查完源码事实后**建不起来**，原因是三条壳能力都缺，题侧绕不过去：

| # | 缺什么 | 源码事实（`#21` 当时） | 挡住了哪条攻击面 |
|---|---|---|---|
| 1 | 按模块切分的策略 | `decompose/strategies.py` 只有 `split_by_source` / `split_by_field` | ① 接口密度、`partition` 写区互斥都还在，但"按模块分"这个轴不存在 |
| 2 | 候选答案落盘 | `produce.py:530` 是唯一写 `files={}` 的地方，没有生产者 | ④ **编译器 + 测试这个天然判官根本没得跑**；③ 没有可比对的测试文件 |
| 3 | 冻结测试文件摘要 | 无（`sources` 门冻结的是快照，不是代码仓库的测试文件） | ③ **"测试文件本身可能被改"无法判** |

</details>

### `#33` 补上的三件（逐条对应上表）

| # | `#33` 的做法 | 落点 |
|---|---|---|
| 1 | `split_by_module`：接口块 → 共享抽象块 → 一模块一块（`partition=module:<名>`）；模块由 `sources[].module` **声明** | `decompose/strategies.py` · `core/model.py` · `contract/parser.py` · `services/tools.py`（`SOURCE_READING_AXES`）· `cli.py --strategy module` |
| 2 | `parse_candidate` 收 `{"files": {工作区相对路径: 原文}}`；`LoopIOImpl` 落盘到 `--workspace`，并**执行写区互斥**（越界具名拒绝） | `assemble_impl/produce.py` · `cli.py --workspace` · `acceptance/harness.py`（工作区播种） |
| 3 | 冻结载体（内容寻址）+ **装配期比对**：新判据 `frozen_files`（第 15 种 kind），外侧算"冻结摘要 / 工作区摘要"两个值 | `core/checkers/frozen.py` · `core/checkers/base.py` · `sources/store.py::digest` · `assemble_impl/runner.py::_frozen_files` |

### 四攻击面读数（`#33`，建了 / 判了 / 读了 三者分开）

| 攻击面 | 建了 | 判了 | 读了 |
|---|---|---|---|
| ① 接口先冻结、再实现 | ✅ | ✅ `C-shared-interface` | ✅ `exit 0` |
| ② 测试是全局的（块内过 ≠ 整体过） | ✅ | ✅ `C-global-tests` | ✅ 块内 ok、末块 FAIL（`local` 跑） |
| ③ 测试文件可能被改 | ✅ 冻结摘要 + `frozen_files` | ✅ `C-tests-frozen`（每块 + 装配关） | ✅ 改松测试 → 每块被点名 |
| ④ 编译器 + 测试是天然判官 | ✅ `custom` 跑 `py_compile`/`unittest` | ✅ 退出码被记录 | ✅ 四组命令全 `exit 0` |

★ 已知缺口：装配关仍报 `provenance_connected`（代码类产物没有行级溯源）——
逐字读数与切点见 `.bobanana6/b33/报告.md §6`。
