"""反例集：一个**不该被拆**的任务（`架构设计.md §15.8`）。

构造：三方在**同一份状态机**上做增量式互相依赖的推理 —— 每一步的输入是上一步的
结果，三方**频繁共享中间状态**。这类任务拆开就是 `fake parallelism`：切了也能做对
只能说明运气好，代价会在别的任务上还回来（**验收判机制，不判这一次的结果**）。

★ 两种给法都写出来，因为它们考的是不同的东西：

  * `machine-events.csv`  一份事件日志 —— 状态机是【一份】来源，天然不该切；
  * `party-*.csv`        三方的局部视图 —— **看起来**可以按来源切，但切完共享状态
    就断了。这一份是给「缺口测试」用的：今天的切分器会把它切开，而门禁看不出来。

真值（每一步之后的机器状态）由生成器持有，写在 `truth/`，运行只看 `input/`。
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: 顺序状态机的迁移：(step, actor, event) -> state
TRANSITIONS = (
    ("1", "party-a", "open", "OPEN"),
    ("2", "party-b", "reserve", "RESERVED"),
    ("3", "party-a", "confirm", "CONFIRMED"),
    ("4", "party-c", "ship", "SHIPPED"),
    ("5", "party-b", "invoice", "INVOICED"),
)
#: ★ 每一方【只看得见自己那几步】，但下一步依赖上一步的产物 —— 这就是共享状态。
PARTY_VIEWS = {
    "party-a": ("1", "3"),
    "party-b": ("2", "5"),
    "party-c": ("4",),
}


def generate(*, root: Path | None = None) -> dict[str, Path]:
    base = HERE if root is None else Path(root)
    given = base / "input"
    truth = base / "truth"
    given.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)

    (given / "machine-events.csv").write_text(
        "step,actor,event\n" + "\n".join(f"{s},{a},{e}" for s, a, e, _ in TRANSITIONS) + "\n",
        encoding="utf-8",
    )
    for party, steps in PARTY_VIEWS.items():
        rows = [f"{s},{a},{e}" for s, a, e, _ in TRANSITIONS if s in steps]
        (given / f"{party}.csv").write_text("step,actor,event\n" + "\n".join(rows) + "\n", encoding="utf-8")

    (truth / "truth.json").write_text(
        json.dumps(
            {
                "deliverable": "final-state.csv",
                "sequence": [{"step": s, "actor": a, "event": e, "state": st} for s, a, e, st in TRANSITIONS],
                "note": "每一行依赖上一行的 state：切块后共享状态就断了",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"input": given, "truth": truth}


def main() -> int:
    paths = generate()
    print(f"input : {paths['input']}")
    print(f"truth : {paths['truth']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
