"""Wire-name translation for OpenAI-compatible endpoints.

★ 这是**适配器层**的账，不是工具设计层的账：工具叫 `semantics.lookup` 本身没问题
（它是壳内部的稳定标识，`core/ports.py::_BASE_TOOL_SPECS` / 注册表 / 离线客户端 /
归档都按它说话），问题是**发出去之前没做合规转换**。

硬约束（OpenAI 兼容端点，DeepSeek 官方实测逐字）：

```
Invalid 'tools[0].function.name': string does not match pattern. Expected a s...
```

即 `function.name` 只允许 `[a-zA-Z0-9_-]+`、长度 ≤ 64。宽松端点（硅基流动）不校验，
所以这个病一直没现形；换任何严格端点 ⇒ **每一个工具调用都发不出去**。

三条设计选择：

1. **不做名字清洗式的"猜"还原。** 出站是一张显式表；入站查**同一张表的反向**，
   查不到就原样放行（模型编的名字交给既有的 `unknown tool` 路径 —— 我们只还原
   *自己改过* 的那些）。所以映射是**一一对应且可逆**的，不是"把第一个下划线换成点"。
2. **冲突必须显式报错。** 两个内部名字清洗后撞车 ⇒ `ToolNameCollision` 在**发请求之前**
   抛出（具名、可定位），绝不允许静默合并 —— 否则模型调 A 会被派到 B。
3. **只改出站的线格式。** 壳内部（菜单、注册表、离线客户端、归档、判定）一律仍是
   `semantics.lookup`；因此既有读数不受影响。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

__all__ = [
    "MAX_WIRE_NAME",
    "WIRE_NAME_PATTERN",
    "ToolNameCollision",
    "ToolNameMap",
    "is_wire_name",
    "wire_name",
]

#: OpenAI-compatible `function.name` / `json_schema.name`: `[a-zA-Z0-9_-]+`.
WIRE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
#: The same two fields are capped at 64 characters.
MAX_WIRE_NAME = 64

_ILLEGAL = re.compile(r"[^a-zA-Z0-9_-]")


class ToolNameCollision(RuntimeError):
    """Two internal tool names would become the same wire name.

    Refused **before the request is built**: a request that silently merged them
    would let the model's call for one tool be dispatched to another, which is
    strictly worse than a failed call. The request shape has to change, so the
    declared action is the request-shape one.
    """

    reason = "format_error"


def is_wire_name(name: str) -> bool:
    """Whether a name is already acceptable to a strict OpenAI-compatible endpoint."""
    return bool(WIRE_NAME_PATTERN.match(name)) and len(name) <= MAX_WIRE_NAME


def wire_name(internal: str) -> str:
    """The wire spelling of one internal tool name.

    Characters outside ``[a-zA-Z0-9_-]`` become ``_``. A name longer than 64
    characters keeps a digest suffix instead of being truncated blindly, so it
    stays a function of the whole name (and any residual collision is still
    caught by :meth:`ToolNameMap.of`).
    """
    cleaned = _ILLEGAL.sub("_", internal)
    if len(cleaned) <= MAX_WIRE_NAME:
        return cleaned
    digest = sha256(internal.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[: MAX_WIRE_NAME - 9]}_{digest}"


@dataclass(frozen=True)
class ToolNameMap:
    """A one-to-one, explicit map between internal names and wire names."""

    forward: Mapping[str, str]
    reverse: Mapping[str, str]

    @classmethod
    def of(cls, internal_names: Sequence[str]) -> ToolNameMap:
        """Build the map, refusing any collision **loudly**.

        ``of(("a.b", "a_b"))`` raises rather than picking a winner: the two names
        are different tools and one of them would otherwise become unreachable.
        Names that are already wire-compliant map to themselves, so a menu of
        compliant names round-trips byte-identically.
        """
        forward: dict[str, str] = {}
        reverse: dict[str, str] = {}
        for internal in internal_names:
            wire = wire_name(internal)
            forward[internal] = wire
            existing = reverse.get(wire)
            if existing is not None and existing != internal:
                raise ToolNameCollision(
                    f"tool names {existing!r} and {internal!r} both become {wire!r} on the wire; "
                    "rename one of them (the translation must stay one-to-one)"
                )
            reverse[wire] = internal
        return cls(forward=forward, reverse=reverse)

    def wire(self, internal: str) -> str:
        """Internal -> wire. An unknown name is passed through (nothing to restore)."""
        return self.forward.get(internal, internal)

    def internal(self, name: str) -> str:
        """Wire -> internal. A name we never renamed is passed through unchanged."""
        return self.reverse.get(name, name)

    def wire_any(self, name: str) -> str:
        """Internal -> wire, for a name that may not be in this request's menu.

        An assistant transcript row can name a tool advertised by an *earlier* menu
        (a resumed turn, say). Falling back to the stateless rule keeps the
        translation total, so no dotted name can slip onto the wire.
        """
        return self.forward.get(name) or wire_name(name)

    def wire_tools(self, tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Rewrite the advertised tool menu into its wire spelling.

        Only ``function.name`` is touched; every other key (description,
        parameters, ...) is copied as-is. A menu entry without a ``function``
        object is passed through untouched.
        """
        out: list[dict[str, Any]] = []
        for tool in tools:
            entry = dict(tool)
            function = entry.get("function")
            if isinstance(function, Mapping):
                entry["function"] = {**function, "name": self.wire(str(function.get("name", "")))}
            out.append(entry)
        return out

    def internal_calls(self, calls: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
        """Rewrite the model's ``tool_calls`` back into internal names.

        The shape is preserved exactly (id / type / arguments untouched) so the
        transcript and the registry see what they always saw.
        """
        out: list[dict[str, Any]] = []
        for call in calls:
            entry = dict(call)
            function = entry.get("function")
            if isinstance(function, Mapping):
                entry["function"] = {**function, "name": self.internal(str(function.get("name", "")))}
            out.append(entry)
        return tuple(out)

    def wire_tool_choice(self, choice: Any) -> Any:
        """A forced tool choice names a function too -- translate it the same way."""
        if not isinstance(choice, Mapping):
            return choice
        function = choice.get("function")
        if not isinstance(function, Mapping) or "name" not in function:
            return choice
        return {**choice, "function": {**function, "name": self.wire(str(function["name"]))}}

    def wire_response_format(self, response_format: Any) -> Any:
        """``response_format.json_schema.name`` obeys the same pattern as a tool name.

        It is a label we own (no reverse mapping needed), so it is sanitized
        rather than refused -- the request stays valid without inventing a second
        failure mode. Already-compliant labels are returned unchanged.
        """
        if not isinstance(response_format, Mapping):
            return response_format
        schema = response_format.get("json_schema")
        if not isinstance(schema, Mapping) or "name" not in schema:
            return response_format
        name = str(schema["name"])
        if is_wire_name(name):
            return response_format
        return {**response_format, "json_schema": {**schema, "name": wire_name(name)}}


def changed_names(tools: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """``{internal: wire}`` for the entries whose wire spelling differs.

    Used by the acceptance archive so the record of "what the model saw" stays
    truthful: the shell's menu is internal, the wire menu is what actually went out.
    """
    changed: dict[str, str] = {}
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, Mapping):
            continue
        internal = str(function.get("name", ""))
        wire = wire_name(internal)
        if internal and wire != internal:
            changed[internal] = wire
    return changed
