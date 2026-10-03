"""装配实作：把 `core/ports.py` 的端口接到具体实现上（`AGENTS.md §1.5` 的两处同名区分之一）。

★ 这里不做判定：判定在 `core/`。本包负责「把谁接到谁」以及端到端的执行顺序。
"""

from __future__ import annotations

from .produce import (
    CANDIDATE_SCHEMA,
    EXPECTED_SHAPE,
    CandidateFormatError,
    LoopIOImpl,
    ResponseFormatSupport,
    candidate_response_format,
    parse_candidate,
    rejects_response_format,
    tool_schemas,
)
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
    "CANDIDATE_SCHEMA",
    "EXPECTED_SHAPE",
    "BlockDeclined",
    "BlockRuntime",
    "BlockRuntimeError",
    "CandidateFormatError",
    "ContractConstraintSource",
    "LoopIOImpl",
    "PerWorkerCheckpointStore",
    "ResponseFormatSupport",
    "RunRefused",
    "RunResult",
    "Runner",
    "block_bundle",
    "candidate_response_format",
    "llm_summarizer",
    "parse_candidate",
    "rejects_response_format",
    "tool_schemas",
]
