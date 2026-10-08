#!/usr/bin/env python
"""防新增的编码守卫：每个判定点必须显式标注类别（规格 §五）。

```
parse  —— 解析 / 参数校验
safety —— 安全 / 资源边界
result —— ★ 结果类：**必须附"怎么算出来对错的"**
```

★ 为什么：`core/` 是零 I/O 的判定内核，它**结构上读不到数据、只能查形状** ——
所以"编码守卫"会一直长出来。把类别显式化，等于让每次误伤都能归因到**具体那一条**。
★ 第 0 档：没有标注 ⇒ **报警**（不拒收）。

    .venv/bin/python acceptance/reject_audit.py            # 机读清单
    .venv/bin/python acceptance/reject_audit.py --check     # 有未标注 ⇒ exit 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.services.ci import guard_audit

#: 判定点出现在这些模块里（壳层 + 数据层）；`core/` 的判定语义不在这里扫
#: （它是纯函数，没有 `reject(...)` 这种壳层判定点）。
SCANNED = (
    "src/bobanana6/assemble_impl/produce.py",
    "src/bobanana6/assemble_impl/runner.py",
)


def collect() -> dict:
    sources = {}
    for relative in SCANNED:
        path = REPO / relative
        if path.is_file():
            sources[relative] = path.read_text(encoding="utf-8")
    return guard_audit(sources)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="有未标注 / 说不清的 ⇒ exit 1")
    arguments = parser.parse_args(argv)
    document = collect()
    print(json.dumps(document, ensure_ascii=False, indent=1))
    if arguments.check and document["problems"]:
        print(f"\n{len(document['problems'])} 个判定点缺类别或说不清：", file=sys.stderr)
        for problem in document["problems"]:
            print(f"  {problem.get('location')} {problem.get('code')}", file=sys.stderr)
            print(f"    {problem.get('how_to_annotate')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
