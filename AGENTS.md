# AGENTS.md —— bobanana6 开发指引

> 给在本仓库工作的 coding agent。**先读 §0**，它告诉你该读哪份文档。
> 本文件只写「不写下来就一定会做错」的东西；设计理由在架构文档里，别在这里重复。

---

## 0. 这是什么 / 先读什么

**bobanana6**：用充裕并发资源，把复杂任务切成 N 块分工砸完；
判定内核保证**每块合格、语义一致、整体拼得上**。

**当前状态（诚实标注）**：**主线 21 步全部落地，`bobanana6 run` 第一次能端到端跑通**
（`examples/task.yaml` + `--offline`，exit 0；见 `实现规格 §10` 的 1–21 步）。

- ✅ 已实现：`core/`（1–7）· `ontology/` `contract/` `decompose/`（8–10）· `sources/` `adapters/`
  `services/` `assemble_impl/` `cli.py`（11–13）· `report/`（14）· `agentloop/`（15–21，含
  `LICENSE.hermes` + `README.md` 的接线点清单勾选结果）· `tests/` · `tools/`
- ✅ 验收集现在是**可执行的**：`acceptance/inject/`（A01–A11 逐行注入 + 三步分开报）+ `acceptance/judge.py`
  （三口径、禁总分、强制覆盖声明）· `tools/reverse_validation.py`（故意改坏、证明测试会红）
- ⚠️ 已知缺口（不是残留，是明确报告的未接线项）：`join_truncated_parts` /
  `continuation_overlap_length` 无调用方（本项目的 loop 没有流式文本路径）；
  `PROGRESS_RULER` 无读者（没有跨 turn 的「无进展检测」）；`ArbiterPort` 无实作
  （分歧只记录、不自动补约束）。三条都写在 `agentloop/README.md` 的清单里。
- ⚠️ 判定缺口（`acceptance/inject/README.md` 有逐条读数，**不是代码残留，是判定器看不见的东西**）：
  `target="*"` 在多块装配下永远「找不到产物」（A08，留给下一块）· 口径不一致要到 compare 期
  才被发现（A02，时序）· 持久故障的出口是前置门拒跑而非降级（A06，出口选择）。
- ✅ `#16` 已收口的四条判定缺口：缺失成为一等状态 `MISSING`（A04）· 单位检查读**值里的单位**（A10）·
  归因读**自带病因**、两层互相指认则 `unknown`（A07）· 溯源经 `SnapshotReader` 端口逐点重放
  —— 引用的行要在、值要对得上（A11）。四条都有修前/修后对照读数。
- ✅ `#17` 收掉 `#16` 暴露的两个真问题：**单位归一放在读取块**（合并块仍只搬运，
  `untraceable_row` 不放宽；A11 改成比【量】，换过单位的同一个量算真、丢掉单位没换算算假）·
  A07 的声明通道**补上了生产者**（工具/参数/缺输入/判定器各自在事件上标 `cause`），
  真实 run 里三条根因拿到三个不同 blame。

| 命令 | 现状 |
|---|---|
| `pytest tests/ -q` | ✅ 737 passed（不再有 xfail） |
| `pytest -m arch_guard -q` | ✅ 37 passed（含 §1.2 依赖方向 + §1.4 唯一装配点守卫，装配点规则从源码树推导而非手写名单） |
| `pytest -m acceptance_smoke -q` | ✅ 2 passed（本机用 `.venv/bin/python -m pytest`） |
| `uv run bobanana6 run task.yaml` | ⚠️ 入口已配（`[project.scripts]`），但**本机没装 `uv`，也没有 setuptools 可做 editable 安装**；等价的真实调用是 `PYTHONPATH=src .venv/bin/python -m bobanana6 run examples/task.yaml --ontology examples/ontology.yaml --offline` |

**所以 §3 的命令是目标形态，不是现状。**

| 你要做什么 | 先读 |
|---|---|
| 理解为什么这么设计 | `架构设计.md`（§3 架构推导 / §4 迭代切分） |
| 知道每个文件放什么、端口怎么定 | `实现规格.md`（§2 仓库结构 / §3 端口 / §4 职责表） |
| 改 `agentloop/`（fork 来的运行时） | `FORK-NOTES.md`（§C 抄/扔/改三类决策） |
| **建验收集** | `架构设计.md` **§15**（系统级）+ **§5.3**（单 agent 级）+ `实现规格.md` §14 |
| **调单个 worker 的能力** | `架构设计.md` **§5.2**（单调性曲线 / 两种亏欠的区分） |
| 知道哪些缺口是已知的 | `agentloop-缺口-vs-AIE3672.md` |
| 看业界实证（谁做了什么、该抄什么） | `架构设计.md` §12 + §12.1 · `多agent协作-文献情报.md` |

**两份主文档必须一起看**，单独看任何一份都会漏掉依赖方向。

---

## 1. 硬规则（违反了要返工）

### 1.1 `core/` 是零模型的

```python
# core/ 里【不允许】出现：
from openai import ...        # 任何模型 SDK
import httpx / requests       # 任何 HTTP 客户端
# 也不允许出现这些词（源码级检查，含注释和字符串）：
prompt · token · temperature · llm · model_id · http · url · html
```

**这是有强制测试守着的**（`test_core_is_environment_free`），不是风格建议。
理由：`core/` 是判定语义，它必须**在任何环境下都能跑、且结果完全确定**。

### 1.2 依赖箭头全部指向稳定侧

```
adapters/ ─┐
agentloop/ ─┼─→ core/        ✅
sources/  ─┘

core/ ─→ adapters/           ❌ 绝对不行
```

**没有一条箭头从 `core/` 指出去**（`test_no_outbound_edges_from_core`）。

**`agentloop/` 不 import `adapters/`** —— 它需要的模型客户端是
`core/ports.py::LLMClient`，由装配点注入。

### 1.3 端口定义在稳定侧，实现在外侧

```python
# ✅ 对：core/ports.py 定义，外侧实现
# core/ports.py
class LLMClient(Protocol): ...

# adapters/glm.py
class GLMClient:            # 实现它
    ...

# ❌ 错：core 里 import 具体实现
```

理由：**端口定义在稳定侧，箭头才指向稳定侧**。
上游 Hermes 代码里到处是 `from hermes_cli.config import ...` 这类反向依赖 ——
**fork 时全部换成 core 定义的端口**，方向就正过来了。

### 1.4 唯一装配点

**`cli.py::build_app()` 是全项目唯一 `new` 具体实现的地方。**
其他任何文件出现 `GLMClient()` / `PlaywrightFetcher()` 这类构造 = 违规。

### 1.5 两处命名故意区分（别合并）

```
core/assemble.py    （零模型：装配合法性判定）  ≠  assemble_impl/  （装配实作）
core/ontology.py    （零模型：本体校验规则）    ≠  ontology/       （本体数据 + 加载器）
```

### 1.6 `agentloop/` 是 fork，不是依赖

fork 自 `NousResearch/hermes-agent`（MIT）。

- **必须随附 `agentloop/LICENSE.hermes`**（上游许可 + 版权声明）
- **必须写 `agentloop/README.md`**，注明派生来源 + 改了哪些
- **不要 `pip install hermes-agent`** —— 我们要的是可改的代码，不是会漂的依赖

---

## 2. 目录与依赖方向

```
core/        判定/切分/语义/装配/端口      (年)   ← 一切依赖的终点，零模型
agentloop/   fork 的 worker 运行时         (年)   ← 只依赖 core
ontology/    本体文件 + 加载器              (年)
decompose/   切分策略                       (半年)
contract/    契约解析                       (半年)
services/    ToolSet 实作                   (半年)
adapters/    模型/provider 对接 (实现端口)   (季度)
sources/     取网 + 快照                    (周/季度)
cli.py       ★ 唯一装配点
```

**分层轴是「变化频率」，不是「零模型 vs 有模型」。** 后者是**可审计性**轴，别用错。

---

## 3. 命令（目标形态）

```bash
uv sync                                   # 装依赖
uv run pytest tests/ -q                   # 全量单测
uv run pytest -m "arch_guard" -q          # ★ 架构守卫（改动后【必跑】）
uv run pytest -m "acceptance_smoke" -q    # 验收冒烟（门禁）
uv run bobanana6 run task.yaml            # 端到端
```

**环境纪律**：

- **不要设 `HTTP_PROXY` / `HTTPS_PROXY`** —— 但要**读** `env`：某些环境（如官方 House）里代理是**必须用**的，
  代码要支持从环境变量取代理，**不能自己清理掉**
- 判分容器 `--network=none`；其他容器 `--network=bridge`
- **模型侧两个口子只从 `env` 读，绝不写死**：
  `BOBANANA6_EXTRA_BODY`（JSON 对象，合并进请求体最后）—— 厂商独有参数（`enable_thinking`、
  `top_k`…）走它，**不许覆盖 `model` / `messages`**，覆盖了要报错不要静默丢；
  `BOBANANA6_TIMEOUT_S`（秒，**默认仍然是 60**，不要为了让某个慢服务通过就调大默认值）。
  ★ 实测依据：同一个端点同一个模型，冷启动首次 **90.8s**、之后 **1.6s**；thinking 开着必超时。
  「模型 1.6 秒能答对，是壳把它按死了」—— 别把厂商差异改写成默认值。

---

## 4. 测试纪律

### 4.1 三层分明，别混

```
§9.1  架构守卫     确定性、无模型、无网络     ← 改动后必跑
§9.2  单元测试     确定性、用 Fake            ← 每层各测
§14   系统验收     可接真模型，但必须可控      ← 里程碑
```

### 4.2 六条不能破的口径

```
① 端到端单测不用真模型 / 真网络 / 真本体（用 tests/fakes.py 里的迷你本体）
② 验收【禁合成单一总分】—— 块级 / 装配 / 归因 三口径分开报
③ 冒烟判据用【通过数对比容差】，不用 reward 二值
   （reward 二值会把「能力波动」误诊成「架构回归」）
④ 主验必须【同窗】对照旧版 —— 跨窗差异混了环境漂移，不可归因
⑤ 每条验收结论必须附「本次哪些出口没被走到」
⑥ 汇总数字必须【拆开算】—— 表面数字里常混着「回退必对」白送的部分
```

### 4.3 单测是「有没有（存在性）」，集成是「对不对（正确性）」

硬编码 fixture 可能匹配到**别的地方**的同一串。**存在性检查不能替代正确性检查。**

---

## 5. 工作流纪律

### 5.1 改动的顺序

```
改完全做完 → 先架构纯洁性检查 → 洁净才跑全量
```

**不要**改一半就跑全量。**不要**在脏架构上做性能对比。

### 5.2 归因必须到具体角色 / 具体缺陷

**不能停在现象层。** 「这版变差了」不是结论；「`attribution.py` 把 `context` 误判成 `model`，
导致该改约束的去重试了」才是。

### 5.3 判据 vs 数据的层级

```
✅ 判据指方向，数据只负责「没有反证」
❌ 拿数据去「证明」方向 —— 这是两个不同强度的证据
```

**报不显著就要说「不显著」** —— 措辞强度是结论的一部分。

### 5.4 一次到位，不分期

**交付不分 V0/V1/V2。** 分阶段会留残骸，而且残骸没人删。
范围边界（明确不做）要保留，但**备选方案只进决策记录，不写第二套实现**。

### 5.5 引擎确定性，审美在环外

`core/` 与 `agentloop/` 必须**可复现**。判断「好不好」的事放在环外（人 / LLM），
不要把它编进判定内核。

---

## 6. 明确不做

- 不做投票 / 辩论 / 多数决
- 不做非结构化 swarm（agent 互相协商的自由网络）
- 不合并三类分歧（`BlindSpot` / `Conflict` / `DefinitionMismatch`）
- 不让每个 agent 各自形成语义理解（默认自动起草，禁止的是「各立一套」）
- 不做同级 write 操作并行（除非 partition 互斥）
- 不做无轮数上限的迭代切分
- 不做环状切分 · 不做 worker 之间的运行期通信
- `custom` 约束不执行模型生成的代码
- **不引入 LangChain / LlamaIndex / AutoGen / Swarms**

**运行时层：**

- **不把「错误分类」当「责任归因」用**（`errors.py` 与 `attribution.py` 是两层）
- 不做不可恢复的压缩 · 不做没有事件流的归因
- 不让 worker 自己宣布完成 · 不假设 worker 独占运行
- **不把拒答当失败** —— 它是第三种合法出口
- 不让归因硬猜 —— 归不出来就产出 `confidence="unknown"`
- 不做会破坏前缀缓存的 prompt 组装（动态数据一律放尾部）

---

## 7. 本项目的坑（血换的）

**这些是「不知道就一定会踩」的，不是风格问题。**

```
① 索引在列表被改写后一律不可信
   压缩/修复会替换 dict、插入行 → 陈旧索引让重放前缀在 tool 行中间裂开，
   孤立的 tool result 被静默丢掉。
   → 用【内容指纹或稳定 id】定位，不要用位置。

② 「失败」也是一种必须被记录的状态
   「候选失败了」和「候选从没跑过」必须可区分，否则续跑会重放旧的未回答输入。
   → Checkpoint.status ∈ {ok, failed, never_started}

③ 单点收口
   surrogate 清洗 / tail 关闭 / 落盘，都要放在【唯一必经之处】，
   不要每条分支各写一遍。

④ 只有 provider 上报的真实用量才能重置熔断器
   估算不行 —— rough 估算对非 ASCII 文本低估最多 ~2×，会把熔断器误重置成死循环。

⑤ 计分尺子分场景
   413 用【字节】、token 溢出用 token/消息数。用错尺子会永远报「无进展」并卡死。

⑥ 分类优先级是 load-bearing
   413 必须排在通用 4xx 之前；context-length（含被包成 429 的输出上限错）
   必须排在非重试客户端错之前。

⑦ 临时验证不得 shadow 真套件
   ad-hoc 验证只在该项目【完全没有】canonical 命令时才启用，且一律 scope=targeted。

⑧ exit code 归因
   只有 `;` 序列的最后一节能代表自己的状态；管道 `|` / 后台 `&` 会隐藏状态；
   `&&` 链只在成功时证明各成员。
   → 防「用一个 exit 0 冒充验证通过」

⑨ 验证会过期
   「改完代码后旧测试不算数」是【显式时间戳比较】，不是靠 agent 记得。
   → stale 判定 = last_edit_at > evidence.created_at

⑩ 同一个工具反复失败要主动提示换工具（taint），不是继续通用重试
   —— 弱模型最典型的死法就是在一个坑里反复跳。

⑪ 内部标识别用 id()
   幂等标记挂 dict/weakref，CPython 地址复用会误判。

⑫ 错误分类别靠消息字符串 —— 谁来「说」自己的错，谁来「判」它的类
   实测教训：同一个异常类，只因为消息里带不带类名，分类结果完全相反
   （`CandidateFormatError` 的名字里恰好有 "Format"，`str()` 里没有）。
   → 自定义异常在类上标 `reason = <FailoverReason 或 BobananaAction 的值>`；
     `agentloop/errors.py` 先读它，再退回状态码 / 类名 / 词表。
   → 词表是【兜底】（第三方异常我们标注不了），别再往词表里加词当修法。
   → 新增自定义异常忘了标注不会报错，只会悄悄退回兜底 ——
     所以有一条守卫在扫全仓的 `reason = "..."` 是否都在词表里。

⑬ 「回填」要确认真的到了模型手上，不是到了某个字段里
   实测教训：反思/回填文本先写进 per-turn scratch（`turn.injected`），
   而 ASSEMBLE 阶段在它之前就跑完了、下一轮又是全新的 scratch ——
   文本被写下来、**又从未被读过**。决策层单测全绿（它只断言 `decision.injected`
   有内容），而链路上一句话都没送到。
   → 跨 turn 要传的东西放进 state，不要放 per-turn scratch；
     验证断言「模型收到的 messages」，不要断言「决定里有这段文本」。
```

---

## 8. 提交与交付

- **commit message 也是对外输出** —— 不要写内部代号、本机路径、身份标识
- **凭据只走 `env` / secrets 文件，绝不入库**
- 改 `agentloop/` 时，**commit 里要能看出改了上游哪一块**（方便将来同步上游大版本）
- 文档用中文（`架构设计.md` / `实现规格.md` / `FORK-NOTES.md`），**代码与注释用英文**

---

## 9. 拿不准的时候

```
官方要求 → 你写的设计文档 → 源码事实 → 推断
```

**优先级从高到低。** 推断出来的结论必须标注 `[待验]`，不要写成事实。

**改了设计文档里的东西，要同步改另一份。** 两份是配套的，不是主从。
