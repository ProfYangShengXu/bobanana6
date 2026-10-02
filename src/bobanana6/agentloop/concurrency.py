"""Concurrency: proactive throttling, single-flight, session isolation, routing.

Forked rule (实现规格 §5.9.4 / 架构设计 §7.6): concurrency is not an outer-layer
problem. The limiter lives *inside* the runtime because the thing being limited
is a model endpoint, and only the runtime knows which endpoint each call is
hitting.

Four jobs, all here:

  * **proactive throttle** -- over-budget calls are never sent, instead of being
    sent and bouncing off a 429. The incident behind this: four targets started
    within 30s smashed a provider's TPM and the whole batch was wasted.
  * **single-flight** -- while a key's call is in flight, later callers wait for
    that result instead of firing a duplicate. Without it, per-block retries
    multiply into duplicate model calls.
  * **session isolation** -- workers never share mutable state.
  * **request-level routing** -- easy blocks get the cheap route.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, runtime_checkable

__all__ = [
    "GateConfig",
    "ResourceGate",
    "RouteTable",
    "RuntimeGate",
    "SessionRegistry",
]

T = TypeVar("T")


@runtime_checkable
class ResourceGate(Protocol):
    def acquire(self, key: str, timeout: float) -> bool: ...

    def release(self, key: str) -> None: ...

    def single_flight(self, key: str) -> Callable[[Callable[[], T]], T]: ...


@dataclass(frozen=True)
class GateConfig:
    #: Per-key concurrency ceiling. One in-flight call per key is the safe default.
    max_concurrent: int = 1
    #: How long ``acquire`` may wait for a slot before giving up (0 = do not wait).
    default_timeout: float = 0.0

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise ValueError("GateConfig.max_concurrent must be >= 1")
        if self.default_timeout < 0:
            raise ValueError("GateConfig.default_timeout must be >= 0")


class RuntimeGate:
    """Thread-safe per-key limiter plus single-flight.

    ``acquire`` is the proactive throttle: it returns ``False`` without sending
    anything when the key is at its ceiling and the timeout elapses.
    """

    def __init__(self, config: GateConfig | None = None) -> None:
        self._config = config or GateConfig()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active: dict[str, int] = {}
        self._futures: dict[str, dict[str, Any]] = {}
        self._waiters: dict[str, int] = {}

    @property
    def config(self) -> GateConfig:
        return self._config

    def active(self, key: str) -> int:
        with self._lock:
            return self._active.get(key, 0)

    def waiters(self, key: str) -> int:
        """How many callers are currently waiting on ``key``'s in-flight call."""
        with self._lock:
            return self._waiters.get(key, 0)

    def acquire(self, key: str, timeout: float | None = None) -> bool:
        limit = self._config.max_concurrent
        wait_for = self._config.default_timeout if timeout is None else timeout
        if wait_for < 0:
            raise ValueError("timeout must be >= 0")
        deadline = time.monotonic() + wait_for
        with self._condition:
            while self._active.get(key, 0) >= limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False  # ★ not sent
                self._condition.wait(remaining)
            self._active[key] = self._active.get(key, 0) + 1
            return True

    def release(self, key: str) -> None:
        with self._condition:
            current = self._active.get(key, 0)
            if current <= 0:
                raise ValueError(f"release without a matching acquire for key {key!r}")
            self._active[key] = current - 1
            self._condition.notify_all()

    def single_flight(self, key: str) -> Callable[[Callable[[], T]], T]:
        """Later callers wait for the in-flight result instead of firing a duplicate.

        Each waiter holds a reference to the same future, so a waiter arriving
        while a call is in flight always observes *that* call's result. A caller
        arriving after it settled starts a new flight, which is correct.
        """

        def run(call: Callable[[], T]) -> T:
            with self._condition:
                future = self._futures.get(key)
                if future is None:
                    future = {"done": False, "ok": True, "payload": None}
                    self._futures[key] = future
                    leader = True
                else:
                    leader = False
                    self._waiters[key] = self._waiters.get(key, 0) + 1
                    try:
                        while not future["done"]:
                            self._condition.wait()
                    finally:
                        remaining = self._waiters.get(key, 0) - 1
                        if remaining > 0:
                            self._waiters[key] = remaining
                        else:
                            self._waiters.pop(key, None)
            if not leader:
                if future["ok"]:
                    return future["payload"]
                raise future["payload"]

            try:
                outcome: tuple[bool, Any] = (True, call())
            except BaseException as exc:  # noqa: BLE001 - delivered to every waiter
                outcome = (False, exc)
            with self._condition:
                future["ok"], future["payload"] = outcome
                future["done"] = True
                self._futures.pop(key, None)
                self._condition.notify_all()
            if outcome[0]:
                return outcome[1]
            raise outcome[1]

        return run


class SessionRegistry:
    """One isolated mutable session per worker; nothing is shared by default."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def session(self, worker_id: str) -> dict[str, Any]:
        with self._lock:
            return self._sessions.setdefault(worker_id, {})

    def clear(self, worker_id: str) -> None:
        with self._lock:
            self._sessions.pop(worker_id, None)

    def workers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._sessions))


@dataclass(frozen=True)
class RouteTable:
    """Maps an opaque block key to an opaque route name.

    The runtime never learns what a route *is*; the assembly point maps route
    names onto concrete clients. That is what keeps the cheap/strong model choice
    out of the kernel.
    """

    routes: Mapping[str, str]
    default: str = "default"

    def route(self, key: str) -> str:
        return self.routes.get(key, self.default)
