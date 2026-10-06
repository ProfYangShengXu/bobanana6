"""Deterministic ``LLMClient`` stand-ins for offline runs and CI.

These are **not** models: neither one generalizes past the point of "exercise
the real path without a network". They exist because the acceptance run has to
be reproducible, and because a test that needs a provider is not a test of the
kernel or of the wiring.

  * :class:`ScriptedClient` replays a fixed script (data, not logic). The script
    can live in a JSON file, so no example-specific behaviour is compiled in.
  * :class:`EchoToolResultClient` is message-driven and therefore generic: while
    it has no tool result it asks for the block's source snapshot; afterwards it
    hands the tool payload back as its final answer. The *registry* is what binds
    the call to the block's authorized source -- the client never decides who is
    allowed to read what.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.model import ModelResponse

__all__ = ["EchoToolResultClient", "ScriptedClient"]


def _as_response(entry: Mapping[str, Any]) -> ModelResponse:
    calls = entry.get("tool_calls") or ()
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
        raise TypeError("script entry tool_calls must be a list")
    return ModelResponse(
        text=str(entry.get("text") or ""),
        tool_calls=tuple(calls),
        usage=dict(entry.get("usage") or {}),
    )


class ScriptedClient:
    """Replay a fixed sequence of responses; the last one repeats."""

    def __init__(self, responses: Sequence[Mapping[str, Any] | ModelResponse]) -> None:
        parsed = tuple(r if isinstance(r, ModelResponse) else _as_response(r) for r in responses)
        if not parsed:
            raise ValueError("ScriptedClient needs at least one response")
        self.responses = parsed
        self.calls: list[tuple[int, int]] = []

    @classmethod
    def from_file(cls, path: str | Path) -> ScriptedClient:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(document, list):
            raise TypeError(f"{path}: a script must be a JSON list of responses")
        return cls(document)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        **kw: Any,
    ) -> ModelResponse:
        index = min(len(self.calls), len(self.responses) - 1)
        self.calls.append((len(messages), len(tools)))
        return self.responses[index]


@dataclass
class EchoToolResultClient:
    """Ask for the authorized snapshot, then answer with what the tool returned."""

    tool_name: str = "sources.snapshot"
    calls: int = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        **kw: Any,
    ) -> ModelResponse:
        self.calls += 1
        available = {
            str(spec.get("function", {}).get("name")) for spec in tools if isinstance(spec, Mapping)
        }
        last_tool = _last_tool_content(messages)
        if last_tool is None and self.tool_name in available:
            # No ref: the registry binds the call to this block's authorized
            # source. A worker picks *which* resource, not whether it may.
            return ModelResponse(
                tool_calls=(
                    {
                        "id": f"offline-{self.calls}",
                        "type": "function",
                        "function": {"name": self.tool_name, "arguments": "{}"},
                    },
                )
            )
        if last_tool is None:
            return ModelResponse(text=json.dumps({"rows": [], "columns": []}))
        return ModelResponse(text=_declared_final(last_tool), usage={"completion_tokens": 0})


def _declared_final(payload: str) -> str:
    """The echoed tool payload is this client's **final answer**: say so.

    The loop only terminates on a candidate the model itself declared finished
    (``loop._gate_phase``). This stand-in answers exactly once and has nothing
    more to do, so it marks the echoed object ``"done": true``; without it the
    loop would keep asking and eventually degrade a perfectly good run.
    A payload that is not a JSON object is returned unchanged.
    """
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if not isinstance(document, dict):
        return payload
    document["done"] = True
    return json.dumps(document, ensure_ascii=False)


def _last_tool_content(messages: Sequence[Mapping[str, Any]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") == "tool":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return None
