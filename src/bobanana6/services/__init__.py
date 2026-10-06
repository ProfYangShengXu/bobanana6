"""ToolSet 的实作与工具注册表（易变侧，`实现规格 §2` 的 `services/`）。"""

from __future__ import annotations

from .checks import (
    SHELL_COMMAND_CHECK,
    BlockJudge,
    RecordedEvidence,
    command_unrunnable,
    judge_environment_problems,
    shell_command_check,
    verdict_summary,
)
from .registry import (
    TOOL_NAMES,
    RegisteredTool,
    ToolRegistry,
    artifact_payload,
    build_registry,
    snapshot_payload,
)
from .textops import DEFAULT_LIMIT, line_window, search_lines
from .tools import (
    BoundChecks,
    BoundSemantics,
    BoundSources,
    BoundTask,
    build_toolset,
)
from .workspace import (
    BoundWorkspace,
    WorkspaceError,
    WorkspaceLedger,
    WorkspaceUnavailable,
    WriteOutsideRegion,
)

__all__ = [
    "DEFAULT_LIMIT",
    "SHELL_COMMAND_CHECK",
    "TOOL_NAMES",
    "BlockJudge",
    "BoundChecks",
    "BoundSemantics",
    "BoundSources",
    "BoundTask",
    "BoundWorkspace",
    "RecordedEvidence",
    "RegisteredTool",
    "ToolRegistry",
    "WorkspaceError",
    "WorkspaceLedger",
    "WorkspaceUnavailable",
    "WriteOutsideRegion",
    "artifact_payload",
    "build_registry",
    "build_toolset",
    "command_unrunnable",
    "judge_environment_problems",
    "line_window",
    "search_lines",
    "shell_command_check",
    "snapshot_payload",
    "verdict_summary",
]
