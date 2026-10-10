# `shell.run` 补四条（截断=换页 + 头尾预览 + 描述写默认超时 + 超时收进程树）

日期：2026-10-09 · 基线：`922dd4a`（上一轮删 `code.run` 的提交）
> **一句结论**：上一轮如实退回的两条真损失补上了 —— `shell.run` 现在**头尾预览 + 全量落盘**
> （`full_output_path` / `full_output_lines` / `full_output_bytes`），超时**收整棵进程树**
> 且**收尸绝不阻塞在逃逸后代上**，描述里写明默认 60s / 上限 600s。全量 **1181 passed**
> （0 skip / 0 xfail），arch_guard 41、ruff 绿；新增 8 条断言在旧树 **8 红 / 4 绿**（反证见 §4）。
> 600s 上限**未改**（给了实测与建议，§5）。

---

## 0. 背景

删 `code.run` 时，能力对照表断言"输出截断有替代、唯一真损失是会话持久"——实测两条不成立：

```
① 输出截断  code.run：尾 2000 行/50KB + full_output_path + 规模读数
            shell.run：只留【开头】4000 字符、只有 truncated:true，尾部（报错/最终值）丢且取不回
② 超时      code.run：无超时；shell.run：默认 60s、上限 600s，★ 描述里没写默认值
```

本轮把①②③④四条补上，且**全是"告知"类**：不改任何判据、不改 `shell.run` 参数表、不新造工具。

---

## 1. 四条改动

### ① 截断 = 头尾预览

阈值是模块顶部常量（不散落）：

```python
SHELL_OUTPUT_HEAD_LINES = 40      SHELL_OUTPUT_HEAD_BYTES = 4 * 1024
SHELL_OUTPUT_TAIL_LINES = 200     SHELL_OUTPUT_TAIL_BYTES = 8 * 1024
```

- 头（表头 / 前几行数据）+ 尾（报错 / 最终值 / traceback）两段都留，中间**可见地**省略：
  `…[output truncated: omitted N lines / M bytes; full stdout+stderr at <path>]…`
- 行数与字节数"先到者为准"；单行超长（无换行）也能头尾各取一段（用**字节**保证两段不重叠）。
- ★ 边界：`<= 240 行 且 <= 12KB` 不截断（`240/241` 两条边界见 §4 泛化）。

### ② 完整输出落盘 + `full_output_path`

- 截断时把 `stdout + "\n----- stderr -----\n" + stderr`（只有一股流时就是那一股）**原样**落盘到
  工作区 `_shell/`，文件名 `_shell/out-<pid>-<每次进程随机6位>-<进程内序号>.log`
  （序号全局单调 ⇒ 同进程多块不撞车；随机尾巴 ⇒ 跨 run 同 pid 也不覆盖）。
- 返回只增不改：`full_output_path` / `full_output_lines` / `full_output_bytes`；**没截断一个都不给**。
- ★ **落盘文件不是产物**：`SHELL_OUTPUT_DIR` 进 `IGNORED_PARTS`（照 `_code/` 先例）⇒
  `_digests` 看不见它 ⇒ `ledger.changed()` 不含它 ⇒ 通道 2 对账不收它（§4⑥ 有断言）。
- ★ **落盘失败就不销毁**：返回**全量**正文 + `full_output_error`、`truncated=False`
  （照上一轮删掉的那个"先落盘再跑"工具的同一守卫）—— 绝不出现"截了却取不回"。

### ③ 描述写明默认超时

`core/ports.py` 的 `shell.run` 描述（照现有"何时用 / 输入 / 何时不用"文体，长度量级不变）：

```
输入：command = 命令；workdir = 工作目录；
timeout = 秒（★ 默认 60、上限 600，不够要显式给）；background = 后台跑。
★ 输出太长时正文只留【头 + 尾】两段，中间省掉多少会说清；被截时完整输出落盘，
路径在返回的 `full_output_path` 里。
```

### ④ 超时收整棵进程树 + 收尸不阻塞

- 前台 Popen 加 `start_new_session=True`（自成进程组）；超时时 `_kill_tree` 用
  `os.killpg(os.getpgid(pid), SIGKILL)` 收整组；组已不在 / 没权限 / Windows 无 `killpg` 时
  退回 `process.kill()`，两次都落空**也不掩盖 `TimeoutExpired`**（对外契约不变）。
- ★★ **`_reap`：killpg 之后绝不用裸 `communicate()`**。`setsid`/daemonize 的孙进程脱离进程组、
  `killpg` 收不到它，而它还握着 stdout/stderr 的写端 ⇒ 裸 `communicate()` 会一直等 EOF，把整块
  **挂死**（连 `TimeoutExpired` 都抛不出来）。改为 `wait(timeout=5)` 只收直接子进程，再关掉管道。
  实测（§4⑤）：pre-fix 挂 **19.0s**，fix 后 **~1s** 抛 `TimeoutExpired`。

---

## 2. 逐字读数：同一条命令，改动前后返回 JSON

（`cwd` 是每次的临时目录，只影响该字段本身；正文按需截断显示。）

### 2.1 截断样例 `seq 1 5000`

**改前（`922dd4a`）**
```json
keys: ["background","exit_code","cwd","stdout","stderr","truncated"]
{ "background": false, "exit_code": 0, "cwd": "<ws>",
  "stdout": "1\n2\n…\n1021\n10",          // ← 只留开头 4000 字符，切在行中间
  "stderr": "", "truncated": true }
spill_files: []
```

**改后**
```json
keys: ["background","exit_code","cwd","truncated","full_output_path","full_output_lines","full_output_bytes","stdout","stderr"]
{ "background": false, "exit_code": 0, "cwd": "<ws>", "truncated": true,
  "full_output_path": "_shell/out-<pid>-<rand6>-0001.log",
  "full_output_lines": 5000, "full_output_bytes": 23893,
  "stdout": "1\n…\n40\n…[output truncated: omitted 4760 lines / 22782 bytes; full stdout+stderr at _shell/out-<pid>-<rand6>-0001.log]…\n4801\n…\n5000\n",
  "stderr": "" }
spill_files: ["_shell/out-<pid>-<rand6>-0001.log"]   // 盘上 5000 行，首 1 尾 5000
```

### 2.2 不截断样例 `printf 'small'`

**改前 / 改后**：键序与值**逐字相同**（除 `cwd` 是本次临时目录）：

```json
keys: ["background","exit_code","cwd","stdout","stderr","truncated"]
{ "background": false, "exit_code": 0, "cwd": "<ws>", "stdout": "small", "stderr": "", "truncated": false }
spill_files: []      // ★ 一个文件都不落
```

⇒ **"没截断 = 原样"是可逐字对上的**（键序也一样），新增键只在截断时出现。

---

## 3. ★ 与 flash `bash` 的四处差异（每处说明取舍）

| # | 差异 | flash `tools/bash.py` | 本仓 | 为什么这样取舍 |
|---|---|---|---|---|
| 1 | 截断方向 | **只留末尾** 2000 行/50KB | **头 + 尾**两段 | bb6 的任务里"先看表头/前几行数据"与"看末尾报错"一样常见；只留尾会让模型连表结构都看不到。同类先例：goose 的行数维度省略（保留前 20 行 + `... (N more lines)`）、Claude Code 对**失败**结果给头+尾摘录 |
| 2 | 默认超时 | `timeout` 描述写"**默认无超时**" | 默认 **60s** / 上限 **600s**，且**写进描述** | bb6 要求整跑有界；默认值不写进描述正是上一轮暴露的缺口。官方 Claude Code 也把 `Bash` 的 timeout 默认/上限写进工具描述 |
| 3 | 收树 | `killpg(...)`，失败退 `proc.kill()` | 同为 `start_new_session` + `killpg`，但**失败路径两级**、吞掉 fallback 的 `OSError`，并额外用 `_reap` 避免挂死 | 只剩 shell 已消失时 `proc.kill()` 自己也会抛（`ESRCH`），会**盖掉 `TimeoutExpired`**；逃逸出进程组的孙进程还握着管道 ⇒ 裸 `communicate()` 挂死（实测 19.0s）。对齐 codex `process_group.rs`"把 ESRCH 当成功"+ 有界收尸 |
| 4 | 落盘位置 | `ctx.tmp_dir`（工作区之外） | 工作区里 `_shell/`（`IGNORED_PARTS` 排除） | bb6 的执行工具 cwd 与读写都在工作区内，模型**只能**用工作区里的文件工具回读；放工作区外等于拿不回。放工作区内则必须显式排除出 `deliverables`（照 `_code/` 先例） |

另：flash 把 stderr 并进 stdout（`stderr=STDOUT`），bb6 保持 `stdout`/`stderr` 两个返回键；
落盘那份用 `----- stderr -----` 分界，合并与否都不丢信息。

---

## 4. 验收读数（顺序写死）

### ① 反证：新增测试旧代码红、新代码绿

`HEAD`（`922dd4a`）独立 worktree + 拷入新测试（12 条）：

```
旧树：8 failed, 4 passed
  红 = 长输出头尾+落盘 / stderr 换页 / 多块不撞车 / 落盘失败不销毁 / 超时收树 /
       落盘不进产物 / 元数据抗压缩 / 描述写默认超时
  绿 = 短输出原样 / 超时参数真的掐 / 命令副作用进 ledger /
       逃逸后代不挂死（★ 见下）
新树：12 passed
```

★ 那条"逃逸后代不挂死"在旧树也绿：旧实现走 `subprocess.run(timeout=)`，POSIX 上超时后是
`wait()` 而非 `communicate()`，本来就不挂 —— 它是**本轮中间实现的回归守卫**（`killpg` 后裸
`communicate()` 挂 19.0s，见 §1④/§4⑤），不是旧树的缺口。如实标注，不冒充反证。

### ② 架构纯洁性门

```
pytest -m arch_guard -q  ⇒ 41 passed
ruff check src tests     ⇒ All checks passed
```

### ③ 全量 pytest

```
改前：1173 passed    改后：1181 passed（54.27s）
差 = +8 = 能力测试文件 4 → 12 条（新增/改动逐条见 §4① 反证清单）
★ 0 skipped / 0 xfailed / 无注释掉的断言
```

### ④ 四条泛化检验

| 检验 | 读数 |
|---|---|
| 换规模 | 1/100/240 行**不截断**无 marker 不落盘；241/5000 行与单行 60000 字符**截断**：marker 在、头尾都在、落盘文件在 |
| 换领域 | 端到端离线 `examples/task.yaml --offline` ⇒ **exit 0**（`reports: CONFLICTS.md, ONTOLOGY-MAP.md, SOURCES.md, summary.json`） |
| 换块类型 | 无工作区**不广告** `shell.run`；有工作区 / 有语义 / 有交付物 / 有 traces 都广告；真注册表里跑截断 ⇒ `full_output_path` 在、头尾都在 |
| 反向 | 短输出键序 `[background, exit_code, cwd, stdout, stderr, truncated]` **与改前逐字相同**、值相同、`files: []`（一个文件都不落） |

### ⑤ 孤儿进程自证

```
A) 普通孙进程（收树）
   命令：'<python> child.py ; echo done'，timeout=1；child.py 写 grandchild.pid 后 sleep 30
   旧树（922dd4a）：TimeoutExpired after 1.00s  grandchild_pid=5  still_running=True   ← 孤儿留下
   新树           ：TimeoutExpired after 1.00s  grandchild_pid=7  still_running=False  ← 整树收掉

B) 逃逸出进程组的孙进程（收尸不阻塞）
   命令：'setsid sh -c "…sleep 20" & sleep 20; echo done'，timeout=1
   pre-fix（killpg 后裸 communicate()）：被阻塞 19.0s  ← 块挂死
   新树（killpg + _reap）           ：~1s 抛 TimeoutExpired
```

（`still_running` 读 `/proc/<pid>/stat` 的状态，`Z` 视为已死。）

### ⑥ 落盘文件不进 deliverables

`test_the_spill_file_is_not_a_deliverable`：截断发生、`_shell/…` 真在盘上，
而 `ledger.changed() == ()` —— 产物清单**一个字节都不变**。

---

## 5. 超时上限要不要抬（§二）—— 实测 + 建议

造代表性工作量（本机 `.venv`，Linux/WSL）：

```
1,500,000 行 × 3 列 → CSV 38.9 MB
read_csv      0.70 s
groupby(2 键, sum/mean/count)  0.16 s
to_csv        0.45 s
────────────────────────────
合计          1.31 s
```

对照：上一轮真跑的 13f 产物是 1848 行 / 212KB，量级更小；社区实测 1M 行 ≈ 98.7MB 的 groupby
也在 1.85s。⇒ **600s 上限对这类任务余量约 450×，不需要抬**。
真正会挂超时的不是 CPU 计算，而是 **OOM / swap（大文件读进内存）、网络、卡死的外部进程**。

**建议：本轮不改 `MAX_TIMEOUT_S`（保持 600s），默认 60s 也不动**；600s 与 Claude Code 官方
`BASH_MAX_TIMEOUT_MS=600000` 同值。若要抬，应先解决"大输出读进内存"（§6 第 1 条），而不是加时间。
（按任务书：只给读数 + 建议，改不改下一轮定。）

---

## 6. 不确定项 / 留待下一轮

1. **落盘与内存都无上限**：`Popen(PIPE,PIPE)+communicate()` 先把**全部**输出读进内存，落盘写的是
   同一份全量；`shell.run("cat <1GB>")` 仍可能 OOM，落盘文件也可达 GB 级，且 `_shell/` 被
   `IGNORED_PARTS` 排除 ⇒ **不进 64MiB 的 `WriteBudget` 闸**。（审计 #107/#108；Claude Code 对
   spill 文件有 64 MiB 上限 + 输出超 5 GB 直接杀。）本任务不处置：§一② 要"完整原样落盘"，
   加上限与任务书冲突。
2. **后台任务仍按单进程收**：`close()` 对 `background=True` 的 Popen 只 `process.kill()`
   （它们没 `start_new_session`，不能对同组 `killpg`，否则会连自己一起杀）；`cmd &` 让 sh 先退时
   `poll()` 非 None 会跳过。审计 #109/#32/#90 仍在。
3. **长会话压缩仍会削掉预览尾段**：`compress.truncate_tool_results(cap=2000)` 按头部截，
   头尾预览的**尾段**在 compact 提交后不可见；但元数据键（含 `full_output_path`）**已实测**排在
   前 2000 字符内 ⇒ 模型仍能按路径回读全量。（审计 #112）
4. **超时失败丢 partial 输出**：`TimeoutExpired` 只按 `f"{type}: {exc}"` 汇报，`cause=""`。
   审计 #110；§三 不改归因语义。
5. **`timeout=0` 被注册表 `or None` 归成默认 60s**（绕过 `_timeout` 的 `<=0` 具名拒）。
   审计 #113；§三 不许改参数语义。
6. **`IGNORED_PARTS` 是 deliverables 排除的唯一单点**，按目录名整棵剪：真产物落 `_shell/` 或
   交付物恰叫 `_shell` 会被静默排除。审计 #118；照 `_code/` 先例的既有代价。
7. **本任务未跑变异与跨种子**（AGENTS §4.1bis 排在系列末尾）—— **没跑就是没跑**，不冒充通过。

---

## 7. 本块改了哪些文件

新增：

- `/mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6/shell.run补四条-20261009.md`（本报告）

生产代码：

- `…/bobanana6/src/bobanana6/services/workspace.py` —— 新增 `SHELL_OUTPUT_HEAD/TAIL_LINES/BYTES`、
  `SHELL_OUTPUT_DIR`（进 `IGNORED_PARTS`）、`SHELL_OUTPUT_STDERR_DIVIDER`、`_SHELL_OUTPUT_SEQ`、
  `_SHELL_OUTPUT_RUN`；`BoundWorkspace._shell_spill_failures`；重写 `run()`（头尾预览 + 落盘 +
  落盘失败不销毁 + 元数据键序 + `start_new_session`/`killpg`/`_reap`）；新增
  `_spill_shell_output` 与模块级 `_full_shell_output` / `_kill_tree` / `_reap` / `_fit_head` /
  `_fit_tail` / `_head_tail_page` / `_render_page`；`import signal`、`from itertools import count`
- `…/bobanana6/src/bobanana6/core/ports.py` —— `shell.run` 描述：写入默认 60 / 上限 600 与头尾换页语义

测试：

- `…/bobanana6/tests/test_shell_run_capabilities.py` —— 重写为 12 条（头尾预览 / 双流落盘 /
  短输出原样 / 多块不撞车 / 超时真掐 / **逃逸后代不挂死** / 超时收树 / **落盘失败不销毁** /
  副作用记账 / 落盘不进产物 / 元数据抗压缩 / 描述写默认超时）

仓库外（不进提交）：

- `_probe_shell_run.py` · `_shell_readings_old.json` · `_shell_readings_new.json`（§2 逐字读数）
- `_probe_tree.py`（§4⑤ 孤儿自证）· `_shell_run_generalize.py`（§4④ 四条泛化）
- `_shell_run_bench/`（§5 工作量实测）· `_wt_shell_old/`（`922dd4a` 的 worktree，用于 §4① 反证；用完已删）
