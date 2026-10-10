"""The single door for tool returns: over the threshold -> head + tail + pointer.

★ What is being pinned here is **the gate, not the tools**: every ``tool`` row is
built in one place (``loop._tool_round``), and the rule is applied there for every
tool alike. The tests below assert the four behaviours the rule promises:

```
① over the threshold    -> paged; both ends kept; the full text is on disk and
                           readable; how much was dropped is stated
② under the threshold   -> byte-for-byte unchanged, nothing spilled, no event
③ exactly at threshold  -> unchanged (the comparison is strict, no ambiguity)
④ spill failed          -> NOT paged: the full text is handed over
```

Plus the boundary of the change itself: the spilled file is **not a deliverable**
(it lands in the already-ignored ``_shell/`` area).

The replay test at the bottom uses the real archived return from
``qwen4b6-A-c1.jsonl`` (294,394 chars) -- the record the offline estimate was
measured on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bobanana6.agentloop.gate import GateVerdict
from bobanana6.agentloop.loop import LoopConfig, TurnProposal, WorkerLoop
from bobanana6.agentloop.tool_output import (
    DEFAULT_HEAD_CHARS,
    DEFAULT_MAX_CHARS,
    DEFAULT_TAIL_CHARS,
    ENV_HEAD_CHARS,
    ENV_MAX_CHARS,
    ENV_TAIL_CHARS,
    ToolOutputPolicy,
    page_tool_output,
)
from bobanana6.agentloop.tools import ToolCall, ToolResult
from bobanana6.core.model import Candidate
from bobanana6.services.workspace import (
    IGNORED_PARTS,
    SHELL_OUTPUT_DIR,
    BoundWorkspace,
    WorkspaceLedger,
    _ignored,
)

REPO = Path(__file__).resolve().parents[1]
ARCHIVE = REPO / "tests" / "data" / "archived_tool_return.json"


# --------------------------------------------------------------------------- #
# Fakes: a loop whose tool round returns canned results (no model, no network)
# --------------------------------------------------------------------------- #


class ToolRoundIO:
    """One tool round, then a candidate. ``execute`` returns scripted results.

    Results are keyed by ``call_id`` because the loop may split a batch into
    segments (a write is a barrier) and call ``execute`` once per segment.
    """

    def __init__(self, contents: dict[str, str], calls: list[ToolCall]) -> None:
        self._contents = dict(contents)
        self._calls = tuple(calls)
        self.produced = 0

    def produce(self, state):
        self.produced += 1
        if self.produced == 1:
            return TurnProposal(tool_calls=self._calls)
        return TurnProposal(candidate=Candidate(subtask_id="T-01", source_id="src-1", hint="done"))

    def execute(self, calls):
        return tuple(
            ToolResult(call_id=call.call_id, ok=True, content=self._contents[call.call_id])
            for call in calls
        )

    def verify(self, _candidate):
        return GateVerdict("passed", "full")


def _run_tool_round(
    tmp_path: Path,
    contents: list[str],
    calls: list[ToolCall],
    *,
    policy: ToolOutputPolicy | None = None,
    workspace: BoundWorkspace | None = None,
    make_workspace: bool = True,
):
    """Drive one tool round through ``WorkerLoop`` with a real workspace spill."""
    from bobanana6.agentloop.events import EventLog

    io = ToolRoundIO(dict(zip([c.call_id for c in calls], contents, strict=True)), calls)
    if workspace is None and make_workspace:
        root = tmp_path / "ws"
        root.mkdir(parents=True, exist_ok=True)
        workspace = BoundWorkspace(root=root)
    log = EventLog()
    loop = WorkerLoop(
        io,
        config=LoopConfig(max_turns=3),
        log=log,
        tool_output=policy or ToolOutputPolicy(),
        spill_tool_output=workspace.spill_tool_output if workspace is not None else None,
    )
    result = loop.run(task_id="t", worker_id="w")
    return result, log


def _tool_rows(result) -> list[dict]:
    return [row for row in result.messages if row.get("role") == "tool"]


def _read_call(call_id: str = "c1") -> ToolCall:
    return ToolCall(call_id=call_id, name="sources.read", arguments={"ref": "infotable"})


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


def test_defaults_are_the_ones_the_offline_estimate_supports() -> None:
    policy = ToolOutputPolicy()
    assert (policy.max_chars, policy.head_chars, policy.tail_chars) == (
        DEFAULT_MAX_CHARS,
        DEFAULT_HEAD_CHARS,
        DEFAULT_TAIL_CHARS,
    )
    assert (DEFAULT_MAX_CHARS, DEFAULT_HEAD_CHARS, DEFAULT_TAIL_CHARS) == (4000, 800, 800)


def test_policy_refuses_a_window_that_could_overlap_or_invert() -> None:
    with pytest.raises(ValueError, match="overlap"):
        ToolOutputPolicy(max_chars=100, head_chars=80, tail_chars=80)
    with pytest.raises(ValueError, match="max_chars"):
        ToolOutputPolicy(max_chars=0)
    with pytest.raises(ValueError, match="tail_chars"):
        ToolOutputPolicy(max_chars=100, head_chars=0, tail_chars=10)


def test_threshold_comparison_is_strict() -> None:
    policy = ToolOutputPolicy(max_chars=100, head_chars=10, tail_chars=10)
    assert policy.pages(100) is False
    assert policy.pages(101) is True


def test_env_overrides_the_defaults_and_absence_keeps_them() -> None:
    env = {ENV_MAX_CHARS: "50", ENV_HEAD_CHARS: "10", ENV_TAIL_CHARS: "5"}
    assert ToolOutputPolicy.from_env(env) == ToolOutputPolicy(max_chars=50, head_chars=10, tail_chars=5)
    assert ToolOutputPolicy.from_env({}) == ToolOutputPolicy()
    assert ToolOutputPolicy.from_env({ENV_MAX_CHARS: "  "}) == ToolOutputPolicy()


def test_env_rejects_garbage_and_impossible_windows() -> None:
    with pytest.raises(ValueError, match=ENV_MAX_CHARS):
        ToolOutputPolicy.from_env({ENV_MAX_CHARS: "many"})
    with pytest.raises(ValueError, match="overlap"):
        ToolOutputPolicy.from_env({ENV_MAX_CHARS: "10", ENV_HEAD_CHARS: "9", ENV_TAIL_CHARS: "9"})


# --------------------------------------------------------------------------- #
# the pure page
# --------------------------------------------------------------------------- #


def test_page_keeps_both_ends_and_states_how_much_is_gone() -> None:
    policy = ToolOutputPolicy(max_chars=100, head_chars=10, tail_chars=10)
    content = "".join(chr(ord("a") + i % 26) for i in range(1000))
    page = page_tool_output(content, policy, full_path="_shell/tool-x.txt")
    assert page.total_chars == 1000
    assert page.total_lines == 1
    assert page.omitted_chars == 1000 - 20
    assert page.body.startswith(content[:10])
    assert page.body.endswith(content[-10:])
    # the omitted reading and the location are both in the body -- not silent
    assert "980" in page.body and "98.0%" in page.body
    assert "_shell/tool-x.txt" in page.body


def test_page_refuses_content_that_does_not_need_paging() -> None:
    policy = ToolOutputPolicy(max_chars=100, head_chars=10, tail_chars=10)
    with pytest.raises(ValueError, match="threshold"):
        page_tool_output("short", policy, full_path="_shell/x.txt")


# --------------------------------------------------------------------------- #
# the gate inside the loop
# --------------------------------------------------------------------------- #


def test_over_threshold_return_is_paged_with_a_readable_pointer(tmp_path: Path) -> None:
    content = "0123456789" * 900  # 9000 chars
    result, log = _run_tool_round(tmp_path, [content], [_read_call()])
    rows = _tool_rows(result)
    assert len(rows) == 1
    body = rows[0]["content"]
    assert body != content and "tool output paged" in body
    assert body.startswith(content[: DEFAULT_HEAD_CHARS])
    assert body.endswith(content[-DEFAULT_TAIL_CHARS :])
    # the pointer's path really exists and holds the full content, byte for byte
    relative = body.split("Full content: ", 1)[1].split(" (workspace-relative)", 1)[0]
    spilled = tmp_path / "ws" / relative
    assert spilled.is_file()
    assert spilled.read_text(encoding="utf-8") == content
    # how much was dropped is written down
    assert str(len(content)) in body
    assert str(len(content) - DEFAULT_HEAD_CHARS - DEFAULT_TAIL_CHARS) in body
    # and it is visible in the event stream
    truncations = log.of_kind("truncate")
    assert len(truncations) == 1
    assert truncations[0].ok is True
    assert truncations[0].detail["full_path"] == relative
    assert truncations[0].detail["omitted_chars"] == len(content) - 1600


def test_reverse_under_threshold_is_byte_identical_and_spills_nothing(tmp_path: Path) -> None:
    content = "x" * 3999  # one char under the default threshold
    result, log = _run_tool_round(tmp_path, [content], [_read_call()])
    rows = _tool_rows(result)
    assert rows[0]["content"] == content
    assert rows[0].keys() == {"role", "tool_call_id", "content"}
    assert not (tmp_path / "ws" / SHELL_OUTPUT_DIR).exists()
    assert log.of_kind("truncate") == ()


def test_reverse_exactly_at_threshold_is_byte_identical(tmp_path: Path) -> None:
    content = "x" * DEFAULT_MAX_CHARS
    result, log = _run_tool_round(tmp_path, [content], [_read_call()])
    assert _tool_rows(result)[0]["content"] == content
    assert log.of_kind("truncate") == ()


def test_one_over_the_threshold_is_paged(tmp_path: Path) -> None:
    content = "x" * (DEFAULT_MAX_CHARS + 1)
    result, _log = _run_tool_round(tmp_path, [content], [_read_call()])
    body = _tool_rows(result)[0]["content"]
    assert body != content and "tool output paged" in body


def test_a_parallel_batch_goes_through_the_same_rule_for_every_tool(tmp_path: Path) -> None:
    """No tool is exempt: every row in the batch is handled by the one gate."""
    calls = [
        ToolCall(call_id="c1", name="sources.read"),
        ToolCall(call_id="c2", name="shell.run", effect="write"),
        ToolCall(call_id="c3", name="task.upstream"),
    ]
    contents = ["a" * 5000, "b" * 10, "c" * 6000]
    result, log = _run_tool_round(tmp_path, contents, calls)
    rows = _tool_rows(result)
    assert [row["tool_call_id"] for row in rows] == ["c1", "c2", "c3"]
    assert "tool output paged" in rows[0]["content"]
    assert rows[1]["content"] == "b" * 10  # untouched, byte for byte
    assert "tool output paged" in rows[2]["content"]
    assert len(log.of_kind("truncate")) == 2
    # the two spilled files are distinct and each holds its own full text
    paths = [tmp_path / "ws" / row.detail["full_path"] for row in log.of_kind("truncate")]
    assert len(set(paths)) == 2
    assert [p.read_text(encoding="utf-8") for p in paths] == ["a" * 5000, "c" * 6000]


def test_no_spill_target_means_no_paging_and_the_event_says_so(tmp_path: Path) -> None:
    content = "x" * 9000
    _result, log = _run_tool_round(tmp_path, [content], [_read_call()], make_workspace=False)
    assert log.of_kind("truncate")[0].ok is False
    assert "could not be written" in log.of_kind("truncate")[0].detail["reason"]


def test_a_failed_spill_keeps_the_full_content(tmp_path: Path) -> None:
    """★ 落盘失败 ⇒ 宁可不换页，绝不留下"截了却取不回"。"""
    content = "x" * 9000

    def broken_spill(_call_id: str, _text: str) -> str:
        return ""

    from bobanana6.agentloop.events import EventLog

    io = ToolRoundIO({"c1": content}, [_read_call()])
    log = EventLog()
    loop = WorkerLoop(
        io,
        config=LoopConfig(max_turns=3),
        log=log,
        tool_output=ToolOutputPolicy(),
        spill_tool_output=broken_spill,
    )
    result = loop.run(task_id="t", worker_id="w")
    assert _tool_rows(result)[0]["content"] == content
    assert log.of_kind("truncate")[0].ok is False


def test_env_threshold_changes_what_the_loop_pages(tmp_path: Path, monkeypatch) -> None:
    content = "y" * 40
    # default policy: 40 chars is far under 4000 -> untouched
    result, log = _run_tool_round(tmp_path, [content], [_read_call()])
    assert _tool_rows(result)[0]["content"] == content
    assert log.of_kind("truncate") == ()
    # env-raised sensitivity: the same 40 chars is now over the threshold
    monkeypatch.setenv(ENV_MAX_CHARS, "20")
    monkeypatch.setenv(ENV_HEAD_CHARS, "5")
    monkeypatch.setenv(ENV_TAIL_CHARS, "5")
    policy = ToolOutputPolicy.from_env()
    result, _log = _run_tool_round(tmp_path, [content], [_read_call()], policy=policy)
    body = _tool_rows(result)[0]["content"]
    assert "tool output paged" in body
    assert body.startswith(content[:5]) and body.endswith(content[-5:])


def test_reusing_a_call_id_across_rounds_does_not_overwrite_the_earlier_spill(tmp_path: Path) -> None:
    """★ ``call_id`` is only unique within one response: the names must not collide."""
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    workspace = BoundWorkspace(root=root)
    first = workspace.spill_tool_output("call-1", "first" * 100)
    second = workspace.spill_tool_output("call-1", "second" * 100)
    assert first and second and first != second
    assert (root / first).read_text(encoding="utf-8") == "first" * 100
    assert (root / second).read_text(encoding="utf-8") == "second" * 100


# --------------------------------------------------------------------------- #
# the spilled page is not a deliverable
# --------------------------------------------------------------------------- #


def test_the_spill_area_is_the_already_ignored_one(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    ledger = WorkspaceLedger(root=root, region=())
    ledger.note_use()
    workspace = BoundWorkspace(root=root, ledger=ledger)

    where = workspace.spill_tool_output("c1", "z" * 9000)

    assert where.startswith(f"{SHELL_OUTPUT_DIR}/")
    assert SHELL_OUTPUT_DIR in IGNORED_PARTS
    assert _ignored(where) is True
    assert ledger.changed() == ()  # ★ 一个字节的落盘都不算块改动的文件


# --------------------------------------------------------------------------- #
# generalization: scale / domain / block type / reverse
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", [1, 3999, 4000, 4001, 120_000, 1_000_000])
def test_scale_is_handled_the_same_way(tmp_path: Path, size: int) -> None:
    content = "s" * size
    result, log = _run_tool_round(tmp_path, [content], [_read_call()])
    body = _tool_rows(result)[0]["content"]
    if size <= DEFAULT_MAX_CHARS:
        assert body == content
        assert log.of_kind("truncate") == ()
    else:
        assert body.startswith(content[:DEFAULT_HEAD_CHARS])
        assert body.endswith(content[-DEFAULT_TAIL_CHARS:])
        assert log.of_kind("truncate")[0].detail["total_chars"] == size


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"rows": [{"a": i, "b": f"v{i}"} for i in range(2000)]}),  # structured data
        "\n".join(f"line {i}: " + "x" * 40 for i in range(2000)),  # text/log
        "SELECT " + "col, " * 4000 + "1",  # one very long single line
    ],
)
def test_domains_share_the_same_rule(tmp_path: Path, content: str) -> None:
    result, log = _run_tool_round(tmp_path, [content], [_read_call()])
    body = _tool_rows(result)[0]["content"]
    assert len(content) > DEFAULT_MAX_CHARS
    assert body.startswith(content[:DEFAULT_HEAD_CHARS])
    assert body.endswith(content[-DEFAULT_TAIL_CHARS:])
    assert log.of_kind("truncate")[0].ok is True


@pytest.mark.parametrize("role_profile", ["read", "write", "human-adjacent"])
def test_block_types_share_the_same_rule(tmp_path: Path, role_profile: str) -> None:
    call = {
        "read": ToolCall(call_id="c1", name="sources.grep"),
        "write": ToolCall(call_id="c1", name="files.write", effect="write"),
        "human-adjacent": ToolCall(call_id="c1", name="task.contract"),
    }[role_profile]
    result, _log = _run_tool_round(tmp_path, ["z" * 9000], [call])
    assert "tool output paged" in _tool_rows(result)[0]["content"]


# --------------------------------------------------------------------------- #
# replay: the real archived return the estimate was measured on
# --------------------------------------------------------------------------- #


def test_the_archived_294394_char_return_is_paged_and_readable(tmp_path: Path) -> None:
    record = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    content = record["content"]
    assert record["tool"] == "sources.read"
    assert len(content) == 294_394  # the record the offline estimate used
    assert ToolOutputPolicy().pages(len(content)) is True

    result, log = _run_tool_round(tmp_path, [content], [_read_call(record["tool_call_id"])])
    body = _tool_rows(result)[0]["content"]
    assert "tool output paged" in body
    assert body.startswith(content[:DEFAULT_HEAD_CHARS])
    assert body.endswith(content[-DEFAULT_TAIL_CHARS:])

    relative = log.of_kind("truncate")[0].detail["full_path"]
    spilled = tmp_path / "ws" / relative
    full = spilled.read_text(encoding="utf-8")
    assert full == content  # 换页不销毁
    assert len(full) == 294_394

    # read a window back from the pointer target -- the paging promise, checked
    offset, limit = 1000, 200
    assert full[offset : offset + limit] == content[offset : offset + limit]
    # the pointer states the size and the omission
    assert "294394" in body
    assert str(294_394 - DEFAULT_HEAD_CHARS - DEFAULT_TAIL_CHARS) in body
    # the payload's own structural keys survive in the head (offset/limit are as
    # relevant as the data itself: the model can page the source again)
    assert '"ref"' in body and '"total_lines"' in body


def test_archived_fixture_provenance_is_recorded() -> None:
    record = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    assert record["source"].endswith("qwen4b6-A-c1.jsonl#L20")
    assert record["block"] == "T-02" and record["turn"] == 4
