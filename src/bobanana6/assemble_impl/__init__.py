"""装配实作：把 `core/ports.py` 的端口接到具体实现上（`AGENTS.md §1.5` 的两处同名区分之一）。

★ 这里不做判定：判定在 `core/`。本包负责「把谁接到谁」以及端到端的执行顺序。
"""

from __future__ import annotations

from .produce import CandidateFormatError, LoopIOImpl, parse_candidate, tool_schemas
from .runner import Runner, RunRefused, RunResult
from .runtime import (
    BlockDeclined,
    BlockRuntime,
    BlockRuntimeError,
    ContractConstraintSource,
    PerWorkerCheckpointStore,
    block_bundle,
    llm_summarizer,
)

__all__ = [
    "BlockDeclined",
    "BlockRuntime",
    "BlockRuntimeError",
    "CandidateFormatError",
    "ContractConstraintSource",
    "LoopIOImpl",
    "PerWorkerCheckpointStore",
    "RunRefused",
    "RunResult",
    "Runner",
    "block_bundle",
    "llm_summarizer",
    "parse_candidate",
    "tool_schemas",
]
