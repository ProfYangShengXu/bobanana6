# 压力任务 B：多模块代码库 → 收敛到共享抽象（`架构设计.md §15.5`）

> `#33` 建成。题目与读数见 `.bobanana6/b33/报告.md`；本文件写**这道题是什么、怎么跑、判什么**。

## 1 形状

```
给定一个多模块代码库，把散落在各模块的【重复实现】收敛到一个共享抽象。
```

`generate.py` 生成的东西（**只把 `input/` 交给运行**，`truth/` 只给判分器读）：

```
input/repo/interface.py          冻结的共享抽象接口（签名先定死；实现留给块）
input/repo/shop_a.py|b|c.py      三个模块，各有一份行为相同、写法不同的 total
input/repo/test_interface.py     冻结测试：实现出来的签名必须与 interface.py 一致
input/repo/test_shop_a|b|c.py    冻结测试：块内（各自行为）
input/repo/test_convergence.py   冻结测试：全局（三模块行为一致 + 都不再自带实现）
truth/truth.json                 interface.json 该长什么样（第四口径）
```

## 2 四个硬点 → 落点

| 硬点 | 落点 |
|---|---|
| 接口密度 | 模块边界由 `sources[].module` **声明**，切口与它对齐（壳体不解析路径猜包结构） |
| 写区互斥 | `partition: module:<名>`；`write_region` 只放行本模块声明的来源路径，越界**具名拒绝** |
| 跨块契约 | 共享抽象块排在接口块之后、模块块之前；模块块把共享抽象**声明成输入**（壳在派活前投递），`iface_match` 在切分期比 descriptor |
| 不可独立验证 | 每模块一条 `custom`（块内）+ **最后一块**一条 `custom`（全量）；测试文件由冻结摘要守着 |

## 3 怎么跑

```bash
.venv/bin/python acceptance/stress/code-convergence/generate.py     # 先造 fixture
.venv/bin/python -m bobanana6 run acceptance/stress/code-convergence/task.yaml \
    --ontology acceptance/stress/code-convergence/ontology.yaml \
    --strategy module --workspace acceptance/stress/code-convergence/_run/workspace
```

★ `--strategy module`（按模块切）+ `--workspace`（代码落盘目录）是这道题的两个前提；
缺 `--workspace` 时交代码类产物会被**具名拒绝**（`no_workspace`），不会静默丢。

验收层里更省事的一条路（播种工作区 + 读数都在 `TaskRun` 上）：

```python
from acceptance.harness import run_task
run = run_task("acceptance/stress/code-convergence", client=..., env={}, workspace=True, strategy="module")
```

## 4 四条攻击面（`#33` 的读数；三者分开报）

| 攻击面 | 建了 | 判了 | 读了 |
|---|---|---|---|
| ① 接口先冻结、再实现 | ✅ 接口块在共享抽象之前；模块块声明共享抽象为输入 | ✅ `C-shared-interface`（冻结测试比 `inspect.signature`） | ✅ `exit 0` |
| ② 测试是全局的（块内过 ≠ 整体过） | ✅ 块内 + 全局两条 | ✅ `C-global-tests`（全量 unittest） | ✅ 一个"行为对但没收敛"的模块：块内 ok、末块 FAIL |
| ③ 测试文件可能被改 | ✅ 内容寻址冻结 + `frozen_files` | ✅ `C-tests-frozen`（每块 + 装配关） | ✅ 开跑前改松测试 → 每块都被点名（两个摘要都报出来） |
| ④ 编译器 + 测试是天然判官 | ✅ `custom` 跑 `py_compile` / `unittest`（**冻结命令**，来自契约） | ✅ 退出码被记录 | ✅ 四组命令全 `exit 0` |

★ 已知缺口（逐字读数在 `.bobanana6/b33/报告.md §6`）：**装配关仍报 `provenance_connected`** ——
代码类产物是文本、没有行级溯源，而那条连通性检查是给行状产物写的。

## 5 判据清单（`task.yaml`）

```
C-shared-interface  custom        共享抽象与冻结接口的签名一致（①）
C-shared-compiles   custom        py_compile（④）
C-shop-a / C-shop-b custom        块内：模块自己的行为（块级判据）
C-global-tests      custom        全局：全量测试（②，挂在最后一块）
C-tests-frozen      frozen_files  测试文件与接口逐字节未变（③，target="*" ⇒ 每块 + 装配关都判）
                                  ★ `#37`：它 `spec.sources` 点名的六个冻结载体（含 `interface.py`）
                                  也由壳**在派活前**投递到每个块手上 —— `T-01` 的 `inputs` 是空的，
                                  在此之前它只能靠 `sources.snapshot` 自己去取（`#36` 真模型 0 次）。
C-iface-fields      schema        interface.json 的列
C-iface-prov        provenance    接口每一行能指回冻结的 interface.py
```

## 6 环境注意

* 冻结命令用 `python3 -m unittest`（**标准库**）：这道题不依赖第三方包，跑测试的那台机不需要装东西。
* 测试文件**不要**放 `__init__.py`：`unittest discover -s .` 要把 `input/repo` 当顶层，
  测试里的 `import shop_a` 才成立（有 `__init__.py` 会被当成包，import 立刻找不到模块）。
* 工作区是 `input/` 的可写拷贝，路径与 `locator` 同一套（`<ws>/input/repo/...`）。
