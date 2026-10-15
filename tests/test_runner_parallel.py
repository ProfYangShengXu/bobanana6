"""★ 块级并发（``--parallel``，默认关闭）的验收。

四条硬约束各留一条可重放的读数：

  1. **默认串行不变**：``parallel == 1`` 时 ``runtime_factory`` 一次都不被调用，
     且跑出来的结果与串行路径逐字相同（与 ``test_runner`` 的既有断言同源）。
  2. **并发确实重叠**：独立块在 ``parallel >= 2`` 时区间相交，串行时必然不相交。
  3. **依赖与写区互斥**：B 依赖 A ⇒ B 必须在 A 结束之后才开始；
     同 ``partition`` 的两个块即使并行度足够也不得同时跑。
  4. **判定一致 / 旋钮正交**：串行与并发的 ``verdicts`` 相同；
     ``--parallel`` 不改 ``--concurrency`` 的能力申报（``capability_short`` 照旧）。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bobanana6.adapters import EchoToolResultClient
from bobanana6.agentloop.concurrency import GateConfig, RuntimeGate
from bobanana6.agentloop.events import EventLog
from bobanana6.assemble_impl import RunRefused
from bobanana6.assemble_impl.runner import Runner
from bobanana6.core.model import Subtask
from bobanana6.services.traces import TraceStore

from test_runner import ONE_SOURCE, TWO_SOURCES, _ByBlock, _csv, _runner, _tool_call


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class _Sleepy:
    """Widen every model call so overlapping block intervals are observable."""

    def __init__(self, inner: Any, delay: float) -> None:
        self.inner = inner
        self.delay = delay

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        time.sleep(self.delay)
        return self.inner.complete(*args, **kwargs)


def _capture_spans(runner: Runner) -> tuple[Any, dict[str, tuple[float, float]]]:
    """Run ``runner`` for real, recording each block's ``_run_one`` time span."""
    spans: dict[str, tuple[float, float]] = {}
    lock = threading.Lock()
    real = Runner._run_one

    def wrapped(self: Runner, subtask: Subtask, **kwargs: Any) -> Any:
        start = time.monotonic()
        try:
            return real(self, subtask, **kwargs)
        finally:
            end = time.monotonic()
            with lock:
                spans[subtask.id] = (start, end)

    Runner._run_one = wrapped  # type: ignore[assignment]
    try:
        result = runner.run()
    finally:
        Runner._run_one = real  # type: ignore[assignment]
    return result, spans


def _overlap(spans: dict[str, tuple[float, float]], first: str, second: str) -> bool:
    a0, a1 = spans[first]
    b0, b1 = spans[second]
    return a0 < b1 and b0 < a1


def _parallel_runner(runner: Runner, parallel: int) -> Runner:
    """A copy of ``runner`` that runs blocks concurrently, one isolated runtime each."""

    def factory() -> Any:
        return replace(
            runner.runtime,
            traces=TraceStore(),
            event_log=EventLog(),
            gate=RuntimeGate(GateConfig(max_concurrent=1, default_timeout=0.0)),
            last_judge=None,
            last_cleanliness={},
            last_candidate=None,
            summarize=None,
        )

    return replace(runner, parallel=parallel, runtime_factory=factory)


def _payload(ref: str, rows: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "ref": ref,
            "columns": [{"name": "obligor", "dtype": "str"}, {"name": "pd", "dtype": "str"}],
            "rows": rows,
            "done": True,
        }
    )


def _two_source_text(tmp_path: Path, tag: str) -> str:
    one = _csv(tmp_path, f"{tag}-one.csv", "obligor,pd\nA-1,0.01\n")
    two = _csv(tmp_path, f"{tag}-two.csv", "obligor,pd\nA-2,0.02\n")
    return TWO_SOURCES.replace("ONE_CSV", str(one)).replace("TWO_CSV", str(two))


def _two_source_client() -> _ByBlock:
    merged = _payload("src-1", [{"obligor": "A-1", "pd": "0.01"}, {"obligor": "A-2", "pd": "0.02"}])
    return _ByBlock(
        {
            "T-01": [
                {"tool_calls": (_tool_call("sources.snapshot", {"ref": "src-1"}, "c1"),)},
                {"text": _payload("src-1", [{"obligor": "A-1", "pd": "0.01"}])},
            ],
            "T-02": [
                {"tool_calls": (_tool_call("sources.snapshot", {"ref": "src-2"}, "c2"),)},
                {"text": _payload("src-2", [{"obligor": "A-2", "pd": "0.02"}])},
            ],
            "T-03": [
                {"tool_calls": (_tool_call("task.upstream", {"name": "part-src-1.csv"}, "c3"),)},
                {"tool_calls": (_tool_call("task.upstream", {"name": "part-src-2.csv"}, "c4"),)},
                {"text": merged},
            ],
        }
    )


def _stub_runner(tmp_path: Path, parallel: int) -> Runner:
    source = _csv(tmp_path, "stub.csv", "obligor,pd\nA-1,0.01\n")
    runner = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(source)), EchoToolResultClient())
    return _parallel_runner(runner, parallel)


def _run_stub_scheduler(
    runner: Runner, blocks: tuple[Subtask, ...], *, sleep: float = 0.15
) -> dict[str, tuple[float, float]]:
    """Run ``_run_parallel`` with a stubbed block body; return per-block time spans."""
    spans: dict[str, tuple[float, float]] = {}
    lock = threading.Lock()

    def fake_run_one(self: Runner, subtask: Subtask, **kwargs: Any) -> Any:
        start = time.monotonic()
        time.sleep(sleep)
        end = time.monotonic()
        with lock:
            spans[subtask.id] = (start, end)
        return SimpleNamespace(runtime=kwargs["runtime"], event_log=kwargs["event_log"], decisions=())

    real_run_one, real_merge = Runner._run_one, Runner._merge
    Runner._run_one = fake_run_one  # type: ignore[assignment]
    Runner._merge = lambda self, outcome, **kwargs: None  # type: ignore[assignment]
    try:
        runner._run_parallel(
            plan=SimpleNamespace(subtasks=blocks),
            bundle=None,
            ontology=None,
            snapshots={},
            context=None,
            pieces={},
            handoffs={},
            piece_provenance={},
            producers={block.output: block.id for block in blocks},
            candidates={},
            verdicts={},
            commits={},
            decisions=[],
        )
    finally:
        Runner._run_one, Runner._merge = real_run_one, real_merge  # type: ignore[assignment]
    return spans


def _block(**overrides: Any) -> Subtask:
    base = dict(id="T", goal="g", inputs=(), output="o.csv", contract_ref="t", axis="product", mode="write")
    base.update(overrides)
    return Subtask(**base)


# --------------------------------------------------------------------------- #
# 1. default stays serial: the factory is never called
# --------------------------------------------------------------------------- #


def test_default_parallel_is_serial_and_the_factory_is_never_called(tmp_path: Path) -> None:
    text = _two_source_text(tmp_path, "def")
    runner = _runner(tmp_path, text, _two_source_client())

    def forbidden() -> Any:
        raise AssertionError("runtime_factory must not be called when parallel == 1")

    result = replace(runner, parallel=1, runtime_factory=forbidden).run()
    assert result.ok is True, result.verdicts


# --------------------------------------------------------------------------- #
# 2. concurrency really overlaps (and the serial control does not)
# --------------------------------------------------------------------------- #


def test_independent_blocks_overlap_when_parallel(tmp_path: Path) -> None:
    text = _two_source_text(tmp_path, "ovl")
    runner = _parallel_runner(_runner(tmp_path, text, _Sleepy(_two_source_client(), 0.05)), 4)
    result, spans = _capture_spans(runner)
    assert result.ok is True, result.verdicts
    assert _overlap(spans, "T-01", "T-02"), spans  # 两个独立读块的时间区间相交
    assert spans["T-03"][0] >= max(spans["T-01"][1], spans["T-02"][1]) - 1e-6, spans  # 合并块等两个读块


def test_the_serial_control_does_not_overlap(tmp_path: Path) -> None:
    text = _two_source_text(tmp_path, "ctl")
    runner = _runner(tmp_path, text, _Sleepy(_two_source_client(), 0.05))
    result, spans = _capture_spans(runner)  # parallel == 1（默认）
    assert result.ok is True, result.verdicts
    ordered = sorted(spans.items(), key=lambda item: item[1][0])
    for (_first, (_a0, a1)), (_second, (b0, _b1)) in zip(ordered, ordered[1:]):
        assert b0 >= a1 - 1e-6, spans  # 串行：严格先后，无重叠


# --------------------------------------------------------------------------- #
# 3. dependency + write-region exclusion
# --------------------------------------------------------------------------- #


def test_a_dependent_block_starts_only_after_its_producer(tmp_path: Path) -> None:
    blocks = (
        _block(id="A", output="a.out", partition="deliverable:a"),
        _block(id="B", inputs=("a.out",), output="b.out", partition="deliverable:b"),
    )
    spans = _run_stub_scheduler(_stub_runner(tmp_path, 4), blocks)
    a0, a1 = spans["A"]
    b0, b1 = spans["B"]
    assert b0 >= a1 - 1e-6, spans  # B 不得早于 A 结束
    assert not (a0 < b1 and b0 < a1), spans


def test_two_blocks_in_the_same_partition_never_run_together(tmp_path: Path) -> None:
    blocks = (
        _block(id="A", output="a.out", partition="deliverable:shared"),
        _block(id="B", output="b.out", partition="deliverable:shared"),
    )
    spans = _run_stub_scheduler(_stub_runner(tmp_path, 2), blocks)
    (a0, a1), (b0, b1) = spans["A"], spans["B"]
    assert not (a0 < b1 and b0 < a1), spans  # 写区互斥：不得重叠


def test_partition_conflict_helper() -> None:
    assert Runner._partition_conflict(_block(partition="p"), {"p"}) is True
    assert Runner._partition_conflict(_block(partition="p"), {"q"}) is False
    assert Runner._partition_conflict(_block(partition=None), {"p"}) is False


def test_inputs_ready_helper_covers_missing_and_never_produced() -> None:
    waiting = _block(inputs=("a.out",))
    producers = {"a.out": "A"}
    assert Runner._inputs_ready(waiting, {"a.out": object()}, set(), producers) is True
    assert Runner._inputs_ready(waiting, {}, set(), producers) is False  # A 还没跑
    assert Runner._inputs_ready(waiting, {}, {"A"}, producers) is True  # A 跑完但没产出
    assert Runner._inputs_ready(waiting, {}, set(), {}) is True  # 没人负责这份输入


# --------------------------------------------------------------------------- #
# 4. parallel and serial judge the same; --parallel is orthogonal to --concurrency
# --------------------------------------------------------------------------- #


def test_serial_and_parallel_produce_the_same_verdicts(tmp_path: Path) -> None:
    text_serial = _two_source_text(tmp_path, "ser")
    text_parallel = _two_source_text(tmp_path, "par")
    serial = _runner(tmp_path, text_serial, _two_source_client()).run()
    parallel = _parallel_runner(_runner(tmp_path, text_parallel, _two_source_client()), 4).run()

    assert serial.exit_reason == parallel.exit_reason
    assert serial.assembly.codes() == parallel.assembly.codes()
    assert {k: (v.ok, v.failed) for k, v in serial.verdicts.items()} == {
        k: (v.ok, v.failed) for k, v in parallel.verdicts.items()
    }
    # 证据逐块逐键相同（缺输入、拒收理由、artifact_on_disk…）
    for block_id, verdict in serial.verdicts.items():
        assert dict(verdict.evidence) == dict(parallel.verdicts[block_id].evidence), block_id
    assert set(serial.candidates) == set(parallel.candidates)
    for block in serial.plan.subtasks:
        first = serial.candidates.get(block.id)
        second = parallel.candidates.get(block.id)
        assert (first is None) == (second is None), block.id
        if first is not None and second is not None:
            assert first.artifact(block.output) == second.artifact(block.output), block.id
    assert set(serial.plan.subtask_ids) == set(parallel.plan.subtask_ids)


def test_parallel_does_not_change_the_capability_declaration(tmp_path: Path) -> None:
    """``--parallel`` 只调执行；``--concurrency`` 的能力判定照旧。"""
    from bobanana6.cli import _parser

    args = _parser().parse_args(["run", "x.yaml", "--parallel", "4"])
    assert args.parallel == 4
    assert args.concurrency == 8  # 未被 --parallel 触碰

    from acceptance.harness import run_task

    task = Path("acceptance/main/panel-three-sources")
    short_serial = run_task(task, offline=True, concurrency=1, parallel=1)
    short_parallel = run_task(task, offline=True, concurrency=1, parallel=4)
    enough = run_task(task, offline=True, concurrency=8, parallel=4)

    # 申报不够 ⇒ 无论串行还是并发都照旧 capability_short（并行不会掩盖能力缺口）
    assert "capability_short" in short_serial.assembly_codes
    assert "capability_short" in short_parallel.assembly_codes
    assert "capability_short" not in enough.assembly_codes


def test_parallel_with_a_workspace_is_refused_by_name(tmp_path: Path) -> None:
    """★ 并发 + 工作区会改变"谁的改动算谁的" ⇒ 具名拒，不静默让判定分叉。"""
    text = _two_source_text(tmp_path, "ws")
    runner = _parallel_runner(_runner(tmp_path, text, _two_source_client()), 4)
    runner = replace(runner, workspace=tmp_path / "ws")
    with pytest.raises(RunRefused) as refusal:
        runner.run()
    assert refusal.value.gate == "parallel"
    assert refusal.value.codes == ("workspace_concurrency_unsupported",)
