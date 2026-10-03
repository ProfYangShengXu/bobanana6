# main/ —— 主验集（同形状 × 3 域）

`架构设计.md §15.3` 规定主验的判据是**三口径**（§15.6），并且**必须同窗对照旧版**；
规格还要求 `同形状 × 3 个域 × 各 N 次` —— **本块补齐了那 3 个域。**

## 三个域（同形状、不同领域）

| 域 | 题目 | 实体 | 字段 | 单位异构 | 真值键列 |
|---|---|---|---|---|---|
| 1 金融 | `panel-three-sources/` | 债务人 `A-001…` | `obligor` / `pd` / `amount` | `panel-c` 用**千元** | `obligor` |
| 2 能源 | `panel-energy-sites/` | 风电场站 `WF-01…` | `site` / `capacity_mw` / `output_mwh` | `site-coast` 用**千瓦时** | `site` |
| 3 货运 | `panel-freight-lanes/` | 货主 `SH-01…` | `shipper` / `weight_kg` / `cost_cny` | `lane-west` 用**吨** | `shipper` |

**同形状** = 都是「同一批实体 + 三份来源 + 合并成一张表」，都切出 **3 读 + 1 写**、
跨块依赖被【声明】、四条主验约束一模一样（`C-rows` / `C-prov` / `C-fields` / `C-units`）。
**不同域** = 实体、字段、业务含义、单位全都换掉 —— 验的是**泛化**，不是过拟合一道题。

```
每个域切出来的形状（相同）：
  T-01 read  part-<source-1>.csv
  T-02 read  part-<source-2>.csv
  T-03 read  part-<source-3>.csv
  T-04 write <deliverable>       ← inputs = 三个 part，expects 与生产者的 interfaces 对得上
```

它要考的是**单块题根本考不到的四条**：切分器真的切出多块 / 跨块依赖被【声明】
而不是被硬切掉（§14.2 A01）/ 装配检查能发现「拼不上」/ 各块 provenance 各自指回
自己的快照、装配后整条链可走。

## 数据从哪来（§15.4）

**主体是合成的**（每个域一个 `generate.py`，真值由生成器持有）：合成才能精确注入攻击面、
也才有 ground truth 判对错。**每个域都注入了同样两条**：

```
A01-mesh-dependency  一个实体同时出现在两份来源里（域 1 是 A-003，域 2 是 WF-03，域 3 是 SH-03）
A10-units            第三份来源的单位写在值里（千元 / 千瓦时 / 吨），读取块负责归一到基准单位
```

★ **真值自己声明键列**（`"key": "site"` 这样的单列）：3 个域的实体列名不同，
对照必须按真值声明的键列比 —— 否则两边的键集都是空的、只剩行数在比，
第四口径会**静默失效**（本块加 `acceptance/truth.py::declared_key` 修掉，并留了一条见证变体）。
复合键（压力题声明的 `["obligor", "year"]`）**今天还不支持**：`declared_key` 刻意回落，
把缺口记在那里，不假装按两列比。

**野生部分（混少量真实公开源）留给后续块**：它的判据不是「答对没」，而是
「有没有正确声明不确定」，因此它需要一套「不确定声明」的判据，本块不含。

## 怎么跑

```
python acceptance/main/<domain>/generate.py                   # 物化 input/ + truth/
PYTHONPATH=src python -m bobanana6 run acceptance/main/<domain>/task.yaml \
    --ontology acceptance/main/<domain>/ontology.yaml \
    --out .bobanana6/acceptance-reports --snapshots .bobanana6/acceptance-snapshots
```
（`task.yaml` 里的 locator 是相对路径，所以要么 cd 进题目目录，要么用 `harness.run_task`。）

三口径分开报、**禁合成单一总分**；三个域要**同窗**跑（不许跑完一个再跑下一个），
读数见本块报告（`.bobanana6/b24/报告.md`）。
