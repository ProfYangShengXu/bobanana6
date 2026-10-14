"""Round budget: iterative splitting is bounded.

``架构设计 §4`` is explicit that rounds have a ceiling -- an unbounded refine loop
refunds its own budget forever. ``SplitPlan.round`` exists for exactly this, and
``core/split.py::round_budget`` enforces it; this module is where the decomposer
stops before it has to be refused.

It also carries :class:`BlockBudget` -- the *other* ceiling, the one on a single
block's input. The two are deliberately different things: ``RoundBudget`` caps how
many times the plan may be re-cut, ``BlockBudget`` caps how much material one read
block may carry (which is what decides split-into-windows vs merge-into-one-block).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..core.model import MAX_ROUNDS, SplitPlan

__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "READ_FRAME_CHARS_PER_LINE",
    "BlockBudget",
    "RoundBudget",
    "RoundBudgetExceeded",
    "can_refine",
]

#: The default ceiling, shared with ``core`` so the two cannot drift.
DEFAULT_MAX_ROUNDS = MAX_ROUNDS

#: Characters the read tool's per-line framing adds on top of a line's own text:
#: ``sources.read`` returns ``{"line": N, "text": "..."}`` for every line, so a
#: window costs more than the sum of its lines. Measured on the acceptance research
#: corpus (10 sources / 4,365 lines): the framed payload is larger than the raw text
#: by ~26.4 chars per line, JSON wrapper included. This is the conservative round-up.
#: Without it a "fits the budget" window would sail past the compression trigger
#: (the exact failure mode LangChain only warns about).
READ_FRAME_CHARS_PER_LINE = 32


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


@dataclass(frozen=True)
class BlockBudget:
    """How much source material one read block may carry, in characters.

    The number is produced *outside* this module (``agentloop.compress`` derives it
    from the same threshold ``compact`` compares against); ``decompose/`` owns only
    the decision (split a source into windows / merge small sources into one block),
    never the deployment's capacity.

    ``max_chars`` must be positive: a degenerate budget (e.g. ``--max-output >=
    --context-length`` collapses the threshold to 0) is *not* a budget that splits
    everything -- callers express "no size policy" by passing no budget at all.
    """

    max_chars: int

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ValueError("BlockBudget.max_chars must be > 0")

    def cost_of(self, line_chars: Sequence[int]) -> int:
        """What a read window holding these lines roughly costs to carry.

        = the lines' own characters + the per-line framing the read tool adds.
        """
        return sum(line_chars) + READ_FRAME_CHARS_PER_LINE * len(line_chars)


def can_refine(plan: SplitPlan, budget: RoundBudget) -> bool:
    """Whether one more refinement round is still inside the ceiling."""
    return budget.allows(plan.round + 1)
