"""Concurrency: proactive throttle, single-flight, session isolation, routing."""

from __future__ import annotations

import threading
import time

import pytest

from bobanana6.agentloop.concurrency import (
    GateConfig,
    ResourceGate,
    RouteTable,
    RuntimeGate,
    SessionRegistry,
)


def test_gate_config_validation() -> None:
    with pytest.raises(ValueError):
        GateConfig(max_concurrent=0)
    with pytest.raises(ValueError):
        GateConfig(default_timeout=-1)


def test_runtime_gate_satisfies_the_port() -> None:
    gate = RuntimeGate()
    assert isinstance(gate, ResourceGate)


def test_acquire_is_proactive_not_reactive() -> None:
    """The third call is never sent -- no slot, no request."""
    gate = RuntimeGate(GateConfig(max_concurrent=2))
    assert gate.acquire("endpoint", 0.0) is True
    assert gate.acquire("endpoint", 0.0) is True
    assert gate.acquire("endpoint", 0.0) is False  # ★ not sent
    assert gate.active("endpoint") == 2


def test_release_frees_a_slot() -> None:
    gate = RuntimeGate(GateConfig(max_concurrent=1))
    assert gate.acquire("k", 0.0) is True
    assert gate.acquire("k", 0.0) is False
    gate.release("k")
    assert gate.acquire("k", 0.0) is True


def test_release_without_acquire_raises() -> None:
    with pytest.raises(ValueError, match="without a matching acquire"):
        RuntimeGate().release("k")


def test_acquire_can_wait_for_a_slot() -> None:
    gate = RuntimeGate(GateConfig(max_concurrent=1))
    assert gate.acquire("k", 0.0) is True
    outcome: list[bool] = []
    ready = threading.Event()
    release = threading.Event()

    def waiter() -> None:
        ready.set()
        outcome.append(gate.acquire("k", 1.0))

    thread = threading.Thread(target=waiter)
    thread.start()
    ready.wait(1.0)
    release.set()
    gate.release("k")
    thread.join(2.0)
    assert outcome == [True]


def _wait_for_waiters(gate: RuntimeGate, key: str, count: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while gate.waiters(key) < count and time.monotonic() < deadline:
        time.sleep(0.001)
    assert gate.waiters(key) >= count, "waiters never registered on the in-flight call"


def test_single_flight_dedupes_and_every_waiter_gets_the_result() -> None:
    """The leader is held in flight while the other callers arrive."""
    gate = RuntimeGate()
    calls: list[int] = []
    results: list[str] = []
    in_flight = threading.Event()
    release = threading.Event()

    def leader() -> str:
        calls.append(1)
        in_flight.set()
        release.wait(3.0)
        return "answer"

    holder = threading.Thread(target=lambda: results.append(gate.single_flight("model:glm")(leader)))
    holder.start()
    assert in_flight.wait(2.0)

    waiters = [
        threading.Thread(
            target=lambda: results.append(gate.single_flight("model:glm")(lambda: (calls.append(1), "dup")[1]))
        )
        for _ in range(3)
    ]
    for thread in waiters:
        thread.start()
    _wait_for_waiters(gate, "model:glm", 3)
    release.set()
    holder.join(3.0)
    for thread in waiters:
        thread.join(3.0)

    assert len(calls) == 1  # ★ only one call went out
    assert results == ["answer"] * 4


def test_single_flight_delivers_a_failure_to_every_waiter() -> None:
    gate = RuntimeGate()
    calls: list[int] = []
    in_flight = threading.Event()
    release = threading.Event()

    def boom() -> str:
        calls.append(1)
        in_flight.set()
        release.wait(3.0)
        raise RuntimeError("provider down")

    outcomes: list[str] = []

    def holder() -> None:
        try:
            gate.single_flight("k")(boom)
        except RuntimeError:
            outcomes.append("error")

    leader = threading.Thread(target=holder)
    leader.start()
    assert in_flight.wait(2.0)
    waiters = [threading.Thread(target=holder) for _ in range(2)]
    for thread in waiters:
        thread.start()
    _wait_for_waiters(gate, "k", 2)
    release.set()
    leader.join(3.0)
    for thread in waiters:
        thread.join(3.0)

    assert len(calls) == 1
    assert outcomes == ["error", "error", "error"]


def test_sessions_are_isolated_per_worker() -> None:
    registry = SessionRegistry()
    alice = registry.session("worker-a")
    bob = registry.session("worker-b")
    alice["note"] = "a"
    assert bob == {}
    assert registry.workers() == ("worker-a", "worker-b")
    registry.clear("worker-a")
    assert registry.workers() == ("worker-b",)


def test_session_is_stable_for_the_same_worker() -> None:
    registry = SessionRegistry()
    registry.session("w")["x"] = 1
    assert registry.session("w")["x"] == 1


def test_route_table_maps_keys_to_opaque_routes() -> None:
    table = RouteTable(routes={"easy": "cheap"}, default="strong")
    assert table.route("easy") == "cheap"
    assert table.route("hard") == "strong"
