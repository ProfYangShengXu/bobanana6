# `code.run` 改成【先落盘再跑】（方案 B）

> 一句话：让它从「贴一段代码就跑」变成「**写下一个文件、跑那个文件**」。
> 于是那段代码变成磁盘上一个**可被 `files.patch` 按原文精确改的对象** —— 定点修改第一次够得着。

---

## 0. 结论

```
① 落盘      满足（逐字节相同，sha256 对照）
② 返回      满足（只增不减：原有 ok/stdout/value/error/cwd 一字不动）
③ 截断换页  满足（正文留末尾、truncated/full_output_path、全量可读回、总量可读）
④ 不污染    满足（_code/ 不进产物：ledger.changed() 与痕迹仓库里都没有它）
⑤ 真模型    ★ **判据未满足**：`files.patch` 仍 0（机制到位、31 份代码在盘上，模型不改；如实报）
⑥⑦⑧        满足（1127 条两臂逐条同结果 · arch 41 · ruff · 1145 passed · preflight 空）
```

---

## 1. 为什么做（实测，不是设计洁癖）

**A 臂 `qwen4b3` 的工具直方图**：`code.run 18` · `shell.run 7` · `snapshot 10` · `read 2` ·
`files.write 0~2` · **`files.patch 0`**。

**根因是结构性的**：模型把代码塞在 tool-call 参数里跑，**代码从不落盘** ⇒
它想改的东西只活在对话里 ⇒ `files.patch`（改工作区文件）**永远没有用武之地**。
昨天已经验证过：把 `files.patch` 的语义补好了（多条 edits、原子、具名报错），
模型**看到了新描述和新 schema**，仍然 0 次调用 —— **它够不着**。

---

## 2. 改了什么（4 文件）

| # | 位置 | 改动 |
|---|---|---|
| ① | `services/workspace.py::run_code` | **先 `_save_code`，再执行**；返回加 `code_path` |
| ② | 同上 | **换页**：`_tail_page` 留**末尾**（2000 行 / 50KB，先到者为准）；完整输出写 `.out`，返回 `full_output_path` + 总量读数 |
| ③ | `services/workspace.py` | `CODE_DIR="_code"` 进 `IGNORED_PARTS` —— **单点排除**（`_digests` 看不见它 ⇒ `changed()` 不含它 ⇒ 通道 2 对账不收它） |
| ④ | `core/ports.py` | `code.run` 的描述照实补一句（代码被原样存成文件、改哪一行去改它） |
| ⑤ | `services/registry.py` | `build_registry` 给工作区**上块号**（草稿纸按块分目录） |

**落盘约定（确定性规则，壳定、模型不许选）**：

```
目录：<工作区>/_code/<块 id>/          ← 窄分量排除（不是 _code* 那种宽 glob）
文件：step-NNNN.py（原样，write_bytes）  step-NNNN.out（该次输出超限时的全量）
序号：每块从 0001 起、单调；★ 不用时间戳（不可复现）、reset **不重置**序号也不删文件
```

**★ 只读审计挖出的四条，一并修了**（都直接打在本任务的验收上）：

```
#71 压缩按【头部】截 2000 字符，而 code_path/full_output_path 排在几千字符 stdout 之后
    ⇒ 被整段砍掉，"取回得回来"在长会话里等于没有
    ⇒ 返回体改成**元数据键排在 stdout 之前**（只重排、不增不减）
#65 每个块的 BoundWorkspace 都是新的、序号从 1 开始，而工作区是任务级同一个
    ⇒ 每个块第一条 code.run 都盖上一个块的存底 ⇒ 改成 `_code/<块>/…`
#66 全量那份是**无上限**的全量 stdout ⇒ 加 8 MiB 上限，绑到就如实标 full_output_truncated
#72 write_text 文本模式会把 \n 按 os.linesep 改写 ⇒ 改 write_bytes（"逐字节相同"才真成立）
```

---

## 3. 验收读数（逐条）

### ① 落盘：逐字节相同

```
code_path        : _code/T-01/step-0001.py
sha256(code)     : 15f932d3cce25f3d7e0479a2ef93f5e1474f224cf3b3475fc032c83b697be35d
sha256(落盘那份) : 15f932d3cce25f3d7e0479a2ef93f5e1474f224cf3b3475fc032c83b697be35d   ← 相同
```
护栏：`test_the_code_is_saved_verbatim`、`test_every_call_gets_its_own_file_and_reset_keeps_them`
（每次一份、序号确定）、`test_the_scratch_dir_is_per_block`（跨块不覆盖）。

### ② 返回：只增不减（改前 / 改后）

```
改前（HEAD）成功 : {"ok": true,  "stdout": "42\n42\n", "value": null, "cwd": "…"}
改后        成功 : {"ok": true, "code_path": "_code/T-01/step-0001.py", "truncated": false,
                    "stdout": "42\n42\n", "value": null, "cwd": "…"}

改前（HEAD）失败 : {"ok": false, "stdout": "", "error": "ValueError: boom", "cwd": "…"}
改后        失败 : {"ok": false, "code_path": "_code/T-01/step-0002.py", "truncated": false,
                    "stdout": "", "error": "ValueError: boom", "cwd": "…"}
```
★ 原有键一个没动、一个没删（`test_the_return_only_gains_keys`）；`code_path` **恒在**（失败为 `""` + `code_save_error`），
`truncated` 恒在，`full_output_path` 只在真截断时出现。

### ③ 截断 = 换页（反证：能拿回被截掉的部分）

```
5000 行输出 ⇒ truncated: true
  marker : …[output truncated: kept the last 2000 lines / 10000 bytes of 5000 lines / 23890 bytes;
             full output at _code/T-01/step-0003.out]
  full   : _code/T-01/step-0003.out —— 5000 行（全量）
  读数   : full_output_lines=5000  full_output_bytes=23890（结构化，不只是一句自然语言）
```
反证（`test_a_long_output_is_paged_and_the_full_output_is_readable`）：正文里搜不到开头的 `0`，
但 `full_output_path` 那份里**有**；`test_the_byte_cap_binds_for_one_huge_line`（一行 60KB ⇒ 按字节截）。
★ 没超上限时**一个字节都不动**（`test_a_small_output_is_untouched`：不写 `.out`、不改 stdout）。

### ④ `_code/` 不是产物

```
unit : ledger.changed() == ("out.csv",)        ← 落盘的代码**不在** changed 里
e2e  : _run/artifacts/tree/** 里没有 _code 分量；commit.json 的 files 里也没有
```
护栏：`test_the_scratch_dir_never_shows_up_as_a_changed_file`、`test_the_judge_never_sees_the_scratch_dir`。

### ⑤ ★ 真模型（`Qwen/Qwen3.5-4B`，tag=`qwen4b5`）—— 判据**未满足**：`files.patch` 仍是 0

T-01 就是那个"整段重贴、从不 patch"的块，读数：

```
                     改前 qwen4b3     改后 qwen4b5
轮次                 31              42
时长                 ~55 min         61.9 min
prompt_tokens        —               2,089,755
completion_tokens    —               42,893
code.run             18              31
★ files.patch        0               0            ← ★ 判据未满足
落盘的草稿纸          0（根本没落过）  31 份 _code/T-01/step-0001..31.py（真跑里确实落了盘）
.out（换页文件）      —               0（这轮没触发截断）
产物 output/          —               未交付（T-01 未跑完，端点又慢又卡，中止）
痕迹仓库里的 _code    —               **没有**（④ 在真跑里也成立）
```

⇒ **机制到位了、31 份代码真的躺在磁盘上，模型仍然一次没改它** —— 它继续在 `code.run` 里整段重贴 31 次。

★ 按任务书 §五⑤ 的预先规定：**不许硬凑，如实报**。这条**不**推翻本改动
（判据本身就说"如果还是 0，那说明还有别的原因（下一轮再查）"）。下一轮可查的候选方向：

```
① 它不知道那个文件存在（描述在工具菜单里，但没进它的"当前处境"）—— 返回里给的是一个 JSON 键；
② `files.patch` 要它先读回文件、比直接重贴一段更费事（省的是"消灭错误"，不是"少打字"）；
③ 它压根没在"改代码"—— 它在反复重试一种**思路**，而不是在修补一段**文本**。
```


### ⑥ 反向：既有全量两臂逐条同结果

```
新臂（HEAD+本改动）：1145 passed
旧臂（HEAD 树 + 新测试文件）：1127 passed
逐条比（都排除新测试文件）：identical = 1127，differing = 0，only-new = 0，only-old = 0
新测试文件在旧臂上**整个模块 ImportError**（`CODE_DIR` 在旧代码里不存在）
```
⇒ 既有 **1127** 条两臂逐条同结果；没有一条"只在新版通过"。
（★ 任务书写"基线 1109"，那是**上上轮**的数字；上一轮（过程级信息交换）加了 18 条 ⇒ 现基线 **1127**。）

### ⑦ 架构纯洁性 / ruff / 全量

```
pytest -m arch_guard -q   -> 41 passed, 1104 deselected
ruff check src/ tests/    -> All checks passed!
default_audit.py --check  -> exit 0
pytest tests/ -q          -> 1145 passed（基线 1127 + 18）
```

### ⑧ preflight

```
tools.reverse_validation.preflight() -> mutations: 256, problems: 0  ⇒ 空
```

### ⑨ 不确定项

见 §5。

---

## 4. 与业界对照（外部调研，只列影响决策的）

```
· Claude Code：成功结果内联 ~30,000 字符，超出给「会话目录里的文件路径 + 前 2,000 字符预览」；
  落盘文件 >64 MiB 再截；失败结果内联 ~10,000 且**不给路径**（这是它的缺口，没照抄）
  ⇒ 印证"两层上限 + 落盘可寻址"。2000 行 / 50KB 落在业界区间内（OpenHands 正是 50,000 字符）。
· OpenHands：截断 notice 同时给**保存路径**与**总量**；写盘包 try/except，失败只记日志（= 我们的 fail-open）。
  它还主动**教模型"先落盘再跑"**（`/tmp/run.py`）—— 与方案 B 同向。
· Codex：通用截断器是"头尾对半 + …N chars truncated… + Total output lines: N"。
  ★ 我们**只留末尾**（任务书 §三③ 的指定），这是**有意识的偏离**，依据是社区硬规则
  "`tail -n 200`, never `tail -3`"——**点明失败的那一行在末尾**；偏离写在注释与这里。
· 反面教材：Jupyter 超限就"停止发送"，**超出的输出永久丢失** —— 正是本任务要避免的那一半。
· Aider / Cline：把"定点改"设为默认、"整段重写"降为特例 —— 与"落盘让 patch 够得着"同向。
· 命名：reproducible-builds「Timestamps are best avoided」⇒ 用**块 + 单调序号**，不用时间戳。
```

---

## 5. 不确定项（如实列）

1. **★ ⑤ 判据未满足（如实报）**：改后 T-01 跑了 31 次 `code.run`（31 份代码真的落盘），
   `files.patch` **仍然是 0**。⇒ "够得着"这一步做到了，但模型**不去够**。
   这是下一轮的调优点（候选见 §3⑤），本轮**没有**在 prompt 里加任何命令式要求。
2. **`value` 不截（审计 #67）**：`code.run("'x'*200000")` 这类"最后一行求值出巨大对象"时，
   `value`（末尾表达式的 repr）能到 20 万字符，不受 2000 行/50KB 的约束。截它等于改语义，
   本轮不截 —— **这是剩下的一个上下文风险**。
3. **`code.run` 无超时（审计 #68，既有缺口）**：`while True: pass` 会挂住整跑。给它加超时是行为变更，不在本轮。
4. **`_code/` 不进 `WriteBudget` 的闸、`close()` 不清理（审计 #66 余下部分）**：
   按硬边界⑦"落盘不许把块拦下来"故意不设闸；补偿是 8 MiB 上限 + 窄分量排除 + 只记证据字节。
5. **`tool_result` 事件不带 `code_path`（审计 #69）**：事件在 `assemble_impl/produce.py`，不在允许改动范围。
   替代观测面：上一轮的 `traces.jsonl` 里存着 code.run 的完整返回 JSON。
6. **`files.patch` 的语义没变**：本改动只负责"够得着"；改完之后**它是不是会被用**，是下一层的事。
7. **变异 256 / 跨种子 42 未跑**（不在验收项；与真模型跑互斥）。
8. **落盘位置在工作区内**（业界多放仓库外）：受任务书 §三① 约束（要让 `files.patch` 够得着），
   补偿是窄分量排除 —— 代价是工作区里会多一个 `_code/` 目录（模型 `ls` 能看到）。

---

## 6. 工作记忆结账

* suspicious：`#65/#66/#70/#71/#72` 已处置；`#67/#68/#69` 标「已确认」并写明不处置理由。**0 条未验证**。
* interesting：`#92–#95/#97–#99/#101–#106/#107–#110/#112` 已采纳；`#96/#100/#111` 不适用。**0 条未采纳**。

---

## 附录：qwen4b5 读数（T-01，未跑完）

来源：`.bobanana6/ab/archives/qwen4b5-A-c1.jsonl`（流式落盘，T-01 共 42 条）。

```
T-01 turns = 42        时长 = 61.9 min
prompt_tokens = 2,089,755   completion_tokens = 42,893
工具直方图 = {task.contract:1, sources.snapshot:7, shell.run:7, sources.grep:1,
              code.run:31}          ★ files.patch = 0
落盘的草稿纸：31 份 _code/T-01/step-0001..31.py（0 份 .out）
产物：_run/workspace/output/ 为空（T-01 未交付）
④ 复验：_run/artifacts/tree/** 里没有任何 _code 分量（没污染判定器）
★ 判据（files.patch 从 0 变非 0）**未满足** —— 如实报；整跑（8 块）未等完。
```

提交：`47693e3`（`git diff --stat` 见提交信息）。本轮代码在跑测时已在工作树里；
提交不改文件，故读数锚定同一份代码。
