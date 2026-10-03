# inject/ —— 压力集 + 已知缺陷（本块未做）

`实现规格.md §14.5` 要求**三步全对才算过**，而且**三步分开断言**：

```python
def test_injected_defect_found_attributed_and_handled():
    out = run("inject/")
    assert out.found_defect                    # ① 发现了
    assert out.attribution.blame != "model"    # ② 归因对（不是甩锅给模型）
    assert out.action != "retry_local"         # ③ 走对出口（重试无用还烧钱）
```

合成一个「整体通过」会掩盖「发现对了但归因错」与「归因对了但动作错」的差别。

**本块只留占位**：注入执行器要能按 `attacks.yaml` 逐行开关（§14.2），
而 `attacks.yaml` 现在是**清单不是执行器**（见 `../stress/attacks.yaml`）。
