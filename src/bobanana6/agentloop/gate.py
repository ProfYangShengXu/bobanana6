"""Blocking completion gate: only the system's own verification counts.

Hermes' ``verification_evidence.py`` is a *passive ledger* -- it explicitly never
blocks completion. bobanana6 turns it into a **hard gate** (实现规格 §5.8): an
agent's own report never authorises a deliverable; the system's independently
recorded evidence does.

Three fork rules are load-bearing:

  1. evidence is *command + exit code + output digest*, never the agent's prose;
  2. exit-code attribution is shell-aware: only the last ``;``-separated command
     reports its own status, pipes and background hide it, an ``&&`` chain proves
     its members only on success. This is what stops "one exit 0 impersonating a
     verification".
  3. ``unverified`` and ``stale`` are different states ("never checked" vs
     "checked, then the code changed"). Staleness is an explicit timestamp
     comparison, never the agent remembering.

Plus the project's own third exit (实现规格 §5.9.7): ``declined`` -- the worker
explicitly reports it cannot obtain the information. It is not a failure, it does
not trigger a retry, and it feeds refusal accuracy.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from hashlib import sha256

__all__ = [
    "GATE_SCOPES",
    "GATE_STATUSES",
    "ExitAttribution",
    "GateVerdict",
    "ShellSegment",
    "VerificationEvidence",
    "attribute_exit",
    "can_complete",
    "evaluate",
    "is_stale",
    "record_evidence",
    "scope_covers",
    "should_retry",
    "split_shell_segments",
]

GATE_STATUSES: tuple[str, ...] = ("unverified", "stale", "passed", "failed", "declined")
GATE_SCOPES: tuple[str, ...] = ("targeted", "full")


@dataclass(frozen=True)
class GateVerdict:
    status: str
    scope: str

    def __post_init__(self) -> None:
        if self.status not in GATE_STATUSES:
            raise ValueError(f"unknown gate status {self.status!r}; expected one of {GATE_STATUSES}")
        if self.scope not in GATE_SCOPES:
            raise ValueError(f"unknown gate scope {self.scope!r}; expected one of {GATE_SCOPES}")

    @property
    def is_declined(self) -> bool:
        return self.status == "declined"


def scope_covers(provided: str, required: str) -> bool:
    """``full`` covers everything; ``targeted`` only covers ``targeted``."""
    if provided not in GATE_SCOPES or required not in GATE_SCOPES:
        raise ValueError(f"scopes must be one of {GATE_SCOPES}, got provided={provided!r} required={required!r}")
    return provided == required or provided == "full"


def can_complete(gate: GateVerdict, required_scope: str) -> bool:
    """★ Only a system-run verdict that covers the required scope may complete."""
    return gate.status == "passed" and scope_covers(gate.scope, required_scope)


def should_retry(gate: GateVerdict) -> bool:
    """★ ``declined`` is a legal exit, not a failure -- it never triggers a retry."""
    return gate.status in {"failed", "unverified", "stale"}


# --------------------------------------------------------------------------- #
# Shell-aware exit-status attribution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShellSegment:
    tokens: tuple[str, ...]
    following_operator: str | None


def split_shell_segments(command: str, *, posix: bool = True) -> tuple[ShellSegment, ...] | None:
    """Tokenize top-level shell commands, preserving their control operators.

    Returns ``None`` for anything unparseable (unbalanced quotes, empty segment,
    trailing operator other than ``;``) so a partial parse is never matched.
    """
    raw_segments: list[tuple[str, str | None]] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0

    while index < len(command):
        char = command[index]
        if escaped or (char == "\\" and quote != "'"):
            escaped = not escaped
            index += 1
            continue
        if quote or char in "'\"":
            quote = None if char == quote else (quote or char)
            index += 1
            continue

        operator = None
        if command.startswith(("&&", "||", "|&"), index):
            operator = command[index : index + 2]
        elif char == "\n":
            operator = ";"
        elif char in ";|" or (
            char == "&" and (index == 0 or command[index - 1] not in "<>") and not command.startswith(("&>", "&>>"), index)
        ):
            operator = char

        if operator is None:
            index += 1
            continue

        raw = command[start:index].strip()
        if not raw:
            return None
        raw_segments.append((raw, operator))
        index += 1 if char == "\n" else len(operator)
        start = index

    if quote or escaped:
        return None
    trailing = command[start:].strip()
    if trailing:
        raw_segments.append((trailing, None))
    elif raw_segments and raw_segments[-1][1] != ";":
        return None

    try:
        segments = tuple(ShellSegment(tuple(shlex.split(raw, posix=posix)), operator) for raw, operator in raw_segments)
    except ValueError:
        return None
    return segments if all(segment.tokens for segment in segments) else None


@dataclass(frozen=True)
class ExitAttribution:
    code: int | None
    attributable: bool
    reason: str


def attribute_exit(command: str, exit_code: int) -> ExitAttribution:
    """Whether this command's status proves the command's own result.

    Rules (fork 坑 17): the last ``;``-separated sequence reports its own status;
    pipes, ``|&``, ``||`` and backgrounding hide it; an ``&&`` chain proves each
    member only on success.
    """
    segments = split_shell_segments(command)
    if not segments:
        return ExitAttribution(None, False, "command could not be parsed")
    if any(segment.following_operator == "&" for segment in segments):
        return ExitAttribution(None, False, "backgrounded command hides its status")

    # Only the last `;`-separated sequence can speak for itself.
    last_index = len(segments) - 1
    sequence_start = max((i + 1 for i, segment in enumerate(segments[:-1]) if segment.following_operator == ";"), default=0)
    if last_index < sequence_start:
        return ExitAttribution(None, False, "matched command is not in the last sequence")

    operators = {segment.following_operator for segment in segments[sequence_start:last_index]}
    if operators & {"|", "|&", "||"}:
        return ExitAttribution(None, False, "pipe or || hides the exit status")
    if operators and operators != {"&&"}:
        return ExitAttribution(None, False, "unattributable control operators")
    if operators == {"&&"} and exit_code != 0:
        return ExitAttribution(None, False, "a failing && chain does not prove which member failed")
    return ExitAttribution(exit_code, True, "last command owns its exit status")


# --------------------------------------------------------------------------- #
# Evidence + the three-way gate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerificationEvidence:
    command: str
    exit_code: int
    output_digest: str
    created_at: float
    attributable: bool

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("VerificationEvidence.command must not be empty")
        if not self.output_digest:
            raise ValueError("VerificationEvidence.output_digest must summarise the output, not be empty")


def record_evidence(
    command: str,
    exit_code: int,
    output: str,
    *,
    created_at: float,
) -> VerificationEvidence:
    """Record what actually ran. ``created_at`` is injected -- never read a clock."""
    attribution = attribute_exit(command, exit_code)
    return VerificationEvidence(
        command=command,
        exit_code=exit_code,
        output_digest=sha256(output.encode("utf-8", errors="replace")).hexdigest(),
        created_at=created_at,
        attributable=attribution.attributable,
    )


def is_stale(evidence: VerificationEvidence, last_edit_at: float) -> bool:
    """★ "Checked, then changed" is not "checked" -- explicit timestamps, not memory."""
    return last_edit_at > evidence.created_at


def evaluate(
    evidence: VerificationEvidence | None,
    *,
    required_scope: str,
    last_edit_at: float,
) -> GateVerdict:
    """Turn evidence into a verdict. ``declined`` is never produced here: only the
    worker can decline, and the loop records that separately."""
    if evidence is None:
        return GateVerdict(status="unverified", scope=required_scope)
    if is_stale(evidence, last_edit_at):
        return GateVerdict(status="stale", scope=required_scope)
    if not evidence.attributable or evidence.exit_code != 0:
        return GateVerdict(status="failed", scope=required_scope)
    return GateVerdict(status="passed", scope=required_scope)
