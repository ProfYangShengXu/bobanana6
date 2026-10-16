"""`freshness` -- judged by snapshot timestamps.

`now` is injected, never read from the clock: a kernel that calls the system
clock is not deterministic and cannot be replayed.
"""

from __future__ import annotations

from datetime import datetime

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "freshness"

#: Human sentence for the self-check's third state: the snapshot refs this check
#: ages are read off the candidate's citations, which the producer builds at
#: block end -- a pre-submission product carries none, so "unverifiable" here
#: means "not yet", not "failed". Declared by the checker at its own failure
#: point; read only by the read-only self-check. Judgment never reads it.
PENDING_INPUT = (
    "此刻验不了：这条要按快照时间判断新旧，而它引用的快照坐标在交答案、块末对账时才挂上去；"
    "磁盘上的产物还没带它们，所以现在说不清新旧。交答案时判定会照常做。"
)


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    if context.now is None:
        return fail(constraint, "context.now is required for freshness (the kernel never reads the clock)")
    now = _parse(context.now)
    if now is None:
        return fail(constraint, f"context.now {context.now!r} is not an ISO-8601 timestamp")

    referenced = sorted({p.snapshot_ref for p in candidate.provenance})
    if not referenced:
        # ★ The refs are attached at block end; absent here is "not yet", not
        #   "failed". Judgment still fails; only the self-check reads this.
        return fail(
            constraint,
            "no snapshots referenced; freshness is unverifiable",
            pending_input=PENDING_INPUT,
        )

    facts: list[str] = []
    for ref in referenced:
        snapshot = context.snapshots.get(ref)
        if snapshot is None:
            facts.append(f"snapshot {ref!r} not available")
            continue
        captured = _parse(snapshot.captured_at)
        if captured is None:
            facts.append(f"snapshot {ref!r} captured_at {snapshot.captured_at!r} is not a timestamp")
            continue
        max_age_s = constraint.spec.get("max_age_s")
        if max_age_s is not None:
            if not isinstance(max_age_s, (int, float)) or max_age_s < 0:
                return fail(constraint, f"max_age_s must be a non-negative number, got {max_age_s!r}")
            age = (now - captured).total_seconds()
            if age > max_age_s:
                facts.append(f"snapshot {ref!r} age {age:.0f}s > max {max_age_s}")
        not_before = constraint.spec.get("not_before")
        if not_before is not None:
            floor = _parse(str(not_before))
            if floor is None:
                return fail(constraint, f"not_before {not_before!r} is not a timestamp")
            if captured < floor:
                facts.append(f"snapshot {ref!r} captured {snapshot.captured_at!r} < not_before {not_before!r}")
    if facts:
        return fail(constraint, *facts)
    return ok(constraint, f"{len(referenced)} snapshots pass freshness")
