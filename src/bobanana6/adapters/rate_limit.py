"""Token-budget rate limiting + minute-scale 429 backoff (adapter layer, stdlib only).

★ Why this exists (measured, not a hunch): run `qwen4b8` died 8/8 blocks with
  `HTTP 429 TPM limit reached`. The cause was **not the average** -- the sliding
  60-second peak was 417,633 tokens against a 120,000 TPM limit (3.48x), and 62.9%
  of windows were over. RPM never came close (peak 14 vs a 1,200 limit). A few
  tool calls in a row, each ~5.5e4 tokens, is enough to blow a whole minute.

Two mechanisms, both here (`core/` stays zero-I/O; the adapters own the socket):

* **sliding-window token budget** -- before a request we add its *estimated* size
  to what the last 60s actually spent; if that would exceed the limit we wait until
  the oldest consumption slides out. The estimate is the last request's
  ``prompt_tokens`` at the same ``max_tokens``; with no history we use a
  conservative default (underestimating is what blows the budget).
* **minute-scale backoff on 429** -- wait for the current window to turn over and
  retry a bounded number of times, **reusing the same request body**. The old path
  retried immediately (there is no ``sleep`` anywhere in the old source), so every
  attempt landed inside the same window.

```text
BOBANANA6_TPM_LIMIT   default 120000   (the run above was measured against this)
BOBANANA6_RPM_LIMIT   default 1200
```

The limits are **configuration, never hard-coded**: the new model ``Xing4.0-29B``
has a 60,000 TPM, so the same binary must run under a different number.

★ The limiter **never gates work**: `needed` is capped at the limit, so a single
  request larger than the whole budget still goes out (waiting could not help) -- an
  under-provisioned deployment degrades to the old behaviour instead of refusing to
  start. What it will *not* do is send while the window is already carrying spend:
  if the estimate overshoots, it waits for that spend to slide out.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from statistics import median
from typing import Any

__all__ = [
    "DEFAULT_MAX_429_RETRIES",
    "DEFAULT_RPM_LIMIT",
    "DEFAULT_TOKEN_ESTIMATE",
    "DEFAULT_TPM_LIMIT",
    "WINDOW_S",
    "TokenRateLimiter",
    "format_rate_limit_readings",
]

#: The budget window. A minute is what TPM is defined against on the endpoints we use.
WINDOW_S = 60.0
DEFAULT_TPM_LIMIT = 120_000
DEFAULT_RPM_LIMIT = 1_200
#: Bounded retries on 429 -- never unlimited (a hot loop burns more than it saves).
DEFAULT_MAX_429_RETRIES = 3
#: Per-request estimate before any history exists. The measured calls were ~5.5e4
#: input tokens; the point of a *conservative* default is that we would rather wait
#: on the first call than discover the size only after a 429.
DEFAULT_TOKEN_ESTIMATE = 60_000


@dataclass
class TokenRateLimiter:
    """A sliding-window token budget (and request counter), thread-safe.

    ``clock`` and ``sleeper`` are injectable so the behaviour is testable without
    real time: the budget arithmetic is the thing under test, not ``time.sleep``.
    """

    tpm_limit: int = DEFAULT_TPM_LIMIT  # blame: whitelist — 限额是部署配置：生产由 cli 从 env 传入，默认即实测服务端额度
    rpm_limit: int = DEFAULT_RPM_LIMIT  # blame: whitelist — 同上（RPM 从未接近，留着是为了有读数）
    window_s: float = WINDOW_S  # blame: whitelist — 窗口长度是 TPM 的定义（60s），不是调优旋钮
    clock: Callable[[], float] = time.monotonic  # blame: whitelist — 只为可测注入（假 clock），生产用默认
    sleeper: Callable[[float], None] = time.sleep  # blame: whitelist — 只为可测注入（假 sleeper），生产用默认
    #: (at, tokens) of every completed request inside the window.
    _spent: list[tuple[float, int]] = field(default_factory=list, repr=False)  # blame: whitelist — 运行期内部状态，省略即空
    #: Request start times inside the window (the RPM side).
    _sent: list[float] = field(default_factory=list, repr=False)  # blame: whitelist — 运行期内部状态，省略即空
    #: Last observed ``prompt_tokens`` per ``max_tokens`` -- the next estimate.
    _last_prompt_by_size: dict[int, int] = field(default_factory=dict, repr=False)  # blame: whitelist — 运行期内部状态，省略即空
    _prompt_tokens: deque[int] = field(default_factory=lambda: deque(maxlen=10_000), repr=False)  # blame: whitelist — 运行期内部状态；有界（maxlen）防长跑线性增长
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)  # blame: whitelist — 运行期内部状态，省略即新建
    _requests: int = 0  # blame: whitelist — 运行期计数，省略即 0
    _wait_seconds: float = 0.0  # blame: whitelist — 运行期计数，省略即 0
    _waits: int = 0  # blame: whitelist — 运行期计数，省略即 0
    _rate_limit_waits: int = 0  # blame: whitelist — 运行期计数，省略即 0
    _rate_limit_retries: int = 0  # blame: whitelist — 运行期计数，省略即 0

    def __post_init__(self) -> None:
        if self.tpm_limit <= 0:
            raise ValueError(f"tpm_limit must be > 0, got {self.tpm_limit!r}")
        if self.rpm_limit <= 0:
            raise ValueError(f"rpm_limit must be > 0, got {self.rpm_limit!r}")
        if self.window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {self.window_s!r}")

    # -- estimate / record -------------------------------------------------- #

    def estimate_for(self, max_tokens: int) -> int:
        """Size estimate for the next request, keyed by the same ``max_tokens``.

        Falls back to the last prompt size seen (any size), then to the conservative
        default. Never returns 0 -- a zero estimate is how a budget gets blown.
        """
        with self._lock:
            guess = self._last_prompt_by_size.get(int(max_tokens))
            if guess is None and self._prompt_tokens:
                guess = self._prompt_tokens[-1]
            return max(1, int(guess or DEFAULT_TOKEN_ESTIMATE))

    def record(self, total_tokens: int, *, prompt_tokens: int | None = None, max_tokens: int | None = None) -> None:  # blame: whitelist — 省略 = 这次不记 prompt/尺寸（端点没报 usage），读数不被 0 污染
        """Account one completed request (usage came from the provider)."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            self._spent.append((now, max(0, int(total_tokens))))
            if prompt_tokens is not None:
                prompt = max(0, int(prompt_tokens))
                self._prompt_tokens.append(prompt)
                if max_tokens is not None:
                    self._last_prompt_by_size[int(max_tokens)] = prompt

    # -- the pre-send wait -------------------------------------------------- #

    def acquire(self, estimate: int) -> float:
        """Wait until the window has room for ``estimate``; return seconds waited.

        Never refuses outright: ``needed`` is capped at the whole limit, so a request
        bigger than the budget is still sent when the window is empty. When the window
        already carries spend, an oversized estimate waits for it to slide out -- that
        wait is the whole point of a *token* budget."""
        waited = 0.0
        while True:
            with self._lock:
                delay = self._seconds_until_room(estimate)
            if delay <= 0:
                break
            self.sleeper(delay)
            waited += delay
            with self._lock:
                self._wait_seconds += delay
                self._waits += 1
        with self._lock:
            now = self.clock()
            self._prune(now)
            self._sent.append(now)
            self._requests += 1
        return waited

    # -- the 429 backoff ---------------------------------------------------- #

    def next_window_delay(self) -> float:
        """Seconds until the current window is **empty** (a full window if it is).

        ★ 用**最新一笔** + 窗口，不是最早一笔（只读审计 #119）：等最早那笔滑出时，
          窗口里靠后的消耗还在，重试照样撞在同一个窗口里。空窗时给一整个窗口 ——
          说不清原因的 429 值得一次分钟级等待，而不是立刻重试。
        """
        with self._lock:
            now = self.clock()
            self._prune(now)
            latest = max((at for at, _ in self._spent), default=now)
            return max(0.0, latest + self.window_s - now)

    def backoff(self, retry: int) -> float:
        """Wait out the current window for a 429; account it as a rate-limit wait."""
        delay = self.next_window_delay()
        with self._lock:
            self._wait_seconds += delay
            self._waits += 1
            self._rate_limit_waits += 1
            self._rate_limit_retries += 1
        if delay > 0:
            self.sleeper(delay)
        return delay

    # -- visibility --------------------------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        """The readings the run summary prints (never log-only, never debug-only)."""
        with self._lock:
            self._prune(self.clock())
            prompts = self._prompt_tokens
            return {
                "tpm_limit": self.tpm_limit,
                "rpm_limit": self.rpm_limit,
                "requests": self._requests,
                "waits": self._waits,
                "wait_seconds": round(self._wait_seconds, 3),
                "rate_limit_waits": self._rate_limit_waits,
                "rate_limit_retries": self._rate_limit_retries,
                "prompt_tokens_median": int(median(prompts)) if prompts else 0,
                "prompt_tokens_max": max(prompts) if prompts else 0,
            }

    # -- internals ---------------------------------------------------------- #

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        if self._spent and self._spent[0][0] <= cutoff:
            self._spent = [entry for entry in self._spent if entry[0] > cutoff]
        if self._sent and self._sent[0] <= cutoff:
            self._sent = [at for at in self._sent if at > cutoff]

    def _seconds_until_room(self, estimate: int) -> float:
        now = self.clock()
        self._prune(now)
        needed = min(max(1, int(estimate)), self.tpm_limit)
        spent = sum(tokens for _, tokens in self._spent)
        if spent + needed <= self.tpm_limit:
            if len(self._sent) < self.rpm_limit:
                return 0.0
            return max(0.0, self._sent[0] + self.window_s - now)
        remaining = spent + needed - self.tpm_limit
        freed = 0
        oldest = now
        for at, tokens in self._spent:
            oldest = at
            freed += tokens
            if freed >= remaining:
                break
        return max(0.0, oldest + self.window_s - now)

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeper(seconds)


def format_rate_limit_readings(snapshot: Mapping[str, Any], run_seconds: float) -> str:
    """One line for the run summary -- the same line as "块级 … 调用=…"."""
    waited = float(snapshot["wait_seconds"])
    share = (waited / run_seconds * 100.0) if run_seconds > 0 else 0.0
    return (
        f"限速 等={snapshot['waits']} 次/{waited:.1f}s({share:.1f}%)"
        f" · 429 触发={snapshot['rate_limit_waits']} 次"
        f" (共 {snapshot['rate_limit_retries']} 次重试)"
        f" · 输入 token 中位={snapshot['prompt_tokens_median']}"
        f" 最大={snapshot['prompt_tokens_max']}"
        f" · TPM={snapshot['tpm_limit']}/RPM={snapshot['rpm_limit']}"
    )
