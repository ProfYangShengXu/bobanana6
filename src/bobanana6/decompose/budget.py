"""Round budget: iterative splitting is bounded.

``架构设计 §4`` is explicit that rounds have a ceiling -- an unbounded refine loop
refunds its own budget forever. ``SplitPlan.round`` exists for exactly this, and
``core/split.py::round_budget`` enforces it; this module is where the decomposer
stops before it has to be refused.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.model import MAX_ROUNDS, SplitPlan

__all__ = ["RoundBudget", "RoundBudgetExceeded", "can_refine"]

#: The default ceiling, shared with ``core`` so the two cannot drift.
DEFAULT_MAX_ROUNDS = MAX_ROUNDS


class RoundBudgetExceeded(RuntimeError):
    """A refine was requested past the ceiling."""


@dataclass(frozen=True)
class RoundBudget:
    max_rounds: int = DEFAULT_MAX_ROUNDS

    def __post_init__(self) -> None:
        if self.max_rounds < 0:
            raise ValueError("RoundBudget.max_rounds must be >= 0")

    def allows(self, round_number: int) -> bool:
        return round_number <= self.max_rounds

    def exhausted(self, round_number: int) -> bool:
        return round_number >= self.max_rounds

    def next_round(self, round_number: int) -> int:
        if self.exhausted(round_number):
            raise RoundBudgetExceeded(f"round {round_number} has reached the ceiling of {self.max_rounds}")
        return round_number + 1


def can_refine(plan: SplitPlan, budget: RoundBudget) -> bool:
    """Whether one more refinement round is still inside the ceiling."""
    return budget.allows(plan.round + 1)
