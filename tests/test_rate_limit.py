"""Token-budget rate limiting + minute-scale 429 backoff. Deterministic, no endpoint.

The measured failure this pins down (§〇): the sliding-60s peak was 417,633 tokens
against a 120,000 TPM limit (3.48x), 62.9% of windows over -- a *burst* problem, not
an average one. RPM never came close (14 vs 1,200).

Everything here drives an injectable clock/sleeper, so the budget arithmetic is what
is under test; no test sleeps for real.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path
from typing import Self

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.adapters import (
    LLMConfigError,
    LLMError,
    LLMSettings,
    OpenAICompatibleClient,
    TokenRateLimiter,
    format_rate_limit_readings,
)
from bobanana6.adapters.rate_limit import DEFAULT_TOKEN_ESTIMATE, WINDOW_S


class _Clock:
    """A monotonic clock the test controls, plus a recorded (never real) sleeper."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _RetryOpener:
    """Fails the first ``failures`` calls with 429, then answers; keeps every body."""

    def __init__(self, failures: int, payload: bytes) -> None:
        self.failures = failures
        self.payload = payload
        self.bodies: list[bytes] = []

    def open(self, request: object, timeout: float = 0.0) -> _FakeResponse:
        self.bodies.append(request.data)  # type: ignore[attr-defined]
        if len(self.bodies) <= self.failures:
            raise urllib.error.HTTPError("u", 429, "too many requests", {}, None)
        return _FakeResponse(self.payload)


def _reply(*, prompt: int | None = None, completion: int | None = None) -> bytes:
    usage: dict[str, int] = {"total_tokens": 7}
    if prompt is not None:
        usage["prompt_tokens"] = prompt
    if completion is not None:
        usage["completion_tokens"] = completion
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": "answer"}}], "usage": usage}
    ).encode()


def _limiter(clock: _Clock, **kw: object) -> TokenRateLimiter:
    kw.setdefault("tpm_limit", 120_000)
    kw.setdefault("rpm_limit", 1_200)
    return TokenRateLimiter(clock=clock.monotonic, sleeper=clock.sleep, **kw)  # type: ignore[arg-type]


def _client(opener: object, limiter: TokenRateLimiter | None, **settings: object) -> OpenAICompatibleClient:
    base = {"base_url": "http://x.invalid/v1", "model": "m"}
    base.update(settings)
    return OpenAICompatibleClient(LLMSettings(**base), opener=opener, limiter=limiter)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ① the sliding window (a burst over the limit waits; under it, never)
# --------------------------------------------------------------------------- #


def test_a_burst_over_the_tpm_limit_waits_for_the_window_to_turn_over() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    assert limiter.acquire(50_000) == 0.0
    limiter.record(50_000, prompt_tokens=50_000, max_tokens=8192)
    assert limiter.acquire(50_000) == 0.0  # 100k <= 120k
    limiter.record(50_000, prompt_tokens=50_000, max_tokens=8192)
    waited = limiter.acquire(50_000)  # 150k > 120k -> the oldest must slide out
    assert waited == pytest.approx(WINDOW_S)
    snapshot = limiter.snapshot()
    assert snapshot["waits"] == 1
    assert snapshot["wait_seconds"] == pytest.approx(WINDOW_S)


def test_under_the_limit_costs_exactly_zero() -> None:
    """★ 反向：不超限 ⇒ 一次都不等、一次都不 sleep（零开销路径必须真零）。"""
    clock = _Clock()
    limiter = _limiter(clock)
    for _ in range(5):
        assert limiter.acquire(1_000) == 0.0
        limiter.record(1_000, prompt_tokens=1_000, max_tokens=100)
    snapshot = limiter.snapshot()
    assert (snapshot["waits"], snapshot["wait_seconds"]) == (0, 0.0)
    assert clock.slept == []


def test_an_estimate_bigger_than_the_limit_still_sends() -> None:
    """★ 限速不是前置门：一次请求就比整个限额还大时照发（否则死锁）。"""
    clock = _Clock()
    limiter = _limiter(clock, tpm_limit=10_000)
    assert limiter.acquire(60_000) == 0.0
    assert clock.slept == []


def test_the_estimate_uses_the_last_same_size_request() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    assert limiter.estimate_for(8192) == DEFAULT_TOKEN_ESTIMATE  # no history -> conservative
    limiter.record(10, prompt_tokens=55_000, max_tokens=8192)
    assert limiter.estimate_for(8192) == 55_000
    assert limiter.estimate_for(4096) == 55_000  # fallback: the last prompt seen


def test_the_rpm_side_also_waits() -> None:
    clock = _Clock()
    limiter = _limiter(clock, tpm_limit=10_000_000, rpm_limit=2)
    assert limiter.acquire(1) == 0.0
    assert limiter.acquire(1) == 0.0
    assert limiter.acquire(1) == pytest.approx(WINDOW_S)


# --------------------------------------------------------------------------- #
# ② 429 minute-scale backoff, replaying the SAME request body
# --------------------------------------------------------------------------- #


def test_a_429_waits_a_window_and_replays_the_same_body_bytes() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    opener = _RetryOpener(2, _reply())
    client = _client(opener, limiter)

    response = client.complete([{"role": "user", "content": "hi"}], [], 128)

    assert response.text == "answer"
    assert len(opener.bodies) == 3
    # ★★ 冻结上下文：三次尝试的请求体**逐字节相同**（重试不重新组装 prompt）
    assert opener.bodies[0] == opener.bodies[1] == opener.bodies[2]
    assert clock.slept == [pytest.approx(WINDOW_S), pytest.approx(WINDOW_S)]
    snapshot = limiter.snapshot()
    assert snapshot["rate_limit_waits"] == 2
    assert snapshot["rate_limit_retries"] == 2


def test_the_429_retries_are_bounded() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    opener = _RetryOpener(99, _reply())
    client = _client(opener, limiter, max_rate_limit_retries=3)

    with pytest.raises(LLMError) as error:
        client.complete([], [], 1)

    assert error.value.status_code == 429  # 超了照旧把 429 交给既有路径
    assert len(opener.bodies) == 4  # 1 次原始 + 3 次重试
    assert limiter.snapshot()["rate_limit_waits"] == 3


def test_without_a_limiter_a_429_is_still_immediate() -> None:
    """★ 反向：没挂 limiter（离线替身 / 单测）⇒ 429 立刻抛，行为与改前一致。"""
    opener = _RetryOpener(99, _reply())
    client = _client(opener, None)
    with pytest.raises(LLMError) as error:
        client.complete([], [], 1)
    assert error.value.status_code == 429
    assert len(opener.bodies) == 1


# --------------------------------------------------------------------------- #
# ③ limits are configuration (never hard-coded) + usage feeds the readings
# --------------------------------------------------------------------------- #


def test_the_limits_come_from_the_environment() -> None:
    settings = LLMSettings.from_env(
        {
            "BOBANANA6_BASE_URL": "http://x.invalid/v1",
            "BOBANANA6_TPM_LIMIT": "60000",  # e.g. Xing4.0-29B
            "BOBANANA6_RPM_LIMIT": "600",
            "BOBANANA6_MAX_429_RETRIES": "1",
        }
    )
    assert (settings.tpm_limit, settings.rpm_limit, settings.max_rate_limit_retries) == (60_000, 600, 1)


def test_a_bad_limit_is_refused_not_guessed() -> None:
    for env in ({"BOBANANA6_TPM_LIMIT": "0"}, {"BOBANANA6_RPM_LIMIT": "nope"}):
        with pytest.raises(LLMConfigError):
            LLMSettings.from_env({"BOBANANA6_BASE_URL": "http://x.invalid/v1", **env})


def test_the_assembly_point_attaches_a_limiter_using_the_env_limits() -> None:
    from bobanana6 import cli

    args = argparse.Namespace(offline=False, offline_script=None)
    client = cli.build_client(
        args,
        {
            "BOBANANA6_BASE_URL": "http://x.invalid/v1",
            "BOBANANA6_MODEL": "m",
            "BOBANANA6_TPM_LIMIT": "60000",
        },
    )
    assert client.limiter is not None  # type: ignore[attr-defined]
    assert client.limiter.tpm_limit == 60_000  # type: ignore[attr-defined]


def test_a_successful_call_records_usage_for_the_readings() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    client = _client(_RetryOpener(0, _reply(prompt=1234, completion=56)), limiter)
    client.complete([], [], 128)
    snapshot = limiter.snapshot()
    assert snapshot["requests"] == 1
    assert snapshot["prompt_tokens_max"] == 1234
    assert snapshot["prompt_tokens_median"] == 1234


def test_the_readings_line_prints_waits_and_token_stats() -> None:
    line = format_rate_limit_readings(
        {
            "tpm_limit": 120_000,
            "rpm_limit": 1_200,
            "requests": 140,
            "waits": 3,
            "wait_seconds": 90.0,
            "rate_limit_waits": 1,
            "rate_limit_retries": 1,
            "prompt_tokens_median": 55_000,
            "prompt_tokens_max": 61_234,
        },
        run_seconds=100.0,
    )
    assert "等=3 次/90.0s(90.0%)" in line
    assert "429 触发=1 次" in line
    assert "中位=55000" in line and "最大=61234" in line
    assert "TPM=120000/RPM=1200" in line
