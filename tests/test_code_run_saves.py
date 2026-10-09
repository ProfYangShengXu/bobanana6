"""`code.run` 改成【先落盘再跑】（方案 B）。

背景（实测，不是设计洁癖）——A 臂 `qwen4b3` 的工具直方图：

```
code.run  18 次   ← 模型把代码直接塞参数里跑，代码【从不落盘】
files.write 0~2   files.patch 0 次   ← 想改的东西从来不在磁盘上，改哪一行都只能重贴整段
```

本文件考四件事（照任务书 §五）：

```
① 落盘     跑一次 code.run ⇒ 目录里出现那个文件，内容与传入的 code【逐字节相同】
② 返回     只增不减（ok / stdout / value / error / cwd 一个不动）
③ 截断换页 超大输出 ⇒ 正文截到末尾、truncated: true、full_output_path 指向【全量】
④ 不污染   _code/ 里的东西【不进产物】（判定器看到的 deliverables 里没有它）
```
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.core.model import ModelResponse
from bobanana6.services.workspace import (
    CODE_DIR,
    CODE_OUTPUT_FULL_MAX_BYTES,
    CODE_OUTPUT_MAX_BYTES,
    CODE_OUTPUT_MAX_LINES,
    BoundWorkspace,
    WorkspaceLedger,
)
from tests.test_convert_three import _block_of, _task, _tool_call
from tests.test_services import _full_toolset


def _workspace(tmp_path: Path, **kw) -> BoundWorkspace:  # type: ignore[no-untyped-def]
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    ledger = WorkspaceLedger(root=root, region=kw.get("write_region", ()))
    kw.setdefault("block", "T-01")
    return BoundWorkspace(root=root, ledger=ledger, **kw)


# --------------------------------------------------------------------------- #
# ① 落盘：原样、确定性、每次一份
# --------------------------------------------------------------------------- #


def test_the_code_is_saved_verbatim(tmp_path: Path) -> None:
    """★ 逐字节相同（sha256）：不加工、不重排、不加壳 —— 否则 `files.patch` 按原文就改不动。"""
    workspace = _workspace(tmp_path, write_region=("out.csv",), frozen_paths=())
    code = "import math\n\nvalue = math.factorial(5)\nprint(value)\n"
    result = workspace.run_code(code)
    assert result["code_path"] == f"{CODE_DIR}/T-01/step-0001.py"
    saved = tmp_path / "ws" / result["code_path"]
    assert saved.is_file()
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == hashlib.sha256(code.encode()).hexdigest()
    assert saved.read_text(encoding="utf-8") == code


def test_every_call_gets_its_own_file_and_reset_keeps_them(tmp_path: Path) -> None:
    """★ `reset` 清的是**命名空间**，不是文件；序号是确定性的、接着往下走（不用时间戳）。"""
    workspace = _workspace(tmp_path, write_region=("out.csv",), frozen_paths=())
    first = workspace.run_code("counter = 41\ncounter + 1")
    second = workspace.run_code("counter += 1\ncounter")
    third = workspace.run_code("counter", reset=True)
    fourth = workspace.run_code("counter = 1\ncounter")
    assert [first["code_path"], second["code_path"], third["code_path"], fourth["code_path"]] == [
        f"{CODE_DIR}/T-01/step-0001.py",
        f"{CODE_DIR}/T-01/step-0002.py",
        f"{CODE_DIR}/T-01/step-0003.py",
        f"{CODE_DIR}/T-01/step-0004.py",
    ]
    # 既有语义一字未变：`reset` 之后命名空间是空的（NameError），文件却都在
    assert third["ok"] is False and "NameError" in third["error"], third
    assert all((tmp_path / "ws" / f"{CODE_DIR}/T-01/step-000{i}.py").is_file() for i in range(1, 5))
    assert first["value"] == "42" and second["value"] == "42"


# --------------------------------------------------------------------------- #
# ② 返回只增不减
# --------------------------------------------------------------------------- #


def test_the_return_only_gains_keys(tmp_path: Path) -> None:
    """★ 改前的字段一个不动：`ok` / `stdout` / `value` / `cwd`（失败时还有 `error`）。"""
    workspace = _workspace(tmp_path, write_region=("out.csv",), frozen_paths=())
    ok_result = workspace.run_code("print('hi')")
    for key in ("ok", "stdout", "value", "cwd"):
        assert key in ok_result, (key, sorted(ok_result))
    assert ok_result["ok"] is True and "hi" in ok_result["stdout"] and ok_result["value"] is None
    # 新增的是【只增】的那几个
    assert ok_result["code_path"] == f"{CODE_DIR}/T-01/step-0001.py"
    assert ok_result["truncated"] is False
    assert "full_output_path" not in ok_result  # 没截就没有它

    failed = workspace.run_code("raise ValueError('boom')")
    for key in ("ok", "stdout", "error", "cwd"):
        assert key in failed, (key, sorted(failed))
    assert failed["ok"] is False and "ValueError" in failed["error"] and "boom" in failed["error"]
    assert failed["code_path"] == f"{CODE_DIR}/T-01/step-0002.py"  # 失败的那次也存底了


def test_the_registry_payload_carries_the_new_fields(tmp_path: Path) -> None:
    """★ 接线上也要到：注册表把整个返回原样 JSON 出来，`code_path` 在里面。"""
    _tools, registry = _full_toolset(tmp_path)
    raw = {
        "id": "c-code",
        "type": "function",
        "function": {"name": "code.run", "arguments": json.dumps({"code": "1 + 1"})},
    }
    result = registry.execute((registry.calls([raw])[0],))[0]
    assert result.ok is True, result.content
    payload = json.loads(result.content)
    assert payload["value"] == "2" and payload["code_path"] == f"{CODE_DIR}/T-01/step-0001.py"
    assert payload["truncated"] is False


# --------------------------------------------------------------------------- #
# ③ 截断 = 换页（不销毁）
# --------------------------------------------------------------------------- #


def test_a_long_output_is_paged_and_the_full_output_is_readable(tmp_path: Path) -> None:
    """★ 反证：读 `full_output_path` 能把被截掉的部分**拿回来**。"""
    workspace = _workspace(tmp_path, write_region=("out.csv",), frozen_paths=())
    result = workspace.run_code("for i in range(5000): print(i)")
    assert result["truncated"] is True
    assert result["full_output_path"] == f"{CODE_DIR}/T-01/step-0001.out"
    full = tmp_path / "ws" / result["full_output_path"]
    assert full.is_file()
    full_text = full.read_text(encoding="utf-8")
    assert len(full_text.splitlines()) == 5000  # 全量
    # 正文只留末尾，而且**可见**地说了全文在哪
    assert "output truncated" in result["stdout"]
    assert result["full_output_path"] in result["stdout"]
    body = [line for line in result["stdout"].splitlines() if line.isdigit()]
    assert len(body) <= CODE_OUTPUT_MAX_LINES
    assert body[-1] == "4999"  # 末尾在
    assert "0" not in body[:100]  # 开头那段确实不在正文里
    assert "\n0\n" in f"\n{full_text}\n"  # 但它能从全量文件里拿回来


def test_the_byte_cap_binds_for_one_huge_line(tmp_path: Path) -> None:
    """★ 两个上限"先到者为准"：一行 60KB（不足 2000 行）时按**字节**截。"""
    workspace = _workspace(tmp_path, write_region=("out.csv",), frozen_paths=())
    result = workspace.run_code("print('x' * 60000)")
    assert result["truncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= CODE_OUTPUT_MAX_BYTES + 200  # + 那句标记
    assert (tmp_path / "ws" / result["full_output_path"]).stat().st_size >= 60000


def test_a_small_output_is_untouched(tmp_path: Path) -> None:
    """反证：没超上限就一个字节都不动（不写全量文件、不改 stdout）。"""
    workspace = _workspace(tmp_path, write_region=("out.csv",), frozen_paths=())
    result = workspace.run_code("print('small')")
    assert result["truncated"] is False
    assert result["stdout"] == "small\n"
    assert not (tmp_path / "ws" / f"{CODE_DIR}/T-01/step-0001.out").exists()


# --------------------------------------------------------------------------- #
# ④ `_code/` 不是产物
# --------------------------------------------------------------------------- #


def test_the_scratch_dir_never_shows_up_as_a_changed_file(tmp_path: Path) -> None:
    """★ 硬边界③的**单点**：`_code/` 在 `IGNORED_PARTS` 里 ⇒ ledger 看不见它。"""
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    ledger = WorkspaceLedger(root=root, region=("out.csv",))
    workspace = BoundWorkspace(root=root, write_region=("out.csv",), frozen_paths=(), ledger=ledger)
    assert ledger.used is False
    workspace.run_code("print('scratch')")
    assert ledger.used is True  # 通道 2 用过了
    workspace.write("out.csv", "obligor\nA-1\n")  # 真产物
    changed = ledger.changed()
    assert changed == ("out.csv",), changed  # ★ 落盘的代码**不在**里面
    assert all(not name.startswith(f"{CODE_DIR}/") for name in changed)


def test_the_judge_never_sees_the_scratch_dir(tmp_path: Path) -> None:
    """端到端：块跑完，判定器读到的产物清单/痕迹仓库里**没有** `_code/`。"""
    task = _task(tmp_path, name="code-scratch")
    turns: dict[str, int] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages)
            turn = turns.get(block, 0)
            turns[block] = turn + 1
            if block == "T-01" and turn == 0:
                return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
            if block == "T-01" and turn == 1:
                return ModelResponse(
                    tool_calls=(
                        _tool_call("c1", "code.run", {"code": "print(1); open('a.csv','w').write('obligor\\nA-1\\n')"}),
                    )
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
    assert run.workspace is not None
    # ① 落盘真的发生了（在块自己的工作区里）
    assert (run.workspace / f"{CODE_DIR}/T-01/step-0001.py").is_file()
    # ④ 判定器/痕迹仓库里没有它
    tree = task / "_run" / "artifacts" / "tree"
    if tree.is_dir():
        leaked = [p for p in tree.rglob("*") if CODE_DIR in p.relative_to(tree).parts]
        assert leaked == [], leaked
        for manifest in tree.glob("*/commit.json"):
            recorded = json.loads(manifest.read_text(encoding="utf-8"))
            assert all(CODE_DIR not in str(entry["path"]) for entry in recorded["files"]), recorded
    assert run.verdicts.get("T-01") is not None


# --------------------------------------------------------------------------- #
# 硬边界⑦：落盘是辅助，不是前置门
# --------------------------------------------------------------------------- #


def test_saving_is_auxiliary_and_never_blocks_the_run(tmp_path: Path) -> None:
    """★ 写不成就**如实报**、代码照跑 —— 落盘失败不许把整块搞挂。"""
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    # 把 `_code` 占成一个**文件** ⇒ 建目录必失败（FileExistsError，OSError 子类）
    (root / CODE_DIR).write_text("not a directory", encoding="utf-8")
    workspace = BoundWorkspace(root=root, write_region=("out.csv",), frozen_paths=())
    result = workspace.run_code("print('still runs')")
    assert result["ok"] is True and result["stdout"] == "still runs\n"  # ★ 照跑
    assert result["code_path"] == ""  # ★ 如实报"没存成"
    # `_code` 被占成文件 ⇒ 建目录必失败（`FileExistsError` / `NotADirectoryError` 都是 OSError）
    assert "code_save_error" in result and "Error" in result["code_save_error"]


def test_a_code_dump_failure_does_not_lose_an_oversized_output(tmp_path: Path) -> None:
    """★ 换不成页就**不销毁**：宁可把全量给它，也不留一份"截了却取不回"的输出。"""
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / CODE_DIR).write_text("not a directory", encoding="utf-8")
    workspace = BoundWorkspace(root=root, write_region=("out.csv",), frozen_paths=())
    result = workspace.run_code("for i in range(3000): print(i)")
    assert result["truncated"] is False  # 没换成页 ⇒ 不截
    assert "full_output_path" not in result
    assert "page_error" in result
    assert result["stdout"].splitlines()[-1] == "2999"  # 全量还在（末尾在）
    assert result["stdout"].splitlines()[0] == "0"  # 开头也在（没销毁）


# --------------------------------------------------------------------------- #
# 契约的静态面：规格没被偷偷加参数
# --------------------------------------------------------------------------- #


def test_code_run_still_takes_exactly_two_parameters() -> None:
    """★ 硬边界①：`code` + `reset`，没有第三个（落盘位置由壳定）。"""
    from bobanana6.core.ports import _BASE_TOOL_SPECS

    spec = next(item for item in _BASE_TOOL_SPECS if item.name == "code.run")
    assert spec.params == ("code",)
    assert spec.optional == ("reset",)


# --------------------------------------------------------------------------- #
# 只读审计 #65 / #71 / #66 的回归护栏
# --------------------------------------------------------------------------- #


def test_the_scratch_dir_is_per_block(tmp_path: Path) -> None:
    """★ #65：一个 run 里所有块共用同一个工作区 —— 不打块号就会**跨块覆盖**存底。

    触发：多块任务里 ≥2 个块各调一次 `code.run`（各自序号从 1 开始）。
    """
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    first = BoundWorkspace(root=root, write_region=("a.csv",), frozen_paths=(), block="T-01")
    second = BoundWorkspace(root=root, write_region=("b.csv",), frozen_paths=(), block="T-02")
    left = first.run_code("print('first')")
    right = second.run_code("print('second')")
    assert left["code_path"] == f"{CODE_DIR}/T-01/step-0001.py"
    assert right["code_path"] == f"{CODE_DIR}/T-02/step-0001.py"
    # ★ 反证：第一个块那份**还在**，内容仍是它自己的代码（没被第二个块盖掉）
    assert "first" in (root / left["code_path"]).read_text(encoding="utf-8")
    assert "second" in (root / right["code_path"]).read_text(encoding="utf-8")


def test_an_unscoped_workspace_does_not_impersonate_a_block(tmp_path: Path) -> None:
    """没上块号的部署落到 `unscoped/` —— 不冒充任何块（fail-closed），也不会打乱别块。"""
    workspace = BoundWorkspace(root=tmp_path / "ws", write_region=("out.csv",), frozen_paths=())
    result = workspace.run_code("print(1)")
    assert result["code_path"] == f"{CODE_DIR}/unscoped/step-0001.py"


def test_the_metadata_keys_are_ordered_before_stdout(tmp_path: Path) -> None:
    """★ #71：压缩按**头部**截 2000 字符 ⇒ `code_path`/`full_output_path` 必须排在 stdout 之前。

    反证用真压缩的尺子量：按头部截到 2000 字符之后，那几个键仍然看得见。
    """
    workspace = _workspace(tmp_path, write_region=("out.csv",), frozen_paths=())
    result = workspace.run_code("for i in range(3000): print(i)")
    keys = list(result)
    for key in ("ok", "code_path", "truncated", "full_output_path"):
        assert keys.index(key) < keys.index("stdout"), (key, keys)
    head = json.dumps(result, ensure_ascii=False)[:2000]
    assert '"code_path"' in head and '"full_output_path"' in head and '"truncated"' in head


def test_the_registry_payload_also_puts_the_metadata_first(tmp_path: Path) -> None:
    """★ #71 的**接线读数**：模型拿到的是注册表 JSON，所以键序要在那一层也成立。"""
    _tools, registry = _full_toolset(tmp_path)
    raw = {
        "id": "c-big",
        "type": "function",
        "function": {"name": "code.run", "arguments": json.dumps({"code": "for i in range(3000): print(i)"})},
    }
    result = registry.execute((registry.calls([raw])[0],))[0]
    assert result.ok is True
    head = result.content[:2000]  # 压缩按头部截的那 2000 字符
    assert '"code_path"' in head and '"full_output_path"' in head and '"truncated"' in head


def test_a_huge_full_output_is_capped_and_flagged(tmp_path: Path) -> None:
    """★ #66：全量那份也有上限（8 MiB）；绑到就如实标 —— 不假装那份文件是"全量"。"""
    workspace = _workspace(tmp_path, write_region=("out.csv",), frozen_paths=())
    result = workspace.run_code("print('y' * (9 * 1024 * 1024))")
    assert result["truncated"] is True
    assert result["full_output_truncated"] is True  # ★ 如实标
    full = tmp_path / "ws" / result["full_output_path"]
    assert full.stat().st_size <= CODE_OUTPUT_FULL_MAX_BYTES


def test_writing_the_dump_does_not_go_through_the_write_gate(tmp_path: Path) -> None:
    """★ 硬边界⑦：草稿纸**不过 `WriteBudget` 的闸**（被拦下来就等于落盘把块搞挂了）。

    只记证据字节；块自己写产物时那本账照旧。
    """
    workspace = _workspace(tmp_path, write_region=("out.csv",), frozen_paths=())
    before = workspace.write_budget.written_total
    workspace.run_code("print('x')")
    assert workspace.write_budget.written_total > before  # 有证据
    assert workspace.write_budget.paths == set()  # 但没进"本块写过哪些路径"那本闸账
