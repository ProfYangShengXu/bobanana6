"""块之间的【过程级信息交换】（ARTEX 机制 ②）：存过程 + 下游按需检索。

本文件考四件事（照任务书 §五）：

```
① 存储      一次跑测后，产品路径里真有一份过程记录（条数 / 大小 / 落在哪）
② 检索      按关键字命中上游某一步；按 block_id+seq 取回完整内容
③ 可见范围  下游 ⇒ 拒；非上游的同级块 ⇒ 拒；上游 ⇒ 放行
④ 硬边界    上游某步读了工作区外的检查文件 ⇒ 命令可见，**结果内容不在索引里**
⑤ 限额      超限 ⇒ 具名拒（且被拒的坏调用不吃额度）
```
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl.produce import LoopIOImpl
from bobanana6.core.model import ModelResponse, Subtask
from bobanana6.services.tools import BoundTraces
from bobanana6.services.traces import (
    REDACTED_RESULT,
    TRACE_MAX_ENTRIES_PER_BLOCK,
    TRACE_SEARCH_LIMIT,
    TraceError,
    TraceStore,
    upstream_closure,
)
from tests.test_convert_three import _block_of, _task, _tool_call


def _block(block_id: str, output: str, inputs: tuple[str, ...] = ()) -> Subtask:
    return replace(
        Subtask(id="x", goal="g", inputs=(), output="o", contract_ref="c", axis="source", mode="write"),
        id=block_id,
        output=output,
        inputs=inputs,
    )


def _store_with_chain() -> TraceStore:
    """T-01 → T-02 → 另一条支线上的 T-09（不可见）。"""
    store = TraceStore()
    store.add(block_id="T-01", turn=1, tool="code.run", args_text='code.run {"code": "print(boom)"}',
              result_text="NameError: name 'boom' is not defined", at=1.0, ok=False,
              error_type="WorkspaceError", result_indexed=True)
    store.add(block_id="T-01", turn=2, tool="files.write", args_text='files.write {"path": "a.csv"}',
              result_text='{"path": "a.csv", "bytes": 12}', at=2.0, ok=True, result_indexed=True)
    store.add(block_id="T-02", turn=1, tool="code.run", args_text='code.run {"code": "print(42)"}',
              result_text="42", at=3.0, ok=True, result_indexed=True)
    store.add(block_id="T-09", turn=1, tool="code.run", args_text='code.run {"code": "print(secret)"}',
              result_text="SECRET_OF_A_SIBLING", at=4.0, ok=True, result_indexed=True)
    return store


# --------------------------------------------------------------------------- #
# 上游闭包：按 DAG 依赖现算，不硬编码块名
# --------------------------------------------------------------------------- #


def test_the_upstream_closure_follows_the_declared_dependencies() -> None:
    t1 = _block("T-01", "a.csv")
    t2 = _block("T-02", "b.csv", ("a.csv",))
    t3 = _block("T-03", "c.csv", ("b.csv",))
    t4 = _block("T-04", "d.csv", ("a.csv",))  # 与 T-02 同级
    dag = (t1, t2, t3, t4)
    assert upstream_closure(t3, dag) == ("T-01", "T-02")  # 传递闭包
    assert upstream_closure(t2, dag) == ("T-01",)
    assert upstream_closure(t1, dag) == ()  # 入口块没有上游
    # ★ 同级块之间互不可见：T-04 的闭包只有 T-01，没有 T-02/T-03
    assert upstream_closure(t4, dag) == ("T-01",)


# --------------------------------------------------------------------------- #
# ① 存储：写得到、读得回、上限具名
# --------------------------------------------------------------------------- #


def test_the_store_keeps_entries_and_reports_size_distribution() -> None:
    store = _store_with_chain()
    summary = store.summary()
    assert summary["blocks"] == ["T-01", "T-02", "T-09"]
    assert summary["entries"] == 4
    assert summary["entries_by_block"]["T-01"] == 2
    assert summary["chars_total"] >= summary["chars_max"] >= summary["chars_min"] > 0
    assert summary["dropped"] == 0


def test_the_store_drops_by_budget_by_name_never_silently() -> None:
    store = TraceStore()
    for index in range(TRACE_MAX_ENTRIES_PER_BLOCK + 3):
        store.add(block_id="T-01", turn=1, tool="code.run", args_text=f"n={index}", result_text="ok",
                  at=float(index), ok=True, result_indexed=True)
    assert len(store.entries("T-01")) == TRACE_MAX_ENTRIES_PER_BLOCK
    assert store.dropped_for("T-01") == 3
    assert store.summary()["dropped"] == 3


def test_a_lone_surrogate_never_breaks_the_record() -> None:
    """★ 只读审计 #60：代理字符不许把业务跑挂（trace 没有这种权力）。"""
    store = TraceStore()
    entry = store.add(block_id="T-01", turn=1, tool="code.run", args_text="code.run {'code': '\\ud800'}",
                      result_text="print('\ud800')", at=0.0, ok=True, result_indexed=True)
    assert entry is not None
    entry.result_text.encode("utf-8")  # 不抛


# --------------------------------------------------------------------------- #
# ② 检索：先搜后读
# --------------------------------------------------------------------------- #


def test_search_and_read_round_trip() -> None:
    store = _store_with_chain()
    bt = BoundTraces(store=store, visible=("T-01", "T-02"), block="T-03")
    found = bt.search("NameError")
    assert found["total"] == 1 and found["truncated"] is False
    assert len(found["hits"]) == 1
    assert found["hits"][0]["block_id"] == "T-01" and found["hits"][0]["seq"] == 1
    assert found["hits"][0]["tool"] == "code.run"
    assert "NameError" in found["hits"][0]["snippet"]
    assert found["searched_blocks"] == ["T-01", "T-02"]

    steps = bt.read("T-01", "1,2")
    assert steps["count"] == 2
    assert steps["steps"][0]["result"].startswith("NameError")
    assert steps["steps"][0]["turn"] == 1  # ★ 第几轮
    assert steps["steps"][1]["tool"] == "files.write"


def test_search_says_when_it_truncated() -> None:
    """★ 只读审计 #56：'真就这么多'与'被砍了'必须分得清。"""
    store = TraceStore()
    for index in range(30):
        store.add(block_id="T-01", turn=1, tool="code.run", args_text=f"common-token {index}",
                  result_text="", at=float(index), ok=True, result_indexed=True)
    found = store.search("common-token", ["T-01"])
    assert len(found["hits"]) == 20 and found["total"] == 30 and found["truncated"] is True


def test_an_empty_query_is_refused_and_does_not_spend_the_allowance() -> None:
    """★ 只读审计 #57：坏调用不该吃掉额度（拒完还能照常用）。"""
    store = _store_with_chain()
    bt = BoundTraces(store=store, visible=("T-01",), block="T-02", limit=2)
    for _ in range(3):
        with pytest.raises(TraceError):
            bt.search("   ")
    assert bt.used == 0
    assert bt.search("NameError")["total"] == 1
    assert bt.search("files.write")["total"] == 1


# --------------------------------------------------------------------------- #
# ③ 可见范围：上游放行 / 下游与同级具名拒
# --------------------------------------------------------------------------- #


def test_reading_a_non_upstream_block_is_refused_by_name() -> None:
    """下游块与同级块都不是"我的上游" —— 判据是 DAG 依赖，不是"跑过没有"。"""
    store = _store_with_chain()
    bt = BoundTraces(store=store, visible=("T-01", "T-02"), block="T-03")
    with pytest.raises(TraceError) as caught:
        bt.read("T-09", "1")  # 同级/下游：不在闭包里
    message = str(caught.value)
    assert "not in this block's upstream closure" in message
    assert "T-01" in message and "T-09" in message  # 报文说清"你能看谁"
    assert bt.used == 0  # 越权被拒**不吃**额度


def test_search_cannot_see_a_non_upstream_blocks_steps() -> None:
    store = _store_with_chain()
    bt = BoundTraces(store=store, visible=("T-01",), block="T-02")
    assert bt.search("SECRET_OF_A_SIBLING")["total"] == 0  # 同级块的内容搜不到
    assert bt.search("NameError")["total"] == 1  # 上游的搜得到


def test_an_unknown_step_is_refused_with_what_is_recorded() -> None:
    store = _store_with_chain()
    bt = BoundTraces(store=store, visible=("T-01",), block="T-02")
    with pytest.raises(TraceError) as caught:
        bt.read("T-01", "99")
    assert "no recorded step" in str(caught.value) and "[1, 2]" in str(caught.value)


# --------------------------------------------------------------------------- #
# ⑤ 限额：具名拒
# --------------------------------------------------------------------------- #


def test_the_per_block_trace_call_limit_is_named() -> None:
    store = _store_with_chain()
    bt = BoundTraces(store=store, visible=("T-01", "T-02"), block="T-03", limit=2)
    assert bt.search("NameError")["total"] == 1
    assert bt.read("T-01", "1")["count"] == 1
    with pytest.raises(TraceError) as caught:
        bt.search("files.write")
    message = str(caught.value)
    assert "per-block trace-call limit" in message and "2/2" in message
    assert bt.used == 2


def test_the_default_limit_is_a_number_not_a_vibe() -> None:
    assert TRACE_SEARCH_LIMIT == 12
    assert BoundTraces(store=TraceStore(), visible=("T-01",)).limit == TRACE_SEARCH_LIMIT


# --------------------------------------------------------------------------- #
# ④ 硬边界：工作区外的检查文件 —— 命令可见，结果不进索引
# --------------------------------------------------------------------------- #


class _OutsideCase:
    """驱动 `_trace_reads_outside` 的最小替身（只读 workspace 与两个类常量）。"""

    _EXECUTION_ARGUMENTS = LoopIOImpl._EXECUTION_ARGUMENTS
    _PATH_SUFFIXES = LoopIOImpl._PATH_SUFFIXES

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace


def test_reading_outside_the_workspace_is_detected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "checks"
    outside.mkdir()
    (outside / "test_outputs.py").write_text("SECRET\n", encoding="utf-8")
    case = _OutsideCase(workspace)

    # ① 绝对路径（判分器）
    assert LoopIOImpl._trace_reads_outside(
        case, "shell.run", {"command": f"cat {outside/'test_outputs.py'}"}
    )
    # ①' 绝对路径**通配**形式：token 本身不是文件 —— 只读审计 #58 的那个绕法
    assert LoopIOImpl._trace_reads_outside(case, "shell.run", {"command": f"cat {outside}/*.py"})
    # ② 向上逃逸
    assert LoopIOImpl._trace_reads_outside(case, "shell.run", {"command": "cat ../checks/x.py"})
    # ③ 相对路径 + 通配（解析不出它最终指哪）
    assert LoopIOImpl._trace_reads_outside(case, "code.run", {"code": "print(open('sub/*.py').read())"})
    # 反证：工作区内的相对路径**不许**误伤
    assert not LoopIOImpl._trace_reads_outside(case, "shell.run", {"command": "cat input/data/seed.txt"})
    assert not LoopIOImpl._trace_reads_outside(case, "code.run", {"code": "print(open('input/data/seed.txt').read())"})
    # ★ `sources.*` 读的是**声明的来源**（已在工作区里），不遮
    assert not LoopIOImpl._trace_reads_outside(case, "sources.read", {"ref": "seed"})


def test_a_redacted_result_is_unreachable_from_every_read_path() -> None:
    """★ 内容**根本没存**（不是存了再遮）⇒ 从 search 与 read 两条路都取不回来。"""
    store = TraceStore()
    store.add(block_id="T-01", turn=1, tool="shell.run",
              args_text='shell.run {"command": "cat /outside/checks/test_outputs.py"}',
              result_text="SECRET_GRADER_CONTENT", at=0.0, ok=True, result_indexed=False)
    searched = store.search("SECRET_GRADER_CONTENT", ["T-01"])
    assert searched["total"] == 0  # 搜内容：搜不到
    by_command = store.search("test_outputs.py", ["T-01"])
    assert by_command["total"] == 1  # 搜命令：搜得到（"它跑了什么"是可见的）
    step = store.read("T-01", [1])[0]
    assert step["result"] == REDACTED_RESULT
    assert step["result_indexed"] is False
    assert "SECRET_GRADER_CONTENT" not in json.dumps(step, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 端到端：真的经过 runner → runtime → registry（接线上就必须对）
# --------------------------------------------------------------------------- #


def _e2e(tmp_path: Path):
    """T-01（上游）跑两条命令，T-02（下游）检索它。

    ★ T-01 的第 1 步读**工作区外**的 `input/checks/test_outputs.py`（题目原目录），
      第 2 步在工作区内算一个可搜的记号。
    """
    task = _task(tmp_path, name="traces-e2e")
    outside = (task / "input" / "checks" / "test_outputs.py").resolve()
    menus: dict[str, set[str]] = {}
    seen: dict[str, list] = {"search": [], "read": []}
    turns: dict[str, int] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages) or "?"
            menus[block] = {(tool.get("function") or {}).get("name") for tool in tools}
            # 收下游那两次 traces.* 的返回（tool 行在 transcript 里）
            for message in messages:
                if message.get("role") != "tool":
                    continue
                try:
                    payload = json.loads(str(message.get("content")))
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(payload, dict) and "hits" in payload:
                    seen["search"].append(payload)
                if isinstance(payload, dict) and "steps" in payload:
                    seen["read"].append(payload)
            turn = turns.get(block, 0)
            turns[block] = turn + 1
            if block == "T-01":
                if turn == 0:
                    return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
                if turn == 1:
                    return ModelResponse(
                        tool_calls=(_tool_call("c1", "shell.run", {"command": f"cat {outside}"}),)
                    )
                if turn == 2:
                    return ModelResponse(
                        tool_calls=(_tool_call("c2", "code.run", {"code": "print('NEEDLE_FROM_T01')"}),)
                    )
            if block == "T-02":
                if turn == 0:
                    return ModelResponse(tool_calls=(_tool_call("r2", "sources.snapshot", {"ref": "seed"}),))
                if turn == 1:
                    return ModelResponse(
                        tool_calls=(_tool_call("s1", "traces.search", {"query": "NEEDLE_FROM_T01"}),)
                    )
                if turn == 2:
                    return ModelResponse(
                        # raise 两步：第 2 步是"读判分器"（结果应被遮），第 3 步是可搜的记号
                        tool_calls=(_tool_call("p1", "traces.read", {"block_id": "T-01", "seq": "2,3"}),)
                    )
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}],
                        "rows": [{"obligor": "A-1"}],
                        "done": True,
                    }
                )
            )

    run = run_task(task, client=_Client(), env={}, workspace=True)
    return run, task, menus, seen


def test_the_tools_are_advertised_to_the_downstream_block_only(tmp_path: Path) -> None:
    """★ 只读审计 #19：要断言"目标块**实际拿到的**工具集里含它"。"""
    _run, _task_dir, menus, _seen = _e2e(tmp_path)
    assert "traces.search" in menus.get("T-02", set()), menus
    assert "traces.read" in menus.get("T-02", set()), menus
    # 入口块没有上游 ⇒ traces.* 根本不广告（拿不到的能力不该被广告出来）
    assert not (menus.get("T-01", set()) & {"traces.search", "traces.read"}), menus


def test_the_downstream_block_can_search_and_read_upstream_process(tmp_path: Path) -> None:
    _run, _task_dir, _menus, seen = _e2e(tmp_path)
    assert seen["search"], "T-02 never got a search result"
    hits = [hit for payload in seen["search"] for hit in payload["hits"]]
    assert any(hit["block_id"] == "T-01" and hit["seq"] == 3 for hit in hits), hits
    assert seen["read"], "T-02 never got a read result"
    steps = [step for payload in seen["read"] for step in payload["steps"]]
    assert any("NEEDLE_FROM_T01" in step["result"] for step in steps), steps
    assert all(step["turn"] >= 1 for step in steps)


def test_the_grader_content_read_upstream_is_not_indexed(tmp_path: Path) -> None:
    """★ 硬边界端到端：上游 `cat <题目原目录>/input/checks/test_outputs.py` 的那一步，
    命令在索引里、**结果内容不在** —— 从 search 与 read 两条路都验。"""
    _run, _task_dir, _menus, seen = _e2e(tmp_path)
    hits = [hit for payload in seen["search"] for hit in payload["hits"]]
    # 下游搜的是记号：命中的是 T-01 第 3 步；第 2 步（读判分器）不在命中里
    assert all(hit["seq"] != 2 for hit in hits), hits
    # 而同一次 read 把两步都取了回来：第 2 步的结果**只能是那句"未索引"**
    steps = {step["seq"]: step for payload in seen["read"] for step in payload["steps"]}
    assert steps[2]["result_indexed"] is False
    assert steps[2]["result"] == REDACTED_RESULT
    assert "official grader" not in json.dumps(steps[2], ensure_ascii=False)
    assert "test_outputs.py" in steps[2]["args"]  # ★ 命令可见（"它跑过什么"是过程的一部分）
    assert steps[3]["result_indexed"] is True and "NEEDLE_FROM_T01" in steps[3]["result"]


def test_the_process_record_lands_in_the_product_path(tmp_path: Path) -> None:
    """① 存储读数：条数 + 单条大小分布 + 总大小（产品路径，不是跑测脚本侧）。"""
    _run, task, _menus, _seen = _e2e(tmp_path)
    traces_file = task / "_run" / "artifacts" / "tree" / "T-01" / "traces.jsonl"
    assert traces_file.is_file()
    records = [json.loads(line) for line in traces_file.read_text(encoding="utf-8").splitlines()]
    sizes = [len(record["args"]) + len(record["result"]) for record in records]
    assert len(records) >= 2
    assert min(sizes) > 0 and max(sizes) >= min(sizes)
    assert sum(sizes) > 0
    # `commit.json` 里带指针（复盘时不必猜文件在哪）
    manifest = json.loads(
        (task / "_run" / "artifacts" / "tree" / "T-01" / "commit.json").read_text(encoding="utf-8")
    )
    assert manifest["traces"]["entries"] == len(records)
    assert manifest["traces"]["stored"] is True
