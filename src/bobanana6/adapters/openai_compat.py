"""An OpenAI-compatible ``LLMClient``.

Only protocol translation happens here: HTTP in, :class:`ModelResponse` out. No
judgment lives in this module -- it does not know what a good answer looks like,
it does not check constraints, and it does not decide whether a block passed.

Configuration is read from ``env`` (``BOBANANA6_BASE_URL`` / ``_API_KEY`` /
``_MODEL``, falling back to the ``OPENAI_*`` spellings). The HTTP transport is
``urllib``, which picks the proxy up from the environment on its own -- the same
rule as ``sources/``: the proxy is never cleared.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from os import environ
from typing import Any

from ..core.model import ModelResponse
from .tool_names import ToolNameMap

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "PROTECTED_PAYLOAD_KEYS",
    "LLMConfigError",
    "LLMConnectionError",
    "LLMError",
    "LLMSettings",
    "OpenAICompatibleClient",
]


class LLMError(RuntimeError):
    """A model call that did not produce a usable response.

    ``status_code`` is carried because ``agentloop.errors`` classifies on it --
    an error without a status is classified as transport trouble, not as a
    request-shape problem, and the two recover differently.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class LLMConnectionError(LLMError):
    """The request never reached a server.

    Declared as the retryable network case: the vocabulary has no separate
    "connection" reason, and ``timeout`` is the one that maps to a local retry.
    """

    reason = "timeout"


class LLMConfigError(RuntimeError):
    """No usable endpoint was configured."""


#: Payload keys that describe *the call itself* rather than tuning it. A vendor
#: extension has no business replacing them: overwriting ``model`` or
#: ``messages`` would make "a model call" mean something else while still
#: returning 200, which is the most expensive kind of silence.
PROTECTED_PAYLOAD_KEYS: tuple[str, ...] = ("model", "messages")

DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str = ""
    #: ★ Cold starts are real: one measured service answered in 90.8s on the
    #: first request and 1.6s afterwards. The default stays at 60s -- raising it
    #: to make one slow service pass would slow down every other caller.
    timeout_s: float = DEFAULT_TIMEOUT_S
    #: Vendor extensions that are *not* part of the OpenAI request schema
    #: (``enable_thinking``, ``top_k``, ...). OpenAI-compatible servers and the
    #: OpenAI SDKs all call this ``extra_body``; it is the general escape hatch,
    #: not a per-vendor branch. It is merged last, so it also wins over
    #: ``max_tokens`` for services that spell it ``max_completion_tokens``.
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.base_url:
            raise LLMConfigError("base_url is required")
        if not self.model:
            raise LLMConfigError("model is required")
        if self.timeout_s <= 0:
            raise LLMConfigError(f"timeout_s must be > 0, got {self.timeout_s!r}")
        clashes = sorted(set(self.extra_body) & set(PROTECTED_PAYLOAD_KEYS))
        if clashes:
            # ★ Refused, never silently dropped: a setting that is accepted and
            #   then ignored sends the caller looking in the wrong place.
            raise LLMConfigError(
                f"extra_body must not override {clashes!r}: "
                f"{list(PROTECTED_PAYLOAD_KEYS)!r} are part of the call contract, not tuning"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMSettings:
        source = environ if env is None else env
        base_url = source.get("BOBANANA6_BASE_URL") or source.get("OPENAI_BASE_URL") or ""
        model = source.get("BOBANANA6_MODEL") or source.get("OPENAI_MODEL") or "gpt-4o-mini"
        api_key = source.get("BOBANANA6_API_KEY") or source.get("OPENAI_API_KEY") or ""
        if not base_url:
            raise LLMConfigError(
                "no endpoint configured: set BOBANANA6_BASE_URL (or OPENAI_BASE_URL), "
                "or run the offline path"
            )
        return cls(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_s=_timeout_from_env(source),
            extra_body=_extra_body_from_env(source),
        )

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def _timeout_from_env(source: Mapping[str, str]) -> float:
    raw = source.get("BOBANANA6_TIMEOUT_S")
    if raw is None or raw == "":
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError as exc:
        raise LLMConfigError(f"BOBANANA6_TIMEOUT_S must be a number of seconds, got {raw!r}") from exc


def _extra_body_from_env(source: Mapping[str, str]) -> Mapping[str, Any]:
    raw = source.get("BOBANANA6_EXTRA_BODY")
    if raw is None or raw.strip() == "":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMConfigError(f"BOBANANA6_EXTRA_BODY must be a JSON object: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise LLMConfigError(f"BOBANANA6_EXTRA_BODY must be a JSON object, got {type(parsed).__name__}")
    return dict(parsed)


def _error_detail(exc: urllib.error.HTTPError, limit: int = 400) -> str:
    """The service's error body, shortened. Best effort: an unreadable body is fine."""
    try:
        body = exc.read()
    except Exception:  # noqa: BLE001 - a missing body must not mask the status
        return ""
    if not body:
        return ""
    text = body.decode("utf-8", errors="replace").strip().replace("\n", " ")
    return f": {text[:limit]}"


@dataclass
class OpenAICompatibleClient:
    """``core/ports.py::LLMClient`` over the OpenAI chat-completions shape."""

    settings: LLMSettings
    opener: Any = None

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        **kw: Any,
    ) -> ModelResponse:
        # ★ 出站翻译：严格端点（DeepSeek 官方 / OpenAI 官方 / 多数企业网关）只接受
        #   `function.name` ∈ [a-zA-Z0-9_-]+。工具的内部名字含点号，是**壳内部**的稳定标识，
        #   不改（那会散到全仓）；在**发出去之前**换成线格式，入站再换回来。
        #   冲突（两个内部名字清洗后撞车）在这里就抛，绝不静默合并。
        names = ToolNameMap.of(
            [
                str(tool["function"].get("name", ""))
                for tool in tools
                if isinstance(tool, Mapping) and isinstance(tool.get("function"), Mapping)
            ]
        )
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": list(messages),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = names.wire_tools(tools)
        for key in ("temperature", "tool_choice", "response_format"):
            if key in kw:
                payload[key] = kw[key]
        if "tool_choice" in payload:
            payload["tool_choice"] = names.wire_tool_choice(payload["tool_choice"])
        if "response_format" in payload:
            payload["response_format"] = names.wire_response_format(payload["response_format"])
        # ★ Vendor extensions last. ``LLMSettings`` already refused to override
        #   ``model`` / ``messages``, so this can only tune the request.
        payload.update(self.settings.extra_body)

        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        request = urllib.request.Request(self.settings.endpoint, data=body, headers=headers, method="POST")
        opener = self.opener if self.opener is not None else urllib.request.build_opener()
        try:
            response = opener.open(request, timeout=self.settings.timeout_s)
        except urllib.error.HTTPError as exc:
            # ★ Carry the service's own words: the status alone cannot tell
            #   "response_format is not supported" from "your key is wrong", and the
            #   caller needs that difference to negotiate a capability instead of
            #   failing hard. Bounded, and never required to succeed.
            raise LLMError(
                f"model call failed with HTTP {exc.code}{_error_detail(exc)}",
                status_code=int(exc.code),
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LLMConnectionError(f"model call did not reach the server: {exc}") from exc
        with response:
            status = int(getattr(response, "status", 200))
            raw = response.read()
        if status != 200:
            raise LLMError(f"model call failed with HTTP {status}", status_code=status)
        return self._parse(raw, names)

    @staticmethod
    def _parse(raw: bytes, names: ToolNameMap | None = None) -> ModelResponse:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMError(f"model response is not JSON: {exc}") from exc
        if not isinstance(document, Mapping):
            raise LLMError("model response must be a JSON object")
        choices = document.get("choices")
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
            raise LLMError("model response has no choices")
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        if not isinstance(message, Mapping):
            raise LLMError("model response choice has no message")
        calls = message.get("tool_calls") or ()
        if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
            raise LLMError("tool_calls must be a list")
        # ★ 入站翻译：模型回的是线格式（`semantics_lookup`），壳内部只认 `semantics.lookup`。
        #   查**同一张表的反向** —— 只还原我们自己改过的名字，模型编的名字原样放行
        #   （交给既有的 unknown tool 路径，不在这里猜）。
        restored = names.internal_calls(calls) if names is not None else tuple(dict(call) for call in calls)
        usage = document.get("usage")
        return ModelResponse(
            text=str(message.get("content") or ""),
            tool_calls=restored,
            usage=dict(usage) if isinstance(usage, Mapping) else {},
        )
