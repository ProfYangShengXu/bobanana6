"""产物追溯建在 git 上 + 验收钉到 commit 上（`规格-git与CI取代编码守卫-20261008.md` §3）。

这一层判的四件事，全是**确定性**的（无模型、无网络）：

  ① 每个块跑完留下**一个真的 git commit**：作者 = 块 id，信息 = summary + verdict +
     它声明的输入（输入名 → 产出它的 commit sha）⇒ 任何产物都能 `git log -- <path>`
     追出「哪块、什么时候、用什么输入」（规格 §3①⑥）。
  ② **大产物只提交指针**：正文进内容寻址存储，git 里的那个 blob 是指针 JSON —— 仓库
     不会被 212KB / 631KB 的中间产物撑爆（规格 §0.5①⑥）。
  ③ **归档预算**：超预算的正文连 CAS 都不放，但**指针照样提交**（`stored: false`）——
     痕迹留下、正文丢掉，且是**具名**记录（`store.evicted`），不静默。
  ④ 验收 `at_commit`：不认识的 hash **具名拒**；工作树不在那一版上**也具名拒**
     （规格 §0.5③：跑错版本 = 白验）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from acceptance.harness import REPO as ACCEPTANCE_REPO
from acceptance.harness import CommitMismatch, UnknownCommit, pin_commit, run_task
from bobanana6.core.model import ModelResponse
from bobanana6.services.artifacts import ArtifactStore

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _briefing(messages):  # type: ignore[no-untyped-def]
    for message in reversed(messages):
        if message.get("role") != "user":  # ★ tool results are JSON too -- skip them
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith("{"):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue
    return {}


def _block_of(messages) -> str:  # type: ignore[no-untyped-def]
    return str(_briefing(messages).get("subtask", {}).get("id", ""))


def _tool_call(call_id: str, name: str, arguments) -> dict:  # type: ignore[no-untyped-def]
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _workdir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


# --------------------------------------------------------------------------- #
# ① 一个块 = 一个 commit，git 追得出来
# --------------------------------------------------------------------------- #


def test_each_block_leaves_a_commit_that_git_can_trace(tmp_path: Path) -> None:
    """★ 规格 §3①⑥ 的正面读数：`git log` 能回答「哪块、什么时候、用什么输入」。"""
    work = _workdir(tmp_path)
    (work / "part-a.csv").write_text("obligor,value\nA-1,3\n", encoding="utf-8")
    store = ArtifactStore(tmp_path / "trace", source_root=work)

    first = store.commit_block(
        block_id="T-01", verdict="ok", paths=["part-a.csv"], summary="read the panel"
    )
    (work / "panel.csv").write_text("obligor,value\nA-1,3\n", encoding="utf-8")
    second = store.commit_block(
        block_id="T-02",
        verdict="failed",
        paths=["panel.csv"],
        summary="merge the parts",
        inputs=(("part-a.csv", first.commit),),
    )

    assert first.commit and second.commit and first.commit != second.commit
    # 作者 = 块 id ⇒ `git log` 直接说出是哪个块写的这份文件
    log = store.log("part-a.csv")
    assert log, "no commit traces part-a.csv"
    assert log[0] == (first.commit, "T-01", "[T-01] verdict=ok")

    # ★ verdict 与 inputs 写在 commit **本体**里，不只在内存的 BlockRecord 里
    body = _git("log", "-1", "--format=%B", second.commit, cwd=store.tree)
    assert "verdict=failed" in body
    assert f"part-a.csv <- {first.commit}" in body

    # 内存侧同一份映射：路径 → 产出它的那个 commit
    assert store.record_for("panel.csv") is not None
    assert store.record_for("panel.csv").block_id == "T-02"  # type: ignore[union-attr]
    assert store.head() == second.commit


def test_a_block_that_wrote_nothing_does_not_leave_an_empty_commit(tmp_path: Path) -> None:
    """重复提交同一个块（内容一字未改）⇒ 不留空 commit，但记录仍指向 HEAD。"""
    work = _workdir(tmp_path)
    (work / "a.csv").write_text("x\n", encoding="utf-8")
    store = ArtifactStore(tmp_path / "trace", source_root=work)
    first = store.commit_block(block_id="T-01", verdict="ok", paths=["a.csv"])
    before = _git("rev-list", "--count", "HEAD", cwd=store.tree)
    second = store.commit_block(block_id="T-01", verdict="ok", paths=["a.csv"])
    assert _git("rev-list", "--count", "HEAD", cwd=store.tree) == before
    assert second.commit == first.commit


# --------------------------------------------------------------------------- #
# ② 大产物只提交指针
# --------------------------------------------------------------------------- #


def test_a_big_artifact_reaches_git_as_a_pointer_and_the_body_goes_to_the_cas(tmp_path: Path) -> None:
    """★ 规格 §0.5①⑥：git 里的那个 blob 必须是**指针**，正文在 CAS。"""
    work = _workdir(tmp_path)
    body = ("obligor,value\n" + "".join(f"A-{n:04d},{n}\n" for n in range(400))).encode()
    assert len(body) > 4096
    (work / "panel.csv").write_bytes(body)

    store = ArtifactStore(tmp_path / "trace", source_root=work, inline_max_bytes=4096)
    record = store.commit_block(block_id="T-01", verdict="ok", paths=["panel.csv"])
    traced = record.file("panel.csv")
    assert traced is not None
    assert traced.inline is False and traced.stored is True
    assert traced.bytes == len(body)

    # git 树里那一份是 JSON 指针（路径 + 摘要 + 规模都可追）
    committed = _git("show", f"{record.commit}:T-01/panel.csv", cwd=store.tree)
    pointer = json.loads(committed)
    assert pointer["path"] == "panel.csv"
    assert pointer["sha256"] == traced.sha256
    assert pointer["bytes"] == len(body)
    assert pointer["lines"] == traced.lines

    # ★ 仓库没被撑爆：这个 blob 比正文小一个数量级以上
    blob_bytes = int(_git("cat-file", "-s", f"{record.commit}:T-01/panel.csv", cwd=store.tree))
    assert blob_bytes < len(body) // 10, (blob_bytes, len(body))

    # 正文在 CAS 里逐字节一致
    archived = store.cas / traced.sha256[:2] / traced.sha256[2:]
    assert archived.read_bytes() == body


def test_a_small_artifact_is_committed_in_full(tmp_path: Path) -> None:
    """小产物直接进 git（读起来就是原文，不需要再解一层指针）。"""
    work = _workdir(tmp_path)
    (work / "a.csv").write_text("obligor\nA-1\n", encoding="utf-8")
    store = ArtifactStore(tmp_path / "trace", source_root=work)
    record = store.commit_block(block_id="T-01", verdict="ok", paths=["a.csv"])
    traced = record.file("a.csv")
    assert traced is not None and traced.inline is True and traced.stored is True
    assert _git("show", f"{record.commit}:T-01/a.csv", cwd=store.tree) == "obligor\nA-1\n"


# --------------------------------------------------------------------------- #
# ③ 归档预算
# --------------------------------------------------------------------------- #


def test_the_archive_budget_evicts_bodies_but_never_the_pointer(tmp_path: Path) -> None:
    """★ 规格 §0.5④：预算用尽 ⇒ 正文连 CAS 都不放，**但指针照样提交**且**具名记录**。

    处置刻意**不是**「具名拒」：拒收会让一个块因为归档卫生而失败 —— 那正是这份规格
    要消灭的误伤；也不是「滚动」，那会删证据。
    """
    work = _workdir(tmp_path)
    (work / "a.csv").write_text("obligor\nA-1\nA-2\n", encoding="utf-8")
    store = ArtifactStore(tmp_path / "trace", source_root=work, budget_bytes=4)

    record = store.commit_block(block_id="T-01", verdict="ok", paths=["a.csv"])
    traced = record.file("a.csv")
    assert traced is not None
    assert traced.stored is False  # 正文没进 CAS
    assert [entry.path for entry in store.evicted] == ["a.csv"]  # 具名，不静默
    assert not (store.cas / traced.sha256[:2] / traced.sha256[2:]).exists()

    # 指针仍在 git 里 —— 痕迹没丢
    pointer = json.loads(_git("show", f"{record.commit}:T-01/a.csv", cwd=store.tree))
    assert pointer["sha256"] == traced.sha256
    assert pointer["stored"] is False


def test_the_budget_counts_every_body_not_only_the_oversized_ones(tmp_path: Path) -> None:
    """预算管**所有**提交正文（inline 的也算）—— 只算 CAS 会在小文件上永不触发。"""
    work = _workdir(tmp_path)
    (work / "a.csv").write_text("x" * 10, encoding="utf-8")
    (work / "b.csv").write_text("y" * 10, encoding="utf-8")
    store = ArtifactStore(tmp_path / "trace", source_root=work, budget_bytes=12)
    record = store.commit_block(block_id="T-01", verdict="ok", paths=["a.csv", "b.csv"])
    assert [entry.path for entry in store.evicted] == ["b.csv"]
    assert record.file("a.csv") is not None and record.file("a.csv").stored is True  # type: ignore[union-attr]
    assert record.file("b.csv") is not None and record.file("b.csv").stored is False  # type: ignore[union-attr]


def test_the_budget_survives_across_runs_through_the_cas_on_disk(tmp_path: Path) -> None:
    """★ 预算必须算**磁盘上已有的 CAS 正文** —— 痕迹仓库跨 run 复用。

    只数"本次进程新增"的预算永远不会触发：工作区每次重建，`_run/artifacts` 却留着，
    于是残留越攒越多（AgentFootprint 的 measurement target）。这里开两个 store
    （= 两次 run）共用同一个 root，第二次必须**一上来**就知道预算已经用掉。
    """
    work = _workdir(tmp_path)
    body = b"x" * 200
    (work / "big.csv").write_bytes(body)
    root = tmp_path / "trace"

    first = ArtifactStore(root, source_root=work, inline_max_bytes=1, budget_bytes=1000)
    first.commit_block(block_id="T-01", verdict="ok", paths=["big.csv"])
    assert first.archived_bytes == len(body)

    second = ArtifactStore(root, source_root=work, inline_max_bytes=1, budget_bytes=1000)
    assert second.archived_bytes == len(body), "the CAS already on disk is not in the budget"
    (work / "more.csv").write_bytes(b"y" * 50)
    record = second.commit_block(block_id="T-02", verdict="ok", paths=["more.csv"])
    traced = record.file("more.csv")
    assert traced is not None and traced.stored is True  # 还没超（200 + 50 <= 1000）

    third = ArtifactStore(root, source_root=work, inline_max_bytes=1, budget_bytes=260)
    assert third.archived_bytes >= 250  # 前两次的正文都在账上
    assert [entry.path for entry in third.evicted] == []
    (work / "huge.csv").write_bytes(b"z" * 100)
    record = third.commit_block(block_id="T-03", verdict="ok", paths=["huge.csv"])
    traced = record.file("huge.csv")
    assert traced is not None and traced.stored is False
    assert [entry.path for entry in third.evicted] == ["huge.csv"]


def test_a_path_that_was_never_written_is_simply_absent(tmp_path: Path) -> None:
    """块声明了却没写出来的路径 ⇒ 不假造记录（`missing` 不能被伪装成「有产物」）。"""
    work = _workdir(tmp_path)
    store = ArtifactStore(tmp_path / "trace", source_root=work)
    record = store.commit_block(block_id="T-01", verdict="failed", paths=["nope.csv"])
    assert record.files == ()
    assert record.has("nope.csv") is False


# --------------------------------------------------------------------------- #
# ④ 验收钉到 commit 上
# --------------------------------------------------------------------------- #


def _tiny_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "one", cwd=repo)
    first = _git("rev-parse", "HEAD", cwd=repo).strip()
    (repo / "f.txt").write_text("two\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "two", cwd=repo)
    return repo, first, _git("rev-parse", "HEAD", cwd=repo).strip()


def test_the_harness_refuses_an_unknown_commit_by_name(tmp_path: Path) -> None:
    """★ 规格 §3④：不认识的 hash ⇒ **具名拒**，不是默默跑当前工作区。"""
    repo, _first, _second = _tiny_repo(tmp_path)
    with pytest.raises(UnknownCommit) as error:
        pin_commit("deadbeef", repo=repo)
    assert "unknown commit 'deadbeef'" in str(error.value)
    assert str(repo) in str(error.value)  # 拒得有据：说出在哪个仓库里找过


def test_the_harness_refuses_a_tree_that_is_not_at_the_pinned_commit(tmp_path: Path) -> None:
    """工作树不在被钉的那一版上 ⇒ **也具名拒**（跑错版本 = 白验）。"""
    repo, first, head = _tiny_repo(tmp_path)
    assert pin_commit("HEAD", repo=repo) == head
    assert pin_commit(head, repo=repo) == head
    with pytest.raises(CommitMismatch) as error:
        pin_commit(first, repo=repo)
    # 拒得具体：说出被钉的是哪一版、当前树在哪一版（都按 git 的短 sha 记）
    assert first[:12] in str(error.value) and head[:12] in str(error.value)


def test_a_run_reports_the_commit_it_was_pinned_to(tmp_path: Path) -> None:
    """`run_task(at_commit=...)` 把钉住的 sha 放进读数里，报告能锚定到确定的代码状态。"""
    from tests.test_acceptance import _chain_task

    task = _chain_task(tmp_path, name="pinned-chain")

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            return ModelResponse(text="not json at all")

    head = pin_commit("HEAD", repo=ACCEPTANCE_REPO)
    run = run_task(task, client=_Client(), env={}, at_commit="HEAD")
    assert run.pinned_commit == head


# --------------------------------------------------------------------------- #
# ⑥ 端到端：失败块的产物照样进 git，且下游知道是谁给的
# --------------------------------------------------------------------------- #


def test_the_trace_repo_works_inside_this_very_repository(tmp_path: Path) -> None:
    """★★ 痕迹仓库的实际落点是 `acceptance/**/_run/artifacts` —— 在**本仓库的工作树里面**。

    这个环境差异曾把真跑打挂：本仓库挂在 Windows 挂载点上，git 对「可疑属主」**拒答任何
    子命令** —— `git init` 成功，紧接着的 `git config user.name` 就是 exit 128。
    `tmp_path` 造的 store 永远碰不到这件事（/tmp 的属主没问题），所以这条测试**故意**
    把 store 建在真仓库里面：环境差异只有在真环境里才抓得住。
    """
    root = ACCEPTANCE_REPO / "acceptance" / "smoke" / "_run" / "trace-probe"
    shutil.rmtree(root, ignore_errors=True)
    try:
        work = _workdir(tmp_path)
        (work / "a.csv").write_text("obligor\nA-1\n", encoding="utf-8")
        store = ArtifactStore(root, source_root=work)  # ★ 会在这里跑 git init + git config
        record = store.commit_block(block_id="T-01", verdict="ok", paths=["a.csv"])
        assert record.commit
        assert _git("log", "--format=%an", cwd=store.tree).strip() == "T-01"
        assert store.head() == record.commit
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_failed_block_that_left_no_file_still_leaves_a_commit(tmp_path: Path) -> None:
    """★ 规格 §2 第1层：**每个块跑完 = 一个 commit** —— 包括磁盘上什么都没留下的失败块。

    `_publish_disk_artifact` 只在盘上**真有**产物时才提交。纯行数据的块失败时盘上没有文件，
    于是原来**一个 commit 都没有**：实测 `panel-three-sources` 离线跑（四块全失败）
    连 `_run/artifacts/tree` 都**不存在** —— "这个块跑过、结果是失败"在 git 里查不到。
    """
    from tests.test_acceptance import _chain_task

    task = _chain_task(tmp_path, name="no-file-fail-chain")

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            return ModelResponse(text="I am still working on it.")  # 不写文件，信封也不合法

    run = run_task(task, client=_Client(), env={}, workspace=True)
    assert run.verdicts["T-01"].ok is False
    assert "artifact_on_disk" not in run.verdicts["T-01"].evidence  # ★ 盘上真的没有产物

    trace = task / "_run" / "artifacts" / "tree"
    assert (trace / ".git").is_dir(), f"no artifact repo at {trace}"
    authors = _git("log", "--format=%an", cwd=trace).split()
    assert "T-01" in authors, authors

    head = _git("log", "--format=%H", "--author=T-01", cwd=trace).split()[0]
    body = _git("log", "-1", "--format=%B", head, cwd=trace)
    assert "verdict=failed" in body, body
    names = _git("show", "--pretty=format:", "--name-only", head, cwd=trace).split()
    # ★ 痕迹是 commit.json（verdict/inputs/summary），files 为空是**事实**：它真没写出文件
    assert "T-01/commit.json" in names, names
    assert [n for n in names if not n.endswith("commit.json")] == [], names


def test_a_run_leaves_a_per_block_git_trail_down_to_the_failed_upstream(tmp_path: Path) -> None:
    """★★ 规格 §3①⑥ 的端到端读数：判定失败 ≠ 产物没进 git。

    两个块都只是**信封没封好**（散文开头）⇒ `ok=False`；但它们写过的文件
    （`a.csv` / `b.csv`）各自留下一个作者是本块 id 的 commit，下游 `T-02` 的 brief 里
    带着 `producer_commit`（"这份东西是谁给我的"的确定答案，见 §2 第1层）。

    ★ 顺带钉住一个副作用：只要块声明了输入，它就**不能**再引 `seed` 这类快照
    （`ref` 不被下游块接受）—— 这就是规格 §3③ 与"不许放宽既有断言"冲突的现场证据，
    所以这里让两个块都走**磁盘通道**交付，不去撞那道门。
    """
    from tests.test_acceptance import _chain_task

    task = _chain_task(tmp_path, name="traced-chain")
    briefs: dict[str, dict] = {}
    data = "obligor\nA-1\n"

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages)
            briefs.setdefault(block, _briefing(messages))
            tool_count = sum(1 for message in messages if message.get("role") == "tool")
            output = str(_briefing(messages).get("subtask", {}).get("output", ""))
            if tool_count == 0:
                return ModelResponse(
                    tool_calls=(_tool_call("w1", "files.write", {"path": output, "content": data}),)
                )
            return ModelResponse(text="I am still working on it.")  # ★ 信封没封好

    run = run_task(task, client=_Client(), env={}, workspace=True)
    for block in ("T-01", "T-02"):
        assert run.verdicts[block].ok is False, run.verdicts[block].evidence  # ★ 绝不改绿
        assert run.verdicts[block].evidence.get("artifact_on_disk"), run.verdicts[block].evidence

    trace = task / "_run" / "artifacts" / "tree"
    assert (trace / ".git").is_dir(), f"no artifact repo at {trace}"
    authors = set(_git("log", "--format=%an", cwd=trace).split())
    assert {"T-01", "T-02"} <= authors, authors

    # ★ 失败块的产物在 git 里有作者、有 verdict、有它写过的那个文件
    upstream_commit = _git("log", "--format=%H", "--author=T-01", cwd=trace).split()[0]
    body = _git("log", "-1", "--format=%B", upstream_commit, cwd=trace)
    assert "verdict=failed" in body
    names = _git("show", "--pretty=format:", "--name-only", upstream_commit, cwd=trace).split()
    assert "T-01/a.csv" in names, names  # 产物正文/指针真的在这个 commit 里

    entry = (briefs["T-02"].get("upstream") or {}).get("a.csv") or {}
    assert entry.get("producer_verdict") == "failed", entry
    assert entry.get("producer_commit") == upstream_commit, entry
