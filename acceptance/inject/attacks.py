"""注入执行器：按 `attacks.yaml` **逐行开关**，按行报三步（`实现规格.md §14.2/§14.5`）。

★★ 三步**分开报**，不合成一个「整体通过」：

    ① 发现了吗        found   —— 系统注意到注入的缺陷了吗
    ② 归因对不对      blame   —— `attribute()` 指到的层对不对
    ③ 出口对不对      action  —— 走的是不是对症的那条出口

合成一个总分会把「发现对了但归因错」与「归因对了但动作错」压成同一个数
（§14.5 原话）—— 而这两种失败的处置完全相反。

★ 这一层只**观察**、不判定：判定在 `core/`，这里只负责注入、跑、如实读结果。
★ 红了就是产出：`miss` 不是 bug，是被测系统当前的真实状态。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "ATTACKS_PATH",
    "HIT",
    "MISS",
    "NA",
    "Attack",
    "AttackOutcome",
    "Observation",
    "load_attacks",
    "render_table",
    "run_attacks",
]

ATTACKS_PATH = Path(__file__).resolve().parents[1] / "stress" / "attacks.yaml"

HIT = "hit"
MISS = "miss"
NA = "n/a"


@dataclass(frozen=True)
class Attack:
    """One row of the attack table: what it injects and what it expects."""

    id: str
    assertion: str
    inject: str
    expect: str
    fail_if: str = ""
    enabled: bool = True
    #: ② 的期望。``None`` = 这条攻击不适用②（**必须显式声明**，不能默认通过）。
    expect_blame: str | None = None
    #: ③ 的期望。``None`` = 不适用③。
    expect_action: str | None = None


@dataclass(frozen=True)
class Observation:
    """What the system actually did under one injection."""

    found: bool
    blame: str | None = None
    action: str | None = None
    #: A07 专用：一次注入三个失败，归因给的三个结论必须各不相同。
    blames: tuple[str | None, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttackOutcome:
    attack: Attack
    observation: Observation | None = None
    #: 注入本身失败（如实报，绝不当成通过）
    error: str = ""

    @property
    def ran(self) -> bool:
        return self.attack.enabled

    def _step(self, expected: str | None, actual: Any) -> str:
        if not self.ran:
            return NA
        if self.error:
            return MISS
        if expected is None:
            return NA
        return HIT if actual == expected else MISS

    @property
    def found(self) -> str:
        if not self.ran or self.error:
            return NA if not self.ran else MISS
        assert self.observation is not None
        return HIT if self.observation.found else MISS

    @property
    def blame_step(self) -> str:
        if not self.ran:
            return NA
        if self.error:
            return MISS
        expected = self.attack.expect_blame
        if expected is None:
            return NA
        observation = self.observation
        if observation is None:
            return MISS
        if expected == "three_distinct_blames":
            # ★ A07：期望不是某一个 blame，而是三个【互不相同】的 blame。
            if len(observation.blames) < 2:
                return MISS
            return HIT if len(set(observation.blames)) == len(observation.blames) else MISS
        return HIT if observation.blame == expected else MISS

    @property
    def action_step(self) -> str:
        actual = None if self.observation is None else self.observation.action
        return self._step(self.attack.expect_action, actual)

    @property
    def evidence(self) -> tuple[str, ...]:
        if not self.ran:
            return ("disabled (enabled: false)",)
        if self.error:
            return (f"injection raised: {self.error}",)
        assert self.observation is not None
        return self.observation.evidence


def load_attacks(path: str | Path | None = None) -> tuple[Attack, ...]:
    """Every attack row, in file order, with its per-row switch and expectations."""
    target = Path(path) if path is not None else ATTACKS_PATH
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    rows = list(document.get("attacks", ())) + list(document.get("attacks_extra", ()))
    expected = document.get("expected", {}) or {}
    attacks: list[Attack] = []
    for row in rows:
        want = expected.get(row["id"], {})
        attacks.append(
            Attack(
                id=str(row["id"]),
                assertion=str(row.get("assert", "")),
                inject=str(row.get("inject", "")),
                expect=str(row.get("expect", "")),
                fail_if=str(row.get("fail_if", "")),
                enabled=bool(row.get("enabled", True)),
                expect_blame=(row.get("expect_blame") if "expect_blame" in row else want.get("expect_blame")),
                expect_action=(row.get("expect_action") if "expect_action" in row else want.get("expect_action")),
            )
        )
    return tuple(attacks)


def run_attacks(
    *,
    only: Sequence[str] | None = None,
    path: str | Path | None = None,
    injectors: Mapping[str, Callable[[], Observation]] | None = None,
    target: str = "main",
) -> tuple[AttackOutcome, ...]:
    """Run the enabled rows (optionally a subset), reporting each row on its own.

    ``target`` picks the base the rows are measured on: ``main`` = the panel task
    (what this executor used to only do), ``stress`` = pressure task A. Only the
    rows that actually read a task differ; a fixture-only injector measures the
    same thing on both, and that identity is itself a reading.
    """
    from .injections import injectors_for

    table = injectors_for(target) if injectors is None else injectors
    outcomes: list[AttackOutcome] = []
    for attack in load_attacks(path):
        if only is not None and attack.id not in only:
            continue
        if not attack.enabled:
            outcomes.append(AttackOutcome(attack=attack))
            continue
        injector = table.get(attack.id)
        if injector is None:
            outcomes.append(AttackOutcome(attack=attack, error="no injector is registered for this attack"))
            continue
        try:
            outcomes.append(AttackOutcome(attack=attack, observation=injector()))
        except Exception as exc:  # noqa: BLE001 - an injection that breaks is a miss, never a pass
            outcomes.append(AttackOutcome(attack=attack, error=f"{type(exc).__name__}: {exc}"))
    return tuple(outcomes)


def render_table(outcomes: Sequence[AttackOutcome]) -> str:
    """The three columns, per row. ★ No total, and disabled rows say so."""
    lines = [
        f"{'attack':34} {'① found':9} {'② blame':9} {'③ action':11} evidence",
        "-" * 120,
    ]
    for outcome in outcomes:
        evidence = "; ".join(outcome.evidence)[:140]
        lines.append(
            f"{outcome.attack.id:34} {outcome.found:9} {outcome.blame_step:9} "
            f"{outcome.action_step:11} {evidence}"
        )
    ran = [o for o in outcomes if o.ran]
    steps = {"found": [o.found for o in ran], "blame": [o.blame_step for o in ran], "action": [o.action_step for o in ran]}
    lines.append("-" * 120)
    lines.append(f"rows: {len(outcomes)}   ran: {len(ran)}   disabled: {len(outcomes) - len(ran)}")
    for step, values in steps.items():
        lines.append(
            f"  {step:7}: hit {values.count(HIT)}  miss {values.count(MISS)}  n/a {values.count(NA)}"
        )
    return "\n".join(lines)


def render_gap_notes(outcomes: Sequence[AttackOutcome]) -> str:
    """★ 没走到的出口 / 没有判据的攻击，逐条写出来（§14.3 的覆盖声明）。"""
    misses = [o for o in outcomes if o.ran and MISS in (o.found, o.blame_step, o.action_step)]
    if not misses:
        return "no misses"
    return "\n".join(f"- {o.attack.id}: {o.attack.fail_if or 'expected behaviour not met'}" for o in misses)
