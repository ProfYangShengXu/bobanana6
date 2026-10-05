"""`frozen_files` -- 冻结的载体在**装配期**还是不是原来那份字节（`#33`）。

为什么需要它：代码类任务里【测试文件本身】是最容易作弊的地方 —— 把测试改松，
自己的实现就"通过"了。冻结发生在取回阶段（快照按内容寻址，文件名就是摘要），
但**装配期没有任何东西回头看工作区里的那份还是不是原文**。

判据只比较两个摘要，两个都由外侧算好放进上下文（`core/` 不碰文件系统）：

```
context.frozen_files[ref] = (冻结时的摘要, 工作区里那份的摘要)
```

★ 没有这一项 = 这一份**没被观测过** ⇒ 具名失败，绝不静默通过。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "frozen_files"


def _requested(constraint: Constraint, context: CheckContext) -> tuple[str, ...]:
    raw = constraint.spec.get("sources")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return tuple(str(item) for item in raw)
    # 没点名 = 上下文里观测到的全都要查（观测过哪些就查哪些）。
    return tuple(sorted(context.frozen_files))


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    refs = _requested(constraint, context)
    if not refs:
        return fail(constraint, "spec.sources must name at least one frozen source")
    facts: list[str] = []
    for ref in refs:
        observed = context.frozen_files.get(ref)
        if observed is None:
            return fail(
                constraint,
                f"frozen source {ref!r} was never observed in the workspace "
                "(no path, or the carrier was not frozen)",
            )
        frozen, actual = observed
        if frozen != actual:
            return fail(
                constraint,
                f"frozen source {ref!r} was modified in the workspace: "
                f"{str(frozen)[:12]}… -> {str(actual)[:12]}…",
            )
        facts.append(f"{ref!r} is byte-identical to its frozen carrier ({str(frozen)[:12]}…)")
    return ok(constraint, "; ".join(facts))
