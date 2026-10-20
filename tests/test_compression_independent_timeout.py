"""★ 缺口②：压缩调用要有**自己的**超时，并且这个超时真的传到 HTTP 层。

改前：`complete()` 的 `**kw` 只认 `temperature/tool_choice/response_format`
（`openai_compat.py:301-306`），传 `timeout=` 会被**静默忽略**；`llm_summarizer`
也不接受超时参数 ⇒ 压缩沿用块内主调用的全局 timeout（跑批实测 240s）。

这里钉住三件事：

1. `complete(..., timeout_s=N)` 真的把 N 交给 opener（不是被 `**kw` 吞掉）；
2. `llm_summarizer(timeout_s=N)` 只在该给的时候传这个键（缺省 = 旧路径逐字不变）；
3. 装配点默认给 `min(块超时, 120s)`，env 可覆盖，坏值具名拒。
"""

from __future__ import annotations

import pytest

from bobanana6.adapters import LLMConfigError, LLMSettings, OpenAICompatibleClient
from bobanana6.assemble_impl.runtime import llm_summarizer
from bobanana6.cli import DEFAULT_COMPRESS_TIMEOUT_S, compression_timeout_s
from bobanana6.core.model import ModelResponse
from tests.test_adapters import _FakeResponse, _RecordingOpener, _reply


def test_a_per_call_timeout_reaches_the_transport() -> None:
    opener = _RecordingOpener(_FakeResponse(_reply()))
    client = OpenAICompatibleClient(
        LLMSettings(base_url="http://x.invalid/v1", model="m", timeout_s=240.0), opener=opener
    )
    client.complete([], [], 1, timeout_s=7)
    assert opener.timeouts == [7.0]
    # 不给 = 沿用 settings（既有调用方逐字不变）
    client.complete([], [], 1)
    assert opener.timeouts[-1] == 240.0


def test_a_bad_per_call_timeout_is_refused_by_name() -> None:
    opener = _RecordingOpener(_FakeResponse(_reply()))
    client = OpenAICompatibleClient(LLMSettings(base_url="http://x.invalid/v1", model="m"), opener=opener)
    with pytest.raises(ValueError, match="timeout_s must be > 0"):
        client.complete([], [], 1, timeout_s=0)
    assert opener.requests == []  # 发都没发


class _CapturingClient:
    def __init__(self) -> None:
        self.kwargs: list[dict] = []

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        self.kwargs.append(dict(kw))
        return ModelResponse(text="summary")


def test_the_summarizer_passes_its_own_timeout() -> None:
    client = _CapturingClient()
    assert llm_summarizer(client, timeout_s=12.5)(({"role": "user", "content": "x"},)) == "summary"
    assert client.kwargs == [{"timeout_s": 12.5}]


def test_the_summarizer_without_a_timeout_keeps_the_old_call_shape() -> None:
    """缺省 = 一个键都不加（离线/宽松端点读数逐字不变）。"""
    client = _CapturingClient()
    llm_summarizer(client)(({"role": "user", "content": "x"},))
    assert client.kwargs == [{}]


def test_the_compression_timeout_defaults_below_a_240s_block_timeout() -> None:
    settings = LLMSettings(base_url="http://x.invalid/v1", model="m", timeout_s=240.0)
    client = OpenAICompatibleClient(settings)
    assert compression_timeout_s({}, client) == DEFAULT_COMPRESS_TIMEOUT_S


def test_the_compression_timeout_is_env_overridable_and_refuses_junk() -> None:
    settings = LLMSettings(base_url="http://x.invalid/v1", model="m", timeout_s=240.0)
    client = OpenAICompatibleClient(settings)
    assert compression_timeout_s({"BOBANANA6_COMPRESS_TIMEOUT_S": "33"}, client) == 33.0
    with pytest.raises(LLMConfigError, match="must be > 0"):
        compression_timeout_s({"BOBANANA6_COMPRESS_TIMEOUT_S": "0"}, client)
    with pytest.raises(LLMConfigError, match="must be a number"):
        compression_timeout_s({"BOBANANA6_COMPRESS_TIMEOUT_S": "soon"}, client)
