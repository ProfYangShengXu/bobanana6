"""代码类压力题（`架构设计.md §15.5`）：多模块仓库 → 收敛重复实现到共享抽象。

`#33` 的载体。它和压力题 A（24 源信息类）的分工：

```
压力题 A：异源 → 一张面板      （信息类；切口在【来源】边界上）
压力题 B：多模块 → 共享抽象    （代码类；切口在【模块】边界上）
```

生成的东西（**只把 `input/` 交给运行**，`truth/` 只给判分器读）：

```
input/repo/
  interface.py          冻结的共享抽象接口（签名先定死；实现留给 T-01）
  shop_a.py             ★ 三个模块各有一份重复实现
  shop_b.py
  shop_c.py
  test_interface.py     冻结测试：shared.total 的签名必须与 interface.total 一致
  test_shop_a.py        冻结测试：模块 a 自己的行为（块内）
  test_shop_b.py
  test_shop_c.py
  test_convergence.py   冻结测试：跨模块 —— 三个模块行为一致，且不再各带一份实现（全局）
truth/truth.json        `interface.json` 该长什么样（第四口径：接口是机器可判的）
```

★ 源码里**没有**真正的重复实现逻辑差异：三份 `total` 行为相同（都是求和），
只是各写了一遍 —— 这正是"收敛"要消灭的东西。收敛判据（全局测试）要求
模块文件里不再出现 `def total`，而是从 `shared` 借用。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent

MODULES = ("shop_a", "shop_b", "shop_c")

#: 每份重复实现（行为相同、写法不同 —— 收敛的目标就是它们）。
IMPLEMENTATIONS = {
    "shop_a": "def total(items):\n    return sum(item for item in items)\n",
    "shop_b": "def total(items):\n    running = 0\n    for item in items:\n        running += item\n    return running\n",
    "shop_c": "def total(items):\n    import functools\n    import operator\n    return functools.reduce(operator.add, items, 0)\n",
}

INTERFACE = '''"""冻结的共享抽象接口（★ 先定，再由 T-01 实现）。"""


def total(items):
    """把一批条目加成一个总数。"""
    raise NotImplementedError("the shared abstraction is not implemented yet")
'''

TEST_MODULE = '''"""冻结测试：{module} 自己的行为（块内判据）。"""

import unittest

import {module}


class TotalTests(unittest.TestCase):
    def test_sums_a_list(self):
        self.assertEqual({module}.total([1, 2, 3]), 6)

    def test_empty_list(self):
        self.assertEqual({module}.total([]), 0)

    # ★ 块内测试【只查行为】：收敛与否留给全局测试 —— 否则"块内过 ≠ 整体过"就没有空间。
    #   （一个模块自带实现、行为却对，块内照样过；只有全局测试会指出它没收敛。）


if __name__ == "__main__":
    unittest.main()
'''

TEST_INTERFACE = '''"""冻结测试：实现出来的共享抽象，签名必须与冻结的接口一致（① 接口先冻结）。"""

import inspect
import unittest

import interface
import shared


class InterfaceTests(unittest.TestCase):
    def test_the_signature_matches_the_frozen_interface(self):
        self.assertEqual(
            str(inspect.signature(shared.total)),
            str(inspect.signature(interface.total)),
        )

    def test_the_behaviour_is_the_declared_one(self):
        self.assertEqual(shared.total([1, 2, 3]), 6)
        self.assertEqual(shared.total([]), 0)


if __name__ == "__main__":
    unittest.main()
'''

TEST_CONVERGENCE = '''"""冻结测试：跨模块的全局判据（② 块内过 ≠ 整体过）。"""

import unittest

import shop_a
import shop_b
import shop_c


class ConvergenceTests(unittest.TestCase):
    def test_every_module_agrees_with_the_shared_abstraction(self):
        sample = [3, 1, 4, 1, 5]
        expected = 14  # 3+1+4+1+5
        for module in (shop_a, shop_b, shop_c):
            self.assertEqual(module.total(sample), expected, module.__name__)

    def test_no_module_keeps_its_own_implementation(self):
        for name in ("shop_a", "shop_b", "shop_c"):
            source = open(name + ".py", encoding="utf-8").read()
            self.assertNotIn("def total", source, name)


if __name__ == "__main__":
    unittest.main()
'''


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def generate() -> None:
    """Build the repo under `input/` and the machine-checkable truth under `truth/`."""
    given = HERE / "input"
    truth_dir = HERE / "truth"
    shutil.rmtree(given, ignore_errors=True)
    shutil.rmtree(truth_dir, ignore_errors=True)
    repo = given / "repo"

    _write(repo / "interface.py", INTERFACE)
    for module, body in IMPLEMENTATIONS.items():
        _write(repo / f"{module}.py", f'"""模块 {module}：自带一份 total 实现（待收敛）。"""\n\n{body}')
        _write(repo / f"test_{module}.py", TEST_MODULE.format(module=module))
    _write(repo / "test_interface.py", TEST_INTERFACE)
    _write(repo / "test_convergence.py", TEST_CONVERGENCE)
    # ★ 不要 `repo/__init__.py`：没有它时 `unittest discover -s repo` 把 `repo` 当顶层
    #   （测试里 `import shop_a` 才成立）；有它就会被当成包，import 立刻找不到模块。

    truth = {
        "deliverable": "interface.json",
        "key": "function",
        "rows": [{"function": "total", "params": "items", "returns": "int"}],
    }
    _write(truth_dir / "truth.json", json.dumps(truth, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    generate()
    print(f"built {HERE / 'input'} and {HERE / 'truth'}")
