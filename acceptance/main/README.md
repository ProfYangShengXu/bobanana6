# main/ —— 主验集（同形状 × 3 域）

`架构设计.md §15.3` 规定主验的判据是**三口径**（§15.6），并且**必须同窗对照旧版**。

## 本块建了什么

`panel-three-sources/` —— **多块 pipeline** 题：

```
三个来源 → split_by_source 切出 4 块
  T-01 read  part-panel-a.csv
  T-02 read  part-panel-b.csv
  T-03 read  part-panel-c.csv
  T-04 write panel.csv   ← inputs = 三个 part，expects 与生产者的 interfaces 对得上
```

它要考的是**单块题根本考不到的四条**：切分器真的切出多块 / 跨块依赖被【声明】
而不是被硬切掉（§14.2 A01）/ 装配检查能发现「拼不上」/ 各块 provenance 各自指回
自己的快照、装配后整条链可走。

## 数据从哪来（§15.4）

**主体是合成的**（`generate.py`，真值由生成器持有）：合成才能精确注入攻击面、
也才有 ground truth 判对错。已注入：

```
A01-mesh-dependency  A-003 同时出现在 panel-a 与 panel-b（跨源同实体）
A10-units            panel-c 的 amount 是【千元】且未标注（真值统一到元）
```

**野生部分（混少量真实公开源）留给后续块**：它的判据不是「答对没」，而是
「有没有正确声明不确定」，因此它需要一套「不确定声明」的判据，本块不含。

## 怎么跑

```
python acceptance/main/panel-three-sources/generate.py        # 物化 input/ + truth/
PYTHONPATH=src python -m bobanana6 run acceptance/main/panel-three-sources/task.yaml \
    --ontology acceptance/main/panel-three-sources/ontology.yaml \
    --out .bobanana6/acceptance-reports --snapshots .bobanana6/acceptance-snapshots
```
（`task.yaml` 里的 locator 是相对路径，所以要么 cd 进题目目录，要么用 `harness.run_task`。）
