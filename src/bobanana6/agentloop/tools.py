"""Tool execution: strict parsing, synthesized refusals, guardrails, call-level
concurrency.

FORK-NOTES §A calls this "the block this project should copy most". Three rules
are copied as-is:

  1. **strict parsing, never repair** (``parse_tool_arguments``). Guessing what the
     model meant hides its real intent *and* can cause side effects. Bad arguments
     become an error result the model can self-correct from.
  2. **a refused call still gets a legal tool result** (``blocked_tool_result``).
     Otherwise the transcript carries a dangling ``tool_call`` and the next
     request is rejected outright by the provider.
  3. **guardrails are pure logic, and ``reset_for_turn`` must run every turn** --
     per-turn state that leaks across turns misjudges a fresh call as a repeat.
  4. **one unusable declaration holds back the whole batch**
     (``declared_arguments_problem``): a truncated / short argument on one call
     means a sibling's arguments may be silently short too, so none of them run.

Plus the call-level concurrency rule from 架构设计 §7.6 ⑤: parallelisability is a
property of *this call*, not of the tool. Reads may overlap; a write is a barrier.
Results are always written back in the model's original call order.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

__all__ = [
    "GuardrailConfig",
    "ToolCall",
    "ToolCallError",
    "ToolCallGuardrailController",
    "ToolResult",
    "blocked_tool_result",
    "call_signature",
    "declared_arguments_problem",
    "invalid_arguments_result",
    "loop_keys",
    "order_results",
    "parse_tool_arguments",
    "plan_tool_segments",
    "shell_paths",
]

TOOL_EFFECTS: tuple[str, ...] = ("read", "write")


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    #: Whether *this call* mutates state. A property of the call, not the tool.
    effect: str = "read"
    #: Paths/resource names this call touches, used only for overlap admission.
    targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.call_id or not self.name:
            raise ValueError("ToolCall.call_id and ToolCall.name must not be empty")
        if self.effect not in TOOL_EFFECTS:
            raise ValueError(f"unknown tool effect {self.effect!r}; expected one of {TOOL_EFFECTS}")


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    ok: bool
    content: str
    blocked: bool = False
    error_type: str | None = None
    #: ★ The layer that owns this failure, declared by whoever produced it --
    #: ``AGENTS.md §7⑫`` ("谁来「说」自己的错") applied to tool results: a tool that
    #: blew up says ``tool``, unusable arguments say ``model``. Attribution reads
    #: it (``agentloop/attribution.py::_trace_declarations``) instead of guessing
    #: from "some tool returned ok=False". Empty means nobody declared one.
    cause: str = ""


class ToolCallError(ValueError):
    """The arguments are unusable. They are never repaired -- see the module docstring."""

    #: The call's shape is wrong, so the caller must change it -- re-sending the
    #: same bytes produces the same refusal.
    reason = "format_error"


def parse_tool_arguments(raw_arguments: Any) -> Mapping[str, Any]:
    """Parse model-emitted arguments without repairing or coercing them."""
    try:
        parsed = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ToolCallError("tool arguments are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ToolCallError("tool arguments must be a JSON object")
    return parsed


def invalid_arguments_result(call_id: str, message: str = "") -> ToolResult:
    """The error result a mis-parsed call returns, so the model can self-correct.

    The caller's input was unusable -- the tool never ran -- so the declaring
    layer is the model, not the tool.
    """
    detail = message or "Tool arguments must be a valid JSON object; the tool was not executed."
    return ToolResult(call_id=call_id, ok=False, content=detail, error_type="invalid_tool_arguments", cause="model")


def declared_arguments_problem(calls: Sequence[ToolCall]) -> str:
    """Why this round of declarations must not be executed (``""`` = it may).

    ★ **One unusable declaration holds back the whole batch.** A call whose own
    argument text does not parse is an *incomplete* declaration -- the model's
    output was cut off, or it emitted junk. Executing its siblings is not safe:
    the same cut can leave a sibling's arguments complete-looking but silently
    short, and a silently short argument still runs. Pi fails every call of a
    length-truncated message for exactly this reason
    (``failToolCallsFromTruncatedMessage``); Hermes answers every call of a batch
    whose JSON is invalid with "Skipped: other tool call in this response had
    invalid JSON" instead of running the rest.

    Empty ``raw_arguments`` is **not** a problem: a call built in-process carries
    its parsed ``arguments`` and :meth:`ToolRegistry.execute` re-serializes them
    for the parser. Only the model's own bytes can be malformed.
    """
    for call in calls:
        if not call.raw_arguments:
            continue
        try:
            parse_tool_arguments(call.raw_arguments)
        except ToolCallError:
            return (
                f"call {call.call_id!r} declared arguments that are not a usable JSON object; "
                "the response may have been truncated, so no call in this round was executed"
            )
    return ""


def blocked_tool_result(call_id: str, reason: str, *, error_type: str = "guardrail_block") -> ToolResult:
    """Synthesize the result for a refused call.

    ★ A blocked call is still answered: a dangling ``tool_call`` makes the next
    provider request invalid, and the model would see an unexplained hole.
    """
    return ToolResult(call_id=call_id, ok=False, content=reason or "blocked", blocked=True, error_type=error_type)


# --------------------------------------------------------------------------- #
# Call-level concurrency
# --------------------------------------------------------------------------- #


def plan_tool_segments(calls: Sequence[ToolCall]) -> tuple[tuple[str, tuple[ToolCall, ...]], ...]:
    """Split a batch into ordered ``("parallel"|"sequential", calls)`` segments.

    A write is a barrier: it closes the current read run and owns its own segment.
    A read joins the open run. Runs shorter than two calls demote to sequential,
    because a single call has no adjacency to exploit.
    """
    segments: list[tuple[str, tuple[ToolCall, ...]]] = []
    run: list[ToolCall] = []

    def close_run() -> None:
        nonlocal run
        if run:
            kind = "parallel" if len(run) >= 2 else "sequential"
            segments.append((kind, tuple(run)))
            run = []

    def extend_sequential(call: ToolCall) -> None:
        if segments and segments[-1][0] == "sequential":
            kind, existing = segments[-1]
            segments[-1] = (kind, (*existing, call))
        else:
            segments.append(("sequential", (call,)))

    for call in calls:
        if call.effect == "write":
            close_run()
            extend_sequential(call)
            continue
        run.append(call)
    close_run()
    return tuple(segments)


def order_results(calls: Sequence[ToolCall], results: Iterable[ToolResult]) -> tuple[ToolResult, ...]:
    """Write results back in the model's original call order.

    Concurrency may finish out of order; the transcript may not. A call with no
    result still gets one synthesized, so no ``tool_call`` is ever left dangling.
    """
    by_id = {result.call_id: result for result in results}
    ordered: list[ToolResult] = []
    for call in calls:
        ordered.append(by_id.get(call.call_id) or blocked_tool_result(call.call_id, "no result was produced"))
    return tuple(ordered)


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


def call_signature(call: ToolCall) -> str:
    """A content fingerprint of a call -- never an index or ``id()`` (坑 11)."""
    payload = json.dumps(
        {"name": call.name, "effect": call.effect, "arguments": call.arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


#: Characters a workspace-relative path token may consist of. A shell command is
#: split on the shell's own separators, and anything left that is not made only of
#: these characters (regex fragments, flags, redirections, URLs) is not a path.
_PATH_TOKEN = re.compile(r"^[A-Za-z0-9_./~+-]+$")
#: A bare file name is a target only when it carries a suffix (``bank.md``); a bare
#: word (``grep``, ``head``, ``-20``) is a command, a flag or a pattern, not a file.
_FILE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_SHELL_SEPARATORS = re.compile(r"[\s|;&()<>='\"`]+")


def shell_paths(command: str) -> tuple[str, ...]:
    """Workspace-relative paths a ``shell.run`` command names, in first-seen order.

    ★ Why this exists: the loop detector needs "the thing this call touches", and
    ``shell.run`` declares no ``target_args`` -- every one of T-01's 16 counting
    commands was a different regex over the *same* file, so ``(tool, args)`` keys
    never repeat. The file is the only stable handle.

    Absolute paths are deliberately **not** targets: ``cd /abs/workspace`` and
    ``/usr/bin/python3`` are host coordinates, and treating a shared prefix as a
    target would manufacture a loop out of unrelated commands. See the 4B T-01
    trace (``t45``-``t60``): every counting command named ``./interview-bank.md``.
    """
    if not isinstance(command, str) or not command.strip():
        return ()
    found: list[str] = []
    for raw in _SHELL_SEPARATORS.split(command):
        token = raw.strip().strip(",;")
        if not token or token in (".", ".."):
            continue
        if token.startswith("/") or (len(token) > 1 and token[1] == ":"):
            continue  # absolute path / drive letter: not a workspace-relative target
        while token.startswith("./"):
            token = token[2:]
        token = token.rstrip("/")
        if not token or token in (".", "..") or not _PATH_TOKEN.match(token):
            continue
        if "/" not in token and not _FILE_SUFFIX.search(token):
            continue
        if any(part == ".." for part in token.split("/")):
            continue
        if token not in found:
            found.append(token)
    return tuple(found)


def loop_keys(call: ToolCall) -> tuple[tuple[str, str], ...]:
    """``((counter key, human-readable target), ...)`` for the repetition detector.

    Two different notions of "the same action on the same target", because the two
    kinds of tool need different ones:

    * A tool that declares ``target_args`` (the registry fills ``call.targets``)
      pages its target: ``sources.read`` over window 1, 2, 3 is **progress**, and so
      is ``sources.grep`` with a different pattern. For those the whole declared
      call is the action -- the content fingerprint. That is exactly what keeps the
      normal 21-call read-in of T-01 from being misreported as a loop.
    * A tool that declares no target (``shell.run``) has only the files in its
      command as a stable handle. Counting by file is what catches the 16 distinct
      regexes over one file.

    A call that names several files contributes one key per file; each is counted.
    """
    declared = tuple(call.targets)
    if declared and declared != (call.name,):
        return ((call_signature(call), declared[0]),)
    command = call.arguments.get("command")
    if isinstance(command, str) and command.strip():
        paths = shell_paths(command)
        if paths:
            return tuple((f"{call.name}\x00{path}", path) for path in paths)
    return ((call_signature(call), ""),)


@dataclass(frozen=True)
class GuardrailConfig:
    repeat_block_after: int = 4  # blame: fail-closed — 省略即 4 次就拦，方向保守
    same_tool_block_after: int = 6  # blame: fail-closed — 省略即 6 次就拦，方向保守
    max_calls_per_turn: int = 0  # 0 = unlimited  # blame: fail-open — 省略即**无上限**（0 = unlimited）；单轮调用数不设闸，方向危险
    #: ★ The *hint* threshold (part of this turn's fix): from the Nth execution of
    #: the same action on the same target -- success or failure, it does not matter
    #: -- the tool result carries an advisory paragraph. 0 disables it.
    #: 4 is the same conservative line the hard failure guard uses: a genuine
    #: four-fold repeat on one target is already suspicious, and the hint only
    #: appends text (it never runs, blocks or refuses anything).
    repeat_hint_after: int = 4  # blame: fail-closed — 省略即第 4 次起附一句提示（不拦），方向保守

    def __post_init__(self) -> None:
        if min(self.repeat_block_after, self.same_tool_block_after, self.max_calls_per_turn) < 0:
            raise ValueError("guardrail thresholds must be >= 0")
        if self.repeat_hint_after < 0:
            raise ValueError("GuardrailConfig.repeat_hint_after must be >= 0")


class ToolCallGuardrailController:
    """Per-turn loop detector. Pure logic; no I/O, no clock.

    ★ ``reset_for_turn`` must be called at the start of every turn. Forgetting it
    lets a previous turn's failures block a legitimate call, or lets a loop run
    forever by resetting at the wrong moment.
    """

    def __init__(self, config: GuardrailConfig | None = None) -> None:
        self._config = config or GuardrailConfig()
        # ★ The repetition-activity counters are **not** per-turn state: a model that
        #   re-issues the same action once per turn (T-01's ``t45``-``t60``) would be
        #   invisible to a per-turn counter. They live on the instance (one loop
        #   drives one block) and are deliberately not cleared by ``reset_for_turn``.
        self._activity: dict[str, int] = {}
        self._activity_marks: dict[str, tuple[int, str]] = {}
        self.reset_for_turn()

    @property
    def config(self) -> GuardrailConfig:
        return self._config

    def reset_for_turn(self) -> None:
        self._repeat_failures: dict[str, int] = {}
        self._tool_failures: dict[str, int] = {}
        self._calls_this_turn = 0
        self._halted = False
        # ★ Only the per-turn marks are dropped; ``_activity`` survives on purpose
        #   (see ``__init__``). The failure counters above stay per-turn -- a fresh
        #   turn is a fresh experiment for *blocking*, but not for *observing a loop*.
        self._activity_marks = {}

    @property
    def halted(self) -> bool:
        return self._halted

    def before_call(self, call: ToolCall) -> ToolResult | None:
        """``None`` = allow; a :class:`ToolResult` = block with that result."""
        self._calls_this_turn += 1
        if self._config.max_calls_per_turn and self._calls_this_turn > self._config.max_calls_per_turn:
            self._halted = True
            return blocked_tool_result(
                call.call_id, f"per-turn call cap of {self._config.max_calls_per_turn} reached", error_type="loop_cap"
            )

        signature = call_signature(call)
        repeats = self._repeat_failures.get(signature, 0)
        if repeats >= self._config.repeat_block_after:
            self._halted = True
            return blocked_tool_result(
                call.call_id, f"the same call already failed {repeats} times", error_type="repeat_failure"
            )
        if self._tool_failures.get(call.name, 0) >= self._config.same_tool_block_after:
            self._halted = True
            return blocked_tool_result(
                call.call_id, f"tool {call.name!r} is failing repeatedly", error_type="same_tool_failure"
            )
        # ★ Only an *allowed* call counts as activity (a blocked call is already
        #   answered by the guardrails above). Success and failure both count -- the
        #   hint is about repetition, not about the outcome.
        marks = [(self._bump(key), target) for key, target in loop_keys(call)]
        if marks:
            self._activity_marks[call.call_id] = max(marks, key=lambda item: item[0])
        return None

    def _bump(self, key: str) -> int:
        count = self._activity.get(key, 0) + 1
        self._activity[key] = count
        return count

    def loop_hint(self, call: ToolCall) -> str:
        """The advisory paragraph appended to a repeated call's result (``""`` = none).

        ★ This is a **hint, not a block**: the call already ran and its real result
        is on its way to the model. The paragraph only names the three things the
        model needs to get itself out of the loop -- the count, the diagnosis
        ("this looks like a side check, not the main line"), and the exit (change
        the approach; drop an optional count; write the artifact and hand it in).

        ★ Domain-free on purpose: the wording is structural (``边缘核对`` /
        ``主线产物``), never this task's subject matter -- the loop detector lives in
        ``agentloop/`` and must not learn any one acceptance task.
        """
        if self._config.repeat_hint_after <= 0:
            return ""
        mark = self._activity_marks.get(call.call_id)
        if mark is None:
            return ""
        count, target = mark
        if count < self._config.repeat_hint_after:
            return ""
        where = f"`{target}`" if target else "同一组参数"
        return (
            f"\n★ 提示（不是拦截）：这是你第 {count} 次用 `{call.name}` 对 {where} 做同一件事了。"
            "这看着像【边缘核对】，不是主线。换个办法；如果这是可有可无的核对"
            "（例如“数一下成品里有多少条”），就放弃它 —— 主线是把产物写出来交掉。\n"
        )

    def observe(self, call: ToolCall, result: ToolResult) -> None:
        if result.ok:
            if call.effect == "write":
                # ★ A successful mutation makes every earlier identical failure a
                # new experiment, so the repeat counter is no longer evidence.
                self._repeat_failures.clear()
            self._tool_failures[call.name] = 0
            return
        self._repeat_failures[call_signature(call)] = self._repeat_failures.get(call_signature(call), 0) + 1
        self._tool_failures[call.name] = self._tool_failures.get(call.name, 0) + 1
