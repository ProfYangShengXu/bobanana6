"""测试进程的环境闸。

★ `code.run` 的部署开关（`BOBANANA6_CODE_RUN`）是**进程级**的：它属于"这个部署要不要
把 `code.run` 放进工具面"，默认开。但单测是**确定性**的 —— 如果跑测的 shell 里恰好
`export BOBANANA6_CODE_RUN=off`（本轮真模型跑测就要这么干），依赖默认值的断言会当场变读数，
而失败原因（"外部环境变量"）在报错里看不出来。

所以这里在**每个测试**前后把该变量从 `os.environ` 里摘掉：
  · 想要"关掉"的测试自己用 `monkeypatch.setenv` 显式打开那个局面；
  · 其余测试见到的都是"没设过" = 默认开。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_code_run_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOBANANA6_CODE_RUN", raising=False)
