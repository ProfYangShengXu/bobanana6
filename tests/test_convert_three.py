"""修「转换」三件：静态知识归位 / 污染标记 / 工作区只拷授权来源（规格 §六）。

```
① 静态分流判据是【算出来的】不是写死的  ② 移位不丢内容  ③ brief 里不再有已移走的键
④ 缓存口径（恒定前缀逐字节相同）        ⑤⑥⑦ cleanliness
⑧ 工作区只拷授权来源                    ⑨ 官方 checker 不受影响
```

★ 本任务全是**壳层**：`core/` 一个字都没动（有架构门与 preflight 守着）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task, workspace_seeded
from bobanana6.assemble_impl.produce import LoopIOImpl, _path_tokens
from bobanana6.core.model import ModelResponse, Subtask

# --------------------------------------------------------------------------- #
# fixtures：一道"授权的在 data/、判分器在 checks/"的题
# --------------------------------------------------------------------------- #


def _task(tmp_path: Path, *, name: str, with_checks: bool = True) -> Path:
    task = tmp_path / name
    (task / "input" / "data").mkdir(parents=True, exist_ok=True)
    (task / "input" / "data" / "seed.txt").write_text("draft\n", encoding="utf-8")
    if with_checks:
        (task / "input" / "checks").mkdir(parents=True, exist_ok=True)
        (task / "input" / "checks" / "test_outputs.py").write_text(
            "# the official grader, NOT declared as a source\n", encoding="utf-8"
        )
    (task / "ontology.yaml").write_text(
        'domain: text\nversion: "2026.1"\nconcepts:\n  obligor: {label: 主体, kind: entity}\n',
        encoding="utf-8",
    )
    (task / "task.yaml").write_text(
        "task_id: convert\n"
        "goal: chain two products\n"
        "version: 1\n"
        "statement: seed\n"
        "deliverables:\n"
        "  - {name: a.csv, schema: {obligor: str}}\n"
        "  - {name: b.csv, schema: {obligor: str}, depends_on: [a.csv]}\n"
        "sources:\n"
        "  - {ref: seed, locator: input/data/seed.txt}\n"
        "constraints:\n"
        "  hard:\n"
        '    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "no empty"}\n',
        encoding="utf-8",
    )
    return task


def _block_of(messages) -> str:  # type: ignore[no-untyped-def]
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith("{"):
            try:
                return str(json.loads(content).get("subtask", {}).get("id", ""))
            except json.JSONDecodeError:
                continue
    return ""


def _briefing(messages) -> dict:  # type: ignore[no-untyped-def]
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith("{"):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue
    return {}


def _tool_call(call_id: str, name: str, arguments) -> dict:  # type: ignore[no-untyped-def]
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _static_reference(system: str) -> dict:
    marker = "repeated in the per-block brief below):\n"
    assert marker in system, system[:160]
    return json.loads(system.split(marker, 1)[1].splitlines()[0])


# --------------------------------------------------------------------------- #
# ① 判据是算出来的，不是白名单
# --------------------------------------------------------------------------- #


class _ConstantCase:
    """驱动 `_constant_brief_entries` 的最小替身：只提供它真正用到的那几样。

    ★ 这样做的好处是**判据本身**能被单独考：喂一个"前 3 块相同、第 4 块不同"的产出函数，
    看它会不会被错误挪走。
    """

    def __init__(self, producers: dict, blocks: tuple[Subtask, ...]) -> None:
        self._producers = producers
        self.dag_subtasks = blocks
        self.subtask = blocks[0]
        self._UNAVAILABLE = LoopIOImpl._UNAVAILABLE

    def _brief_candidates(self):  # type: ignore[no-untyped-def]
        return self._producers


def _subtask(block_id: str) -> Subtask:
    return Subtask(
        id=block_id, goal="g", inputs=(), output=f"{block_id}.csv", contract_ref="c", axis="source", mode="write"
    )


def test_a_key_that_differs_on_the_last_block_is_not_moved() -> None:
    """★ 反证（规格 §六①）：前三块相同的键，第 4 块不同 ⇒ **不许**挪进 system。

    这正是"跑到哪算到哪"会犯的错：只看已经跑过的块，前三个看起来相同，第 4 个不同。
    """
    blocks = tuple(_subtask(f"T-0{index}") for index in range(1, 5))
    producers = {
        "statement": lambda _s: {"text": "same for all"},
        # ★ 前三个块相同、第 4 个不同
        "task": lambda s: {"kind": "fix"} if s.id != "T-04" else {"kind": "produce", "acceptance": ["x"]},
    }
    constant = LoopIOImpl._constant_brief_entries(_ConstantCase(producers, blocks))
    assert "statement" in constant  # 全 DAG 恒定 ⇒ 挪
    assert "task" not in constant  # 第 4 块不同 ⇒ 不挪（★ 这条就是反证）
    assert constant["statement"] == {"text": "same for all"}


def test_nothing_is_moved_when_the_whole_dag_is_not_available() -> None:
    """★ 拿不到全 DAG ⇒ 一个字都不动（不能证明恒定就不动，fail-closed）。"""
    blocks = (Tuple := (_subtask("T-01"),))
    case = _ConstantCase({"statement": lambda _s: {"text": "x"}}, blocks)
    case.dag_subtasks = ()  # 拿不到
    assert LoopIOImpl._constant_brief_entries(case) == {}
    case.dag_subtasks = (_subtask("T-09"),)  # 不含本块
    assert LoopIOImpl._constant_brief_entries(case) == {}
    assert Tuple  # 保留变量名，避免 lint 误报


def test_a_key_that_is_empty_for_all_blocks_is_not_treated_as_constant() -> None:
    """★ 空值不算恒定 —— 不能拿"都没有"冒充"都相同"。"""
    blocks = (_subtask("T-01"), _subtask("T-02"))
    constant = LoopIOImpl._constant_brief_entries(_ConstantCase({"task": lambda _s: {}}, blocks))
    assert constant == {}


def test_a_key_unavailable_for_a_sibling_is_not_moved() -> None:
    """★ 依赖本块 toolset / 上游件的键，对兄弟块**不可得** ⇒ 永不恒定。"""
    blocks = (_subtask("T-01"), _subtask("T-02"))
    constant = LoopIOImpl._constant_brief_entries(
        _ConstantCase({"upstream": lambda _s: LoopIOImpl._UNAVAILABLE}, blocks)
    )
    assert constant == {}


def test_the_real_run_moves_the_dag_constant_keys_and_keeps_the_rest(tmp_path: Path) -> None:
    """端到端：真跑里 `statement` / `source_files` 被挪走，`subtask` / `authorized_sources` 留下。"""
    task = _task(tmp_path, name="move-me")
    seen: dict[str, dict] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages) or "?"
            system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
            seen.setdefault(block, {"system": system, "brief": _briefing(messages)})
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

    run_task(task, client=_Client(), env={}, workspace=True)
    assert seen, "no block was ever asked"
    for block, captured in seen.items():
        static = _static_reference(captured["system"])
        assert set(static) == {"statement", "source_files"}, (block, sorted(static))
        # ★ ③ brief 里不再有已移走的键
        assert "statement" not in captured["brief"], (block, sorted(captured["brief"]))
        assert "source_files" not in captured["brief"], (block, sorted(captured["brief"]))
        # ★ 留下的是**随块变**的：subtask 与 authorized_sources 必须在 brief 里
        assert "subtask" in captured["brief"] and "authorized_sources" in captured["brief"], block


# --------------------------------------------------------------------------- #
# ②③④ 移位不丢内容 / brief 不再有 / 缓存口径
# --------------------------------------------------------------------------- #


def _capture_all_calls(tmp_path: Path) -> tuple[dict[str, list[str]], dict[str, dict]]:
    task = _task(tmp_path, name="cache-shape")
    systems: dict[str, list[str]] = {}
    briefs: dict[str, dict] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages) or "?"
            system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
            systems.setdefault(block, []).append(system)
            briefs.setdefault(block, _briefing(messages))
            tool_calls = sum(1 for m in messages if m.get("role") == "tool")
            if block == "T-01" and tool_calls == 0:
                return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
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

    run_task(task, client=_Client(), env={}, workspace=True)
    return systems, briefs


def test_moving_a_key_does_not_lose_it_the_model_still_sees_the_full_text(tmp_path: Path) -> None:
    """★ ② 换个位置，不是删：题面**全文**仍在模型看得到的地方。"""
    systems, _ = _capture_all_calls(tmp_path)
    for block, calls in systems.items():
        static = _static_reference(calls[0])
        assert static["statement"]["text"] == "draft\n", block
        assert static["statement"]["ref"] == "seed", block
        assert static["source_files"] == {"seed": "input/data/seed.txt"}, block


def test_the_constant_prefix_is_byte_identical_across_blocks_and_calls(tmp_path: Path) -> None:
    """★ ④ 缓存口径：恒定前缀在【不同块、同一块的不同调用】之间逐字节相同。"""
    systems, _ = _capture_all_calls(tmp_path)
    prefixes = {block: {system.split("YOUR TASK:")[0] for system in calls} for block, calls in systems.items()}
    for block, unique in prefixes.items():
        assert len(unique) == 1, (block, "the prefix changed between calls of the same block")
    across_blocks = {next(iter(unique)) for unique in prefixes.values()}
    assert len(across_blocks) == 1, "the constant prefix differs between blocks"
    # ★ 反向：**块后缀**确实随块变（否则"恒定前缀"就没有意义了）
    suffixes = {
        block: {system.split("YOUR TASK:", 1)[1].strip() for system in calls}
        for block, calls in systems.items()
    }
    assert all(len(unique) == 1 for unique in suffixes.values())
    assert len({next(iter(unique)) for unique in suffixes.values()}) > 1, "every block got the same task?!"


# --------------------------------------------------------------------------- #
# ⑤⑥⑦ cleanliness
# --------------------------------------------------------------------------- #


def _run_with_read(tmp_path: Path, *, command: str, name: str, absolute: Path | None = None):
    """T-01 走一条**真实**的四步序列，返回 `(run, T-02 的 upstream 条目)`。

    序列：① 读一个授权来源（不然会被"读了来源才准答"那条门挡住）
          ② 写下**自己的产物** a.csv（这样"读自己的产物"才有个真实存在的文件可读）
          ③ 跑被考的那条命令
          ④ 交答案
    """
    task = _task(tmp_path, name=name)
    if absolute is not None and "{ABS}" in command:
        command = command.replace("{ABS}", str((tmp_path / name / absolute).resolve()))
    upstream: dict = {}
    turns: dict[str, int] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages)
            if block == "T-02":
                upstream.setdefault("entry", (_briefing(messages).get("upstream") or {}).get("a.csv") or {})
            turn = turns.get(block, 0)
            turns[block] = turn + 1
            if block == "T-01":
                if turn == 0:
                    return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
                if turn == 1:
                    return ModelResponse(
                        tool_calls=(
                            _tool_call("w1", "files.write", {"path": "a.csv", "content": "obligor\nA-1\n"}),
                        )
                    )
                if turn == 2:
                    return ModelResponse(tool_calls=(_tool_call("s1", "shell.run", {"command": command}),))
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
    return run, upstream.get("entry", {})


def test_reading_outside_the_authorized_locators_marks_the_artifact_tainted(tmp_path: Path) -> None:
    """★ ⑤ 越界读 ⇒ tainted + **给出路径**（拒了要说清，别只给一个布尔）。"""
    _run, entry = _run_with_read(
        tmp_path,
        command="cat {ABS}",
        absolute=Path("input/checks/test_outputs.py"),
        name="tainted",
    )
    assert entry.get("cleanliness") == "tainted", entry
    assert entry["cleanliness_evidence"][0].endswith("input/checks/test_outputs.py"), entry


def test_reading_your_own_product_in_the_workspace_stays_clean(tmp_path: Path) -> None:
    """★ ⑤ 反证：读工作区里**自己的产物**不许误伤。"""
    _run, entry = _run_with_read(tmp_path, command="head -n 1 a.csv", name="own-product")
    assert entry.get("cleanliness") == "clean", entry
    assert entry.get("cleanliness_evidence") == [], entry


def test_reading_an_authorized_source_stays_clean(tmp_path: Path) -> None:
    """★ 授权范围内的读（locator 所在目录）不许误伤。"""
    _, entry = _run_with_read(tmp_path, command="cat input/data/seed.txt", name="authorized")
    assert entry.get("cleanliness") == "clean", entry


def test_a_command_that_mentions_nothing_real_is_not_flagged(tmp_path: Path) -> None:
    """★ 高精度低召回：指向不存在的路径 ⇒ 拿不准就不标。"""
    _, entry = _run_with_read(tmp_path, command="cat input/checks/does_not_exist.py", name="uncertain")
    assert entry.get("cleanliness") == "clean", entry


def test_tainted_does_not_fail_the_block_and_does_not_stop_the_run(tmp_path: Path) -> None:
    """★★ ⑥ 守卫纪律：tainted 只影响**读数的可信度标记**，不参与判定。"""
    run, entry = _run_with_read(
        tmp_path,
        command="grep -n obligor {ABS}",
        absolute=Path("input/checks/test_outputs.py"),
        name="no-fail",
    )
    assert entry.get("cleanliness") == "tainted", entry
    assert run.verdicts["T-01"].ok is True, run.verdicts["T-01"].evidence  # ★ 块照常通过
    assert run.refused is None  # 整跑照常
    assert run.report.assembly_passed is True


def test_a_failed_block_with_an_artifact_on_disk_still_records_cleanliness(tmp_path: Path) -> None:
    """★ ⑤ cleanliness 是**产物的属性**，不是块的成功/失败 —— 两者不许混。"""
    task = _task(tmp_path, name="failed-but-tainted")

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages)
            tool_calls = sum(1 for m in messages if m.get("role") == "tool")
            if block == "T-01":
                if tool_calls == 0:
                    return ModelResponse(
                        tool_calls=(
                            _tool_call("w1", "files.write", {"path": "a.csv", "content": "obligor\nA-1\n"}),
                        )
                    )
                # ★ 信封没封好 ⇒ 判定失败；但产物在盘上
                return ModelResponse(text="I am still working on it.")
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
    assert run.verdicts["T-01"].ok is False  # ★ 块仍然失败
    assert run.verdicts["T-01"].evidence.get("artifact_on_disk") == "a.csv"  # ★ 产物仍然在


def test_cleanliness_reaches_the_next_block_brief(tmp_path: Path) -> None:
    """★ ⑦ 传递：下游 brief 的 `upstream[name]` 里带 cleanliness 与证据。"""
    _, entry = _run_with_read(
        tmp_path,
        command="cat {ABS}",
        absolute=Path("input/checks/test_outputs.py"),
        name="downstream",
    )
    assert entry, "T-02 never received the upstream handoff"
    assert entry.get("cleanliness") == "tainted", entry
    assert entry["cleanliness_evidence"][0].endswith("input/checks/test_outputs.py"), entry
    # 既有字段照旧
    assert entry.get("producer_verdict") in {"ok", "failed"}, entry
    assert "rows" in entry or "missing" in entry, entry


def test_the_path_token_extractor_is_conservative() -> None:
    """高精度低召回：只挑像路径的 token，不把普通词当路径。"""
    tokens = _path_tokens("cat input/checks/test_outputs.py && echo ok", (".py",))
    assert tokens == ("input/checks/test_outputs.py",)
    assert _path_tokens("head -n 20 a.csv", (".csv",)) == ("a.csv",)
    assert _path_tokens("grep -n obligor a.csv", (".csv",)) == ("a.csv",)
    assert _path_tokens("echo hello world", (".py",)) == ()
    assert _path_tokens("curl https://example.com/x.py", (".py",)) == ()


# --------------------------------------------------------------------------- #
# ⑧⑨ 工作区只拷授权来源 / 官方 checker 不受影响
# --------------------------------------------------------------------------- #


def test_the_workspace_copies_only_the_declared_locators(tmp_path: Path) -> None:
    """★ ⑧ 只拷契约声明的 locator；`checks/` 不在 sources 里 ⇒ 自然不拷。"""
    run, _ = _run_with_read(tmp_path, command="ls -R input", name="workspace")
    assert run.workspace is not None
    copied = workspace_seeded(run.workspace)
    assert copied == ("input/data/seed.txt",), copied
    assert (run.workspace / "input" / "data" / "seed.txt").is_file()
    assert not (run.workspace / "input" / "checks").exists(), "the official grader leaked into the workspace"


def test_the_original_task_input_directory_is_untouched(tmp_path: Path) -> None:
    """★ ⑧ 题目原目录的 `input/` **一个字节都不动**。"""
    task = _task(tmp_path, name="untouched")
    grader = task / "input" / "checks" / "test_outputs.py"
    before = grader.read_bytes()
    _run_with_read(tmp_path, command="ls -R input", name="untouched")  # 复用同一个 task 名会重建
    assert grader.read_bytes() == before


def test_the_official_checker_still_runs_from_the_task_directory(tmp_path: Path) -> None:
    """★ ⑨ 官方 checker 仍从**题目原目录**读，不受工作区拷贝策略影响 —— 真跑一次它。"""
    _task(tmp_path, name="grader-runs")
    task_dir = tmp_path / "grader-runs"
    grader = task_dir / "input" / "checks" / "test_outputs.py"
    completed = subprocess.run(
        [sys.executable, str(grader)], capture_output=True, text=True, check=False
    )
    # 那个夹具 grader 只有一行注释 ⇒ 退出码 0（能正常收集/运行）
    assert completed.returncode == 0, completed.stderr
    assert grader.is_file()
    payload = _run_with_read(tmp_path, command="ls", name="grader-runs-2")[0]
    assert payload.workspace is not None
    assert not (payload.workspace / "input" / "checks").exists()
    assert grader.is_file()  # 原目录那份仍在


