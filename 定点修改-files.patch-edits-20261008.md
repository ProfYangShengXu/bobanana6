# 给 `files.patch` 补上【多条 edits】定点修改（2026-10-08）

> 任务书的前提是「块级工具面里没有定点修改这个动作」。**侦察后这条不成立**（见 §1）。
> 用户裁定选 3：**把 flash 的 `edits[]` 语义灌进已有的 `files.patch`**，不新增第三个写类工具；
> 并先回答「为什么 0 次调用」。本文件是改动记录与逐条读数。

---

## 1. ★ 前提纠正（三条硬证据，不是我推的）

| # | 事实 | 读数 |
|---|---|---|
| 1 | 定点修改工具**早就有** | `core/ports.py` 声明 `ToolSpec("files.patch", ("path","old_string","new_string"), optional=("replace_all",))`；实作 `services/workspace.py::BoundWorkspace.patch()`（精确原文替换、非唯一即拒、写区/账本/字节闸齐全）；注册 `services/registry.py` catalog |
| 2 | 模型**看得见它** | 两次 4B 实测的 `tools[].function.name` 与 `wire_names` 里都有 `files.patch`；**qwen4b 43/43 条、qwen4b2 55/55 条** |
| 3 | 模型**一次没调** | `qwen4b`：`code.run×33` `shell.run×4` `files.write×2` … **`files.patch×0`**；`qwen4b2`：`code.run×41` **`files.patch×0`** |

⇒ 准确缺口是「**没有多条 `edits[]` 形状**」，不是「没有这个动作」。
原描述还写着「改一份已存在文件里的**几处**文字」，而参数只能一次改**一处** —— **说得到、做不到**。

---

## 2. ★「为什么 0 次调用」——三条分开查、分开报

### (a) 描述问题 —— **不是没写三件套，是"说得到做不到"**

`core/ports.py::_TOOL_DESCRIPTIONS["files.patch"]` 改前原文，逐句：

```
何时用：改一份【已存在】文件里的几处文字，且你知道原文确切长什么样。   ← 有"何时用"
输入：path；old_string = 要被替换的原文；new_string = 替换成什么；
      replace_all = 是否替换全部匹配。                                ← 有"输入"
何时不用：整份重写用 files.write；                                    ← 有"何时不用"
★ old_string 在文件里出现多次时会被拒（不猜你要改哪一处）——
  带足上下文让它唯一，或显式 replace_all=true。                      ← 有边界 + 修复建议
```

**三件套都在。** 真正的毛病是**第一句与参数形状矛盾**：说"改**几处**文字"，
而一次调用只能给**一对** `old_string/new_string` ⇒ 模型要改 3 处就得发 3 次调用、
还得自己保证 3 处互不干扰。对一个已经在 `code.run` 里握着整段脚本的弱模型，
**这不是一个"能做那件事"的形状**。→ 本轮把它改成 `path + edits[]`。

### (b) 暴露问题 —— **不是**。它在菜单里

```
qwen4b : records=43  files.patch_in_tools=43/43
qwen4b2: records=55  files.patch_in_tools=55/55
```

**模型看到的是一份 17 个工具、逐轮完整发给端点的菜单，`files.patch` 每轮都在。**
所以"工具没进列表"这条**排除**。

### (c) 竞争问题 —— 竞争者不是 `files.write`，是 `code.run`

```
                 files.write  files.patch  code.run(总)  code.run(内含写文件)
qwen4b            2            0            33            6
qwen4b2           0            0            41            12
```

- `files.write` 本身也只被用了 2 / 0 次 —— **它不是"把场景吃光"的那个**。
- 真正被吃光的路径是 `code.run`：模型在 `code.run` 里**既算又写**（`to_csv(...)`）。
- ★ 更要害的一层（只读审计 #46 同源）：**模型反复重写的东西（那 145 行脚本）
  从来不是磁盘上的文件**，它只存在于 tool-call 参数里（`files.write` 0 次 = 没落过盘）。
  ⇒ `files.patch` 能改的是"工作区里的文件"，而模型想改的是"自己脑子里的脚本"。
  这两件事**在今天的拓扑里不重合**，所以即使补上 `edits[]`，也**不能保证**行为会变
  —— 这一点在 §3⑤ 用真模型读数验证，并预设了"a 没变就如实报"。

---

## 3. 改了什么（4 个文件）

| 文件 | 改了什么 |
|---|---|
| `core/ports.py` | ① `ToolSpec` 新增 `param_schemas`（**参数级 JSON schema 覆盖**，默认仍是 string）；② `files.patch` 广告形状改成 `params=("path",)` + `optional=("edits",)`，`edits` 渲染成**对象数组**（`[{oldText,newText}]`）；③ 重写 `_TOOL_DESCRIPTIONS["files.patch"]`（何时用 / 输入 / 何时不用）；④ `WorkspaceTools` 协议补 `patch_edits` |
| `services/workspace.py` | ⑤ 新增 `BoundWorkspace.patch_edits()`：flash 语义（原文匹配 / 唯一 / 不重叠 / **原子** / 具名报错 + `_nearby`）；⑥ `_clip`/`_nearby` 两个小工具函数；⑦ 非 UTF-8 与 CRLF 两处保真（见下） |
| `services/registry.py` | ⑧ `files_patch` 分派：有 `edits` 走新路径，否则走旧形状；⑨ **旧形状缺 `new_string` 不再默认成空串**（只读审计 #41：那会静默删内容还回 ok=True） |
| `assemble_impl/produce.py` | ⑩ `tool_schemas()` 尊重 `spec.param_schemas`（否则 `edits` 会被广告成字符串） |

**判定逻辑一处未动**：拦截仍在 `services/workspace.py::may_write`、
`assemble_impl/produce.py::_reconcile_workspace`；`main.py` 未动。

---

## 4. 验收读数（逐条）

### ① 三条 fail-closed + 反证

```
tests/test_patch_edits.py  20 条
  找不到   -> "edits[0].oldText was not found in 'out.py'." + expected + got(附近实际内容)
  不唯一   -> "appears 2 times … (not unique …)" + "include the line above/below"
  重叠     -> "edits[0] and edits[1] match overlapping spans (offsets 0-4 and 2)" + "merge them"
  同一条 oldText 写两遍 -> 按重叠拒（不猜、不合并）
  缺 newText -> 拒（不当作删除）；显式 newText:"" 才删除
反证（两臂）：旧代码 20 条**全红**（`tests/test_patch_edits.py`），新代码全绿。
```

### ② 原子性（贴前后 sha256）

```
refused: edits[2].oldText was not found in 'out.py'.
sha256 before: 8863d7897b3039ecbdd0531a4932ef0158906600b5404bfde7c425e7d7a9905a
sha256 after : 8863d7897b3039ecbdd0531a4932ef0158906600b5404bfde7c425e7d7a9905a   unchanged: True
```

实现是**两阶段**：内存里先算完全部 span 与最终全文，任一失败即抛，**最后单次写盘**。

### ③ 写区（与 `files.write` 同一条判据）

```
refused (WriteOutsideRegion): block may not write 'theirs.csv': its write region is ['mine.csv']
```
同一处 `_check_region` → `may_write`；`frozen_paths=None` 时退回旧规则（不在写区就拒）。

### ④ 溯源 / 账本（通道 2 同一条路）

```
ledger.used after write: True
ledger.changed(): ('out.csv',)
on-disk       : 'obligor\nA-1\nC-3\n'
```
另有端到端一条：块先 `files.write` 再 `files.patch` 改一行，**判定器通过**、
盘上就是改完的那份、下游 T-02 拿得到交接件。

### ⑤ 真模型反证（`qwen4b3`）—— ★ b 满足、a 变好，**但工具一次没被调用**

**b. 产物位置与存在性 —— ★ 满足**

```
acceptance/ab/13f-crowding/_run/workspace/output/filing_resolution.csv
    -> 存在，2525 字节，18 行（非空，真内容：17 列，首行 DAVIDSON_KEMPNER…）
acceptance/ab/13f-crowding/output/（错位目录） -> 没有重建
T-02 的 brief 里 input_gaps = null          -> T-01 真的交付了（qwen4b2 是"没到"）
```

**a. 重合度**（同一口径：相邻 code 轮次里，本轮非空代码行与上一轮的行集合交叠占比）

```
                相邻 code 轮次   min    median   max     >=50% 的轮次
qwen4b2 (改前)      33         3.2%   33.3%   85.2%     11/33
qwen4b3 (改后)      15        15.0%   65.9%   85.6%     11/15

qwen4b2: 7 77 47 29 4 85 15 63 11 44 16 60 3 48 28 18 51 15 59 75 40 33 50 10 75 64 33 33 60 8 7 17 22
qwen4b3: 15 40 53 26 45 60 52 66 66 80 71 72 86 73 83
```

⇒ 从"整段推倒为主（median 33%）"变成"增量为主（median 66%，后半程稳定 70~86%）"。

**★★ 但这【不是】因为模型用了新工具**：`files.patch` 本轮**仍然 0 次调用**
（T-01 直方图：`code.run 18` / `shell.run 7` / `sources.snapshot 10` / `sources.read 2` /
`task.* 2` / `check.local 1`）。已核对归档：模型**看到了**新描述与新 schema
（`tools[].function.description` 含"别为了改几行就把整份文件重写一遍"；
`parameters.properties.edits` 是对象数组）。

⇒ 可能的机制两条，**本轮设计分不开**：
  ① 工具**描述文本**本身在给模型立规矩（它每轮都读，即使不调）；
  ② 单轮跑测的**方差**（每臂 n=1）。
  要分开得做 A/B（只改描述、不改 schema），本轮没做 —— 如实记。

**T-01 其它读数对照**

```
                 改前 qwen4b2        改后 qwen4b3
轮次              44                  36
时长              73.8 min            31.2 min
prompt_tokens     2,241,551           1,593,238
completion_tokens 35,262              28,266
files.patch 调用   0                   0
产物（正确位置）   ★ 不存在           ★ 存在（18 行 / 2525 B）
错位目录           没有重建            没有重建
T-01 交付          input_gaps 指认未交付   input_gaps = null（交付了）
```

### ⑥ 反向：两臂逐条同结果

用 `git worktree` 取 HEAD（`c88e3aa`）当旧臂，把同一份新测试拷进去，`--junitxml` 逐条比：

```
new-arm = 1109 passed          old-arm = 1089 passed / 20 failed
identical = 1089   differing = 20   only-new = 0   only-old = 0
20 条 differing 全部是 tests/test_patch_edits.py（旧红 → 新绿）
⇒ 既有 1089 条两臂**逐条同结果**；没有一条"只在新版通过"。
```

### ⑦ 架构纯洁性 / ruff / 全量

```
pytest -m arch_guard -q   -> 41 passed, 1068 deselected
ruff check src/ tests/    -> All checks passed!
pytest tests/ -q          -> 1109 passed（基线 1089 + 20）
tools/default_audit.py --check -> exit 0
```

### ⑧ preflight

```
tools.reverse_validation.preflight() -> mutations: 256, problems: 0  ⇒ 空
```

### ⑨ 不确定项

见 §5。

---

## 5. 不确定项（如实列）

1. **★ 前提不成立**：`files.patch` 早就在菜单里（43/43、55/55），模型 0 次调用。
   本轮补的是「多条 `edits[]` 形状」，不是「从无到有」。
2. **★ (c) 的拓扑判断（实测确认）**：模型反复重写的是 `code.run` 参数里的脚本，
   **不是磁盘文件**（`files.write` 两次实测 0/2 次）。⇒ **`files.patch` 本轮仍是 0 次调用**。
   但 a（重合度）**确实**从 median 33% 变成 66% —— 所以"工具没被用"与"行为变好"同时成立，
   **不能把后者记在"模型用了定点修改工具"头上**（机制归因见 §4⑤：描述文本 vs 方差，分不开）。
3. **真模型整跑没等完**：`strategy="single"` 下这道题 8 块（每块 ~1h）。验收只问 T-01，
   所以 T-01 判定后就取读数（与上一轮同一口径）。
4. **变异 256 / 跨种子 42 未跑**：不在本轮验收项；且变异会临时改源文件、与真模型跑互斥。
5. **`core/ports.py` 被改**：任务书三处写「不许动 core/」，但工具规格表与描述表**只在**
   `core/ports.py`（且 `tests/test_services.py` 把 `TOOL_NAMES == _BASE_TOOL_SPECS` 钉成不变量）。
   这是用户在本轮明确选 3 时批准的；改动只有**端口声明**（ToolSpec/协议/描述），**零判定语义**。
6. **审计 #41/#42 同族**：#41（缺 `new_string` 静默删除）已在**本工具的旧分支**上堵住；
   #42（`files.write` 缺 `content` 静默清空）**未处置**——不同工具，不在本轮范围。
7. **审计 #43–#54 未处置**：见工作记忆，逐条给了不处置理由。

---

## 6. 工作记忆结账

* suspicious：`#41/#43/#44/#45/#50/#52` 已处置（本工具缺字段静默删、`edits[]` 形状、
  非 UTF-8 归因、CRLF 保真、测试覆盖、docstring 过时个数）；`#42/#46/#47/#48/#49/#51/#53/#54`
  标「已确认」并逐条写明不处置理由（另 5 条 flash 线同前）。**0 条未验证**。
* interesting：`#38/#39/#40/#42/#44/#45/#48/#49/#51/#52` 已采纳（两阶段原子性 / 重复目标拒 /
  具名 `got` / 唯一性 + 修复建议 / write↔patch 互相指路 / 原文不用行号 / 何时不用 / 可照做的
  错误文案 / 重叠工具会致选择退化 → 决定了"扩展而非新增"）；`#41/#43/#46/#47/#50/#53` 不适用。
  **0 条未采纳**。

---

## 附：qwen4b3 读数（T-01，已判定）

来源：`.bobanana6/ab/archives/qwen4b3-A-c1.jsonl`（流式落盘，T-01 共 36 条）。

```
T-01 turns = 36      时长 = 31.2 min
prompt_tokens = 1,593,238   completion_tokens = 28,266
工具直方图 = {task.contract:1, task.mysubtask:1, sources.snapshot:10, sources.read:2,
              code.run:18, shell.run:7, check.local:1}   ★ files.patch = 0

判据 a（重合度）：相邻 code 轮次 15 个，min 15.0% / median 65.9% / max 85.6%，>=50% 的 11/15
          （改前 qwen4b2：33 个，3.2% / 33.3% / 85.2%，11/33）
判据 b（产物）：_run/workspace/output/filing_resolution.csv 存在、18 行、2525 B、非空
          错位目录 acceptance/ab/13f-crowding/output/：没有重建
          T-02 的 input_gaps = null（T-01 交付了）

模型看到了新描述与新 schema（归档 tools[].function 逐字核对），但一次没用。
整跑在 T-02 起继续，本轮**未等完**（8 块 × ~1h）。
```

提交：`8fdb915`（`git diff --stat` 见提交信息；本轮代码在 T-01 跑测时已在工作树里，
提交不改文件，故读数锚定同一份代码）。
