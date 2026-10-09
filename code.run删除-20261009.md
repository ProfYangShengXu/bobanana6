# 删掉 `code.run`（能力由 `shell.run` 接管，两个既有缺口随之消失）

日期：2026-10-09 · 提交：见本次 commit（message 同标题）
基线：`773b483` 的工具面 19 条；改后 18 条。

> **一句结论**：`code.run` 从基表、注册表、部署开关、实现（含整条 `_Code*` 机器）四处删净，
> 生产代码 0 处引用；`files.write` / `files.patch` 描述里指向它的两处措辞改指 `shell.run`。
> 全量 **1173 passed**（0 skip / 0 xfail），arch_guard 41、ruff 绿，删除守卫在旧树 **5 红**。
> ★ 但**任务书 §二的能力对照表有一条不成立**：`shell.run` **没有** `full_output_path` 换页，
> 且它把头截到 4000 字符、默认 60s 超时 —— 详见 §4。本轮按 §六「不许改 `shell.run`」不动它。

---

## 0. 为什么删（实测，不是洁癖）

```
qwen4b6：BOBANANA6_CODE_RUN=off ⇒ code.run 0 次，改走 shell.run 72 次
qwen4b7：忘了设（默认开）      ⇒ code.run 立刻回来 30 次（占全场 46%）
```

同一个模型、同一道题、只差一个开关，行为直接翻转；而开关**默认开** ⇒ 兜底路径就是默认路径。
**让正确形态成为唯一形态，不靠配置。**

---

## 1. 工具面清单对照（逐条；★ 只有一行被删，其余逐字保留）

| # | 工具 | 改动前 | 改动后 | 说明 |
|---|---|---|---|---|
| 1 | `semantics.lookup` | ✅ | ✅ | 未动 |
| 2 | `semantics.map` | ✅ | ✅ | 未动 |
| 3 | `semantics.check` | ✅ | ✅ | 未动 |
| 4 | `task.mysubtask` | ✅ | ✅ | 未动 |
| 5 | `task.upstream` | ✅ | ✅ | 未动 |
| 6 | `task.upstream_all` | ✅ | ✅ | 未动 |
| 7 | `task.contract` | ✅ | ✅ | 未动 |
| 8 | `check.local` | ✅ | ✅ | 未动 |
| 9 | `check.provenance` | ✅ | ✅ | 未动 |
| 10 | `check.assemble_dryrun` | ✅ | ✅ | 未动 |
| 11 | `sources.snapshot` | ✅ | ✅ | 未动 |
| 12 | `sources.read` | ✅ | ✅ | 未动 |
| 13 | `sources.grep` | ✅ | ✅ | 未动 |
| 14 | `files.write` | ✅ | ✅ | **描述改**：大文件生成「用 code.run」→「用 shell.run」（描述不改就是指向一个不存在的工具） |
| 15 | `files.patch` | ✅ | ✅ | **描述改**：同上，「产物要程序算」→ shell.run |
| 16 | `shell.run` | ✅ | ✅ | **描述改**：删掉「Python 逻辑用 code.run」，改指「写成 `*.py` 再 `python x.py`」；语义/参数/返回一字未动 |
| 17 | **`code.run`** | ✅ | ❌ **删** | 基表 ToolSpec + `_TOOL_DESCRIPTIONS` 条目 + `TOOL_NAMES` + catalog 绑定 + handler + 实现全删 |
| 18 | `traces.search` | ✅ | ✅ | 未动 |
| 19 | `traces.read` | ✅ | ✅ | 未动 |

合计：**19 → 18**；反向核对（§5.4）证实对称差**恰好**是 `{code.run}`，没有新增、没有误删。

### 1.1 规格之外连带拆掉的机器（不是"接口还在、实现空转"）

| 位置 | 拆了什么 |
|---|---|
| `core/ports.py` | `ToolSpec("code.run")`、描述条目、`ToolSet.code_run` 字段与 `_advertises` 的 `code.` 分支、`WorkspaceTools.run_code` 端口；`files.write`/`files.patch`/`shell.run` 三处描述 |
| `services/registry.py` | `TOOL_NAMES` 条目、catalog 条目、`def code_run`、`build_registry` 里"给工作区上块号"的一小段（只为 `_code/<块>/` 服务） |
| `services/tools.py` | `build_toolset(code_run=…)` 参数、`CODE_RUN_ENV`、`code_run_enabled()`（连带删掉只被它用的 `import os`） |
| `services/workspace.py` | `run_code`、`_code_root`、`_save_code`、`_page_code_output`、`_tail_page`、`_slug`、`_metadata_first`、`_CodeSession`、`_DRIVER`、`CODE_DIR`、`CODE_OUTPUT_MAX_LINES/BYTES/FULL_MAX_BYTES`、`BoundWorkspace.block`/`_code_calls`/`_code_dump_failures`/`_code_session`；`IGNORED_PARTS` 去掉 `_code` |
| `assemble_impl/produce.py` | `_EXECUTION_ARGUMENTS` 去掉死元素 `"code"`（执行类调用只剩 `shell.run` 的 `command`）；5 处 model-facing 文案 + 6 处注释改指/去掉 `code.run` |
| `assemble_impl/answer_contract.py` | `files` 字段描述里"已写盘通道"去掉 `code.run` |
| `services/traces.py` | `_safe_text` 注释里的例子换成不绑定某个工具的表述 |
| `acceptance/harness.py` · `pyproject.toml` | 两处注释改指 `shell.run` |

---

## 2. 测试怎么办（★ 迁移的 3 条 / 删除的 22 条 / 新增的 10 条）

### 2.1 迁移（能力级；换到 `shell.run` 上仍成立，**不许删**）

| 原断言 | 去向 | 理由 |
|---|---|---|
| 长输出正文截断 + 全量可取回 | `test_shell_run_capabilities.py::test_a_long_output_is_truncated_but_the_full_output_is_recoverable` | 能力=「输出不丢、可取回」；形态改为「命令自己重定向到文件」（shell 无 `full_output_path`，见 §4） |
| 小输出一个字符不动（反向） | 同文件 `::test_a_small_output_is_untouched` | 同上，反向样例 |
| 块的执行环境能 import pandas/numpy | `test_services.py::test_pandas_and_numpy_are_importable_in_the_block_execution_environment`（`run_code` → `shell.run` 跑 `python -c`） | 能力=执行环境具备依赖，与用哪个工具跑无关 |

### 2.2 新增（★ 能力守卫 + 删除守卫）

`tests/test_shell_run_capabilities.py`（新增）：
- `test_the_timeout_parameter_reaches_the_shell_and_actually_bites` —— **能力**：超时真的掐（对照审计 `#68`：原工具全程无超时，一个死循环挂住整跑）。旧树也绿（shell 本来就有），它是**能力守卫**不是删除守卫。
- `test_a_shell_side_effect_is_accounted_like_a_file_write` —— **能力**：命令副作用与 `files.write` 走同一本 ledger。

`tests/test_tool_face_removal.py`（新增，★ 反证载体）：
- `test_the_removed_tool_is_gone_from_every_table`（基表/描述/TOOL_NAMES/注册面）
- `test_the_deployment_switch_is_gone_too`（不留"开回来"的暗门）
- `test_no_workspace_runs_code_anymore`（端口与实现对拆）
- `test_every_description_points_at_a_tool_that_exists`（**通用** drift 守卫：描述点名的工具必须真在基表里）
- `test_no_production_source_still_names_the_removed_line`（把 §5.5 的 grep 自检变成可执行断言）
- `test_the_migrated_tests_do_not_import_the_removed_module`（`CODE_DIR` 等常量随实现删净）

### 2.3 删除（工具级；每条具名理由）

| 原测试 | 理由 |
|---|---|
| `test_the_code_is_saved_verbatim` | 逐字节落盘随 `_save_code` 一起消失 |
| `test_every_call_gets_its_own_file_and_reset_keeps_them` | 会话持久 + `reset` = 本轮**唯一真损失**，无对应物 |
| `test_the_return_only_gains_keys` | 是 `code.run` 的返回键形状 |
| `test_the_registry_payload_carries_the_new_fields` | 同上 |
| `test_the_byte_cap_binds_for_one_huge_line` | 字节上限是 code.run 换页机制；shell 按 4000 **字符**截 |
| `test_the_scratch_dir_never_shows_up_as_a_changed_file` | 没有 `_code/` 草稿纸目录了 |
| `test_the_judge_never_sees_the_scratch_dir` | 同上 |
| `test_saving_is_auxiliary_and_never_blocks_the_run` | "落盘失败不阻塞"随落盘一起消失 |
| `test_a_code_dump_failure_does_not_lose_an_oversized_output` | 同上 |
| `test_code_run_still_takes_exactly_two_parameters` | 「恰好两个参数」—— 任务书点名不适用 |
| `test_the_scratch_dir_is_per_block` | 落盘路径 `_code/<块>/…` 序号不撞车 —— 任务书点名不适用 |
| `test_an_unscoped_workspace_does_not_impersonate_a_block` | 同上，没有"未上块号"这回事了 |
| `test_the_metadata_keys_are_ordered_before_stdout` | `#71` 键序随返回体消失 |
| `test_the_registry_payload_also_puts_the_metadata_first` | 同上 |
| `test_a_huge_full_output_is_capped_and_flagged` | 8 MiB 全量文件闸随全量文件消失 |
| `test_writing_the_dump_does_not_go_through_the_write_gate` | 草稿纸不过 `WriteBudget` —— 没有草稿纸了 |
| `test_services.py::test_the_code_session_keeps_its_namespace_and_reset_drops_it` | 会话持久 = 唯一真损失 |
| `test_workspace_told.py::test_run_code_reports_an_absolute_cwd` | code.run 的 `cwd` 字段；`shell.run` 的 cwd 断言原地保留 |
| `test_tool_face.py::test_code_run_defaults_to_on_and_can_be_switched_off` | 开关没了 |
| `test_tool_face.py::test_the_switch_reaches_the_assembled_toolset` | 开关没了 |
| `tests/conftest.py` 的 `_no_ambient_code_run_switch` fixture | 开关没了（整文件删） |
| `test_code_run_saves.py` 的文件级存在（其余断言已逐条列在上表） | 整份文件按上表处置 |

★ 保留（任务书点名）：`files.patch` 的「每条 oldText 唯一」等断言留在 `test_patch_edits.py` 原地不动，
只把 docstring 里对已删工具的指涉去掉。

### 2.4 名称级引用清理（非测试条目）

- `tests/test_traces.py`：8 处把 `code.run` 当"任意工具标签"的用例改成 `shell.run`（并同步 args 形状），
  测试语义不变。
- `tests/test_tool_face.py`：开局面断言 `code.run` 不在菜单，改用 `_has(opened, "code.") == set()`。
- `tests/test_workspace_told.py`：模块 docstring 第③条去掉已删工具的指涉。

---

## 3. 两个既有缺口随删除消失（★ 报告里明确写一句）

```
审计 #67  大对象不截：code.run 只截 stdout，value/error 不截  ⇒ 随实现删除而消失
审计 #68  全程无超时：_CodeSession.readline 无 deadline，死循环挂住整跑 ⇒ 随实现删除而消失
```

这不是"顺手删了个工具"：这两个缺口是**该工具独有**的失败面，删掉实现即消失（不是被修，是不存在了）。

---

## 4. ★ 与任务书 §二不一致的两条**真实损失**（必须写清，不许当没有）

任务书 §二断言「`shell.run` 输出截断：有替代」「唯一真损失是会话持久」。实测**不成立**：

| 能力 | `code.run`（改前） | `shell.run`（改后） | 差距 |
|---|---|---|---|
| 输出截断 | 只留**末尾** 2000 行 / 50 KB + 全量落 `full_output_path` + `full_output_lines/bytes` 读数 | 只留**开头** 4000 **字符**，只有 `truncated: true`，无全量路径、无规模读数 | ★ **尾部（报错/最终值常在末尾）被静默丢掉且取不回**；`full_output_path` 这个键不存在 |
| 长计算 | 无超时，能跑几分钟 | 默认 **60s**、上限 600s；描述未写默认值 | ★ 原本能跑完的 pandas/shell 计算会在 60s 被 `TimeoutExpired` 杀掉 |

**处置**：本轮**严格按 §六不动 `shell.run`**。§三里「完整输出在 `full_output_path` 里取回得回来」
这条**以等价能力迁移**（命令自行 `> full.txt` 重定向，全量一行不少，断言见 §2.1），
但 `full_output_path` 作为**机制**随 `code.run` 消失，如实记在这里。
⇒ **建议下一轮单开**：给 `shell.run` 补「尾截 + 头尾预览 + 全量落盘 + 描述写明默认超时」
（同类先例：qwen-code `#11729` 的两套截断策略打架事故、Codex `#14206` 的 spill 信封契约）。

---

## 5. 验收读数（顺序写死：反证 → 架构门 → 全量 → 泛化 → 死代码）

### 5.1 ① 反证：新增/改动的测试**旧代码红、新代码绿**

用 `HEAD`（`773b483`）建独立 worktree，把两个新测试文件拷进去跑：

```
旧树（HEAD）：5 failed, 5 passed
  红 = 6 条删除守卫里的 5 条（基表/开关/端口/源码扫描/常量）
  绿 = 迁移后的 shell 能力 4 条 + 通用 drift 守卫 1 条（旧树下 code.run 确实存在，描述指向它是对的）
新树：11 passed（见 5.3）
```

### 5.2 ② 架构纯洁性门

```
pytest -m arch_guard -q   ⇒  41 passed
ruff check src tests      ⇒  All checks passed!
```

### 5.3 ③ 全量 pytest

```
改前：1185 passed（60.68s）
改后：1173 passed（52.64s）   ← 0 skipped / 0 xfailed / 无注释掉的断言
差 = −12 = 删 22 − 加 10（逐条去向见 §2）
```

### 5.4 ④ 四条泛化检验

| 检验 | 做法 | 读数 |
|---|---|---|
| 换规模 | 来源数 0/1/5；输出 1/100/5000 行 | 各规模菜单里 `code.run` 全不在；1 行→2 字符不截、100 行→292 字符不截、5000 行→正文 4000 字符 + `truncated`，盘上全量 5000 行 |
| 换领域 | 端到端离线跑 `examples/task.yaml --offline` | **exit 0**（`reports: CONFLICTS.md, ONTOLOGY-MAP.md, SOURCES.md, summary.json`） |
| 换块类型 | 无工作区 / 有工作区 / 有语义 / 有交付物 / 有 traces | 五种菜单 `code.run` 全不在；`shell.run` 恰好只在"有工作区"时出现（fail-closed 不变） |
| 反向 | 新 `TOOL_NAMES` + 那个名字 == 旧 `TOOL_NAMES`？ | 旧 19、新 18，对称差 **恰好 `{code.run}`**；`new − old = ∅`（没有新增、没有误删别的工具） |

### 5.5 ⑤ 死代码自检

```
生产代码（src + acceptance/harness.py + pyproject.toml）
  grep -E 'code\.run|code_run|CODE_RUN|run_code'  ⇒  0 处
src 里 grep 'run_code'                            ⇒  0 处
非 .md、非 .bobanana6、非 acceptance/ab 全仓        ⇒  0 处
剩余只在文档/报告（历史叙述）：AGENTS.md（3 处，已标注"已删"）· 工具面收束-20261009.md · 多agent协作-文献情报.md · 本报告
```

---

## 6. 不确定项 / 留待下一轮

1. **`shell.run` 的尾截 + 全量落盘**（§4，P0）—— 本轮明确不做，建议单开一轮。
2. **`shell.run` 默认 60s 超时**会改变"原本用 code.run 跑长计算"的行为（§4，P1）；描述里没写默认值，
   模型不会主动传 `timeout`。
3. **`shell.run` 的 `capture_output=True` 先把全部输出读进内存再加 4000 截断**（审计 `#88`，P0 同源）——
   大输出可能 OOM。不在本轮范围。
4. **`_background` 无数量上限、后台任务在 `changed()` 之后落地的产物静默丢失**（审计 `#90`，P2）——
   删 `code.run` 后 `shell.run` 成唯一执行通道，暴露面上升。
5. **`consist` 的能力清单仍是静态 `TOOL_NAMES`**（审计 `#77`）—— 删除不影响它，但按需层与它脱钩的问题
   依旧（`requires: ["code.run"]` 的任务现在会在计划期具名拒，属 fail-closed，已 grep 无此类声明）。
6. **本任务未跑变异与跨种子**（AGENTS §4.1bis 把它们排在系列末尾）—— **没跑就是没跑**，不冒充通过。

---

## 7. 本块改了哪些文件（绝对路径 + 每个文件改了什么）

新增：

- `/mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6/code.run删除-20261009.md`（本报告）
- `…/bobanana6/tests/test_shell_run_capabilities.py`（迁移 + 能力守卫，4 条）
- `…/bobanana6/tests/test_tool_face_removal.py`（删除守卫，6 条）

删除：

- `…/bobanana6/tests/test_code_run_saves.py`（18 条，去向见 §2）
- `…/bobanana6/tests/conftest.py`（唯一内容是该开关的 autouse fixture）

生产代码：

- `…/bobanana6/src/bobanana6/core/ports.py` —— 删 spec/描述条目/`ToolSet.code_run`/`_advertises` 分支/端口；改 3 处描述；注释 19→18
- `…/bobanana6/src/bobanana6/services/registry.py` —— 删 `TOOL_NAMES` 条目、catalog 条目、`def code_run`、上块号小段
- `…/bobanana6/src/bobanana6/services/tools.py` —— 删 `code_run` 参数、`CODE_RUN_ENV`、`code_run_enabled`、`import os`
- `…/bobanana6/src/bobanana6/services/workspace.py` —— 删整条 `_Code*` 机器与代码落盘常量/字段；模块 docstring 与 ledger docstring 同步
- `…/bobanana6/src/bobanana6/assemble_impl/produce.py` —— 5 处文案 + 6 处注释 + `_EXECUTION_ARGUMENTS`
- `…/bobanana6/src/bobanana6/assemble_impl/answer_contract.py` —— `files` 字段描述
- `…/bobanana6/src/bobanana6/services/traces.py` —— `_safe_text` 注释
- `…/bobanana6/acceptance/harness.py` —— 2 处注释
- `…/bobanana6/pyproject.toml` —— 1 处注释

文档：

- `…/bobanana6/AGENTS.md` —— `#47` 条目：`code.run` 标注"曾是……已删"，指向本报告

测试（既有文件）：

- `…/bobanana6/tests/test_services.py` —— `_PLAUSIBLE_ARGS` 去条目、无工作区集合去成员、pandas 测试换 shell、删会话测试
- `…/bobanana6/tests/test_tool_face.py` —— 删 2 条开关测试、开局断言改负向、`configurations` 去 2 项、docstring 同步
- `…/bobanana6/tests/test_workspace_told.py` —— 删 code 的 cwd 测试、docstring 第③条
- `…/bobanana6/tests/test_patch_edits.py` —— docstring 去指涉
- `…/bobanana6/tests/test_traces.py` —— 8 处标签/参数改 `shell.run`

仓库外（不进提交）：

- `/mnt/c/Users/45140/OneDrive/Desktop/项目/_code_run_generalize.py`（四条泛化检验脚本）
- `/mnt/c/Users/45140/OneDrive/Desktop/项目/_wt_oldcr/`（`HEAD` 的 worktree，用于 §5.1 反证；用完已删）
